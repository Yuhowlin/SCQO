"""Partial-swap ANGLE calibration — coupler flux x swap count, record-only.

Excite ONE member of a pair, then apply **N** swaps in a chain — each the SAME
swap operation, played at the SAME swept **coupler** flux amplitude — and read
BOTH members out jointly. At a fixed coupler amplitude the transfer oscillates
in N with period ``pi / theta``, so fitting a cosine per coupler row turns the
map into the calibration curve ``theta(coupler flux)``: the exchange angle you
actually get for the volts you actually set.

WHICH KNOB, AND WHY THIS ONE. TUTORIAL section 12 splits the three knobs by
role: the moving member's own flux amplitude is the **resonance** knob (it puts
the two qubits on resonance and is never the angle knob), the pulse duration is
baked into the shaped waveform and kept fixed, and the **coupler** flux is the
angle knob — ``J_eff(Phi_c)`` is continuous, so at fixed duration
``theta = J_eff * t_p`` tunes smoothly from ~0 upward. This experiment sweeps
that knob and MEASURES the resulting angle, which is the step the workflow was
missing: until now the angle was read off a figure by counting the oscillation
period by eye, and nothing recorded what angle a named operation actually is.

THE SIBLING IT IS NOT. ``qc_n_swap_amp`` sweeps the CONTROL member's flux
amplitude with the coupler left at its baked value, to find the resonance by
error amplification; it reports where transfer peaks and fits nothing. Same
sweep shape, different knob and different model — so, per the 1:1 rule, its own
estimator. Run that one FIRST (resonance), then this one (angle).

WHAT THE FIT ACTUALLY RETURNS, and why it is not always the exchange angle.
Repeating the swap does not just amplify the exchange: between swaps the two
members accumulate a RELATIVE phase ``phi`` (during the flux pulse, during
``operation_gap_ns``, and at idle). In the single-excitation subspace the round
is therefore an exchange followed by a Z rotation, and those do not commute. The
composite per-round rotation has half-angle ``theta_eff`` with

    cos(theta_eff) = cos(phi/2) * cos(theta)

and it is ``theta_eff`` that the N-oscillation reports. Two consequences:
``theta_eff >= theta`` ALWAYS — an uncompensated phase can only INFLATE the
apparent angle — and the oscillation's contrast falls as phi grows. Measured on
5Q4C 2026-09-01: at ``coupler_flux_v = 0.0``, where the coupler pulse has zero
amplitude and the swap is therefore physically identical, changing
``operation_gap_ns`` from 0 to 20 moved the fitted angle from 0.993 to 1.561 rad
— the second reading being indistinguishable from a full iSWAP.

So ``theta_rad`` is the PER-ROUND COMPOSITE angle, and equals the exchange angle
only when ``phi`` is nulled. ``compensation_amps`` is how you null it: an
off-resonant AC-Stark tone on one member adds a controllable differential phase,
exactly as ``qc_unidirectional_trotter`` compensates its chain. Only the
DIFFERENCE between the members is observable, so a tone on ONE member is enough.
Calibrate it with ``qc_n_stark_amp`` at the same swap, or scan it here across
runs and take the MINIMUM of ``theta_rad`` — the minimum is the true exchange
angle, and it is immune to decoherence because decay changes the oscillation's
amplitude, not its frequency.

READOUT (the unified readout schema): digital, both modes. ``readout_mode=
"average"`` (default) stores the pair's ``joint_population`` over ``joint_state``
labels; ``"shot"`` keeps every shot as per-member integer levels
(``state @ (target, member, *sweeps, shot_idx)``, member order high, low).
``estimate()`` reduces the shot form to the same joint distribution first, so
both modes yield identical angles.

RECORD-ONLY for the DEVICE: there is no ``update()`` and nothing lands on the
device surface. The angle is reported in ``result.fit`` and the operator sets the
coupler amplitude by re-registering the pulse (TUTORIAL section 12 step 2) —
the same hand-run writeback the rest of the pair family uses. The composite knob
``<op>_coupler_flux`` exists in the catalog but its QM binding expects a
CZ-shaped macro and does not reach ``ISwapImplementation``, so proposing it here
would write nothing.
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
    _flux_member_problems,
    _role_names,
    summarize_transfer_map,
)


def _compensation_problems(roster, params) -> list[str]:
    """Roster gate for ``compensation_amps``: named members, real drive lines.

    A tone on a qubit OUTSIDE the pair would shift a spectator and change nothing
    observable here, and a name that is not a member at all is a typo that would
    otherwise be played silently — so both refuse, naming every problem at once
    and before any instrument time is booked.
    """
    problems: list[str] = []
    for pair in params.targets:
        entity = roster.entities.get(pair)
        members = [m for role in ("high", "low")
                   for m in (getattr(entity, "roles", {}) or {}).get(role, ())]
        for qubit in sorted(params.compensation_amps):
            if qubit not in members:
                problems.append(
                    f"compensation_amps names {qubit!r}, which is not a member of "
                    f"{pair} {members} — only the pair's own members carry a "
                    f"compensation tone, and only their DIFFERENCE is observable")
            elif (qubit, "drive") not in roster.defaults:
                problems.append(
                    f"compensation_amps names {qubit!r}, which has no drive channel "
                    f"— nothing to play {params.stark_operation!r} on")
    return problems


class PairSwapAngleParameters(TargetSelection, AveragingParameters,
                              QubitResetParameters, ReadoutModeParameters):
    """Inputs for the swap-angle calibration. ``targets`` are PAIR components."""

    min_coupler_flux_v: float = Field(
        0.0,
        description="Lowest COUPLER flux amplitude (V at the DAC). This is the angle "
                    "knob, not the resonance knob — the moving member's own flux stays "
                    "at its calibrated baked value throughout. The usable range is the "
                    "coupler flux port's own and the backend refuses past it.")
    max_coupler_flux_v: float = Field(
        0.1, description="Highest COUPLER flux amplitude (V).")
    num_coupler_points: int = Field(
        21, gt=4, description="Number of coupler-flux points.")
    swap_counts: list[int] = Field(
        default_factory=lambda: list(range(21)),
        description="The swap counts N to apply. 0 is the x180-only baseline. The "
                    "default range is WIDER than qc_n_swap_amp's because the angle is "
                    "read from the oscillation PERIOD (pi/theta applications), so the "
                    "axis must span at least one period at the SMALLEST angle in the "
                    "sweep: 21 points resolve angles down to about 0.3 rad. An explicit "
                    "list, so a run that only needs large angles can be shorter.")
    swap_operation: str = Field(
        "partial_swap",
        description="Which named pair operation is repeated each swap (the driver "
                    "resolves it on the vendor pair). It must expose a COUPLER-side flux "
                    "pulse that accepts the swept amplitude; the driver refuses by name "
                    "when the operation is missing, has no coupler pulse, or has that "
                    "pulse baked at zero amplitude — which is unsettable, not merely "
                    "small (the amplitude is the divisor of the volts->scale conversion).")
    target_theta_rad: float | None = Field(
        None, gt=0.0,
        description="The exchange angle you want, in radians (pi/2 is a full iSWAP, "
                    "pi/4 a sqrt-iSWAP). The fitted curve is solved for it and the "
                    "coupler amplitude that delivers it is reported as "
                    "best_coupler_flux_v — interpolated when the target falls between "
                    "two measured points. None just measures the curve.")
    compensation_amps: dict[str, float] = Field(
        default_factory=dict,
        description="Per-member AC-Stark phase compensation, as {qubit name: amplitude "
                    "FACTOR of the stark operation's baked amplitude}, played after every "
                    "swap. This is what makes the fitted angle the EXCHANGE angle rather "
                    "than the per-round composite (see the module docstring): the tone is "
                    "off-resonant, so it shifts the qubit rather than rotating it, and the "
                    "shift integrated over the pulse is the differential phase it cancels. "
                    "Only the DIFFERENCE between the two members is observable, so naming "
                    "ONE member is enough — and only the pair's own members may be named. "
                    "Calibrate with qc_n_stark_amp, or scan across runs and take the "
                    "MINIMUM fitted angle. NOTE {} and {'q1': 0.0} are not the same run: a "
                    "qubit absent from the map plays no tone at all, while a factor of 0.0 "
                    "still plays and so keeps the round's DURATION — and hence its phase — "
                    "identical to a compensated one. Use 0.0 for the baseline you intend "
                    "to compare against.")
    stark_operation: str = Field(
        "stark",
        description="The named XY (RF) operation played on each compensated member after "
                    "every swap; its baked amplitude is the reference each compensation "
                    "factor multiplies. NOT x180 — a dedicated off-resonant tone (the "
                    "driver refuses by name if the operation is missing; register it via "
                    "quam_config/register_stark.py).")
    stark_detuning_hz: float = Field(
        50e6,
        description="FIXED off-resonant detuning (Hz) of the compensation tones from each "
                    "member's drive frequency. Must be off-resonant for a genuine Stark "
                    "shift (a resonant tone drives Rabi rotations instead); tune per chip. "
                    "Not a sweep axis, and shared by both members.")
    operation_gap_ns: int = Field(
        0, ge=0,
        description="Idle gap (ns) on the pair's flux lines after each swap, so the "
                    "pulse settles before the next swap fires. 0 disables; the QM "
                    "backend requires a multiple of 4 ns. It is ALSO a phase knob: the "
                    "members accumulate their relative phase through it, so changing it "
                    "changes the fitted angle unless compensation_amps nulls that phase.")
    drive_side: Literal["high", "low"] = Field("low", description=DRIVE_SIDE_DESC)
    flux_side: Literal["high", "low"] = Field("low", description=FLUX_SIDE_DESC)
    min_transfer: float = Field(
        0.3, ge=0.0, le=1.0,
        description="Peak transfer onto the UNDRIVEN member below which the run is "
                    "reported FAILED. A coherent exchange reaches near-unity transfer at "
                    "N = (pi/2)/theta whatever the angle is, so a low peak means the "
                    "swap is not exchanging at all rather than that the angle is small — "
                    "which is why this keeps the pair family's 0.3 and not the Trotter "
                    "chain's 0.05. Pure reporting: this experiment writes nothing back.")


class PairSwapAngleResult(Result):
    """``fit[pair]``: the angle calibration — ``best_coupler_flux_v`` and
    ``best_theta_rad`` (the solution for ``target_theta_rad``, interpolated when
    ``best_is_interpolated`` is 1), the reachable range ``theta_min_rad`` /
    ``theta_max_rad``, and ``n_theta_ok`` (how many coupler values yielded a
    converged fit) — alongside the usual transfer-map summary
    (``best_transfer`` and its coordinates, the marginal ranges, ``p_ee_max``).
    The full ``theta`` curve is per-coupler-value and so lives in the scqat
    metadata, not in ``fit`` (which is scalars).

    EVERY reported angle is the PER-ROUND COMPOSITE angle ``theta_eff``, which
    equals the exchange angle only when ``compensation_amps`` nulls the
    inter-swap phase; uncompensated it is an UPPER BOUND (see the module
    docstring). Record-only: no ``update()``, nothing written to the device."""


@register
class PairSwapAngle(Experiment):
    """Backend-agnostic partial-swap angle calibration. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "pair_swap_angle"
    description: ClassVar[str] = (
        "Partial-swap ANGLE calibration: excite ONE member of a pair, apply N repeated "
        "swaps at the same swept COUPLER flux amplitude (the angle knob — the member's "
        "own flux stays at its calibrated resonance value), and read both members' joint "
        "populations. At each coupler amplitude the transfer oscillates in N with period "
        "pi/theta, so a cosine fit per coupler row yields the calibration curve "
        "theta(coupler flux) and, given target_theta_rad, the amplitude that delivers a "
        "wanted angle. What is fitted is the PER-ROUND COMPOSITE angle: the members also "
        "accumulate a relative phase between swaps, which can only inflate the apparent "
        "angle, so compensation_amps plays an AC-Stark tone that nulls it and the true "
        "exchange angle is the MINIMUM over that compensation. Run qc_n_swap_amp first to "
        "set the resonance; this one then sets the angle. readout_mode='shot' keeps every "
        "shot instead of the averaged joint distribution. Record-only diagnostic: the "
        "calibration lands in result.fit and nothing is written back to the device."
    )
    Parameters: ClassVar[type] = PairSwapAngleParameters
    Result: ClassVar[type] = PairSwapAngleResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("coupler_flux_v", "swap_count"), sweep_units=("V", ""),
        # readout_mode="average": the joint distribution over the pair's basis
        # states (digit order high, low)...
        variables=("joint_population",), readout_dims=("joint_state",),
        # ...readout_mode="shot": every shot's per-member integer levels.
        alt_variables=(("state",),),
        alt_readout_dims=(("member", "shot_idx"),),
    )
    target_kinds: ClassVar[tuple[str, ...]] = ("qubit_pair",)
    #: none, deliberately: a composite's operations are DECLARED, and this
    #: experiment MEASURES the angle of a swap that is still being brought up.
    #: Requiring "partial_swap" would refuse exactly the chip it is for — the
    #: same rationale as pair_swap_chevron and qc_n_swap_amp.
    required_operations: ClassVar[tuple[str, ...]] = ()

    params: PairSwapAngleParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        # The compensation gate lives here: validate_targets cannot see params,
        # and this still runs before the backend is asked for anything.
        problems = _compensation_problems(self.device.roster, self.params)
        if problems:
            raise ValueError("pair_swap_angle: " + "; ".join(problems))
        return {
            # dict order IS the contract order: coupler flux outer, swap count inner.
            "coupler_flux_v": np.linspace(self.params.min_coupler_flux_v,
                                          self.params.max_coupler_flux_v,
                                          self.params.num_coupler_points),
            "swap_count": np.array(self.params.swap_counts, dtype=int),
        }

    def readout_coords(self) -> dict:
        if self.params.readout_mode == "shot":
            return {"member": ["high", "low"],
                    "shot_idx": np.arange(self.params.num_averages)}
        return {"joint_state": joint_state_labels(2)}

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """A coherent exchange, plus the inter-swap PHASE that contaminates it.

        On resonance one swap advances the exchange by ``theta = J_eff * t_p``,
        so ``N`` swaps would transfer ``sin^2(N*theta)`` — but only if nothing
        rotated the phase between them. Something always does (the flux pulse,
        the gap, the idle), and an exchange followed by a Z rotation does not
        commute, so what repeats is a composite rotation:

            cos(theta_eff) = cos(phi/2) * cos(theta)
            contrast       = sin^2(theta) / (sin^2(theta) + sin^2(phi/2) cos^2(theta))

        Both are planted here, because an offline test against a phase-free
        simulator would pass while the experiment silently mismeasured on
        hardware — which is exactly what happened on 5Q4C 2026-09-01. The
        residual phase is drawn with a COUPLER-DEPENDENT part, so one fixed
        ``compensation_amps`` cannot null it across the whole sweep, and a
        ``gap``-proportional part, so changing ``operation_gap_ns`` moves the
        answer the way the instrument does.

        ``J_eff`` is planted linear in the coupler flux, which is the local
        behaviour a narrow sweep sees; the estimator assumes no such shape.
        """
        v = coords["coupler_flux_v"]
        n = coords["swap_count"].astype(float)
        pairs = self.params.targets
        rng = np.random.default_rng(stable_seed("pair_swap_angle", *pairs))
        span = float(np.ptp(v)) or 1.0
        shot_mode = self.params.readout_mode == "shot"
        num_shots = int(self.params.num_averages)
        per_pair = []
        roles = _role_names(self.device, pairs)
        for _k, pair in enumerate(pairs):
            # Angles the sweep can actually resolve: the largest stays under the
            # integer-N Nyquist limit (theta = pi/2, a full iSWAP) and the
            # smallest completes a period within the default count axis.
            theta_lo = float(rng.uniform(0.25, 0.40))
            theta_hi = float(rng.uniform(1.10, 1.40))
            theta = theta_lo + (theta_hi - theta_lo) * (v - v.min()) / span
            n_tau = float(rng.uniform(30.0, 60.0))              # swaps to decohere
            prep = float(rng.uniform(0.94, 0.99))               # pi-pulse fidelity
            therm = float(rng.uniform(0.005, 0.02))             # residual |ee>

            # The residual differential phase per round: a constant, a part that
            # rides the gap, and a COUPLER-DEPENDENT part (the flux pulse shifts
            # both members while it plays). The last one is why a single fixed
            # compensation cannot null the phase across the whole sweep.
            phi_const = float(rng.uniform(-0.6, 0.6))
            phi_per_ns = float(rng.uniform(0.01, 0.05))         # rad per ns of gap
            phi_slope = float(rng.uniform(-6.0, 6.0))           # rad per volt
            phi = (phi_const
                   + phi_per_ns * float(self.params.operation_gap_ns)
                   + phi_slope * (v - v.min()))

            # The compensation tone: an off-resonant Stark shift, quadratic in
            # amplitude. Only the DIFFERENCE between the members is observable,
            # so the two roles enter with opposite sign.
            stark_k = float(rng.uniform(3.0, 9.0))              # rad per amp^2
            names = roles.get(pair, {})
            for qubit, factor in self.params.compensation_amps.items():
                sign = 1.0 if qubit == names.get("high_name") else -1.0
                phi = phi + sign * stark_k * float(factor) ** 2

            # The composite rotation the N-oscillation actually reports, and the
            # contrast the phase costs it.
            c_phi = np.cos(phi / 2.0)
            theta_eff = np.arccos(np.clip(c_phi * np.cos(theta), -1.0, 1.0))
            s2 = np.sin(theta) ** 2
            contrast = s2 / (s2 + np.sin(phi / 2.0) ** 2 * np.cos(theta) ** 2)
            swap = (contrast[:, None]
                    * np.sin(theta_eff[:, None] * n[None, :]) ** 2
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
            probs = np.stack([p00, p01, p10, p11])              # (4, coupler, count)
            probs /= probs.sum(axis=0, keepdims=True)
            if shot_mode:
                # Draw each shot's joint outcome code from the distribution
                # (inverse CDF), then split into per-member binary levels.
                u = rng.random((v.size, n.size, num_shots))
                cum = np.cumsum(probs, axis=0)                  # (4, coupler, count)
                code = (u[None, :, :, :] > cum[:, :, :, None]).sum(axis=0)
                levels = np.stack([code // 2, code % 2])    # (member, coupler, count, shot)
                per_pair.append(levels.astype(np.int64))
            else:
                # A small jitter only: the angle fit needs the OSCILLATION to
                # survive, and this is what an averaged digital readout looks like.
                jitter = rng.normal(0.0, 0.01, probs.shape)
                per_pair.append(np.clip(probs + jitter, 0.0, 1.0))
        if shot_mode:
            return {"state": (("target", "member", "coupler_flux_v", "swap_count",
                               "shot_idx"), np.stack(per_pair))}
        return {"joint_population": (("target", "joint_state", "coupler_flux_v",
                                      "swap_count"), np.stack(per_pair))}

    def estimate(self) -> PairSwapAngleResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        if "state" in self.dataset.data_vars:
            # shot mode: reduce per-shot member levels to the SAME joint
            # distribution average mode stores, then analyze identically.
            jp = states_to_joint_population(self.dataset["state"],
                                            member_dim="member", shot_dim="shot_idx")
            ds = jp.to_dataset()
        else:
            ds = self.dataset
        ds = ds.transpose("target", "joint_state", "coupler_flux_v", "swap_count")
        from scqat.estimators.pair_swap_angle import PairSwapAngleEstimator
        from .._scqat import per_qubit_results

        analysis = per_qubit_results(
            ds, PairSwapAngleEstimator(), artifact_dir=self.artifact_dir,
            drive_side=self.params.drive_side, flux_side=self.params.flux_side,
            target_theta_rad=self.params.target_theta_rad,
            per_target_kwargs=_role_names(self.device, self.params.targets))

        #: the angle scalars lifted out of the estimator's results into `fit`;
        #: the per-coupler curve itself stays in the scqat metadata.
        angle_keys = ("best_coupler_flux_v", "best_theta_rad", "best_is_interpolated",
                      "theta_min_rad", "theta_max_rad", "n_theta_ok",
                      "target_theta_rad")
        result = PairSwapAngleResult()
        for pair in self.params.targets:
            fit, transfer_ok = summarize_transfer_map(
                ds.sel(target=pair), self.params.drive_side,
                ("coupler_flux_v", "swap_count"), self.params.min_transfer)
            angles = analysis.get(pair, {})
            fit.update({key: float(angles.get(key, float("nan")))
                        for key in angle_keys})
            result.fit[pair] = fit
            # BOTH halves must hold: transfer proves the swap exchanges at all,
            # and a converged angle proves the period was resolvable. A map that
            # transfers but never oscillates within the count axis is a real
            # failure of THIS reading, not a success with a missing number.
            ok = transfer_ok and int(angles.get("n_theta_ok", 0)) > 0
            result.outcomes[pair] = Outcome.SUCCESSFUL if ok else Outcome.FAILED
        return result

    @classmethod
    def validate_targets(cls, roster, targets):
        """Two gates, both pre-probe: the tracked COUPLER whose flux is swept
        (the angle knob — the same coupler ``pair_swap_flux_map`` and
        ``pair_zz_coupler`` need), and a member flux line for the swap pulse
        itself. Which member carries that is a parameter this hook cannot see,
        so the driver refuses the SELECTED member pre-probe."""
        problems = []
        for pair in targets:
            entity = roster.entities.get(pair)
            couplers = getattr(entity, "roles", {}).get("coupler", ())
            if not couplers:
                problems.append(f"{pair}: declares no coupler role — "
                                f"nothing to sweep the swap ANGLE on")
            elif (couplers[0], "flux") not in roster.defaults:
                problems.append(f"{pair}: coupler {couplers[0]!r} has no flux channel")
        problems += _flux_member_problems(
            roster, targets, "nothing to play the swap pulse on")
        return problems

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
