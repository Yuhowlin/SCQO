"""Fixed-time coupler-flux x qubit-flux swap map — the 2D spot, record-only.

The fixed-duration sibling of :mod:`scqo.experiments.pair_swap_chevron`: instead
of sweeping amplitude against duration, the duration is FIXED and two flux
amplitudes are swept simultaneously — the coupler's (x) and one member's (y).
Excite one member, play both flux pulses over the same window, read both members
out jointly. At a fixed time the transfer draws closed contours, so the map
shows the swap **spot** on the resonance line and how the coupler bias moves it.

THE COUPLER FLUX IS THE ANGLE KNOB (TUTORIAL section 12), so a column of this map
— one coupler bias, the transfer swept along the member's flux — is a swap peak
whose HEIGHT is ``sin^2(theta)`` with ``theta = 2*pi*J*t``. The scqat estimator
fits that peak per column and turns the map into the ``J(Phi_c)`` calibration
curve, plus the polynomial through it that locates the decouple (off) point. Two
consequences worth knowing before reading the numbers:

* ``j_hz`` needs the duration the instrument ACTUALLY played, which is why
  ``estimate()`` resolves it before calling the estimator. A shaped pulse plays
  its own native length, which this layer does not know, so such a run reports
  the dimensionless angle only.
* An angle past ``pi/2`` folds back under ``arcsin`` and is UNDER-reported. The
  estimator flags those columns (``n_branch_warn``) and keeps them out of the
  polynomial rather than unfolding them; the fix is a shorter ``swap_time_ns``,
  so the whole coupler axis stays under a full swap.

RECORD-ONLY for the DEVICE, exactly like the chevron: no ``update()``, nothing on
the device surface. In particular the fitted coupling is NOT proposed as the
pair's ``j_hz`` fact — it holds at the PULSED coupler/member flux working point,
not at the standing idle point that field names. The scqat estimator
(``pair_swap_flux_map``) writes a per-pair 2x2 population figure, a coupling
figure and a fit-diagnostic figure plus plotdata/metadata under
``analysis/<pair>/``; see that module's docstring for the rationale.
"""

from __future__ import annotations

from typing import ClassVar, Literal

import numpy as np
from pydantic import Field, model_validator

from ..contract import DatasetContract
from ._capabilities.qubit_reset import QubitResetParameters
from ._capabilities.state_readout import joint_state_labels
from ._sim import stable_seed
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register
from .pair_swap_chevron import (
    DRIVE_SIDE_DESC,
    MIN_TRANSFER_DESC,
    _coupler_problems,
    _flux_member_problems,
    _joint_from_roles,
    _role_names,
    summarize_transfer_map,
)

#: the y axis here is the MEMBER's flux line; the coupler owns the x axis.
FLUX_SIDE_DESC = (
    "Which pair member's flux line carries the swept qubit-flux (y) pulse "
    "(roster roles; the driver maps high/low onto its vendor control/target)."
)

#: the estimator's SCALAR coupling results, lifted onto ``result.fit``. The
#: per-coupler curves (``j_hz``, ``theta_rad``, ``branch_warn``, ...) stay in the
#: estimator's metadata JSON: ``Result.fit`` is a flat scalar surface.
_COUPLING_KEYS = (
    "coupler_off_v", "j_at_off_hz", "off_is_interpolated", "j_max_hz",
    "j_max_coupler_flux_v", "theta_max_rad", "n_j_ok", "n_branch_warn",
    "n_poly_rows",
)


def _coupling_summary(results: dict) -> dict:
    """The scalar half of one pair's coupling fit, NaN-filled when absent.

    Degrades rather than raising: the scqat bridge already keeps a run alive
    through an artifact failure, so a pair whose analysis returned nothing must
    still produce a complete (if empty) row instead of a KeyError.
    """
    return {key: float(results.get(key, float("nan"))) for key in _COUPLING_KEYS}


class PairSwapFluxMapParameters(TargetSelection, AveragingParameters, QubitResetParameters):
    """Inputs for the fixed-time 2D swap map. ``targets`` are PAIR components."""

    min_coupler_flux_v: float = Field(
        -0.15,
        description="Lowest coupler flux-pulse amplitude (V at the DAC; the usable "
                    "range is the coupler port's own, and the backend refuses past it).")
    max_coupler_flux_v: float = Field(
        0.15, description="Highest coupler flux-pulse amplitude (V).")
    num_coupler_points: int = Field(31, gt=4, description="Number of coupler-flux points (x axis).")
    min_qubit_flux_v: float = Field(
        0.0, description="Lowest member flux-pulse amplitude (V).")
    max_qubit_flux_v: float = Field(
        0.2, description="Highest member flux-pulse amplitude (V).")
    num_qubit_points: int = Field(21, gt=4, description="Number of member-flux points (y axis).")
    swap_time_ns: float | None = Field(
        None, ge=16.0,
        description="Fixed duration of BOTH flux pulses (ns; the driver quantizes to its "
                    "clock grid). None plays each waveform's own native length — the only "
                    "legal setting for a shaped pulse.")
    flux_pulse_shape: Literal["square", "flattop_cosine"] = Field(
        "square",
        description="Waveform played on BOTH flux lines. 'square' is flat and its duration "
                    "may be overridden; 'flattop_cosine' has raised-cosine edges and plays "
                    "its native length only (truncating it would change the pulse area).")
    drive_side: Literal["high", "low"] = Field("low", description=DRIVE_SIDE_DESC)
    flux_side: Literal["high", "low"] = Field("low", description=FLUX_SIDE_DESC)
    min_transfer: float = Field(0.3, ge=0.0, le=1.0, description=MIN_TRANSFER_DESC)

    @model_validator(mode="after")
    def _shaped_pulse_plays_its_native_length(self) -> "PairSwapFluxMapParameters":
        if self.flux_pulse_shape != "square" and self.swap_time_ns is not None:
            raise ValueError(
                f"flux_pulse_shape={self.flux_pulse_shape!r} plays its native length: a "
                f"duration override truncates the shaped edges and changes the pulse "
                f"area. Leave swap_time_ns=None, or recalibrate the operation's own "
                f"length.")
        return self


