"""N-swap x flux-amplitude error-amplification map — record-only.

Excite ONE member of a pair, then apply **N** swaps in a chain — each the SAME
swap operation at the SAME swept control-qubit flux amplitude (absolute volts) —
and read BOTH members out jointly. Repeating the swap amplifies a small
swap-amplitude miscalibration: on the correctly calibrated amplitude the
excitation ping-pongs cleanly between the two members with N, while off it the
pattern smears and drifts, and the drift grows with the number of swaps. The
joint populations vs (flux amplitude, N) draw a swap-amplitude fine-tuning map.

The count axis is the analogue of ``pair_swap_chevron``'s pulse-duration axis:
that experiment sweeps ONE swap's duration continuously; this one sweeps the
NUMBER of fixed swaps (N=0 is the x180-only baseline), trading time resolution
for error-amplification sensitivity.

READOUT (the unified readout schema): digital, both modes. ``readout_mode=
"average"`` (default) stores the pair's ``joint_population`` over ``joint_state``
labels; ``"shot"`` keeps every shot as per-member integer levels
(``state @ (target, member, *sweeps, shot_idx)``, member order high, low) — the
full-information / more-memory trade. ``estimate()`` reduces the shot form to the
same joint distribution before analysis, so both modes yield identical maps.

RECORD-ONLY for the DEVICE: there is no ``update()`` and nothing lands on the
device surface; the per-map summary lives in ``result.fit``. A scqat estimator
(``qc_n_swap_amp``) draws the raw joint state populations — a per-pair 2x2
population figure plus plotdata/metadata under ``analysis/<pair>/`` — but it only
VISUALIZES the maps and proposes nothing; the SUCCESS verdict (``min_transfer``)
is made here in ``estimate()``, not by the estimator.
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


class QcNSwapAmpParameters(TargetSelection, AveragingParameters,
                           QubitResetParameters, ReadoutModeParameters):
    """Inputs for the N-swap amplitude map. ``targets`` are PAIR components."""

    min_flux_amp_v: float = Field(
        0.0,
        description="Lowest control-qubit flux amplitude (V at the DAC; the usable range "
                    "is the flux port's own, and the backend refuses past it).")
    max_flux_amp_v: float = Field(
        0.1, description="Highest control-qubit flux amplitude (V).")
    num_amp_points: int = Field(21, gt=4, description="Number of flux-amplitude points.")
    swap_counts: list[int] = Field(
        default_factory=lambda: list(range(11)),
        description="The swap counts N to apply (number of repeated swaps). 0 is the "
                    "x180-only baseline. An explicit list so an error-amplification run "
                    "can pick specific counts (e.g. [0, 1, 3, 7, 15]).")
    swap_operation: str = Field(
        "iswap",
        description="Which named pair operation is repeated each swap (the driver "
                    "resolves it on the vendor pair; it must expose a control-side "
                    "flux pulse that accepts the swept amplitude).")
    operation_gap_ns: int = Field(
        0, ge=0,
        description="Idle gap (ns) on the swap pair's flux lines after each swap, so "
                    "the flux pulse settles before the next swap fires. 0 disables; "
                    "the QM backend requires a multiple of 4 ns.")
    drive_side: Literal["high", "low"] = Field("low", description=DRIVE_SIDE_DESC)
    flux_side: Literal["high", "low"] = Field("low", description=FLUX_SIDE_DESC)
    min_transfer: float = Field(0.3, ge=0.0, le=1.0, description=MIN_TRANSFER_DESC)


class QcNSwapAmpResult(Result):
    """``fit[pair]``: ``best_transfer`` (peak excitation on the UNDRIVEN member —
    which member that is follows from the ``drive_side`` parameter) and its
    ``best_flux_amp_v`` / ``best_swap_count`` coordinates, the per-map marginal
    ranges ``p_high_min/max`` and ``p_low_min/max``, ``p_ee_max`` (the
    double-excitation witness) and the axis sizes. Record-only: no ``update()``,
    nothing written to the device."""


@register
class QcNSwapAmp(Experiment):
    """Backend-agnostic N-swap amplitude map. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "qc_n_swap_amp"
    description: ClassVar[str] = (
        "N-swap swap-amplitude error-amplification map: excite ONE member of a pair, then "
        "apply N repeated swaps (each at the same swept control-qubit flux amplitude, "
        "absolute volts) and read both members' joint populations. Repeating the swap "
        "amplifies a small swap-amplitude miscalibration, so the populations vs "
        "(flux amplitude, N) locate the correctly calibrated amplitude far more finely "
        "than a single swap. readout_mode='shot' keeps every shot (per-member states) "
        "instead of the averaged joint distribution. Record-only diagnostic: the per-map "
        "summary lands in result.fit and nothing is written back to the device."
    )
    Parameters: ClassVar[type] = QcNSwapAmpParameters
    Result: ClassVar[type] = QcNSwapAmpResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("flux_amp_v", "swap_count"), sweep_units=("V", ""),
        # readout_mode="average": the joint distribution over the pair's basis
        # states (digit order high, low)...
        variables=("joint_population",), readout_dims=("joint_state",),
        # ...readout_mode="shot": every shot's per-member integer levels.
        alt_variables=(("state",),),
        alt_readout_dims=(("member", "shot_idx"),),
    )
    target_kinds: ClassVar[tuple[str, ...]] = ("qubit_pair",)
    #: none, deliberately: a composite's operations are DECLARED, and this
    #: experiment amplifies a swap's amplitude error BEFORE the two-qubit gate is
    #: finalized. Requiring "iswap" (or "cz") would refuse exactly the bring-up
    #: chip it is for — same rationale as pair_swap_chevron.
    required_operations: ClassVar[tuple[str, ...]] = ()

    params: QcNSwapAmpParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            # dict order IS the contract order: flux amplitude outer, swap count inner.
            "flux_amp_v": np.linspace(self.params.min_flux_amp_v,
                                      self.params.max_flux_amp_v,
                                      self.params.num_amp_points),
            "swap_count": np.array(self.params.swap_counts, dtype=int),
        }

    def readout_coords(self) -> dict:
        if self.params.readout_mode == "shot":
            return {"member": ["high", "low"],
                    "shot_idx": np.arange(self.params.num_averages)}
        return {"joint_state": joint_state_labels(2)}

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """Fixed-duration exchange repeated N times — the model the map comes from.

        Each swap advances a resonant two-level exchange by one fixed swap time,
        so after ``N`` swaps the transfer is ``(2J)^2/omega^2 * sin^2(pi*omega*N*t_sw)``
        with ``omega = sqrt(delta(v)^2 + (2J)^2)``. On the resonance amplitude
        (``delta = 0``) the swap time is a half exchange period, so the excitation
        ping-pongs cleanly with N; off it the contrast drops and the pattern
        drifts — the error the map amplifies. In shot mode each shot's joint
        outcome is DRAWN from that distribution instead of averaging it.
        """
        v = coords["flux_amp_v"]
        n = coords["swap_count"].astype(float)
        pairs = self.params.targets
        rng = np.random.default_rng(stable_seed("qc_n_swap_amp", *pairs))
        span = float(np.ptp(v)) or 1.0
        v_step = span / max(v.size - 1, 1)
        shot_mode = self.params.readout_mode == "shot"
        num_shots = int(self.params.num_averages)
        per_pair = []
        for k in range(len(pairs)):
            v0 = float(rng.uniform(v.min() + 0.25 * span, v.min() + 0.75 * span))
            j_hz = float(rng.uniform(3e6, 9e6))
            # One swap = a half exchange period on resonance, so N swaps ping-pong.
            t_sw_ns = 1e9 / (4.0 * j_hz)
            # The detuning slope is drawn RELATIVE to the amplitude grid so the
            # resonance stripe is a few points wide — i.e. the sweep resolves it,
            # which is what an operator narrows the window until it does.
            slope = (2 * j_hz) * float(rng.uniform(0.3, 0.8)) / v_step  # Hz per V
            n_tau = float(rng.uniform(20.0, 45.0))              # swaps to decohere
            prep = float(rng.uniform(0.94, 0.99))               # pi-pulse fidelity
            therm = float(rng.uniform(0.005, 0.02))             # residual |ee>
            delta = slope * (v - v0)
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
                u = rng.random((v.size, n.size, num_shots))
                cum = np.cumsum(probs, axis=0)                  # (4, amp, count)
                code = (u[None, :, :, :] > cum[:, :, :, None]).sum(axis=0)
                levels = np.stack([code // 2, code % 2])        # (member, amp, count, shot)
                per_pair.append(levels.astype(np.int64))
            else:
                jitter = rng.normal(0.0, 0.02, probs.shape)
                per_pair.append(np.clip(probs + jitter, 0.0, 1.0))
        if shot_mode:
            return {"state": (("target", "member", "flux_amp_v", "swap_count", "shot_idx"),
                              np.stack(per_pair))}
        return {"joint_population": (("target", "joint_state", "flux_amp_v", "swap_count"),
                                     np.stack(per_pair))}

    def estimate(self) -> QcNSwapAmpResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        if "state" in self.dataset.data_vars:
            # shot mode: reduce per-shot member levels to the SAME joint
            # distribution average mode stores, then analyze identically.
            jp = states_to_joint_population(self.dataset["state"],
                                            member_dim="member", shot_dim="shot_idx")
            ds = jp.to_dataset()
        else:
            ds = self.dataset
        ds = ds.transpose("target", "joint_state", "flux_amp_v", "swap_count")
        # Raw joint-state-population maps -> scqat artifacts (figure + plotdata +
        # metadata, one folder per pair). Record-only: the SUCCESS verdict below
        # (min_transfer) stays here; the estimator only draws the populations.
        from scqat.estimators.qc_n_swap_amp import QcNSwapAmpEstimator
        from .._scqat import per_qubit_results

        per_qubit_results(ds, QcNSwapAmpEstimator(), artifact_dir=self.artifact_dir,
                          drive_side=self.params.drive_side, flux_side=self.params.flux_side,
                          per_target_kwargs=_role_names(self.device, self.params.targets))
        result = QcNSwapAmpResult()
        for pair in self.params.targets:
            fit, ok = summarize_transfer_map(
                ds.sel(target=pair), self.params.drive_side,
                ("flux_amp_v", "swap_count"), self.params.min_transfer)
            result.fit[pair] = fit
            result.outcomes[pair] = Outcome.SUCCESSFUL if ok else Outcome.FAILED
        return result

    @classmethod
    def validate_targets(cls, roster, targets):
        """The flux line the swept amplitude rides. WHICH member carries it is a
        parameter (``flux_side``) and this hook cannot see params, so the roster
        gate is "at least one member can"; the driver refuses the SELECTED member
        pre-probe when it is the one without a flux channel.

        No coupler gate: the swap rides a qubit's own flux line — like
        ``pair_swap_chevron``, unlike ``pair_swap_flux_map``."""
        return _flux_member_problems(roster, targets,
                                     "nothing to play the swap pulse on")

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
