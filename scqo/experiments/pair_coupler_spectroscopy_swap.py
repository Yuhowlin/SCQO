"""The coupler's 0-1 frequency at its idle point, by swapping its excitation into a neighbour.

Per shot: reset both members -> a tone of frequency ``f`` on the TONE member's drive
line (its ``saturation`` operation, ``tone_len_ns`` long, at ``tone_power_dbm``) ->
``flux_buffer_ns`` -> in the RAMP arm, a flux waveform on the ramped line: with
``ramp_v = (first, last)`` it jumps to ``first`` at once, runs linearly to ``last`` at
``ramp_rate_v_per_us``, and drops back to idle at once; in the REFERENCE arm, a wait
of the same length ->
``flux_buffer_ns`` -> both members read out 2-level. Loops: averages (outer) -> ``f``
(start -> end) -> the two arms back to back (inner), so drift cancels between them.

The coupler has no drive line; the tone reaches it through its hybridization with
the tone member (a coupler Rabi of about ``g / delta`` of the member's). At ``f = f_c`` the
coupler is excited. The slow ramp then carries the coupler across a member
ADIABATICALLY - the excitation moves into it - and the sudden return is diabatic, so
it stays there. Which member receives it differs from pair to pair (5Q4C: q1 of
q1_q2, q2 of q2_q3), so the signal is the TOTAL excitation 1 - P00. The reference
arm keeps the excitation in the coupler; a neighbour's readout may still see it
there (5Q4C q1 sees q1_q2_c), so a reference line does not rule a line out. The
scqat estimator (``pair_coupler_spectroscopy_swap``) takes the ramp arm's lines,
keeps those the ramp CHANGES (ramp minus reference), and reads f01 as the HIGHEST of
them: driven hard the coupler shows its multi-photon ladder below it (f02/2 = f01 +
alpha/2, f03/3 ~ f01 + alpha), and every other coupler line must sit on that ladder.
Through the more strongly coupled member's line f01 broadens and f02/2 becomes the
strongest line, so drive through the weaker one. A line one or two sweep steps wide
is ignored - re-measure it with a finer step.

THE RAMP is ``ramp_v = (first, last)`` in PLAY order, and only its slow segment
(first -> last) is adiabatic: every crossing inside a jump (idle -> first, last ->
idle) is passed diabatically, and the member the slow segment crosses FIRST receives
the excitation. So ``(0, 0.14)`` swaps on the way out (slow out, fast back) and
``(0.14, 0)`` jumps out and swaps on the way BACK. On 5Q4C the second is the one that
works: the coupler (7.06 GHz) sits above the readout resonators (5.87-6.08 GHz),
which sit above the qubits, and swapping on the way back means the coupler crosses
the resonators again only after its excitation has gone to a qubit (2026-09-27; with
the coupler above both members the way back meets the OUTER one, the low member,
first). The slope sets the adiabaticity - Landau-Zener depends only on the rate at
which the crossing is passed: on 5Q4C q1_q2 the coupler slope at q1's crossing
(b = +0.109 V) is 22 MHz/mV, so 0.15 V/us is 99 % adiabatic at g = 50 MHz.

Every ramp voltage is a pulse amplitude relative to the ramped line's ``idle_flux``
(recorded as ``old_ramp_idle_flux``); ``ramp_v`` has no default - its far end comes
from ``pair_coupler_crossing_pulse``'s crossing plus about 30 mV.

ONE LO PER RUN: the tone window is ABSOLUTE and at most 500 MHz wide, played as
+-250 MHz of IF around one LO at its center. Proposes the coupler's ``f_01_hz`` (its
frequency at the CURRENT idle) and, when the ladder shows f02/2, its
``anharmonicity_hz``; no knob is written. One pair per run, as for
``pair_coupler_crossing_pulse``.
"""

from __future__ import annotations

import math
from typing import ClassVar, Literal

import numpy as np
from pydantic import Field, model_validator

from ..contract import DatasetContract
from ..experiment import Experiment
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from . import register
from ._capabilities.coupler_flux import pair_coupler
from ._capabilities.qubit_reset import QubitResetParameters
from ._capabilities.state_readout import joint_state_labels
from ._coupler_tone import TONE_AXIS, CouplerToneParameters, tone_lo_hz
from ._drive_power import drive_power_boundary
from ._sim import stable_seed
from ._window import window_bounds
from .pair_coupler_crossing_pulse import one_coupled_pair_problems
from .pair_swap_chevron import _role_names

ARM_AXIS = "ramp_played"

#: the ramp waveform is played on the 4 ns clock, 16 ns at least
_CLOCK_NS = 4
_MIN_RAMP_NS = 16

_FLAG_KEYS = ("no_line", "unexplained_lines", "peak_at_edge")