class PairSwapFluxMapResult(Result):
    """``fit[pair]``: ``best_transfer`` (peak excitation on the UNDRIVEN member)
    and its ``best_qubit_flux_v`` / ``best_coupler_flux_v`` coordinates, the
    per-map ranges ``p_high_min/max`` and ``p_low_min/max``, ``p_ee_max``, the
    axis sizes, and ``swap_time_ns`` — the duration the instrument ACTUALLY
    played (quantized by the driver), not the one that was asked for.

    Plus the SCALAR half of the coupling fit: ``coupler_off_v`` (the decouple
    point) with ``j_at_off_hz`` and ``off_is_interpolated`` (0 = the polynomial
    did not bracket it and the grid minimum was reported instead),
    ``j_max_hz`` / ``j_max_coupler_flux_v`` / ``theta_max_rad`` (the strongest
    QUOTABLE coupling — folded columns are excluded), and the counts ``n_j_ok``
    (columns whose peak fit passed), ``n_branch_warn`` (of those, how many may
    have folded past a full swap) and ``n_poly_rows`` (what the polynomial saw).
    Every one of them is NaN when the duration was unknown or nothing fitted.
    The per-coupler CURVES live in the estimator's metadata JSON.

    Record-only: no ``update()``, nothing written to the device."""


