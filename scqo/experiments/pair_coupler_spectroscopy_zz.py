"""The coupler's 0-1 frequency at its idle point, through its ZZ with a neighbour.

Per shot: reset both members -> a tone of frequency ``f`` on the TONE member's drive
line (its ``saturation`` operation, ``tone_len_ns`` long, at ``tone_power_dbm``) ->
in the PI arm the other member (the PI member) plays a SELECTIVE pi,
``selective_pi_len_ns`` long; in the REFERENCE arm, a wait of the same length -> both
members read out 2-level. Loops: averages
(outer) -> ``f`` (start -> end) -> the two arms back to back (inner), so drift
cancels between them.

The coupler has no drive line; the tone reaches it through its hybridization with the
tone member (a coupler Rabi of about ``g / delta`` of the member's). At a coupler
transition the coupler is excited, and while it is, the qubit-coupler ZZ pulls the pi
member off its drive frequency: a pi NARROWER than the ZZ then misses, and the pi
member's excited population DIPS. The tone is off before the pi, so only what it left
behind can spoil it. The scqat estimator (``pair_coupler_spectroscopy_zz``) finds the
dips of the PI ARM alone and reads f01 as the HIGHEST, every other dip on its
multi-photon ladder (as ``pair_coupler_spectroscopy_swap``). The reference arm (no pi)
is not subtracted: the pi member's readout sees the coupler directly only while the
member is in 0 (5Q4C q1 sees q1_q2_c's f01 and f02/2 as 0.1 bumps; in 1 it does not),
so pi minus reference would dig false dips at the coupler's lines. Its own lines are
reported as a diagnostic. Unlike that experiment the coupler never moves:
no ramp, no crossing, no dependence on where the readout resonators sit.

THE PI is SELECTIVE: a square pulse ``selective_pi_len_ns`` long whose area is the pi
member's calibrated x180's, so no new calibration. On 5Q4C the ZZ is ~0.3-0.9 MHz (two
Duffing oscillators, g = 40-70 MHz): the 16 ns x180 (about +-30 MHz wide) showed no
dip on hardware, as predicted. Square, not a smooth envelope, because the coupler
decays during the pulse (T1 ~5 us on 5Q4C q1_q2_c): a smooth envelope needs about
twice the length for the same selectivity. With that decay a 2 us square leaves
0.67-0.83 of the coupler occupation as dip over the ZZ range, a 4 us cosine 0.62-0.69,
a 4 us Gaussian 0.56-0.69.

THE LINES: the tone rides on ``tone_on``'s line (default the low member - on 5Q4C q2,
the more weakly coupled line for both couplers, where f01 is cleanest) and the pi on
the other member (the more strongly coupled one, with the larger ZZ). Two members, two
ports: no second upconverter. ONE LO PER RUN, as for ``pair_coupler_spectroscopy_swap``
(``_coupler_tone``). Proposes the coupler's ``f_01_hz`` (its frequency at the CURRENT
idle) and, when the ladder shows f02/2, its ``anharmonicity_hz``; no knob is written.
One pair per run.
"""

from __future__ import annotations

import math
from typing import ClassVar, Literal

import numpy as np
from pydantic import Field

from ..contract import DatasetContract
from ..experiment import Experiment
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from . import register
from ._capabilities.coupler_flux import pair_coupler
from ._capabilities.mapped_readout import SELECTIVE_PI_LEN_DESC
from ._capabilities.qubit_reset import QubitResetParameters
from ._capabilities.state_readout import joint_state_labels
from ._coupler_tone import TONE_AXIS, CouplerToneParameters, tone_lo_hz
from ._drive_power import drive_power_boundary
from ._sim import stable_seed
from ._window import window_bounds
from .pair_coupler_crossing_pulse import one_coupled_pair_problems
from .pair_swap_chevron import _role_names

ARM_AXIS = "pi_played"
_OTHER = {"high": "low", "low": "high"}

_FLAG_KEYS = ("no_line", "unexplained_lines", "peak_at_edge")