class PairCouplerSpectroscopySwapParameters(
    TargetSelection, AveragingParameters, QubitResetParameters, CouplerToneParameters,
):
    """Inputs for coupler spectroscopy by swap. ``targets`` is ONE pair component.
    The tone window, points, power and length are ``CouplerToneParameters``."""

    num_averages: int = Field(300, gt=0, description="Number of shots to average per sweep point.")
    tone_on: Literal["high", "low"] = Field(
        "high",
        description="The member whose drive line carries the tone (roster role). The "
        "excitation is counted on BOTH members, so this only picks the line: use the "
        "more weakly coupled member's - through the strongly coupled one f01 broadens "
        "and f02/2 becomes the strongest line (5Q4C: 'low' for both couplers, q2's "
        "line).")
    ramp_on: Literal["coupler", "tone_member"] = Field(
        "coupler",
        description="Which flux line plays the ramp: the coupler's, or the tone member's own "
        "(for a coupler designed below the qubit, or a qubit that can move toward it).")
    ramp_v: tuple[float, float] | None = Field(
        None,
        description="(first, last) pulse amplitudes (V, relative to the ramped line's "
        "idle_flux) in PLAY order: the output jumps to first at once, runs linearly to "
        "last at ramp_rate_v_per_us, and drops to idle at once. Only that slow segment "
        "is adiabatic - the member it crosses first receives the excitation, and every "
        "crossing inside a jump is passed diabatically. (0, 0.14) swaps on the way out; "
        "(0.14, 0) jumps out and swaps on the way back (the one that works on 5Q4C, "
        "where the readout resonators sit between the coupler and the qubits). The far "
        "end is pair_coupler_crossing_pulse's crossing plus ~30 mV. Required: None is "
        "refused before any instrument time.")
    ramp_rate_v_per_us: float = Field(
        0.15, gt=0,
        description="Slope of the slow segment (V/us). Landau-Zener depends only on it.")
    flux_buffer_ns: int = Field(
        100, ge=0,
        description="Wait after the tone before the ramp, and after the ramp before the "
        "readout: 0, or a multiple of 4 ns from 16 ns up.")

    @model_validator(mode="after")
    def _buffer_and_ramp(self) -> "PairCouplerSpectroscopySwapParameters":
        b = self.flux_buffer_ns
        if b != 0 and (b < 16 or b % 4):
            raise ValueError(f"flux_buffer_ns={b}: use 0, or a multiple of 4 ns from 16 ns up")
        if self.ramp_v is not None and self.ramp_v[0] == self.ramp_v[1]:
            raise ValueError(f"ramp_v={list(self.ramp_v)}: both ends are equal - the ramp "
                             f"would not move")
        return self


class PairCouplerSpectroscopySwapResult(Result):
    """``fit[pair]``: ``f_c_hz`` (the coupler's f01, with ``f_c_stderr_hz``),
    ``fwhm_hz``, ``peak_height`` and ``snr`` of that line; ``alpha_hz`` (with
    ``alpha_stderr_hz``) from its f02/2 line, NaN without one; ``n_coupler_lines``
    (lines the ramp changes) and ``n_ladder_lines`` (those on f01's ladder, f01
    included); ``landing_high`` / ``landing_low`` (how much of f01 each member
    received - diagnostic); the flags ``no_line``, ``unexplained_lines`` (a coupler
    line off the ladder), ``peak_at_edge``; and the provenance ``lo_hz``,
    ``ramp_duration_ns``, ``old_ramp_idle_flux``. SUCCESSFUL = an f01 with every
    coupler line on its ladder, away from the window edge. The line lists (coupler,
    member-only, off-ladder) are in the estimator's metadata JSON."""


def simulated_line(pair: str, start_hz: float, end_hz: float, num_points: int) -> dict:
    """The hidden coupler ladder the simulator plants for ``pair`` over a tone
    window: f01 and f02/2 = f01 + alpha/2, split between the members at random, a
    share of f01 in the reference arm (a neighbour's readout seeing the coupler),
    and a line of the tone member's own ABOVE f01, identical in both arms. Widths
    are in sweep steps. Module-level so a test can compare."""
    lo, hi = window_bounds(start_hz, end_hz)
    span = hi - lo
    step = span / max(num_points - 1, 1)
    rng = np.random.default_rng(stable_seed("pair_coupler_spectroscopy_swap", pair))
    f01 = lo + float(rng.uniform(0.55, 0.75)) * span
    alpha = -float(rng.uniform(120e6, 160e6))
    return {"f01_hz": f01, "fwhm_01_hz": 5 * step, "height_01": 0.35,
            "alpha_hz": alpha, "f02_half_hz": f01 + alpha / 2, "fwhm_02_hz": 4 * step,
            "height_02": 0.2, "to_high": float(rng.uniform(0.2, 0.8)),
            "reference_share": float(rng.uniform(0.2, 0.4)),
            "member_line_hz": lo + float(rng.uniform(0.85, 0.93)) * span,
            "member_line_fwhm_hz": 6 * step, "member_line_height": 0.3}


