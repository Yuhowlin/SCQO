"""Trotter-chain AC-Stark compensation scan — record-only.

The same chain ``qc_unidirectional_trotter`` runs, with ONE extra swept axis: the
AC-Stark compensation amplitude on a single chosen qubit. One run replaces the
hand scan that finding the compensation phase used to be — and because the round
axis is swept too, the slice at the optimum IS the population-vs-N curve, so the
run answers "what phase?" and "what transport?" at once.

WHY ONE AMPLITUDE AND NOT THREE. Propagating the chain's single-excitation
amplitudes through one round (swap source->relay, swap relay->sink, reset the
relay, Stark tones) gives

    |K_M| = sin(t1) sin(t2) |S0| * |SUM_j cos^(M-1-j)(t2) cos^j(t1) e^{i j dPhi}|

with ``dPhi = phi_source - phi_sink``. Only that DIFFERENCE survives: a
common-mode phase factors out, so there is one number to calibrate, not one per
qubit. Two consequences the Parameters enforce:

* the tone on the RESET qubit is inert — it fires after the reset, onto a qubit
  that has just been emptied — so ``compensation_target`` refuses it by name;
* the sum peaks at ``dPhi = 0``, so the sweep has a single optimum, and sweeping
  the source or the sink covers opposite signs of ``dPhi`` (the Stark shift's
  sign is fixed by ``stark_detuning_hz``, which is shared).

WHY THE ROUND AXIS IS NOT OPTIONAL. When the rounds cancel, only the LAST one
contributes and the sink peaks at ``N = 1``; when they add, the peak moves out to
several rounds. So ``n_at_max`` reads the phase condition independently of, and
more sharply than, the peak height — and it only exists if N is swept. The
estimator reports both.

The chain itself — the two pairs, the relay, the reset and Stark operations, the
gap, the swap coupler flux — is inherited from
``QcUnidirectionalTrotterParameters`` unchanged, so the scan and the run it
calibrates can never disagree about what the round is.

READOUT (the unified readout schema): digital, both modes. ``readout_mode=
"average"`` (default) stores each chain qubit's averaged marginal ``population``;
``"shot"`` keeps every shot as per-qubit integer levels. Unlike the chain
experiment this one does NOT reconstruct the joint distribution from shot mode:
the extra amplitude axis makes it a four-dimensional object that answers nothing
the scan asks. Shot mode is for keeping the raw record, and ``estimate()``
reduces it to the same marginals average mode stores.

RECORD-ONLY for the DEVICE: there is no ``update()``. A Stark compensation factor
is a per-run sequence choice with no vendor home — there is no knob to write it
to — so the optimum lands in ``result.fit`` and is passed back as the
``compensation_amps`` parameter of the next run.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
import xarray as xr
from pydantic import Field

from ..contract import DatasetContract
from ._sim import stable_seed
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register
from .qc_unidirectional_trotter import (
    CHAIN_LABEL,
    QcUnidirectionalTrotterParameters,
    chain_roles,
)


class QcTrotterCompensationParameters(QcUnidirectionalTrotterParameters):
    """Inputs for the compensation scan. ``targets`` are the chain QUBITS, in
    chain order — the same shape the chain experiment takes, plus the swept
    amplitude and the qubit that carries it."""

    compensation_target: str = Field(
        ...,
        description="The chain qubit whose AC-Stark tone is SWEPT. Must be the chain "
                    "source or the chain sink: only the differential source-sink phase "
                    "is observable, and a tone on the relay fires after that qubit has "
                    "been reset, so sweeping it measures nothing (refused by name). "
                    "Sweeping the source and sweeping the sink reach opposite signs of "
                    "the differential phase, so pick the one whose scan shows an optimum "
                    "inside the amplitude range.")
    min_compensation_amp: float = Field(
        0.0,
        description="Lowest swept compensation amplitude, as a dimensionless FACTOR of "
                    "the stark operation's baked amplitude. 0 is the no-compensation "
                    "baseline and is worth keeping in the window: it is what the chain "
                    "does today, and the scan is only worth acting on if it beats it.")
    max_compensation_amp: float = Field(
        1.0,
        description="Highest swept compensation amplitude factor. The induced phase "
                    "grows with amplitude, so the window must be wide enough to reach a "
                    "full 2*pi if the optimum is to be found for ANY residual phase — "
                    "measure the phase-per-amplitude curve with qubit_stark_phase_echo "
                    "first and read the range off it. Each factor must stay inside the "
                    "instrument's amplitude-scale range (the driver refuses by name).")
    num_amp_points: int = Field(
        21, gt=4, description="Number of compensation-amplitude points.")


class QcTrotterCompensationResult(Result):
    """``fit[qubit]``: the run-wide optimum — ``best_compensation_amp``,
    ``best_sink_p_max``, ``best_n_at_max``, ``worst_compensation_amp`` and
    ``contrast`` (best/worst sink peak; ~1.0 means the phase knob does nothing on
    this chain) — repeated on every row so one target's record is readable on its
    own, plus that qubit's own transport summary AT the optimum
    (``p_initial`` / ``p_final`` / ``p_max`` / ``p_min`` / ``n_at_max``).

    The OUTCOME is the chain's: SUCCESSFUL requires an optimum to have been found
    AND the sink peak there to reach ``min_transfer``. Record-only: no
    ``update()``, nothing written to the device."""


def _trace_summary(rounds: np.ndarray, trace: np.ndarray) -> dict[str, float]:
    """One qubit's population-vs-N summary at the optimum. All-NaN gives NaN."""
    trace = np.asarray(trace, dtype=float)
    out = {
        "p_initial": float(trace[0]) if trace.size else float("nan"),
        "p_final": float(trace[-1]) if trace.size else float("nan"),
    }
    if not np.isfinite(trace).any():
        return {**out, "p_max": float("nan"), "p_min": float("nan"),
                "n_at_max": float("nan")}
    i = int(np.nanargmax(trace))
    return {**out, "p_max": float(trace[i]), "p_min": float(np.nanmin(trace)),
            "n_at_max": float(rounds[i])}