class PairCouplerSpectroscopyZZParameters(
    TargetSelection, AveragingParameters, QubitResetParameters, CouplerToneParameters,
):
    """Inputs for coupler spectroscopy by ZZ. ``targets`` is ONE pair component. The
    tone window, points, power and length are ``CouplerToneParameters``."""

    num_averages: int = Field(300, gt=0, description="Number of shots to average per sweep point.")
    selective_pi_len_ns: int = Field(
        2000, ge=16, multiple_of=4, description=SELECTIVE_PI_LEN_DESC)
    tone_on: Literal["high", "low"] = Field(
        "low",
        description="The member whose drive line carries the tone (roster role); the "
        "OTHER member gets the pi. Put the tone on the more weakly coupled member's line "
        "- through the strongly coupled one f01 broadens and f02/2 becomes the strongest "
        "line - which leaves the pi on the member with the larger ZZ (5Q4C: 'low' for "
        "both couplers, the tone on q2's line).")


class PairCouplerSpectroscopyZZResult(Result):
    """``fit[pair]``: ``f_c_hz`` (the coupler's f01, with ``f_c_stderr_hz``),
    ``fwhm_hz``, ``dip_depth`` and ``snr`` of that dip; ``alpha_hz`` (with
    ``alpha_stderr_hz``) from its f02/2 dip, NaN without one; ``n_lines`` and
    ``n_ladder_lines`` (those on f01's ladder, f01 included); ``pi_contrast`` (median
    pi arm minus median reference: the pi's efficiency times the readout contrast); the flags ``no_line``, ``unexplained_lines`` (a dip off the ladder),
    ``peak_at_edge``; and the provenance ``lo_hz``, ``old_coupler_idle_flux``.
    SUCCESSFUL = an f01 with every dip on its ladder, away from the window edge. The
    line lists - including the reference arm's own lines, where the pi member's
    readout sees the coupler - are in the estimator's metadata JSON."""


def simulated_line(pair: str, start_hz: float, end_hz: float, num_points: int) -> dict:
    """The hidden coupler ladder the simulator plants for ``pair`` over a tone
    window: the coupler's occupation at f01 and f02/2 = f01 + alpha/2, the pi's
    contrast and how much of it the ZZ spoils, and the share of the occupation the pi
    member's readout sees directly while the member is in 0. Widths are in
    sweep steps. Module-level so a test can compare."""
    lo, hi = window_bounds(start_hz, end_hz)
    span = hi - lo
    step = span / max(num_points - 1, 1)
    rng = np.random.default_rng(stable_seed("pair_coupler_spectroscopy_zz", pair))
    f01 = lo + float(rng.uniform(0.55, 0.75)) * span
    alpha = -float(rng.uniform(120e6, 160e6))
    return {"f01_hz": f01, "fwhm_01_hz": 5 * step, "occupation_01": 0.45,
            "alpha_hz": alpha, "f02_half_hz": f01 + alpha / 2, "fwhm_02_hz": 4 * step,
            "occupation_02": 0.3, "pi_contrast": 0.88, "spoil": 0.9,
            "readout_share": float(rng.uniform(0.2, 0.4))}


def _line(f: np.ndarray, center: float, height: float, fwhm: float) -> np.ndarray:
    return height / (1 + ((f - center) / (fwhm / 2)) ** 2)


