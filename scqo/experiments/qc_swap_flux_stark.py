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
2. **The map's 2-D argmax is NOT the calibration**, on either axis. ``T(N)``
   goes as ``sin^2(N*theta_eff)``, so at a fixed N the flux that transfers best
   is the one where ``N*theta_eff = pi/2`` — off resonance whenever the per-round
   angle exceeds ``pi/(2N)``. Measured on 5Q4C q1_q2 (2026-09-20): the N=2 map's
   ``best_flux_amp_v`` sat 3.25 mV from resonance, more than half the 5.75 mV
   FWHM, while the N=1 map's ``best_stark_amp`` was a draw between 21
   statistically identical cells. ``best_transfer`` and its coordinates stay in
   ``result.fit`` as the map summary they are; the CALIBRATION is the
   compensation ridge the estimator fits across the flux rows (see the Result).

HOW THE RIDGE IS READ, and what the prior is for. Along any FIXED flux row the
largest transfer is at ``phi = 0`` exactly — the row's own compensation — as long
as ``theta <= pi/N``, and that row's peak is the transfer ON the ridge. Read along
the flux, those peaks trace an ARCH whose centre is the resonance. The estimator
FITS the arch rather than taking its largest row, because the top is flat and
shot noise decides which row wins (1.3 mV from the fitted centre on 5Q4C's N=5 map
of 2026-09-21, against a +-0.10 mV fit), and it interpolates the compensating
amplitude between the two live rows that bracket that centre. Fitting the arch,
and turning its height into an angle, both need to know how far ``N*theta`` has
run — past ``pi/2`` the arch folds into a dip between two maxima — which a fixed-N
map cannot tell you. That is what ``swap_angle_rad`` supplies — a prior from a
preceding ``pair_swap_flux_map``, used as a branch selector rather than a fitted
value, and accurate to ``pi/(2N)`` is enough.

READOUT (the unified readout schema): digital, both modes, exactly as
``qc_n_swap_amp`` — ``readout_mode="average"`` (default) stores the pair's
``joint_population`` over ``joint_state`` labels; ``"shot"`` keeps every shot as
per-member integer levels (``state @ (target, member, *sweeps, shot_idx)``,
member order high, low). ``estimate()`` reduces the shot form to the same joint
distribution, so both modes yield identical maps.

