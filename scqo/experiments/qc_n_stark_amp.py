"""N-swap x AC-Stark-amplitude error-amplification map — record-only.

Excite ONE member of a pair, then apply **N** swaps in a chain — each the SAME
swap operation at its FIXED, baked control-qubit flux amplitude — and after every
swap play an **off-resonant AC-Stark tone** on the excited qubit at the SAME
swept amplitude, reading BOTH members out jointly. The Stark tone shifts the
qubit's frequency by an amount that grows with its amplitude, detuning the
exchange; the swap-chain amplifies a small residual detuning. On the stark
amplitude that compensates it the excitation ping-pongs cleanly between the two
members with N, while off it the pattern smears and drifts, and the drift grows
with the number of swaps. The joint populations vs (stark amplitude, N) draw a
Stark-amplitude fine-tuning map.

This is the AC-Stark sibling of ``qc_n_swap_amp``: that experiment sweeps the
swap's own flux amplitude to find the swap resonance; this one holds the swap at
its baked amplitude and instead sweeps the amplitude of a separate, off-resonant
RF (XY) drive — a NEW named ``stark`` operation, deliberately not ``x180`` — that
induces an AC-Stark shift. The count axis is the analogue of
``pair_swap_chevron``'s pulse-duration axis: it trades time resolution for
error-amplification sensitivity (N=0 is the x180-only baseline).

The Stark tone must be OFF-RESONANT to shift rather than rotate the qubit; that
detuning is a FIXED parameter (``stark_detuning_hz``), realized by the driver in
the vendor sequence — only the tone's AMPLITUDE is swept.

READOUT (the unified readout schema): digital, both modes. ``readout_mode=
"average"`` (default) stores the pair's ``joint_population`` over ``joint_state``
labels; ``"shot"`` keeps every shot as per-member integer levels
(``state @ (target, member, *sweeps, shot_idx)``, member order high, low) — the
full-information / more-memory trade. ``estimate()`` reduces the shot form to the
same joint distribution before analysis, so both modes yield identical maps.

RECORD-ONLY for the DEVICE: there is no ``update()`` and nothing lands on the
device surface; the summary lives in ``result.fit``. The scqat estimator
(``qc_n_stark_amp``) draws the raw joint state populations — a per-pair 2x2
population figure plus plotdata/metadata under ``analysis/<pair>/`` — and READS
THE COMPENSATING STARK AMPLITUDE off the map: at each stark amplitude it measures
the transfer's oscillation along the swap count, and the compensating amplitude
is the one whose oscillation is the STRONGEST (contrast is maximal when the
residual detuning is nulled) and the SLOWEST (the per-swap composite angle, and
hence the oscillation frequency, bottoms out there). Both criteria, the combined
pick (also interpolated between swept amplitudes) and its ``theta_eff`` are
lifted into ``result.fit``; nothing is proposed and nothing is written back, and
the SUCCESS verdict (``min_transfer``) is still made here in ``estimate()``, not
by the estimator.

THE READING ASSUMES NO ROW SWAPS BY MORE THAN pi/2 — every period at least TWO
counts, the Nyquist period of an integer-N axis. Past that an oscillation aliases
and reads SLOWER the faster it truly is, which inverts the slowest-period
criterion; a FULL iswap ping-pongs at exactly that limit, so this map wants a
PARTIAL ``swap_operation`` and a stark window narrow enough to keep the fastest
row clear of it. Nothing in the data can reveal a violation — an aliased row is
indistinguishable from a slow one — so the assumption is the OPERATOR's to keep
and ``min_osc_period`` (in ``fit``) is the dashboard: the map's fastest row in
counts per cycle, ``pi`` over it being the largest per-swap angle. The 5Q4C q1_q2
map of 2026-09-08 ran at 2.02 — inside by one percent — and resolved 4.5 counts
per cycle at its compensation point, where both criteria agreed.
"""

from __future__ import annotations