def _line(f: np.ndarray, center: float, height: float, fwhm: float) -> np.ndarray:
    return height / (1 + ((f - center) / (fwhm / 2)) ** 2)


@register
class PairCouplerSpectroscopySwap(Experiment):
    """Backend-agnostic coupler swap spectroscopy. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "pair_coupler_spectroscopy_swap"
    description: ClassVar[str] = (
        "Coupler frequency at its idle point: a tone on one pair member's drive line "
        "excites the coupler when it hits a coupler transition, then a slow flux ramp "
        "carries the coupler across a member so the excitation swaps into it (a sudden "
        "return leaves it there), and the pair's total excitation 1 - P00 peaks. Every "
        "frequency also runs a reference shot without the ramp; the lines the ramp "
        "changes are the coupler's, the highest is f01 and the rest must sit on its "
        "multi-photon ladder (f02/2 gives alpha). The window is absolute, at most 500 "
        "MHz (one LO); the ramp end comes from pair_coupler_crossing_pulse. Proposes "
        "the coupler's f_01_hz and anharmonicity_hz. One pair per run; needs state "
        "discrimination."
    )
    Parameters: ClassVar[type] = PairCouplerSpectroscopySwapParameters
    Result: ClassVar[type] = PairCouplerSpectroscopySwapResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        # raw nesting order: frequency outer, the two arms inner
        sweeps=(TONE_AXIS, ARM_AXIS), sweep_units=("Hz", ""),
        variables=("joint_population",), readout_dims=("joint_state",),
    )
    target_kinds: ClassVar[tuple[str, ...]] = ("qubit_pair",)
    required_operations: ClassVar[tuple[str, ...]] = ()

    params: PairCouplerSpectroscopySwapParameters

    # ------------------------------------------------------------------ helpers
    def tone_member(self, pair: str) -> str:
        return self.device.roster.entities[pair].roles[self.params.tone_on][0]

    def ramp_line(self, pair: str) -> str:
        """The entity whose flux channel plays the ramp."""
        if self.params.ramp_on == "coupler":
            return pair_coupler(self.device.roster, pair)
        return self.tone_member(pair)

    def ramp_duration_ns(self) -> int:
        """The played slow segment: |last - first| at the requested slope, rounded UP
        to the 4 ns clock (so the realized slope is never steeper), 16 ns at least."""
        first, last = self.ramp_play_order()
        ns = abs(last - first) / self.params.ramp_rate_v_per_us * 1e3
        return max(_MIN_RAMP_NS, int(math.ceil(ns / _CLOCK_NS - 1e-9)) * _CLOCK_NS)

    def ramp_play_order(self) -> tuple[float, float]:
        """``ramp_v`` as floats: (first, last) AS PLAYED - the output jumps to ``first``
        at once, runs linearly to ``last``, and drops to idle at once."""
        first, last = self.params.ramp_v
        return float(first), float(last)

    def lo_hz(self) -> float:
        """The one LO the window is played around: its center."""
        return tone_lo_hz(self.params)

    # ------------------------------------------------------------------ physics
    def define_sweep(self) -> dict[str, np.ndarray]:
        p = self.params
        if p.ramp_v is None:
            raise ValueError(
                "ramp_v is required: (first, last) in play order, e.g. [0.14, 0] to jump "
                "past the crossings and swap on the slow way back. Take the far end from "
                "pair_coupler_crossing_pulse's crossings (crossing_<role>_upper_v or "
                "_lower_v, on the side the ramp goes) plus ~30 mV - 5Q4C q1_q2: 0.14 V.")
        if p.ramp_on == "tone_member":
            roster = self.device.roster
            for pair in p.targets:
                member = self.tone_member(pair)
                if (member, "flux") not in roster.defaults:
                    raise ValueError(f"{pair}: ramp_on='tone_member' but the tone member "
                                     f"{member!r} has no flux channel to ramp")
        return {
            TONE_AXIS: np.linspace(p.start_tone_freq_hz, p.end_tone_freq_hz,
                                   p.num_tone_freq_points),
            ARM_AXIS: np.array([1, 0]),
        }

    def readout_coords(self) -> dict:
        return {"joint_state": joint_state_labels(2)}

    def run(self) -> Result:
        """define_sweep -> acquire inside the recorded tone-power boundary on the
        TONE member's drive channel -> estimate."""
        self.sweep_axes = self.define_sweep()
        members = [self.tone_member(pair) for pair in self.params.targets]
        with drive_power_boundary(self, self.params.tone_power_dbm, targets=members):
            self.dataset = self.backend.acquire(self)
        self.Contract.validate(self.dataset)
        return self.run_estimate()

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """The planted ladder (``simulated_line``): split between the members in the
        ramp arm, a share of f01 on the high member in the reference arm, the tone member's
        own line in both; members independent, sampled with ``num_averages`` shots."""
        f = coords[TONE_AXIS]
        arms = coords[ARM_AXIS]
        p = self.params
        joint = np.empty((len(p.targets), 4, f.size, arms.size))
        for k, pair in enumerate(p.targets):
            t = simulated_line(pair, p.start_tone_freq_hz, p.end_tone_freq_hz,
                               p.num_tone_freq_points)
            rng = np.random.default_rng(stable_seed("pair_coupler_spectroscopy_swap", pair,
                                                    "noise"))
            f01 = _line(f, t["f01_hz"], t["height_01"], t["fwhm_01_hz"])
            ladder = f01 + _line(f, t["f02_half_hz"], t["height_02"], t["fwhm_02_hz"])
            own = _line(f, t["member_line_hz"], t["member_line_height"],
                        t["member_line_fwhm_hz"])
            for a, arm in enumerate(arms):
                ph, pl = np.full(f.size, 0.02), np.full(f.size, 0.02)
                if arm:
                    ph, pl = ph + t["to_high"] * ladder, pl + (1 - t["to_high"]) * ladder
                else:
                    ph = ph + t["reference_share"] * f01
                if p.tone_on == "high":
                    ph = ph + own
                else:
                    pl = pl + own
                ph, pl = np.clip(ph, 0, 1), np.clip(pl, 0, 1)
                probs = np.stack([(1 - ph) * (1 - pl), (1 - ph) * pl, ph * (1 - pl), ph * pl])
                for j in range(f.size):
                    joint[k, :, j, a] = (rng.multinomial(p.num_averages, probs[:, j])
                                         / p.num_averages)
        return {"joint_population": (("target", "joint_state", TONE_AXIS, ARM_AXIS), joint)}

    def estimate(self) -> PairCouplerSpectroscopySwapResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.pair_coupler_spectroscopy_swap import (
            PairCouplerSpectroscopySwapEstimator,
        )
        from .._scqat import per_qubit_results

        p = self.params
        names = _role_names(self.device, p.targets)
        played = getattr(self, "_ramp_duration_ns", None) or self.ramp_duration_ns()
        idle = {pair: self.anchor(self.ramp_line(pair), "idle_flux") for pair in p.targets}
        ds = self.dataset.transpose("target", "joint_state", TONE_AXIS, ARM_AXIS)
        results = per_qubit_results(
            ds, PairCouplerSpectroscopySwapEstimator(), artifact_dir=self.artifact_dir,
            per_target_kwargs=names, tone_on=p.tone_on, lo_hz=self.lo_hz(),
            ramp_duration_ns=float(played))

        result = PairCouplerSpectroscopySwapResult()
        for pair in p.targets:
            r = results[pair]
            fit: dict = {
                "f_c_hz": r["f_c_hz"], "f_c_stderr_hz": r["f_c_stderr_hz"],
                "fwhm_hz": r["fwhm_hz"], "peak_height": r["peak_height"], "snr": r["snr"],
                "alpha_hz": r["alpha_hz"], "alpha_stderr_hz": r["alpha_stderr_hz"],
                "n_coupler_lines": int(r["n_coupler_lines"]),
                "n_ladder_lines": int(r["n_ladder_lines"]),
                "landing_high": r["landing_high"], "landing_low": r["landing_low"],
                "lo_hz": self.lo_hz(), "ramp_duration_ns": float(played),
                "old_ramp_idle_flux": idle[pair],
            }
            fit.update({flag: int(r[flag]) for flag in _FLAG_KEYS})
            result.fit[pair] = fit
            result.outcomes[pair] = Outcome.SUCCESSFUL if r["success"] else Outcome.FAILED
        return result

    def update(self) -> None:
        """One SUCCESSFUL pair: the coupler's ``f_01_hz`` (its frequency at the current
        idle) and, when the ladder showed f02/2, its ``anharmonicity_hz``. No knob
        moves."""
        if self.result is None or len(self.result.fit) != 1:
            return
        for pair, fit in self.result.fit.items():
            if self.result.outcomes[pair] is not Outcome.SUCCESSFUL:
                continue
            coupler = self.device.component(pair_coupler(self.device.roster, pair))
            coupler.f_01_hz = fit["f_c_hz"]
            if math.isfinite(fit["alpha_hz"]):
                coupler.anharmonicity_hz = fit["alpha_hz"]

    @classmethod
    def validate_targets(cls, roster, targets):
        """One pair with a coupler flux line and two drivable, readable members (the
        same gate as ``pair_coupler_crossing_pulse``)."""
        return one_coupled_pair_problems(roster, targets)

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