@register
class PairCouplerSpectroscopyZZ(Experiment):
    """Backend-agnostic coupler ZZ spectroscopy. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "pair_coupler_spectroscopy_zz"
    description: ClassVar[str] = (
        "Coupler frequency at its idle point, the coupler never moving: a tone on one "
        "pair member's drive line excites the coupler when it hits a coupler "
        "transition, then the other member gets a pi; while the coupler is excited the "
        "qubit-coupler ZZ detunes that member and a pi narrower than the ZZ misses, so "
        "its population dips. Every frequency also runs a reference shot without the "
        "pi. The highest dip of the pi arm is f01 and the rest must sit on its "
        "multi-photon ladder (f02/2 gives alpha). The pi is a square selective pi with "
        "the x180's area, narrower than the ZZ (sub-MHz on 5Q4C). The window is "
        "absolute, at most 500 MHz (one LO). Proposes the coupler's f_01_hz and anharmonicity_hz. One pair per run; "
        "needs state discrimination."
    )
    Parameters: ClassVar[type] = PairCouplerSpectroscopyZZParameters
    Result: ClassVar[type] = PairCouplerSpectroscopyZZResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        # raw nesting order: frequency outer, the two arms inner
        sweeps=(TONE_AXIS, ARM_AXIS), sweep_units=("Hz", ""),
        variables=("joint_population",), readout_dims=("joint_state",),
    )
    target_kinds: ClassVar[tuple[str, ...]] = ("qubit_pair",)
    required_operations: ClassVar[tuple[str, ...]] = ()

    params: PairCouplerSpectroscopyZZParameters

    # ------------------------------------------------------------------ helpers
    def pi_role(self) -> str:
        """The role that gets the pi: the one the tone does NOT ride on."""
        return _OTHER[self.params.tone_on]

    def tone_member(self, pair: str) -> str:
        return self.device.roster.entities[pair].roles[self.params.tone_on][0]

    def pi_member(self, pair: str) -> str:
        return self.device.roster.entities[pair].roles[self.pi_role()][0]

    def lo_hz(self) -> float:
        """The one LO the window is played around: its center."""
        return tone_lo_hz(self.params)

    # ------------------------------------------------------------------ physics
    def define_sweep(self) -> dict[str, np.ndarray]:
        p = self.params
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
        """The planted ladder (``simulated_line``) as the coupler's occupation: the pi
        member loses ``spoil`` of its pi contrast where the coupler is excited, and
        its readout sees ``readout_share`` of the occupation while it is in 0 - all
        of the reference arm, the spoiled part of the pi arm; the tone member stays
        near ground. Members independent, sampled with
        ``num_averages`` shots."""
        f = coords[TONE_AXIS]
        arms = coords[ARM_AXIS]
        p = self.params
        joint = np.empty((len(p.targets), 4, f.size, arms.size))
        for k, pair in enumerate(p.targets):
            t = simulated_line(pair, p.start_tone_freq_hz, p.end_tone_freq_hz,
                               p.num_tone_freq_points)
            rng = np.random.default_rng(stable_seed("pair_coupler_spectroscopy_zz", pair,
                                                    "noise"))
            occupation = (_line(f, t["f01_hz"], t["occupation_01"], t["fwhm_01_hz"])
                          + _line(f, t["f02_half_hz"], t["occupation_02"], t["fwhm_02_hz"]))
            for a, arm in enumerate(arms):
                seen = t["readout_share"] * occupation
                if arm:
                    spoiled = t["spoil"] * occupation
                    mine = 0.02 + t["pi_contrast"] * (1 - spoiled) + seen * t["spoil"]
                else:
                    mine = 0.02 + seen
                other = np.full(f.size, 0.02)
                ph, pl = (mine, other) if self.pi_role() == "high" else (other, mine)
                ph, pl = np.clip(ph, 0, 1), np.clip(pl, 0, 1)
                probs = np.stack([(1 - ph) * (1 - pl), (1 - ph) * pl, ph * (1 - pl), ph * pl])
                for j in range(f.size):
                    joint[k, :, j, a] = (rng.multinomial(p.num_averages, probs[:, j])
                                         / p.num_averages)
        return {"joint_population": (("target", "joint_state", TONE_AXIS, ARM_AXIS), joint)}

    def estimate(self) -> PairCouplerSpectroscopyZZResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.pair_coupler_spectroscopy_zz import (
            PairCouplerSpectroscopyZZEstimator,
        )
        from .._scqat import per_qubit_results

        p = self.params
        names = _role_names(self.device, p.targets)
        idle = {pair: self.anchor(pair_coupler(self.device.roster, pair), "idle_flux")
                for pair in p.targets}
        ds = self.dataset.transpose("target", "joint_state", TONE_AXIS, ARM_AXIS)
        results = per_qubit_results(
            ds, PairCouplerSpectroscopyZZEstimator(), artifact_dir=self.artifact_dir,
            per_target_kwargs=names, pi_member=self.pi_role(), lo_hz=self.lo_hz())

        result = PairCouplerSpectroscopyZZResult()
        for pair in p.targets:
            r = results[pair]
            fit: dict = {
                "f_c_hz": r["f_c_hz"], "f_c_stderr_hz": r["f_c_stderr_hz"],
                "fwhm_hz": r["fwhm_hz"], "dip_depth": r["dip_depth"], "snr": r["snr"],
                "alpha_hz": r["alpha_hz"], "alpha_stderr_hz": r["alpha_stderr_hz"],
                "n_lines": int(r["n_lines"]), "n_ladder_lines": int(r["n_ladder_lines"]),
                "pi_contrast": r["pi_contrast"],
                "lo_hz": self.lo_hz(), "old_coupler_idle_flux": idle[pair],
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
        """One pair with a coupler and two drivable, readable members (the gate of the
        coupler tool experiments)."""
        return one_coupled_pair_problems(roster, targets)

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
