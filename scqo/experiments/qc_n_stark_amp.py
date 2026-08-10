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
device surface; the per-map summary lives in ``result.fit``. A scqat estimator
(``qc_n_stark_amp``) draws the raw joint state populations — a per-pair 2x2
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
                    "baked amplitude (the driver resolves it on the vendor pair).")
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
    double-excitation witness) and the axis sizes. Record-only: no ``update()``,
    nothing written to the device."""


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
        (``delta = 0``) the swap time is a half exchange period, so the excitation
        ping-pongs cleanly with N; off it the contrast drops and the pattern
        drifts — the error the map amplifies. In shot mode each shot's joint
        outcome is DRAWN from that distribution instead of averaging it.
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
            # One swap = a half exchange period on compensation, so N swaps ping-pong.
            t_sw_ns = 1e9 / (4.0 * j_hz)
            # The detuning slope is drawn RELATIVE to the amplitude grid so the
            # compensation stripe is a few points wide — i.e. the sweep resolves
            # it, which is what an operator narrows the window until it does.
            slope = (2 * j_hz) * float(rng.uniform(0.3, 0.8)) / a_step  # Hz per unit factor
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

        per_qubit_results(ds, QcNStarkAmpEstimator(), artifact_dir=self.artifact_dir,
                          drive_side=self.params.drive_side, flux_side=self.params.flux_side,
                          per_target_kwargs=_role_names(self.device, self.params.targets))
        result = QcNStarkAmpResult()
        for pair in self.params.targets:
            fit, ok = summarize_transfer_map(
                ds.sel(target=pair), self.params.drive_side,
                ("stark_amp", "swap_count"), self.params.min_transfer)
            result.fit[pair] = fit
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