from typing import ClassVar, Literal

import numpy as np
from pydantic import Field

from ..contract import DatasetContract
from ._capabilities.qubit_reset import QubitResetParameters
from ._capabilities.state_readout import (
    ReadoutModeParameters,
    joint_state_labels,
    states_to_joint_population,
)
from ._sim import stable_seed
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register
from .pair_swap_chevron import (
    DRIVE_SIDE_DESC,
    FLUX_SIDE_DESC,
    MIN_TRANSFER_DESC,
    _flux_member_problems,
    _role_names,
    summarize_transfer_map,
)


#: the compensation scalars lifted from the scqat estimator's results into
#: ``fit`` — the pick, each criterion on its own, and how far to trust them.
COMPENSATION_KEYS = (
    "compensating_stark_amp", "compensating_stark_amp_refined",
    "compensating_is_refined", "compensation_score",
    "compensating_osc_contrast", "compensating_osc_period",
    "compensating_theta_rad",
    "max_osc_contrast_stark_amp", "max_osc_contrast",
    "max_osc_period_stark_amp", "max_osc_period", "min_osc_period",
    "n_osc_ok", "osc_criteria_agree",
)

#: counts per cycle the SIMULATED map's fastest row lands on. Two would be pi/2 a
#: swap — the limit the estimator's period reading assumes no row crosses — so
#: the offline model stays clear of it, as a real sweep must.
COUNTS_AT_EDGE = 2.5


class QcNStarkAmpParameters(TargetSelection, AveragingParameters,
                            QubitResetParameters, ReadoutModeParameters):
    """Inputs for the N-swap AC-Stark amplitude map. ``targets`` are PAIR components."""

    min_stark_amp: float = Field(
        0.0,
        description="Lowest AC-Stark drive amplitude, as a dimensionless FACTOR of the "
                    "stark operation's baked amplitude (the QUA amplitude_scale). 0 is the "
                    "no-stark baseline.")
    max_stark_amp: float = Field(
        1.0, description="Highest AC-Stark drive amplitude factor.")
    num_amp_points: int = Field(21, gt=4, description="Number of stark-amplitude points.")
    swap_counts: list[int] = Field(
        default_factory=lambda: list(range(11)),
        description="The swap counts N to apply (number of repeated swaps). 0 is the "
                    "x180-only baseline. An explicit list so an error-amplification run "
                    "can pick specific counts (e.g. [0, 1, 3, 7, 15]).")
    swap_operation: str = Field(
        "iswap",
        description="Which named pair operation is repeated each swap, played at its FIXED "
                    "baked amplitude (the driver resolves it on the vendor pair). The "
                    "compensation read-off wants a PARTIAL swap: it measures the transfer's "
                    "period along the count axis, and a full iswap ping-pongs every other "
                    "count — the limit past which an oscillation aliases and reads slower "
                    "the faster it is. Check `min_osc_period` in the result: 2.0 means the "
                    "sweep ran into that limit.")
    stark_operation: str = Field(
        "stark",
        description="The named XY (RF) operation played on the excited/control member after "
                    "each swap to induce the AC-Stark shift; its baked amplitude is the "
                    "reference the swept factor multiplies. NOT x180 — a dedicated off-resonant "
                    "tone (the driver refuses by name if the operation is missing).")
    stark_detuning_hz: float = Field(
        50e6,
        description="FIXED off-resonant detuning (Hz) of the stark tone from the qubit drive "
                    "frequency. Must be off-resonant for a genuine Stark shift (a resonant tone "
                    "drives Rabi rotations instead); tune per chip — far enough to avoid driving "
                    "a transition, near enough for a usable shift. Not a sweep axis.")
    operation_gap_ns: int = Field(
        0, ge=0,
        description="Idle gap (ns) on the swap pair's flux lines after each swap+stark, so the "
                    "pulses settle before the next swap fires. 0 disables; the QM backend "
                    "requires a multiple of 4 ns.")
    drive_side: Literal["high", "low"] = Field("low", description=DRIVE_SIDE_DESC)
    flux_side: Literal["high", "low"] = Field("low", description=FLUX_SIDE_DESC)
    min_transfer: float = Field(0.3, ge=0.0, le=1.0, description=MIN_TRANSFER_DESC)


