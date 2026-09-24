"""Fixed-N flux-amplitude x AC-Stark-amplitude swap map — record-only.

Excite ONE member of a pair, then apply a FIXED number ``swap_count`` of swaps —
each at the same swept control-qubit flux amplitude (absolute volts) and each
followed by an off-resonant RF Stark tone on the excited member at the same swept
amplitude factor — and read BOTH members out jointly. Two amplitudes are swept,
the swap count is not: the map is ``(flux amplitude x stark amplitude)`` at one N.

WHY BOTH AXES AT ONCE. The two knobs are not independent. The control flux brings
the members onto resonance during the pulse, while between swaps they accumulate
a relative phase ``phi`` (from the flux pulse, the gap and the idle), and an
exchange followed by a Z rotation does not commute::

    cos(theta_eff) = cos(phi/2) * cos(theta_exchange)

so the per-round composite angle is inflated, AND the composite rotation axis
tilts off the equator by ``cos(theta)*sin(phi/2)`` — which moves the amplitude
where the transfer peaks away from zero detuning, by roughly ``phi / t_p``. On a
40 ns swap that offset is comparable to the resonance linewidth. Scanning one
knob at a fixed value of the other therefore walks a ridge of a 2D surface;
``qc_n_swap_amp`` (flux x N) and ``qc_n_stark_amp`` (stark x N) each see one
slice of it. This experiment sweeps the surface.

TWO THINGS TO KNOW BEFORE READING THE MAP:

1. **At ``swap_count = 1`` the stark axis is INERT.** The tone plays after each
   swap, so the only tone of a single-swap run sits between the last swap and the
   readout: it imprints a phase on the excited member and cannot move any
   population. The map is flat along the stark axis by construction. The phase
   the Stark tone compensates only exists BETWEEN swaps, so give N at least 2 —
   and more, since repetition is what amplifies a small residual.
2. **The peak is a WORKING POINT, not a decomposition.** At a fixed N the map's
   maximum is the ``(flux, stark)`` pair that transfers best for THAT N; it is
   the compensating point only when the per-round angle is near ``pi/(2N)``.
   Close to a full per-round swap the peak trades a nonzero ``phi`` for a better
   angle — the same trap that makes ``qc_n_stark_amp``'s ``best_stark_amp``
   unusable as a compensation near a full swap. Pick N so that N swaps are
   roughly one full transfer.

READOUT (the unified readout schema): digital, both modes, exactly as
``qc_n_swap_amp`` — ``readout_mode="average"`` (default) stores the pair's
``joint_population`` over ``joint_state`` labels; ``"shot"`` keeps every shot as
per-member integer levels (``state @ (target, member, *sweeps, shot_idx)``,
member order high, low). ``estimate()`` reduces the shot form to the same joint
distribution, so both modes yield identical maps.

RECORD-ONLY for the DEVICE: there is no ``update()`` and nothing lands on the
device surface; the per-map summary lives in ``result.fit``. A scqat estimator
(``qc_swap_flux_stark``) draws the raw joint state populations — a per-pair 2x2
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

#: phase (rad) the SIMULATED map accumulates per round at the far edge of the
#: stark window. Large enough that the compensation feature is resolved by a
#: handful of grid points, small enough that the ridge stays single-peaked.
PHASE_AT_EDGE = 2.5


class QcSwapFluxStarkParameters(TargetSelection, AveragingParameters,
                                QubitResetParameters, ReadoutModeParameters):
    """Inputs for the fixed-N flux x stark map. ``targets`` are PAIR components."""

    min_flux_amp_v: float = Field(
        0.0,
        description="Lowest control-qubit flux amplitude (V at the DAC; the usable range "
                    "is the flux port's own, and the backend refuses past it).")
    max_flux_amp_v: float = Field(
        0.1, description="Highest control-qubit flux amplitude (V).")
    num_flux_points: int = Field(21, gt=4, description="Number of flux-amplitude points.")
    min_stark_amp: float = Field(
        0.0,
        description="Lowest AC-Stark drive amplitude, as a dimensionless FACTOR of the "
                    "stark operation's baked amplitude (the QUA amplitude_scale). 0 is the "
                    "no-stark baseline.")
    max_stark_amp: float = Field(
        1.0, description="Highest AC-Stark drive amplitude factor.")
    num_stark_points: int = Field(21, gt=4, description="Number of stark-amplitude points.")
    swap_count: int = Field(
        4, ge=1,
        description="How many swaps to repeat — a FIXED count, not an axis. At 1 the stark "
                    "axis is inert: the single tone plays after the only swap and before "
                    "readout, where it can only imprint a phase. The phase the tone "
                    "compensates lives BETWEEN swaps, and repeating amplifies it, so use at "
                    "least 2 — ideally the N for which N swaps are about one full transfer, "
                    "since the map's peak is only the compensating point near there.")
    swap_operation: str = Field(
        "iswap",
        description="Which named pair operation is repeated each swap (the driver "
                    "resolves it on the vendor pair; it must expose a control-side "
                    "flux pulse that accepts the swept amplitude). Its coupler pulse plays "
                    "bare at its baked amplitude — the angle knob is the operation, not a "
                    "parameter here.")
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
        description="Idle gap (ns) on the swap pair's flux lines between each swap and the "
                    "stark tone that follows it, so the flux pulse settles before the "
                    "off-resonant tone plays. 0 disables; the QM backend requires a multiple "
                    "of 4 ns.")
    drive_side: Literal["high", "low"] = Field("low", description=DRIVE_SIDE_DESC)
    flux_side: Literal["high", "low"] = Field("low", description=FLUX_SIDE_DESC)
    min_transfer: float = Field(0.3, ge=0.0, le=1.0, description=MIN_TRANSFER_DESC)


class QcSwapFluxStarkResult(Result):
    """``fit[pair]``: ``best_transfer`` (peak excitation on the UNDRIVEN member —
    which member that is follows from the ``drive_side`` parameter) and its
    ``best_flux_amp_v`` / ``best_stark_amp`` coordinates — the working point for
    the run's OWN ``swap_count``, not a compensation valid at every N (see the
    module docstring) — plus the per-map marginal ranges ``p_high_min/max`` and
    ``p_low_min/max``, ``p_ee_max`` (the double-excitation witness) and the axis
    sizes. Record-only: no ``update()``, nothing written to the device."""


@register
class QcSwapFluxStark(Experiment):
    """Backend-agnostic fixed-N flux x stark map. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "qc_swap_flux_stark"
    description: ClassVar[str] = (
        "Fixed-count swap map over TWO amplitudes: excite ONE member of a pair, then apply a "
        "fixed number N of swaps — each at the same swept control-qubit flux amplitude "
        "(absolute volts), each followed by an off-resonant RF Stark tone on the excited "
        "member at the same swept amplitude factor — and read both members' joint "
        "populations. The flux brings the members onto resonance while the Stark tone nulls "
        "the phase they accumulate between swaps, and the two are coupled (the phase moves "
        "where the transfer peaks), so sweeping both at one N shows the working point that "
        "one-knob scans can only walk a ridge of. Set swap_count >= 2: a single swap leaves "
        "the stark axis inert. readout_mode='shot' keeps every shot (per-member states) "
        "instead of the averaged joint distribution. Record-only diagnostic: the per-map "
        "summary lands in result.fit and nothing is written back to the device."
    )
    Parameters: ClassVar[type] = QcSwapFluxStarkParameters
    Result: ClassVar[type] = QcSwapFluxStarkResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("flux_amp_v", "stark_amp"), sweep_units=("V", ""),
        # readout_mode="average": the joint distribution over the pair's basis
        # states (digit order high, low)...
        variables=("joint_population",), readout_dims=("joint_state",),
        # ...readout_mode="shot": every shot's per-member integer levels.
        alt_variables=(("state",),),
        alt_readout_dims=(("member", "shot_idx"),),
    )
    target_kinds: ClassVar[tuple[str, ...]] = ("qubit_pair",)
    #: none, deliberately — same rationale as qc_n_swap_amp / qc_n_stark_amp:
    #: requiring "iswap" (or "stark") would refuse exactly the bring-up chip this
    #: map exists to calibrate.
    required_operations: ClassVar[tuple[str, ...]] = ()

    params: QcSwapFluxStarkParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            # dict order IS the contract order: flux amplitude outer, stark amplitude inner.
            "flux_amp_v": np.linspace(self.params.min_flux_amp_v,
                                      self.params.max_flux_amp_v,
                                      self.params.num_flux_points),
            "stark_amp": np.linspace(self.params.min_stark_amp,
                                     self.params.max_stark_amp,
                                     self.params.num_stark_points),
        }

    def readout_coords(self) -> dict:
        if self.params.readout_mode == "shot":
            return {"member": ["high", "low"],
                    "shot_idx": np.arange(self.params.num_averages)}
        return {"joint_state": joint_state_labels(2)}

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """The repeated detuned exchange with the between-swap phase in it.

        The detuning does TWO things, which is the whole shape of this map. It
        caps the per-round exchange through ``env = (2J)^2/omega^2`` with
        ``omega = sqrt(delta(v)^2 + (2J)^2)`` — that is what localizes the
        feature in flux — and it also winds a phase ``2*pi*delta*t_sw`` between
        the rounds, ON TOP of the idle phase the Stark tone is there to cancel.
        The composite of an exchange and that Z rotation obeys ``cos(theta_eff)
        = cos(phi/2)*cos(theta)`` with its axis tilted off the equator by
        ``cos(theta)*sin(phi/2)``, so after N rounds::

            transfer = env * (1 - tilt^2) * sin^2(N*theta_eff)

        Because ``phi`` depends on BOTH knobs, the map is a RIDGE along the
        ``phi = 0`` line, not a circular spot — and the detuning envelope picks
        one bright segment out of that ridge, where the members are resonant AND
        the phase is nulled. That tilt is exactly why a one-knob scan of either
        axis cannot find the working point on its own. The swap time is drawn so
        the resonant, compensated round is ``pi/(2N)`` — the one N for which the
        peak IS the compensating point.
        """
        v = coords["flux_amp_v"]
        a = coords["stark_amp"]
        pairs = self.params.targets
        rng = np.random.default_rng(stable_seed("qc_swap_flux_stark", *pairs))
        n_swaps = float(self.params.swap_count)
        span_v = float(np.ptp(v)) or 1.0
        span_a = float(np.ptp(a)) or 1.0
        v_step = span_v / max(v.size - 1, 1)
        shot_mode = self.params.readout_mode == "shot"
        num_shots = int(self.params.num_averages)
        per_pair = []
        for k in range(len(pairs)):
            v0 = float(rng.uniform(v.min() + 0.25 * span_v, v.min() + 0.75 * span_v))
            a0 = float(rng.uniform(a.min() + 0.25 * span_a, a.min() + 0.75 * span_a))
            j_hz = float(rng.uniform(3e6, 9e6))
            # The pulse is the one that makes N rounds a full transfer on the
            # compensated resonance: theta = pi/(2N) there.
            t_sw_s = 1.0 / (4.0 * j_hz * n_swaps)
            theta0 = np.pi / (2.0 * n_swaps)
            # The detuning slope is drawn RELATIVE to the amplitude grid so the
            # resonance stripe is a few points wide — i.e. the sweep resolves it,
            # which is what an operator narrows the window until it does.
            slope = (2 * j_hz) * float(rng.uniform(0.3, 0.8)) / v_step  # Hz per V
            # The stark tone's phase per unit factor, relative to the swept
            # window, so the null is reachable and resolved.
            reach = float(np.max(np.abs(a - a0))) or 1.0
            k_phi = PHASE_AT_EDGE / reach                        # rad per unit factor
            n_tau = float(rng.uniform(20.0, 45.0))               # swaps to decohere
            prep = float(rng.uniform(0.94, 0.99))                # pi-pulse fidelity
            therm = float(rng.uniform(0.005, 0.02))              # residual |ee>
            delta = slope * (v - v0)
            omega = np.sqrt(delta ** 2 + (2 * j_hz) ** 2)
            env = ((2 * j_hz) ** 2 / omega ** 2)[:, None]            # (flux, 1)
            theta = (np.sqrt(env) * theta0)                          # (flux, 1)
            # The detuning winds its own phase between rounds; the Stark tone
            # subtracts one that is linear in the swept factor. Both are per
            # round, and they null together on a LINE, not at a point.
            phi = (2 * np.pi * delta * t_sw_s)[:, None] - (k_phi * (a - a0))[None, :]
            theta_eff = np.arccos(np.clip(np.cos(phi / 2) * np.cos(theta), -1.0, 1.0))
            tilt = np.cos(theta) * np.sin(phi / 2)
            swap = (env * (1.0 - tilt ** 2) * np.sin(n_swaps * theta_eff) ** 2
                    * np.exp(-n_swaps / n_tau))
            p_partner = np.clip(prep * swap, 0.0, 1.0)
            p_driven = np.clip(prep * (1.0 - swap) * np.exp(-n_swaps / (8 * n_tau)),
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
            probs = np.stack([p00, p01, p10, p11])              # (4, flux, stark)
            probs /= probs.sum(axis=0, keepdims=True)
            if shot_mode:
                # Draw each shot's joint outcome code from the distribution
                # (inverse CDF), then split into per-member binary levels.
                u = rng.random((v.size, a.size, num_shots))
                cum = np.cumsum(probs, axis=0)                  # (4, flux, stark)
                code = (u[None, :, :, :] > cum[:, :, :, None]).sum(axis=0)
                levels = np.stack([code // 2, code % 2])        # (member, flux, stark, shot)
                per_pair.append(levels.astype(np.int64))
            else:
                jitter = rng.normal(0.0, 0.02, probs.shape)
                per_pair.append(np.clip(probs + jitter, 0.0, 1.0))
        if shot_mode:
            return {"state": (("target", "member", "flux_amp_v", "stark_amp", "shot_idx"),
                              np.stack(per_pair))}
        return {"joint_population": (("target", "joint_state", "flux_amp_v", "stark_amp"),
                                     np.stack(per_pair))}

    def estimate(self) -> QcSwapFluxStarkResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        if "state" in self.dataset.data_vars:
            jp = states_to_joint_population(self.dataset["state"],
                                            member_dim="member", shot_dim="shot_idx")
            ds = jp.to_dataset()
        else:
            ds = self.dataset
        ds = ds.transpose("target", "joint_state", "flux_amp_v", "stark_amp")
        # Raw joint-state-population maps -> scqat artifacts (figures + plotdata +
        # metadata, one folder per pair). The estimator proposes nothing and fits
        # nothing: with the count frozen there is no axis to fit along, so the
        # reading is the map and the peak below.
        from scqat.estimators.qc_swap_flux_stark import QcSwapFluxStarkEstimator
        from .._scqat import per_qubit_results

        per_qubit_results(ds, QcSwapFluxStarkEstimator(), artifact_dir=self.artifact_dir,
                          drive_side=self.params.drive_side, flux_side=self.params.flux_side,
                          per_target_kwargs=_role_names(self.device, self.params.targets))
        result = QcSwapFluxStarkResult()
        for pair in self.params.targets:
            fit, ok = summarize_transfer_map(
                ds.sel(target=pair), self.params.drive_side,
                ("flux_amp_v", "stark_amp"), self.params.min_transfer)
            result.fit[pair] = fit
            result.outcomes[pair] = Outcome.SUCCESSFUL if ok else Outcome.FAILED
        return result

    @classmethod
    def validate_targets(cls, roster, targets):
        """The swap rides a member's flux line, so a pair without one is refused
        before any instrument time is booked. The coupler is NOT gated here: it
        plays bare at the swap operation's baked amplitude, exactly as in
        qc_n_swap_amp."""
        return _flux_member_problems(roster, targets,
                                     "nothing to play the swap pulse on")

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