@register
class QcTrotterCompensation(Experiment):
    """Backend-agnostic Trotter-chain compensation scan. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "qc_trotter_compensation"
    description: ClassVar[str] = (
        "Trotter-chain AC-Stark compensation scan: run the unidirectional-coupling chain "
        "over a 2-D sweep of ONE qubit's Stark compensation amplitude against the Trotter-"
        "step count, and report the amplitude that maximises transport to the sink. Only "
        "the differential source-sink phase is observable, so one amplitude is swept and "
        "the reset qubit is refused as the target. Sweeping N too is what makes the answer "
        "trustworthy: cancelling rounds leave the sink peaking at N=1 while adding rounds "
        "push the peak out to several, so n_at_max reads the phase condition independently "
        "of the peak height — and the slice at the optimum is the population-vs-N curve, so "
        "one run gives both the phase and the transport. Record-only diagnostic: the "
        "optimum lands in result.fit and is fed back as the next run's compensation_amps."
    )
    Parameters: ClassVar[type] = QcTrotterCompensationParameters
    Result: ClassVar[type] = QcTrotterCompensationResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("compensation_amp", "round_count"), sweep_units=("", ""),
        # readout_mode="average": each chain qubit's averaged marginal...
        variables=("population",), readout_dims=(),
        # ...readout_mode="shot": every shot's per-qubit integer level.
        alt_variables=(("state",),),
        alt_readout_dims=(("shot_idx",),),
    )
    #: none, deliberately — the same rationale as the chain experiment this
    #: calibrates: the sequence is run while the two-qubit gates are still being
    #: brought up, so requiring "partial_swap" would refuse the chip it is for.
    required_operations: ClassVar[tuple[str, ...]] = ()

    params: QcTrotterCompensationParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        # The roster gate for the chain PARAMETERS lives here (validate_targets
        # cannot see params), and so does the compensation_target gate — both
        # before the backend is asked for anything.
        source, _relay, sink, _prep = chain_roles(self.device.roster, self.params)
        target = self.params.compensation_target
        if target not in (source, sink):
            raise ValueError(
                f"qc_trotter_compensation: compensation_target={target!r} must be the "
                f"chain source ({source!r}) or the chain sink ({sink!r}). Only the "
                f"DIFFERENTIAL source-sink phase is observable, and the tone on the "
                f"reset qubit fires after it has been emptied, so sweeping anything "
                f"else measures nothing.")
        if target in self.params.compensation_amps:
            raise ValueError(
                f"qc_trotter_compensation: {target!r} is both compensation_target (the "
                f"SWEPT tone) and a key of compensation_amps (the tones held FIXED) — "
                f"two sources of truth for one amplitude. Remove it from "
                f"compensation_amps.")
        return {
            # dict order IS the contract order: amplitude outer, round count inner.
            "compensation_amp": np.linspace(self.params.min_compensation_amp,
                                            self.params.max_compensation_amp,
                                            self.params.num_amp_points),
            "round_count": np.arange(0, self.params.max_rounds + 1, dtype=int),
        }

    def readout_coords(self) -> dict:
        if self.params.readout_mode == "shot":
            return {"shot_idx": np.arange(self.params.num_averages)}
        return {}

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """The COHERENT round recursion — amplitudes, not a stochastic map.

        The chain experiment's own simulator propagates a probability
        distribution over basis states, which is right for the question it asks
        and useless for this one: phases do not exist in it, so the effect this
        scan measures could never appear. Here the single-excitation AMPLITUDES
        are propagated instead,

            S <- cos(t1) e^{i dPhi} S
            K <- cos(t2) K - sin(t1) sin(t2) S

        with the whole differential phase carried on the source (only the
        difference is physical, so where it is placed is a bookkeeping choice),
        and ``dPhi(a) = phi_err + k a^2`` — the residual per-round phase plus the
        quadratic AC-Stark shift the swept tone adds. The relay is emptied every
        round, so it holds only the reset's leakage.

        A target that is not a chain member is initialized and read out but never
        touched, so it stays in |0>.
        """
        amps = np.asarray(coords["compensation_amp"], dtype=float)
        n = np.asarray(coords["round_count"]).astype(int)
        targets = list(self.params.targets)
        source, relay, sink, prep = chain_roles(self.device.roster, self.params)
        chain = [source, relay, sink]
        rng = np.random.default_rng(stable_seed("qc_trotter_compensation", *targets))

        theta1 = float(rng.uniform(0.45, 0.75))
        theta2 = float(rng.uniform(0.45, 0.75))
        prep_fidelity = float(rng.uniform(0.94, 0.99))
        relaxation = float(rng.uniform(0.005, 0.02))     # per round, per qubit
        reset_leak = float(rng.uniform(0.005, 0.02))     # what the reset leaves
        # The residual phase, and the Stark coefficient that cancels it. The
        # optimum is planted INSIDE the swept window so the offline round trip
        # exercises a real peak rather than an edge.
        phi_err = float(rng.uniform(-2.0, -0.5))
        a_opt = float(rng.uniform(0.35, 0.7)) * (amps.max() or 1.0)
        stark_k = -phi_err / (a_opt ** 2) if a_opt else 0.0

        excited = chain.index(prep) if prep in chain else None
        n_max = int(n.max()) if n.size else 0
        source_map = np.zeros((amps.size, n.size))
        sink_map = np.zeros((amps.size, n.size))
        for i, amp in enumerate(amps):
            dphi = phi_err + stark_k * amp ** 2
            a = np.cos(theta1) * np.exp(1j * dphi)
            b = np.cos(theta2)
            c = -np.sin(theta1) * np.sin(theta2)
            s = prep_fidelity ** 0.5 if excited == 0 else 0.0
            s, k = complex(s), 0.0 + 0j
            hist_s, hist_k = [abs(s) ** 2], [abs(k) ** 2]
            for step in range(n_max):
                s, k = a * s, b * k + c * s      # K uses the OLD S: assign together
                loss = float(np.exp(-relaxation * (step + 1)))
                hist_s.append(abs(s) ** 2 * loss)
                hist_k.append(abs(k) ** 2 * loss)
            # index BY VALUE, so a non-contiguous count axis still lines up
            source_map[i] = [hist_s[int(v)] for v in n]
            sink_map[i] = [hist_k[int(v)] for v in n]

        relay_map = np.full((amps.size, n.size), reset_leak)
        by_role = {source: source_map, relay: relay_map, sink: sink_map}
        rows = []
        for name in targets:
            row = by_role.get(name, np.zeros((amps.size, n.size)))
            rows.append(np.clip(row + rng.normal(0.0, 0.008, row.shape), 0.0, 1.0))
        pop = np.stack(rows)                                  # (target, amp, round)

        if self.params.readout_mode == "shot":
            num_shots = int(self.params.num_averages)
            draws = rng.random((len(targets), amps.size, n.size, num_shots))
            state = (draws < pop[..., None]).astype(np.int64)
            return {"state": (("target", "compensation_amp", "round_count", "shot_idx"),
                              state)}
        return {"population": (("target", "compensation_amp", "round_count"), pop)}

    def estimate(self) -> QcTrotterCompensationResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        source, relay, sink, _prep = chain_roles(self.device.roster, self.params)
        targets = [str(t) for t in np.atleast_1d(self.dataset["target"].values)]

        if "state" in self.dataset.data_vars:
            # shot mode: reduce per-shot levels to the same averaged marginals
            # average mode stores. No joint form here — see the module docstring.
            state = self.dataset["state"].transpose(
                "target", "compensation_amp", "round_count", "shot_idx")
            prepared = xr.Dataset({"population": state.clip(0, 1).mean("shot_idx")})
        else:
            prepared = self.dataset[["population"]]
        prepared = prepared.rename({"target": "qubit"})

        # ONE analysis over the whole chain: the optimum is read off the SINK
        # while the curves reported at it span every member, so per_qubit_results'
        # per-target split cannot produce it.
        from scqat.estimators.qc_trotter_compensation import (
            QcTrotterCompensationEstimator,
        )
        from .._scqat import whole_dataset_results

        analysis = whole_dataset_results(
            prepared, QcTrotterCompensationEstimator(),
            artifact_dir=self.artifact_dir, label=CHAIN_LABEL,
            source=source, relay=relay, sink=sink,
            compensation_target=self.params.compensation_target)

        rounds = np.asarray(self.dataset["round_count"].values, dtype=float)
        curves = analysis.get("per_qubit_at_best", {})
        run_wide = {
            key: float(analysis.get(key, float("nan")))
            for key in ("best_compensation_amp", "best_sink_p_max", "best_n_at_max",
                        "worst_compensation_amp", "worst_sink_p_max", "contrast")
        }
        sink_peak = run_wide["best_sink_p_max"]
        chain_ok = bool(analysis.get("success", False)
                        and np.isfinite(sink_peak)
                        and sink_peak >= self.params.min_transfer)

        result = QcTrotterCompensationResult()
        for name in targets:
            fit = dict(run_wide)
            fit["n_compensation_amp"] = float(analysis.get("n_compensation_amp", 0))
            fit["n_round_count"] = float(analysis.get("n_round_count", 0))
            trace = curves.get(name)
            fit.update(_trace_summary(rounds, np.asarray(trace, dtype=float))
                       if trace is not None
                       else {key: float("nan") for key in
                             ("p_initial", "p_final", "p_max", "p_min", "n_at_max")})
            result.fit[name] = fit
            result.outcomes[name] = (Outcome.SUCCESSFUL if chain_ok else Outcome.FAILED)
        return result

    @classmethod
    def validate_targets(cls, roster, targets):
        """Every target is initialized, pulsed and read out in ONE circuit, so
        each needs a drive channel of its own. The chain topology itself (pairs,
        relay, reset qubit, the swept target) is checked in ``define_sweep`` —
        those live in Parameters, which this hook cannot see."""
        return [f"{t}: no drive channel — it cannot be initialized or excited"
                for t in targets if (t, "drive") not in roster.defaults]

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