class QcNStarkAmpResult(Result):
    """``fit[pair]``: ``best_transfer`` (peak excitation on the UNDRIVEN member —
    which member that is follows from the ``drive_side`` parameter) and its
    ``best_stark_amp`` / ``best_swap_count`` coordinates, the per-map marginal
    ranges ``p_high_min/max`` and ``p_low_min/max``, ``p_ee_max`` (the
    double-excitation witness) and the axis sizes.

    Plus the compensation read-off lifted from the estimator:
    ``compensating_stark_amp`` (the swept amplitude that won) and
    ``compensating_stark_amp_refined`` (the same peak interpolated between swept
    points, flagged by ``compensating_is_refined``), with their
    ``compensation_score``, ``compensating_osc_contrast`` /
    ``compensating_osc_period`` / ``compensating_theta_rad``; each criterion on
    its own (``max_osc_contrast`` at ``max_osc_contrast_stark_amp``,
    ``max_osc_period`` at ``max_osc_period_stark_amp``); and how far to trust
    them — ``n_osc_ok`` (rows fitted), ``osc_criteria_agree``, and
    ``min_osc_period``, the map's fastest row: at 2.0 counts per cycle the sweep
    is AT the pi/2-per-swap limit the reading assumes it stays under. The
    per-amplitude curves behind them stay in the scqat metadata.

    Record-only: no ``update()``, nothing written to the device — and the verdict
    is still ``min_transfer`` alone, so a map that transfers without oscillating
    is SUCCESSFUL with NaN compensation fields."""