@register
class PairSwapFluxMap(Experiment):
    """Backend-agnostic fixed-time swap map. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "pair_swap_flux_map"
    description: ClassVar[str] = (
        "Fixed-duration 2D swap map: excite ONE member of a pair, then play a coupler flux "
        "pulse and a member flux pulse simultaneously over a fixed window, sweeping both "
        "amplitudes (absolute volts) and reading both members' joint populations. The "
        "excitation-transfer spot shows where the pair swaps and how the coupler bias "
        "moves that point — the fixed-time sibling of pair_swap_chevron. Record-only "
        "diagnostic: the per-map summary lands in result.fit and nothing is written back."
    )
    Parameters: ClassVar[type] = PairSwapFluxMapParameters
    Result: ClassVar[type] = PairSwapFluxMapResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        # declared in RAW NESTING ORDER: outer loop = member flux, inner = coupler
        sweeps=("qubit_flux_v", "coupler_flux_v"), sweep_units=("V", "V"),
        # the readout schema's digital+average+joint form (see pair_swap_chevron)
        variables=("joint_population",), readout_dims=("joint_state",),
    )
    target_kinds: ClassVar[tuple[str, ...]] = ("qubit_pair",)
    #: none, deliberately — see PairSwapChevron.required_operations.
    required_operations: ClassVar[tuple[str, ...]] = ()

    params: PairSwapFluxMapParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            "qubit_flux_v": np.linspace(self.params.min_qubit_flux_v,
                                        self.params.max_qubit_flux_v,
                                        self.params.num_qubit_points),
            "coupler_flux_v": np.linspace(self.params.min_coupler_flux_v,
                                          self.params.max_coupler_flux_v,
                                          self.params.num_coupler_points),
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """The chevron's exchange kernel with the time frozen: the resonance
        line bends with the coupler bias (which also tunes J), so the fixed-time
        ``sin^2`` draws a closed swap spot on it."""
        vq = coords["qubit_flux_v"]
        vc = coords["coupler_flux_v"]
        pairs = self.params.targets
        rng = np.random.default_rng(stable_seed("pair_swap_flux_map", *pairs))
        t_ns = float(self.params.swap_time_ns or 100.0)
        span_q = float(np.ptp(vq)) or 1.0
        span_c = float(np.ptp(vc)) or 1.0
        shape = (len(pairs), vq.size, vc.size)
        p_driven = np.empty(shape)
        p_partner = np.empty(shape)
        p_ee = np.empty(shape)
        vq_step = span_q / max(vq.size - 1, 1)
        for k in range(len(pairs)):
            vq0 = float(rng.uniform(vq.min() + 0.3 * span_q, vq.min() + 0.7 * span_q))
            # The fixed duration is CHOSEN near a full swap — that is why this
            # map is run after the chevron — so draw the coupling to put
            # 2*J*T near a half cycle, where sin^2 peaks.
            j0_hz = float(rng.uniform(0.4, 0.6)) / (2 * t_ns * 1e-9)
            j1_hz = float(rng.uniform(-0.4, 0.4)) * j0_hz / (0.5 * span_c)
            # slope + curvature relative to the GRID, so the spot is resolvable
            # (see pair_swap_chevron.simulate for why this is not an absolute
            # Hz/V draw)
            slope = (2 * j0_hz) * float(rng.uniform(0.3, 0.8)) / vq_step
            curv = float(rng.uniform(2.0, 5.0)) * vq_step / (0.5 * span_c) ** 2
            tau_ns = float(rng.uniform(300.0, 900.0))
            prep = float(rng.uniform(0.94, 0.99))
            therm = float(rng.uniform(0.005, 0.02))
            # the resonance line bends with the coupler bias
            delta = slope * (vq[:, None] - vq0 - curv * vc[None, :] ** 2)
            j_hz = np.abs(j0_hz + j1_hz * vc)[None, :]
            omega = np.sqrt(delta ** 2 + (2 * j_hz) ** 2)
            swap = ((2 * j_hz) ** 2 / omega ** 2 * np.sin(np.pi * omega * t_ns * 1e-9) ** 2
                    * np.exp(-t_ns / tau_ns))
            p_partner[k] = prep * swap + rng.normal(0, 0.02, (vq.size, vc.size))
            p_driven[k] = (prep * (1.0 - swap) * np.exp(-t_ns / (8 * tau_ns))
                           + rng.normal(0, 0.02, (vq.size, vc.size)))
            p_ee[k] = therm + rng.normal(0, 0.005, (vq.size, vc.size))
        for arr in (p_driven, p_partner, p_ee):
            np.clip(arr, 0.0, 1.0, out=arr)
        joint = _joint_from_roles(self.params.drive_side, p_driven, p_partner, p_ee)
        return {"joint_population": (
            ("target", "joint_state", "qubit_flux_v", "coupler_flux_v"), joint)}

    def readout_coords(self) -> dict:
        return {"joint_state": joint_state_labels(2)}

    def estimate(self) -> PairSwapFluxMapResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        ds = self.dataset.transpose("target", "joint_state", "qubit_flux_v", "coupler_flux_v")
        # the driver stashes the QUANTIZED duration it actually played; fall back
        # to what was asked for when no driver ran (simulated backend). Resolved
        # BEFORE the estimator runs because the estimator needs it: theta is a
        # pure number, but J in Hz is theta/(2*pi*t). A shaped pulse plays its own
        # native length and leaves this None, and then only the angle is reported.
        played = getattr(self, "_flux_time_ns", None)
        if played is None:
            played = self.params.swap_time_ns
        # Raw joint-state-population maps + the per-coupler-column coupling fit
        # -> scqat artifacts (figures + plotdata + metadata, one folder per pair).
        # Record-only: the SUCCESS verdict below (min_transfer) stays here, and
        # nothing — j_hz included — is proposed to the device.
        from scqat.estimators.pair_swap_flux_map import PairSwapFluxMapEstimator
        from .._scqat import per_qubit_results

        per_pair = per_qubit_results(
            ds, PairSwapFluxMapEstimator(), artifact_dir=self.artifact_dir,
            drive_side=self.params.drive_side, flux_side=self.params.flux_side,
            swap_time_ns=float(played) if played is not None else None,
            per_target_kwargs=_role_names(self.device, self.params.targets))
        result = PairSwapFluxMapResult()
        for pair in self.params.targets:
            fit, ok = summarize_transfer_map(
                ds.sel(target=pair), self.params.drive_side,
                ("qubit_flux_v", "coupler_flux_v"), self.params.min_transfer)
            fit["swap_time_ns"] = float(played) if played is not None else None
            # Lift the estimator's SCALAR coupling numbers onto the run record, so
            # find_runs and a campaign aggregate answer "how strong, and where is
            # the decouple point?" without reopening the artifact folder. The
            # per-coupler CURVES stay in the metadata JSON — result.fit is scalars.
            fit.update(_coupling_summary(per_pair.get(pair, {})))
            result.fit[pair] = fit
            result.outcomes[pair] = Outcome.SUCCESSFUL if ok else Outcome.FAILED
        return result

    @classmethod
    def validate_targets(cls, roster, targets):
        """Two gates, both pre-probe: the tracked coupler the x axis rides (the
        same one ``pair_zz_coupler`` needs), and a member flux line for the y
        axis. Which member carries y is a parameter this hook cannot see, so the
        driver refuses the SELECTED member pre-probe."""
        problems = _coupler_problems(roster, targets,
                                     "nothing to sweep on the x axis")
        problems += _flux_member_problems(roster, targets,
                                          "nothing to sweep on the y axis")
        return problems

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
