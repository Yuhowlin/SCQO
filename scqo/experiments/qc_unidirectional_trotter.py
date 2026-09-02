"""Unidirectional (cascaded) coupling by Trotterization — record-only.

Excite ONE end of a three-qubit chain, then repeat a **Trotter step** N times:
a partial swap **source -> relay**, a partial swap **relay -> sink**, a
**parametric reset** of the relay, and a per-qubit off-resonant **AC-Stark**
tone that compensates the phase each qubit picks up in the step. Every chain
qubit is read out jointly at the end, so the populations against N draw the
excitation TRANSPORT along the chain.

The relay reset is the whole mechanism. Both swaps are exchanges, so without it
the sink would feed amplitude back through the relay into the source and the
chain would be bidirectional; dumping the relay every round destroys that return
path, leaving a one-way (cascaded) coupling source -> sink. The picture to read
off the figure is therefore: the source decays, the sink fills, and the relay
stays near zero because it is emptied every round.

The angles are baked, not swept: each swap is the SAME named pair operation at
its fixed amplitude (calibrate it with ``pair_swap_flux_map`` + ``qc_n_swap_amp``
— TUTORIAL section 12), and each qubit's Stark compensation is a fixed amplitude
FACTOR of the ``stark`` operation's baked amplitude. The only swept axis is the
Trotter-step count; ``round_count = 0`` is the prep-only baseline, the same
convention as ``qc_n_swap_amp`` / ``qc_n_stark_amp``.

WHAT THE SINK DOES **NOT** DO is reach unity, and that is the sequence, not a
miscalibration. The second swap is an exchange like the first, so the sink also
emits back into the relay — which the very next reset dumps. The chain is
therefore a DISSIPATIVE cascade: the sink settles at roughly (first-swap
transfer) x (current source population) and then follows the source down, so its
peak is a fraction of an excitation, reached within a few rounds. Capturing the
excitation instead would need the angles shaped in time, which a fixed named
operation cannot express — read ``min_transfer`` as "something arrived", never as
"the transfer was good".

TARGETS are the chain QUBITS (not a pair composite): they are the qubits
initialized and read out, and **their order is the chain order**, which is also
the digit order of the ``joint_state`` labels. The two swap pairs, the reset
qubit and the per-qubit compensation are Parameters, not targets — the same
shape ``qubit_tomography`` uses for a multi-qubit circuit.

READOUT (the unified readout schema): digital, both modes. ``readout_mode=
"average"`` (default) stores each qubit's averaged marginal ``population`` over
the count axis; ``"shot"`` keeps every shot as per-qubit integer levels
(``state @ (target, round_count, shot_idx)``) — the full-information /
more-memory trade, and the only mode from which the JOINT chain distribution can
be reconstructed (``estimate()`` derives it, so shot mode gains a second figure
without a second acquisition).

RECORD-ONLY for the DEVICE: there is no ``update()`` and nothing lands on the
device surface; the per-qubit summary lives in ``result.fit``. A scqat estimator
(``qc_unidirectional_trotter``) draws the transport curves and the joint map
under ``analysis/chain/``, but it only VISUALIZES them and proposes nothing; the
SUCCESS verdict (``min_transfer``) is made here in ``estimate()``.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
import xarray as xr
from pydantic import Field

from ..contract import DatasetContract
from ._capabilities.qubit_reset import QubitResetParameters
from ._capabilities.state_readout import (
    ReadoutModeParameters,
    states_to_joint_population,
)
from ._sim import stable_seed
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register

#: the artifact subfolder the whole-chain analysis writes into. A ROLE word, not
#: an entity name: the transport curves and the joint map belong to the chain as
#: a whole, so filing them under one member would misattribute them.
CHAIN_LABEL = "chain"


def chain_roles(roster, params) -> tuple[str, str, str, str]:
    """``(source, relay, sink, prep)`` — the chain topology, from the two pairs.

    The RELAY is the one member the two swap pairs share; the source and the
    sink are their remaining members. Everything else is derived or checked
    against the roster here, so a mis-wired chain refuses with one message
    naming every problem — before any instrument time is booked. Called from
    ``define_sweep`` (the earliest hook that can see both params and roster;
    ``validate_targets`` sees only targets) and reused by the driver probe.
    """
    problems: list[str] = []
    members: dict[str, list[str]] = {}
    for field in ("first_pair", "second_pair"):
        name = getattr(params, field)
        entity = roster.entities.get(name)
        if entity is None:
            problems.append(f"{field}={name!r} is not in the roster")
            continue
        if entity.kind != "qubit_pair":
            problems.append(
                f"{field}={name!r} is a {entity.kind!r}, not a qubit_pair")
            continue
        roles = getattr(entity, "roles", {}) or {}
        found = [m for role in ("high", "low") for m in roles.get(role, ())]
        if len(found) != 2:
            problems.append(
                f"{field}={name!r} declares {len(found)} high/low member(s) "
                f"{found} — a pair needs exactly one of each")
            continue
        members[field] = found
    if problems:
        raise ValueError("qc_unidirectional_trotter: " + "; ".join(problems))

    first, second = members["first_pair"], members["second_pair"]
    shared = [m for m in first if m in second]
    if len(shared) != 1:
        raise ValueError(
            f"qc_unidirectional_trotter: {params.first_pair} {first} and "
            f"{params.second_pair} {second} share {len(shared)} member(s) "
            f"{shared} — the chain needs exactly ONE, the relay both swaps touch")
    relay = shared[0]
    source = next(m for m in first if m != relay)
    sink = next(m for m in second if m != relay)
    prep = params.prep_qubit or source

    # The parametric reset rides the reset qubit's own z line, and every Stark
    # tone rides its qubit's drive line — both are channel-existence questions,
    # which is the greenfield capability gate (see _flux_component.py).
    if (params.reset_qubit, "flux") not in roster.defaults:
        problems.append(
            f"reset_qubit={params.reset_qubit!r} has no flux channel — the "
            f"parametric reset is played on the qubit's own z line")
    if (prep, "drive") not in roster.defaults:
        problems.append(
            f"prep_qubit={prep!r} has no drive channel — nothing to play "
            f"{params.prep_operation!r} on")
    for qubit in sorted(params.compensation_amps):
        if (qubit, "drive") not in roster.defaults:
            problems.append(
                f"compensation_amps names {qubit!r}, which has no drive "
                f"channel — nothing to play {params.stark_operation!r} on")
    # The swap coupler flux names PAIRS, and only the two the chain declares:
    # a third name is a typo that would otherwise be silently ignored.
    for pair in sorted(getattr(params, "swap_coupler_flux", {}) or {}):
        if pair not in (params.first_pair, params.second_pair):
            problems.append(
                f"swap_coupler_flux names {pair!r}, which is neither first_pair "
                f"({params.first_pair!r}) nor second_pair ({params.second_pair!r})")
            continue
        entity = roster.entities.get(pair)
        couplers = (getattr(entity, "roles", {}) or {}).get("coupler", ())
        if not couplers:
            problems.append(
                f"swap_coupler_flux names {pair!r}, which declares no coupler role "
                f"— there is no coupler flux to set")
        elif (couplers[0], "flux") not in roster.defaults:
            problems.append(
                f"swap_coupler_flux names {pair!r}, whose coupler {couplers[0]!r} "
                f"has no flux channel")
    if problems:
        raise ValueError("qc_unidirectional_trotter: " + "; ".join(problems))
    return source, relay, sink, prep


class QcUnidirectionalTrotterParameters(TargetSelection, AveragingParameters,
                                        QubitResetParameters, ReadoutModeParameters):
    """Inputs for the unidirectional-coupling Trotter chain. ``targets`` are the
    chain QUBITS, in chain order."""

    first_pair: str = Field(
        ...,
        description="Pair component swapped FIRST each round, carrying the excitation "
                    "from the chain source into the relay (e.g. 'q1_q2').")
    second_pair: str = Field(
        ...,
        description="Pair component swapped SECOND each round, carrying the relay into "
                    "the chain sink (e.g. 'q2_q3'). It must share exactly ONE member "
                    "with first_pair — that shared member is the relay.")
    reset_qubit: str = Field(
        ...,
        description="Qubit whose parametric-reset macro fires after both swaps, dumping "
                    "the relay. Normally the relay itself: emptying it is what removes "
                    "the sink's return path and makes the coupling one-way. Its flux "
                    "(z) channel must exist — the reset is a flux-line technique.")
    compensation_amps: dict[str, float] = Field(
        default_factory=dict,
        description="Per-qubit AC-Stark compensation, as {qubit name: amplitude FACTOR "
                    "of the stark operation's baked amplitude}. The tone is off-resonant "
                    "(stark_detuning_hz), so it SHIFTS the qubit rather than rotating it, "
                    "and the shift integrated over the pulse is the phase it compensates. "
                    "A qubit absent from the map gets no tone; {} disables compensation "
                    "entirely. Dimensionless, and each factor must stay inside the "
                    "instrument's amplitude-scale range (the driver refuses by name).")
    max_rounds: int = Field(
        20, ge=0,
        description="N_max: the Trotter step is applied 0, 1, ... max_rounds times, one "
                    "sweep point each. 0 is the prep-only baseline, so the axis always "
                    "carries max_rounds + 1 points.")
    swap_operation: str = Field(
        "partial_swap",
        description="The named pair operation repeated on BOTH pairs each round, played "
                    "at its FIXED baked amplitude (the driver resolves it on each vendor "
                    "pair). A partial swap — not a full iswap — is the Trotter step: the "
                    "angle is what discretizes the continuous cascaded coupling.")
    reset_operation: str = Field(
        "reset",
        description="The named MACRO on reset_qubit applied each round — the mid-circuit "
                    "parametric reset (quam_config/register_reset_macro.py registers it "
                    "under this key; the z pulse it plays is a separate name). This is "
                    "NOT reset_method, which is how targets return to |g> BETWEEN shots.")
    swap_coupler_flux: dict[str, float] = Field(
        default_factory=dict,
        description="Per-pair COUPLER flux amplitude for the swap, as {pair name: volts "
                    "at the DAC}. This is the swap ANGLE knob (TUTORIAL section 12: the "
                    "member's own flux sets resonance, the coupler sets the angle), so "
                    "this is how a run picks a (theta_1, theta_2) pair without "
                    "re-registering pulses. Absolute VOLTS, not an angle and not a "
                    "factor: theta = J_eff(Phi_c) * t_p is not linear in coupler flux, "
                    "so calibrate the curve with pair_swap_angle and read the volts off "
                    "it. A pair absent from the map plays its baked coupler amplitude; "
                    "{} leaves both pairs alone, which is the pre-existing behaviour. "
                    "Only first_pair and second_pair may be named, and the driver "
                    "refuses a coupler that has no flux channel or is baked at zero "
                    "amplitude (unsettable, since it is the divisor of the "
                    "volts->amplitude-scale conversion).")
    stark_operation: str = Field(
        "stark",
        description="The named XY (RF) operation played on each compensated qubit at the "
                    "end of a round; its baked amplitude is the reference each "
                    "compensation factor multiplies. NOT x180 — a dedicated off-resonant "
                    "tone (the driver refuses by name if the operation is missing).")
    stark_detuning_hz: float = Field(
        50e6,
        description="FIXED off-resonant detuning (Hz) of the compensation tones from each "
                    "qubit's drive frequency. Must be off-resonant for a genuine Stark "
                    "shift (a resonant tone drives Rabi rotations instead); tune per chip. "
                    "Not a sweep axis, and shared by every compensated qubit.")
    prep_qubit: str | None = Field(
        None,
        description="Which qubit is excited ONCE before the rounds begin. None = the "
                    "chain source (the first_pair member that is not the relay), which is "
                    "the normal case; set it to watch the chain from somewhere else.")
    prep_operation: str = Field(
        "x180",
        description="The named XY operation that prepares the excitation on prep_qubit.")
    operation_gap_ns: int = Field(
        0, ge=0,
        description="Idle gap (ns) inserted between the operations of a round, so each "
                    "flux pulse settles before the next fires. 0 disables; the QM backend "
                    "requires a multiple of 4 ns.")
    min_transfer: float = Field(
        0.05, ge=0.0, le=1.0,
        description="Peak SINK population below which the run is reported FAILED — no "
                    "transport was seen along the chain. The floor is LOW on purpose and "
                    "is not the pair experiments' near-full-swap threshold: this chain "
                    "dumps the relay every round, so the sink also decays and settles "
                    "near (first-swap transfer) x (source population) — a peak around 0.1 "
                    "is healthy, not broken. The verdict needs the sink among the "
                    "targets; a run that does not read it out cannot judge transport and "
                    "reports FAILED. Pure reporting: this experiment writes nothing back.")


class QcUnidirectionalTrotterResult(Result):
    """``fit[qubit]``: that qubit's transport summary over the count axis —
    ``p_initial`` / ``p_final`` / ``p_max`` / ``p_min`` and ``n_at_max`` (the
    Trotter-step count where it peaks) — plus the run-wide ``sink_p_max`` and
    ``n_round_count``, repeated on every row so one target's record is readable
    on its own.

    The OUTCOME is the chain's, not the qubit's: SUCCESSFUL requires both that
    this qubit's own trace is finite AND that the sink's peak population reached
    ``min_transfer``, because a qubit's curve says nothing about transport on
    its own. Record-only: no ``update()``, nothing written to the device."""


def _exchange_matrix(i: int, j: int, p: float, n_modes: int) -> np.ndarray:
    """Row-stochastic map: exchange bits ``i`` and ``j`` with probability ``p``.

    The classical shadow of a partial swap, and exact for every basis input —
    the exchange is the identity on ``|00>`` and ``|11>``, and sends ``|10>`` to
    ``|01>`` with probability ``sin^2(theta)`` on the single-excitation
    subspace, which is what a readout in the computational basis sees."""
    size = 1 << n_modes
    out = np.zeros((size, size))
    for state in range(size):
        bits = [(state >> (n_modes - 1 - k)) & 1 for k in range(n_modes)]
        out[state, state] += 1.0 - p
        bits[i], bits[j] = bits[j], bits[i]
        swapped = sum(b << (n_modes - 1 - k) for k, b in enumerate(bits))
        out[state, swapped] += p
    return out


def _decay_matrix(i: int, p: float, n_modes: int) -> np.ndarray:
    """Row-stochastic map: bit ``i`` drops to 0 with probability ``p``.

    Serves BOTH the relay's parametric reset (a large p, on purpose) and each
    qubit's per-round relaxation (a small one) — they are the same channel with
    different rates, so writing it twice would be two chances to get it wrong."""
    size = 1 << n_modes
    out = np.zeros((size, size))
    for state in range(size):
        bit = (state >> (n_modes - 1 - i)) & 1
        if bit == 0:
            out[state, state] = 1.0
            continue
        out[state, state] += 1.0 - p
        out[state, state & ~(1 << (n_modes - 1 - i))] += p
    return out


@register
class QcUnidirectionalTrotter(Experiment):
    """Backend-agnostic unidirectional-coupling Trotter chain. ``probe()`` is
    supplied by a driver."""

    name: ClassVar[str] = "qc_unidirectional_trotter"
    description: ClassVar[str] = (
        "Unidirectional (cascaded) coupling by Trotterization on a three-qubit chain: "
        "excite the chain source once, then repeat N times a partial swap source->relay, "
        "a partial swap relay->sink, a parametric reset of the relay and a per-qubit "
        "off-resonant AC-Stark phase compensation, reading every chain qubit out at the "
        "end. Dumping the relay each round destroys the sink's return path, so the "
        "coupling is one-way and the populations vs N show the excitation walking from "
        "source to sink while the relay stays empty. readout_mode='shot' keeps every shot "
        "and additionally reconstructs the JOINT chain distribution. Record-only "
        "diagnostic: the per-qubit summary lands in result.fit and nothing is written "
        "back to the device."
    )
    Parameters: ClassVar[type] = QcUnidirectionalTrotterParameters
    Result: ClassVar[type] = QcUnidirectionalTrotterResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("round_count",), sweep_units=("",),
        # readout_mode="average": each chain qubit's averaged marginal...
        variables=("population",), readout_dims=(),
        # ...readout_mode="shot": every shot's per-qubit integer level.
        alt_variables=(("state",),),
        alt_readout_dims=(("shot_idx",),),
    )
    #: none, deliberately — same rationale as pair_swap_chevron and the two
    #: qc_n_* maps: a composite's operations are DECLARED, and this sequence is
    #: run while the two-qubit gates are still being brought up. Requiring
    #: "partial_swap" would refuse exactly the chip it is for.
    required_operations: ClassVar[tuple[str, ...]] = ()

    params: QcUnidirectionalTrotterParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        # The roster gate for the pair/reset/compensation PARAMETERS lives here:
        # validate_targets cannot see params, and this still runs before the
        # backend is asked for anything.
        chain_roles(self.device.roster, self.params)
        return {"round_count": np.arange(0, self.params.max_rounds + 1, dtype=int)}

    def readout_coords(self) -> dict:
        if self.params.readout_mode == "shot":
            return {"shot_idx": np.arange(self.params.num_averages)}
        return {}

    def _round_map(self, rng, n_modes: int = 3) -> np.ndarray:
        """One Trotter step as a row-stochastic map over the chain's basis
        states (bit order source, relay, sink)."""
        # Partial-swap angles. The FIRST is small — a round has to be a Trotter
        # SLICE of a continuous coupling, not a full exchange, so the source
        # empties over many rounds. The SECOND is large: draining the relay into
        # the sink faster than the source refills it is what keeps the relay near
        # |0> and makes the reset a drain rather than the dominant loss.
        p_first = float(np.sin(rng.uniform(0.30, 0.50)) ** 2)
        p_second = float(np.sin(rng.uniform(0.80, 1.10)) ** 2)
        reset_fidelity = float(rng.uniform(0.90, 0.99))
        relaxation = float(rng.uniform(0.005, 0.02))   # per round, per qubit
        step = (_exchange_matrix(0, 1, p_first, n_modes)
                @ _exchange_matrix(1, 2, p_second, n_modes)
                @ _decay_matrix(1, reset_fidelity, n_modes))
        for mode in range(n_modes):
            step = step @ _decay_matrix(mode, relaxation, n_modes)
        return step

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """The Trotter step as a stochastic map on the chain's 8 basis states.

        Not per-qubit marginals with independent draws: both swaps CORRELATE the
        members (an excitation is in one place at a time), so a product-of-
        marginals simulator would draw a joint distribution the sequence can
        never produce. The chain state is propagated as a distribution over
        ``(source, relay, sink)`` basis states — exchange, exchange, relay reset,
        relaxation — and the marginals are its partial trace, so shot mode draws
        genuinely correlated outcomes.

        A target that is not a chain member is initialized and read out but
        never touched, so it stays in |0>."""
        n = np.asarray(coords["round_count"]).astype(int)
        targets = list(self.params.targets)
        source, relay, sink, prep = chain_roles(self.device.roster, self.params)
        chain = [source, relay, sink]
        rng = np.random.default_rng(stable_seed("qc_unidirectional_trotter", *targets))
        step = self._round_map(rng)

        # The prep: |100> in chain order (source excited), up to pi-pulse
        # fidelity. A prep_qubit off the chain leaves the chain in |000>.
        dist = np.zeros(8)
        prep_fidelity = float(rng.uniform(0.94, 0.99))
        excited = chain.index(prep) if prep in chain else None
        dist[0] = 1.0 if excited is None else 1.0 - prep_fidelity
        if excited is not None:
            dist[1 << (2 - excited)] = prep_fidelity

        history = [dist]
        for _ in range(int(n.max()) if n.size else 0):
            history.append(history[-1] @ step)
        # (round, joint_state) — index BY VALUE, so a non-contiguous count axis
        # would still line up.
        joint = np.stack([history[int(v)] for v in n])

        codes = np.arange(8)
        bits = np.stack([(codes >> (2 - k)) & 1 for k in range(3)])   # (3, 8)
        shot_mode = self.params.readout_mode == "shot"
        num_shots = int(self.params.num_averages)

        if shot_mode:
            # One draw of the JOINT outcome per (round, shot) — inverse CDF —
            # then split into the members' levels, so the correlations survive.
            u = rng.random((n.size, num_shots))
            cumulative = np.cumsum(joint, axis=1)                      # (round, 8)
            code = (u[:, :, None] > cumulative[:, None, :]).sum(axis=2)
            code = np.clip(code, 0, 7)
            rows = []
            for name in targets:
                if name in chain:
                    rows.append(bits[chain.index(name)][code])
                else:
                    rows.append(np.zeros_like(code))

            return {"state": (("target", "round_count", "shot_idx"),
                              np.stack(rows).astype(np.int64))}

        marginals = joint @ bits.T                                     # (round, 3)
        rows = []
        for name in targets:
            if name in chain:
                row = marginals[:, chain.index(name)]
            else:
                row = np.zeros(n.size)
            rows.append(np.clip(row + rng.normal(0.0, 0.01, n.size), 0.0, 1.0))
        return {"population": (("target", "round_count"), np.stack(rows))}

    def estimate(self) -> QcUnidirectionalTrotterResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        source, relay, sink, _prep = chain_roles(self.device.roster, self.params)
        targets = [str(t) for t in np.atleast_1d(self.dataset["target"].values)]

        if "state" in self.dataset.data_vars:
            # shot mode: reduce per-shot levels to the same averaged marginals
            # average mode stores, AND reconstruct the joint distribution the
            # shots make available for free (the target order is the digit
            # order — the chain order, as the module docstring states).
            state = self.dataset["state"].transpose("target", "round_count", "shot_idx")
            prepared = xr.Dataset({
                "population": state.clip(0, 1).mean("shot_idx"),
                "joint_population": states_to_joint_population(
                    state, member_dim="target", shot_dim="shot_idx"),
            })
        else:
            prepared = self.dataset[["population"]]
        prepared = prepared.rename({"target": "qubit"})

        # ONE analysis over the whole chain: the joint distribution spans every
        # member, so per_qubit_results' per-target split cannot draw it.
        from scqat.estimators.qc_unidirectional_trotter import (
            QcUnidirectionalTrotterEstimator,
        )
        from .._scqat import whole_dataset_results

        analysis = whole_dataset_results(
            prepared, QcUnidirectionalTrotterEstimator(),
            artifact_dir=self.artifact_dir, label=CHAIN_LABEL,
            source=source, relay=relay, sink=sink)

        per_qubit = analysis.get("per_qubit", {})
        sink_peak = float(analysis.get("sink_p_max", float("nan")))
        chain_ok = bool(np.isfinite(sink_peak) and sink_peak >= self.params.min_transfer)
        result = QcUnidirectionalTrotterResult()
        for name in targets:
            fit = {k: float(v) for k, v in per_qubit.get(name, {}).items()}
            fit["sink_p_max"] = sink_peak
            fit["n_round_count"] = float(analysis.get("n_round_count", 0))
            result.fit[name] = fit
            own_ok = bool(np.isfinite(fit.get("p_max", float("nan"))))
            result.outcomes[name] = (Outcome.SUCCESSFUL if own_ok and chain_ok
                                     else Outcome.FAILED)
        return result

    @classmethod
    def validate_targets(cls, roster, targets):
        """Every target is initialized, pulsed and read out in ONE circuit, so
        each needs a drive channel of its own. The chain topology itself
        (pairs, relay, reset qubit) is checked in ``define_sweep`` — those live
        in Parameters, which this hook cannot see."""
        return [f"{t}: no drive channel — it cannot be initialized or excited"
                for t in targets if (t, "drive") not in roster.defaults]

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