@register
class QcNStarkAmp(Experiment):
    """Backend-agnostic N-swap AC-Stark amplitude map. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "qc_n_stark_amp"
    description: ClassVar[str] = (
        "N-swap AC-Stark-amplitude error-amplification map: excite ONE member of a pair, then "
        "apply N repeated swaps (each at its fixed baked flux amplitude) and, after every swap, "
        "an off-resonant RF Stark tone on the excited qubit at the same swept amplitude, reading "
        "both members' joint populations. The Stark tone detunes the exchange; repeating the swap "
        "amplifies a small residual detuning, so the populations vs (stark amplitude, N) locate "
        "the compensating stark amplitude far more finely than a single swap. readout_mode='shot' "
        "keeps every shot (per-member states) instead of the averaged joint distribution. "
        "Record-only diagnostic: the per-map summary lands in result.fit and nothing is written "
        "back to the device."
    )
    Parameters: ClassVar[type] = QcNStarkAmpParameters
    Result: ClassVar[type] = QcNStarkAmpResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("stark_amp", "swap_count"), sweep_units=("", ""),
        # readout_mode="average": the joint distribution over the pair's basis
        # states (digit order high, low)...
        variables=("joint_population",), readout_dims=("joint_state",),
        # ...readout_mode="shot": every shot's per-member integer levels.
        alt_variables=(("state",),),
        alt_readout_dims=(("member", "shot_idx"),),
    )
    target_kinds: ClassVar[tuple[str, ...]] = ("qubit_pair",)
    #: none, deliberately: a composite's operations are DECLARED, and this
    #: experiment tunes an AC-Stark shift on a swap BEFORE the two-qubit gate is
    #: finalized. Requiring "iswap"/"stark" would refuse exactly the bring-up chip
    #: it is for — same rationale as pair_swap_chevron and qc_n_swap_amp.
    required_operations: ClassVar[tuple[str, ...]] = ()

    params: QcNStarkAmpParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            # dict order IS the contract order: stark amplitude outer, swap count inner.
            "stark_amp": np.linspace(self.params.min_stark_amp,
                                     self.params.max_stark_amp,
                                     self.params.num_amp_points),
            "swap_count": np.array(self.params.swap_counts, dtype=int),
        }

    def readout_coords(self) -> dict:
        if self.params.readout_mode == "shot":
            return {"member": ["high", "low"],
                    "shot_idx": np.arange(self.params.num_averages)}
        return {"joint_state": joint_state_labels(2)}

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """Fixed swap detuned by the stark tone, repeated N times — the model the map comes from.

        The swap is a fixed-time resonant exchange; the Stark tone adds an
        amplitude-dependent effective detuning ``delta(a) = slope*(a - a0)`` with
        ``a0`` the compensating amplitude, so the transfer after ``N`` swaps is
        ``(2J)^2/omega^2 * sin^2(pi*omega*N*t_sw)`` with
        ``omega = sqrt(delta(a)^2 + (2J)^2)``. On the compensating amplitude
        (``delta = 0``) the exchange is slowest and fullest, so the excitation
        ping-pongs cleanly with N; off it the contrast drops and the pattern
        speeds up and drifts — the error the map amplifies. In shot mode each
        shot's joint outcome is DRAWN from that distribution instead of averaging
        it.

        The simulated swap is deliberately PARTIAL and its detuning slope is
        bounded (``COUNTS_AT_EDGE``), because that is the regime the estimator's
        reading is defined for — see the class docstring.
        """
        a = coords["stark_amp"]
        n = coords["swap_count"].astype(float)
        pairs = self.params.targets
        rng = np.random.default_rng(stable_seed("qc_n_stark_amp", *pairs))
        span = float(np.ptp(a)) or 1.0
        a_step = span / max(a.size - 1, 1)
        shot_mode = self.params.readout_mode == "shot"
        num_shots = int(self.params.num_averages)
        per_pair = []
        for k in range(len(pairs)):
            a0 = float(rng.uniform(a.min() + 0.25 * span, a.min() + 0.75 * span))
            j_hz = float(rng.uniform(3e6, 9e6))
            # A PARTIAL swap: the analysis reads a period off the count axis and
            # can only do so while every row stays under pi/2 per swap — two
            # counts per cycle — so the simulated swap turns by less than that on
            # compensation and the detuning slope is DERIVED to keep the farthest
            # row at COUNTS_AT_EDGE. A full swap would put compensation itself at
            # the limit and alias every detuned row, i.e. simulate the one case
            # the reading is not defined for.
            counts_on_compensation = float(rng.uniform(5.0, 8.0))
            t_sw_ns = 1e9 / (2.0 * j_hz * counts_on_compensation)
            reach = float(np.max(np.abs(a - a0))) or 1.0
            slope = (2 * j_hz) * float(np.sqrt(
                (counts_on_compensation / COUNTS_AT_EDGE) ** 2 - 1.0)) / reach
            n_tau = float(rng.uniform(20.0, 45.0))              # swaps to decohere
            prep = float(rng.uniform(0.94, 0.99))               # pi-pulse fidelity
            therm = float(rng.uniform(0.005, 0.02))             # residual |ee>
            delta = slope * (a - a0)
            omega = np.sqrt(delta ** 2 + (2 * j_hz) ** 2)
            swap = (((2 * j_hz) ** 2 / omega ** 2)[:, None]
                    * np.sin(np.pi * omega[:, None] * n[None, :] * t_sw_ns * 1e-9) ** 2
                    * np.exp(-n[None, :] / n_tau))
            p_partner = np.clip(prep * swap, 0.0, 1.0)
            p_driven = np.clip(prep * (1.0 - swap) * np.exp(-n[None, :] / (8 * n_tau)),
                               0.0, 1.0)
            # The joint basis distribution (digit order high, low): the driven
            # member's role follows drive_side.
            driven_is_high = self.params.drive_side == "high"
            p_high = p_driven if driven_is_high else p_partner
            p_low = p_partner if driven_is_high else p_driven
            p11 = np.full_like(p_high, therm)
            p10 = np.clip(p_high - p11, 0.0, 1.0)
            p01 = np.clip(p_low - p11, 0.0, 1.0)
            p00 = np.clip(1.0 - (p01 + p10 + p11), 0.0, 1.0)
            probs = np.stack([p00, p01, p10, p11])              # (4, amp, count)
            probs /= probs.sum(axis=0, keepdims=True)
            if shot_mode:
                # Draw each shot's joint outcome code from the distribution
                # (inverse CDF), then split into per-member binary levels.
                u = rng.random((a.size, n.size, num_shots))
                cum = np.cumsum(probs, axis=0)                  # (4, amp, count)
                code = (u[None, :, :, :] > cum[:, :, :, None]).sum(axis=0)
                levels = np.stack([code // 2, code % 2])        # (member, amp, count, shot)
                per_pair.append(levels.astype(np.int64))
            else:
                jitter = rng.normal(0.0, 0.02, probs.shape)
                per_pair.append(np.clip(probs + jitter, 0.0, 1.0))
        if shot_mode:
            return {"state": (("target", "member", "stark_amp", "swap_count", "shot_idx"),
                              np.stack(per_pair))}
        return {"joint_population": (("target", "joint_state", "stark_amp", "swap_count"),
                                     np.stack(per_pair))}

    def estimate(self) -> QcNStarkAmpResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        if "state" in self.dataset.data_vars:
            # shot mode: reduce per-shot member levels to the SAME joint
            # distribution average mode stores, then analyze identically.
            jp = states_to_joint_population(self.dataset["state"],
                                            member_dim="member", shot_dim="shot_idx")
            ds = jp.to_dataset()
        else:
            ds = self.dataset
        ds = ds.transpose("target", "joint_state", "stark_amp", "swap_count")
        # Raw joint-state-population maps -> scqat artifacts (figure + plotdata +
        # metadata, one folder per pair). Record-only: the SUCCESS verdict below
        # (min_transfer) stays here; the estimator only draws the populations.
        from scqat.estimators.qc_n_stark_amp import QcNStarkAmpEstimator
        from .._scqat import per_qubit_results

        analysis = per_qubit_results(
            ds, QcNStarkAmpEstimator(), artifact_dir=self.artifact_dir,
            drive_side=self.params.drive_side, flux_side=self.params.flux_side,
            per_target_kwargs=_role_names(self.device, self.params.targets))

        result = QcNStarkAmpResult()
        for pair in self.params.targets:
            fit, ok = summarize_transfer_map(
                ds.sel(target=pair), self.params.drive_side,
                ("stark_amp", "swap_count"), self.params.min_transfer)
            # The compensation scalars lifted out of the estimator's results; the
            # per-amplitude curves themselves stay in the scqat metadata.
            compensation = analysis.get(pair, {})
            fit.update({key: float(compensation.get(key, float("nan")))
                        for key in COMPENSATION_KEYS})
            result.fit[pair] = fit
            # The verdict is the transfer one, unchanged: this map's job is the
            # error-amplification picture, and a run that transfers but never
            # oscillates is a real (NaN-compensation) result, not a failure.
            result.outcomes[pair] = Outcome.SUCCESSFUL if ok else Outcome.FAILED
        return result

    @classmethod
    def validate_targets(cls, roster, targets):
        """The flux line the swap pulse rides. WHICH member carries it is a
        parameter (``flux_side``) and this hook cannot see params, so the roster
        gate is "at least one member can"; the driver refuses the SELECTED member
        pre-probe when it is the one without a flux channel.

        No coupler gate: the swap rides a qubit's own flux line — like
        ``pair_swap_chevron`` and ``qc_n_swap_amp``, unlike ``pair_swap_flux_map``."""
        return _flux_member_problems(roster, targets,
                                     "nothing to play the swap pulse on")

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