RECORD-ONLY for the DEVICE: there is no ``update()`` and nothing lands on the
device surface; the summary and the ridge read both live in ``result.fit``. The
scqat estimator (``qc_swap_flux_stark``) draws the raw joint state populations —
a per-pair 2x2 population figure — plus a second figure for the ridge, with
plotdata/metadata under ``analysis/<pair>/``. It fits, but it proposes nothing;
the SUCCESS verdict (``min_transfer``) is made here in ``estimate()``, not by the
estimator, and a run whose ridge never converged is still SUCCESSFUL if the
transfer cleared the threshold.
"""

from __future__ import annotations

import math
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

#: the SIMULATED resonant, compensated round, as a fraction of a full swap. A
#: QUARTER (N*theta = pi/4) on purpose: at a HALF the reading is undefined —
#: sin^2(N*theta) is at its saturated peak, so the angle has zero sensitivity —
#: and the model would exercise exactly the one case the estimator cannot read.
#: ``qc_n_stark_amp``'s model was corrected the same way when its contract was
#: locked.
SIM_SWAP_FRACTION = 0.25

#: detuning at the edge of the SIMULATED flux window, in units of ``2J``. Sized
#: from the WINDOW, not from the grid step: the resonance is an arch fitted ACROSS
#: the flux rows, so a stripe only a few points wide would leave nothing to fit
#: however finely the axis is sampled. 1.75 is what 5Q4C q1_q2 measured on
#: 2026-09-20 (5.75 mV FWHM in a 10 mV window), and it leaves a comfortable
#: half of the rows carrying a usable stark swing.
SIM_EDGE_DETUNING = 1.75

#: the compensation-ridge scalars the scqat estimator reports, lifted into
#: ``result.fit``. Missing keys degrade to NaN — an older scqat leaves every one
#: of them NaN without raising (see this repo's pyproject scqat-floor block).
RIDGE_KEYS = (
    "compensating_stark_amp",
    "compensating_stark_err",
    "compensation_in_gap",
    "resonance_flux_amp_v",
    "resonance_flux_err_v",
    "resonance_at_edge",
    "resonance_unresolved",
    "arch_r_squared",
    "ridge_peak_flux_amp_v",
    "ridge_peak_transfer",
    "swap_angle_rad_refined",
    "swap_angle_err_rad",
    "swap_angle_rad_prior",
    "swap_angle_consistent",
    "ridge_slope_per_v",
    "ridge_local_rms",
    "ridge_wrap_amp",
    "stark_amp_2pi_prior",
    "wrap_consistent",
    "n_ridge_rows",
    "n_fold_rows",
    "max_row_contrast",
    "ridge_ok",
    "branch_ok",
)


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
                    "least 2. Pick it from the prior angle as N ~ pi/(4*theta): that puts "
                    "N*theta on the STEEPEST part of sin^2(N*theta), which is where the "
                    "angle read is most precise (delta_theta ~ delta_T/N, an N-fold gain "
                    "over a single swap), and it opens both of the estimator's gates with "
                    "margin. N*theta near pi/2 is the WORST choice: that is the saturated "
                    "peak, where the angle has no sensitivity at all.")
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
    swap_angle_rad: float | None = Field(
        None, gt=0.0, le=math.pi / 2,
        description="PRIOR exchange angle (rad) of ONE swap at the coupler flux the chosen "
                    "swap_operation bakes — a full swap is pi/2. Read it from a preceding "
                    "pair_swap_flux_map's per-column theta_rad (in its metadata JSON) at that "
                    "coupler flux; do NOT use theta_max_rad, which is the map's largest "
                    "QUOTABLE column and not the one your macro plays, and do not use a column "
                    "flagged branch_warn, whose arcsin has already folded. It is a BRANCH "
                    "SELECTOR, not a fitted value: it decides which of the estimator's two "
                    "gates open (N*theta <= pi for the compensating amplitude, <= pi/2 for the "
                    "refined angle) and only has to be good to about pi/(2*swap_count). Left "
                    "None, both gates stay shut and only the raw ridge coefficients are "
                    "reported.")
    stark_amp_2pi: float | None = Field(
        None, gt=0.0,
        description="The stark amplitude FACTOR worth one full 2*pi of phase, from a "
                    "preceding qubit_stark_phase_echo on the driven member "
                    "(its amp_2pi_factor). The compensation is only defined modulo one "
                    "turn, and the ridge re-enters the swept window whenever the flux "
                    "drives the phase past it, so this makes that unwrap exact instead "
                    "of measured off the jump. Calibrate max_stark_amp TO this value: a "
                    "shorter window leaves some rows with no compensation point at all "
                    "and pins their peak against the edge, a longer one adds a partial "
                    "second branch. Note the tone's phase is NOT proportional to its "
                    "amplitude (quadratic near zero, near-linear once saturated), which "
                    "is why one scalar cannot replace the curve — it fixes the PERIOD, "
                    "not the shape.")
    min_row_contrast: float = Field(
        0.3, ge=0.0, le=1.0,
        description="How far a single flux row's transfer must swing along the stark axis "
                    "before its peak is believed and the row enters the compensation-ridge "
                    "fit. A row whose swap is near-full carries almost no stark dependence "
                    "(its available signal collapses), so its peak is noise; raise this on a "
                    "noisy map, lower it to use more rows.")
    drive_side: Literal["high", "low"] = Field("low", description=DRIVE_SIDE_DESC)
    flux_side: Literal["high", "low"] = Field("low", description=FLUX_SIDE_DESC)
    min_transfer: float = Field(0.3, ge=0.0, le=1.0, description=MIN_TRANSFER_DESC)


class QcSwapFluxStarkResult(Result):
    """``fit[pair]``: the map summary plus the compensation-ridge read.

    Map summary — ``best_transfer`` (peak excitation on the UNDRIVEN member —
    which member that is follows from the ``drive_side`` parameter) and its
    ``best_flux_amp_v`` / ``best_stark_amp`` coordinates, plus the per-map
    marginal ranges ``p_high_min/max`` and ``p_low_min/max``, ``p_ee_max`` (the
    double-excitation witness) and the axis sizes. **Those two ``best_*``
    coordinates are the raw 2-D argmax and are NOT the calibration** — at N >= 2
    the flux argmax sits where ``N*theta_eff = pi/2``, which is off resonance,
    and at N = 1 the stark argmax is a draw between statistically identical
    cells. Read the ridge keys instead.

    Ridge read (:data:`RIDGE_KEYS`) — ``compensating_stark_amp`` at
    ``resonance_flux_amp_v`` is the answer, with ``swap_angle_rad_refined`` the
    N-amplified exchange angle, each with its one-sigma error
    (``compensating_stark_err``, ``resonance_flux_err_v``,
    ``swap_angle_err_rad``). The resonance is the centre of the arch the
    phase-compensated transfer traces along the flux, FITTED rather than picked
    (``arch_r_squared`` is that fit's), and the compensation is interpolated
    between the two live rows that bracket it. Each is behind its own gate:
    ``ridge_ok`` (``N*theta <= pi``) for the first pair, ``branch_ok``
    (``N*theta <= pi/2``) for the angle, both decided by the ``swap_angle_rad``
    prior and both NaN when shut.

    Refusals — with a gate open, three flags NaN their numbers while leaving the
    errors in place to show why. ``resonance_at_edge``: the fitted centre lies
    outside the live rows, so it was extrapolated — move the flux window onto
    it. ``resonance_unresolved``: its error exceeds a tenth of the live span or
    a stark step's worth of compensation, or the fit failed — widen the window
    or average more. ``compensation_in_gap``: the resonance sits in a dead band
    of rows with no stark signal; the arch reaches it from its two flanks, but
    the compensation would have to be interpolated across the band, so only
    that number is withheld.

    Ungated — ``ridge_peak_flux_amp_v`` is the flux where the phase-compensated
    transfer is largest, with ``ridge_peak_transfer`` its height. It needs no
    prior because it is a MEASUREMENT rather than a claim, so it is the flux to
    act on from a run that carried no priors at all. With a prior, prefer the
    fitted resonance: on a flat-topped arch the largest row is decided by shot
    noise, and past the fold it is one of the two flanks instead — which is
    precisely what the prior is there to tell you.

    Diagnostics — ``ridge_slope_per_v`` is how fast the compensation moves with
    the flux (how tightly the flux must be held) and ``ridge_local_rms`` how
    well the stark axis resolved the ridge; a run whose rms approaches the stark
    step is under-sampled and its compensation is worth no more than that step.
    ``ridge_wrap_amp`` is the ``2*pi`` period the map measured for itself,
    ``wrap_consistent`` compares it with the ``stark_amp_2pi`` prior,
    ``n_fold_rows`` counts the rows whose arcsin was unfolded,
    ``n_ridge_rows`` / ``max_row_contrast`` say whether the stark axis carried
    signal at all, and ``swap_angle_consistent`` compares the refined angle
    against the prior without acting on it.

    Record-only: no ``update()``, nothing written to the device."""


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
        = cos(phi/2)*cos(theta)``, and a state starting at the pole then gives,
        after N rounds::

            transfer = env * sin^2(theta) * [sin(N*theta_eff)/sin(theta_eff)]^2

        Because ``phi`` depends on BOTH knobs, the map is a RIDGE along the
        ``phi = 0`` line, not a circular spot — and the detuning envelope picks
        one bright segment out of that ridge, where the members are resonant AND
        the phase is nulled. That tilt is exactly why a one-knob scan of either
        axis cannot find the working point on its own.

        The swap time is drawn so the resonant, compensated round is
        :data:`SIM_SWAP_FRACTION` of a full swap — a QUARTER, not a half: at
        ``N*theta = pi/2`` the transfer sits on the saturated peak of
        ``sin^2(N*theta)``, which is the one case the estimator's angle read is
        undefined for, so a model drawn there would never exercise the reading.
        """
        v = coords["flux_amp_v"]
        a = coords["stark_amp"]
        pairs = self.params.targets
        rng = np.random.default_rng(stable_seed("qc_swap_flux_stark", *pairs))
        n_swaps = float(self.params.swap_count)
        span_v = float(np.ptp(v)) or 1.0
        span_a = float(np.ptp(a)) or 1.0
        shot_mode = self.params.readout_mode == "shot"
        num_shots = int(self.params.num_averages)
        per_pair = []
        for k in range(len(pairs)):
            v0 = float(rng.uniform(v.min() + 0.25 * span_v, v.min() + 0.75 * span_v))
            a0 = float(rng.uniform(a.min() + 0.25 * span_a, a.min() + 0.75 * span_a))
            j_hz = float(rng.uniform(3e6, 9e6))
            # The pulse is the one that makes N rounds SIM_SWAP_FRACTION of a
            # full transfer on the compensated resonance: theta = that, over N.
            theta0 = SIM_SWAP_FRACTION * np.pi / n_swaps
            t_sw_s = theta0 / (2.0 * np.pi * j_hz)
            # The detuning slope is drawn RELATIVE to the swept WINDOW (see
            # SIM_EDGE_DETUNING) so the resonance stripe spans a usable fraction
            # of the rows — the reading fits a line ACROSS them, so a stripe
            # scaled to the grid step would vanish under a finer sweep.
            slope = ((2 * j_hz) * SIM_EDGE_DETUNING * float(rng.uniform(0.8, 1.2))
                     / (0.5 * span_v))                                  # Hz per V
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
            # the EXACT composite: the axis component along z is normalised by
            # sin(theta_eff), which is what turns the N-round rotation into the
            # Chebyshev ratio below. (An un-normalised `1 - tilt^2` moves the
            # row optimum off phi = 0, i.e. off the thing being modelled.)
            ratio = np.sin(n_swaps * theta_eff) / np.sin(theta_eff)
            swap = (env * np.sin(theta) ** 2 * ratio ** 2
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
        # Raw joint-state-population maps + the AC-Stark compensation ridge ->
        # scqat artifacts (two figures + plotdata + metadata, one folder per
        # pair). With the count frozen there is no axis to FIT along, so the
        # estimator fits ACROSS the flux rows instead: each row's optimum along
        # stark is phi = 0, the transfer on those optima is an arch whose fitted
        # centre is the resonance, and the compensation is read between the
        # rows bracketing it. It still proposes nothing.
        from scqat.estimators.qc_swap_flux_stark import QcSwapFluxStarkEstimator
        from .._scqat import per_qubit_results

        analysis = per_qubit_results(
            ds, QcSwapFluxStarkEstimator(), artifact_dir=self.artifact_dir,
            drive_side=self.params.drive_side, flux_side=self.params.flux_side,
            swap_count=int(self.params.swap_count),
            swap_angle_rad=self.params.swap_angle_rad,
            stark_amp_2pi=self.params.stark_amp_2pi,
            min_row_contrast=float(self.params.min_row_contrast),
            per_target_kwargs=_role_names(self.device, self.params.targets))
        result = QcSwapFluxStarkResult()
        for pair in self.params.targets:
            fit, ok = summarize_transfer_map(
                ds.sel(target=pair), self.params.drive_side,
                ("flux_amp_v", "stark_amp"), self.params.min_transfer)
            ridge = analysis.get(pair, {})
            fit.update({key: float(ridge.get(key, float("nan"))) for key in RIDGE_KEYS})
            result.fit[pair] = fit
            # The verdict stays the transfer's: a map that reached min_transfer but
            # whose ridge never converged is a SUCCESSFUL record-only run, and the
            # two gates say why there is no compensation number.
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
