"""Where a pair's coupler crosses its neighbours - the coupler's apex, period and f_c_max.

Per shot: reset both members -> the coupler plays a square flux pulse of relative
amplitude ``b`` -> after ``flux_buffer_ns`` each MEASURED member plays its x180 at
its own ``drive_freq_hz`` -> ``flux_buffer_ns`` later the coupler returns to idle ->
both members are read out (2-level) and the joint populations are kept. Loops:
averages (outer) -> ``b`` (inner, start -> end).

Where the coupler crosses a member, the two hybridize and the member is pushed off
its drive frequency; its x180 (about +-12 MHz wide) misses, and that member's
excited population DIPS. The member's frequency itself hardly moves over the sweep -
the coupler line's crosstalk shifts 5Q4C q1 by about -3.7 MHz at b = -0.28 V - so the
dips are clean. The scqat estimator (``pair_coupler_crossing``) pairs the two
crossings nearest idle per member into a symmetry point: with both members measured,
whichever member's crossings are INNER says whether the coupler sits above them (the
center is the coupler APEX) or below (the anti-apex), and the two half-separations
solve the transmon arch for the coupler's period and f_c_max. With one member the
symmetry point is all there is, unless ``coupler_side`` names the side.

FRAME (``_pulse`` in the name, ``CouplerFluxPulseSweepParameters`` in the schema):
the window is an excursion from the COUPLER's ``idle_flux``; every written position
is re-referenced to ABSOLUTE (``old_coupler_idle_flux + fitted``). The coupler line's
pulse/DC ratio has not been measured, so a center found far from idle carries that
ratio's error (5Q4C q1_q2_c: center ~-86 mV from idle, ~3 mV on ``flux_offset``,
~4 % on the period if the ratio is 0.96 like a qubit line).

The coupler's ``idle_flux`` is NEVER proposed - where the coupler should sit is
another experiment's question (J = 0, ZZ = 0, or a chosen coupler frequency). The
arch facts are model-dependent ESTIMATES: two crossings about 300 MHz apart pin
f_c_max to a few hundred MHz; measuring f_c directly is the coupler-spectroscopy
experiments' job (``docs/coupler-readout-plan.md``).

ONE PAIR PER RUN: the coupler line crosstalks 5-7 % onto non-neighbours too, so two
pairs pulsed together would move each other's members.

Known systematic: past a crossing the x180 lands again, but the coupler's fast return
to idle sweeps back through the crossing, and Landau-Zener can leave part of the
excitation in the coupler. The baseline beyond a crossing may therefore sit lower
than inside it; the estimator normalizes by a high percentile and never assumes the
two sides agree.
"""

from __future__ import annotations

from typing import ClassVar, Literal

import numpy as np
from pydantic import Field, model_validator

from ..contract import DatasetContract
from ..experiment import Experiment
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from . import register
from ._capabilities.coupler_flux import (
    COUPLER_FLUX_AXIS,
    END_COUPLER_FLUX_DESC,
    NUM_COUPLER_FLUX_DESC,
    START_COUPLER_FLUX_DESC,
    CouplerFluxPulseSweepParameters,
    coupler_anchor_v,
    coupler_flux_sweep,
    pair_coupler,
)
from ._capabilities.qubit_reset import QubitResetParameters
from ._capabilities.state_readout import joint_state_labels
from ._sim import stable_seed
from ._window import window_bounds
from .pair_swap_chevron import _coupler_problems, _role_names

#: the coupler Ec when neither the parameter nor the coupler's ec_hz fact gives one
DEFAULT_COUPLER_EC_HZ = 0.2e9

#: the simulator's members and x180 (it never reads device state)
SIM_F_HIGH_HZ = 5.1e9
SIM_F_LOW_HZ = 4.8e9
SIM_EC_HZ = 0.2e9
SIM_X180_NS = 40.0

#: the estimator's per-member crossing keys lifted onto result.fit
_CROSSING_KEYS = ("v", "stderr_v", "width_v", "min_s")
_FLAG_KEYS = ("crossings_not_bracketed_high", "crossings_not_bracketed_low",
              "center_mismatch", "side_conflict", "arch_unsolved")


class PairCouplerCrossingPulseParameters(
    TargetSelection, AveragingParameters, QubitResetParameters,
    CouplerFluxPulseSweepParameters,
):
    """Inputs for the coupler-crossing scan. ``targets`` is ONE pair component."""

    # capability defaults widened to reach both crossings of both members on 5Q4C
    # q1_q2_c (canonical text constants); 2 mV steps, see the plan doc's precision
    start_coupler_flux_v: float = Field(-0.45, description=START_COUPLER_FLUX_DESC)
    end_coupler_flux_v: float = Field(0.30, description=END_COUPLER_FLUX_DESC)
    num_coupler_flux_points: int = Field(
        376, gt=4, description=NUM_COUPLER_FLUX_DESC + " The default is a 2 mV step: "
        "the arch solve reads the small difference of two half-separations, so its "
        "error scales with the step.")
    num_averages: int = Field(300, gt=0, description="Number of shots to average per sweep point.")
    measure: Literal["both", "high", "low"] = Field(
        "both",
        description="Which members play the x180 (roster roles). Both are always read "
        "out. 'both' is needed to tell the coupler's apex from its anti-apex and to "
        "solve the arch; one member gives the symmetry point only.")
    coupler_side: Literal["auto", "above", "below"] = Field(
        "auto",
        description="Whether the coupler sits above or below the members at idle. "
        "'auto' decides from which member's crossings are inner (measure='both'); with "
        "one member measured it leaves the center unclassified. A named side that the "
        "data contradicts fails the run (side_conflict).")
    coupler_ec_hz: float | None = Field(
        None, gt=0,
        description="The coupler charging energy the arch solve uses (Hz). None = the "
        "coupler mode's ec_hz fact, else 0.2 GHz; the value used is recorded as "
        "ec_hz_used. The period barely depends on it (<1 % from 0.2 to 0.3 GHz on "
        "5Q4C).")
    flux_buffer_ns: int = Field(
        100, ge=0,
        description="Wait inside the coupler pulse before AND after the x180s, so the "
        "coupler line (no predistortion) settles and the x180 never overlaps its "
        "edges: 0, or a multiple of 4 ns from 16 ns up.")

    @model_validator(mode="after")
    def _buffer_on_grid(self) -> "PairCouplerCrossingPulseParameters":
        b = self.flux_buffer_ns
        if b != 0 and (b < 16 or b % 4):
            raise ValueError(
                f"flux_buffer_ns={b}: use 0, or a multiple of 4 ns from 16 ns up "
                f"(a shorter nonzero wait is not playable on every backend)")
        return self


class PairCouplerCrossingPulseResult(Result):
    """``fit[pair]``: the crossings in the pulse frame
    (``crossing_<role>_<lower|upper>_v`` with ``_stderr_v``, ``_width_v``,
    ``_min_s``), the symmetry point ``center_from_idle_v`` and its ``center_kind``
    (``apex`` / ``anti_apex`` / ``unknown``), ``old_coupler_idle_flux``, and - when
    determined - the ABSOLUTE ``flux_offset`` (the coupler apex), ``flux_per_phi0``,
    ``f_q_max_hz`` and the arch's ``f_c_at_idle_hz``, each with a stderr; plus
    ``ec_hz_used`` and the 0/1 flags ``crossings_not_bracketed_high/low``,
    ``center_mismatch``, ``side_conflict``, ``arch_unsolved``. SUCCESSFUL = every
    measured member bracketed and neither center_mismatch nor side_conflict;
    ``arch_unsolved`` alone only withholds the period and f_c_max."""


def simulated_arch(pair: str, start_v: float, end_v: float) -> dict:
    """The hidden coupler arch the simulator plants for ``pair`` over a window.

    The coupler sits ABOVE both members (the apex is the center), and both members'
    crossing pairs land inside the window with a margin. Module-level so a test can
    compare the fit against the truth."""
    lo, hi = window_bounds(start_v, end_v)
    span = hi - lo
    rng = np.random.default_rng(stable_seed("pair_coupler_crossing_pulse", pair))
    apex = 0.5 * (lo + hi) + float(rng.uniform(-0.05, 0.05)) * span
    reach = min(apex - lo, hi - apex) - 0.08 * span
    d_low = float(rng.uniform(0.6, 0.8)) * reach
    f_max = float(rng.uniform(6.5e9, 8.0e9))
    ratio = ((SIM_F_LOW_HZ + SIM_EC_HZ) / (f_max + SIM_EC_HZ)) ** 2
    period = float(np.pi * d_low / np.arccos(ratio))
    return {"apex_v": float(apex), "f_max_hz": f_max, "period_v": period,
            "ec_hz": SIM_EC_HZ, "g_hz": float(rng.uniform(30e6, 60e6))}


def one_coupled_pair_problems(roster, targets) -> list[str]:
    """The pre-probe gate of the coupler tool experiments: ONE pair (the coupler
    line crosstalks onto non-neighbours), whose coupler has a flux channel and whose
    two members can be driven and read out."""
    problems = []
    if len(targets) > 1:
        problems.append(
            f"{list(targets)}: one pair per run - the coupler line crosstalks "
            f"5-7 % onto non-neighbours, so pulsing two couplers together moves "
            f"each other's members. Run the pairs one at a time.")
    problems += _coupler_problems(roster, targets, "no coupler flux line to pulse")
    for pair in targets:
        roles = getattr(roster.entities.get(pair), "roles", {}) or {}
        for role in ("high", "low"):
            members = roles.get(role, ())
            if not members:
                problems.append(f"{pair}: declares no {role} member")
                continue
            for kind in ("drive", "readout"):
                if (members[0], kind) not in roster.defaults:
                    problems.append(f"{pair}: {role} member {members[0]!r} has no "
                                    f"{kind} channel")
    return problems


def _coupler_frequency(b: np.ndarray, truth: dict) -> np.ndarray:
    """The planted arch ``(F + Ec) sqrt(|cos(pi (b - apex) / P)|) - Ec``."""
    phase = np.pi * (b - truth["apex_v"]) / truth["period_v"]
    return ((truth["f_max_hz"] + truth["ec_hz"]) * np.sqrt(np.abs(np.cos(phase)))
            - truth["ec_hz"])


def _x180_excitation(f_c: np.ndarray, f_q: float, g_hz: float) -> np.ndarray:
    """P(excited) after an x180 at ``f_q`` with the coupler at ``f_c``: the member's
    level is pushed away by the hybridization and its drive element shrinks."""
    delta = f_c - f_q
    root = np.sqrt(delta ** 2 + 4 * g_hz ** 2)
    shift = 0.5 * (delta - np.sign(delta) * root)
    rabi = np.sqrt(0.5 * (1 + np.abs(delta) / root)) / (2 * SIM_X180_NS * 1e-9)
    omega = np.sqrt(rabi ** 2 + shift ** 2)
    return rabi ** 2 / omega ** 2 * np.sin(np.pi * omega * SIM_X180_NS * 1e-9) ** 2


@register
class PairCouplerCrossingPulse(Experiment):
    """Backend-agnostic coupler-crossing scan. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "pair_coupler_crossing_pulse"
    description: ClassVar[str] = (
        "Coupler flux period and apex from where the coupler crosses its neighbours: "
        "the coupler plays a flux pulse (swept relative to its idle_flux) while each "
        "measured pair member plays an x180 at its own drive frequency; where the "
        "coupler crosses a member the x180 misses and that member's population dips. "
        "The crossings nearest idle give the coupler's symmetry point; with both "
        "members measured they tell apex from anti-apex and solve the transmon arch "
        "for the period and f_c_max. Proposes flux_offset and flux_per_phi0 on the "
        "coupler's flux channel and f_q_max_hz on the coupler (model-dependent "
        "estimates); never moves the coupler's idle_flux. One pair per run; needs "
        "state discrimination."
    )
    Parameters: ClassVar[type] = PairCouplerCrossingPulseParameters
    Result: ClassVar[type] = PairCouplerCrossingPulseResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=(COUPLER_FLUX_AXIS,), sweep_units=("V",),
        variables=("joint_population",), readout_dims=("joint_state",),
    )
    target_kinds: ClassVar[tuple[str, ...]] = ("qubit_pair",)
    required_operations: ClassVar[tuple[str, ...]] = ()

    params: PairCouplerCrossingPulseParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        return coupler_flux_sweep(self.params)

    def readout_coords(self) -> dict:
        return {"joint_state": joint_state_labels(2)}

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """The hidden arch (:func:`simulated_arch`) seen through each member's x180.
        Members are independent here (no exchange), so the joint distribution is the
        product of the marginals, sampled with ``num_averages`` shots."""
        b = coords[COUPLER_FLUX_AXIS]
        p = self.params
        joint = np.empty((len(p.targets), 4, b.size))
        for k, pair in enumerate(p.targets):
            truth = simulated_arch(pair, p.start_coupler_flux_v, p.end_coupler_flux_v)
            f_c = _coupler_frequency(b, truth)
            rng = np.random.default_rng(stable_seed("pair_coupler_crossing_pulse", pair, "noise"))
            marg = {}
            for role, f_q in (("high", SIM_F_HIGH_HZ), ("low", SIM_F_LOW_HZ)):
                driven = p.measure in ("both", role)
                pe = 0.95 * _x180_excitation(f_c, f_q, truth["g_hz"]) if driven else 0.0
                marg[role] = np.clip(pe + 0.01, 0.0, 1.0) * np.ones_like(b)
            ph, pl = marg["high"], marg["low"]
            probs = np.stack([(1 - ph) * (1 - pl), (1 - ph) * pl, ph * (1 - pl), ph * pl])
            for j in range(b.size):
                joint[k, :, j] = rng.multinomial(p.num_averages, probs[:, j]) / p.num_averages
        return {"joint_population": (("target", "joint_state", COUPLER_FLUX_AXIS), joint)}

    def _ec_hz(self, coupler: str) -> float:
        if self.params.coupler_ec_hz is not None:
            return float(self.params.coupler_ec_hz)
        return float(self.fact(coupler, "ec_hz", DEFAULT_COUPLER_EC_HZ))

    def estimate(self) -> PairCouplerCrossingPulseResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.pair_coupler_crossing import PairCouplerCrossingEstimator
        from .._scqat import per_qubit_results

        p = self.params
        roster = self.device.roster
        per_target: dict[str, dict] = {}
        context: dict[str, dict] = {}
        names = _role_names(self.device, p.targets)
        for pair in p.targets:
            roles = roster.entities[pair].roles
            coupler = pair_coupler(roster, pair)
            ec = self._ec_hz(coupler)
            drives = {role: float(self.anchor(roles[role][0], "drive_freq_hz"))
                      for role in ("high", "low")}
            context[pair] = {"coupler": coupler, "ec": ec,
                             "old_idle": coupler_anchor_v(self, pair)}
            per_target[pair] = {**names[pair], "ec_hz": ec,
                                "f_high_hz": drives["high"], "f_low_hz": drives["low"]}
        ds = self.dataset.transpose("target", "joint_state", COUPLER_FLUX_AXIS)
        results = per_qubit_results(ds, PairCouplerCrossingEstimator(),
                                    artifact_dir=self.artifact_dir,
                                    per_target_kwargs=per_target, measure=p.measure,
                                    coupler_side=p.coupler_side, bracket_at=0.0)

        result = PairCouplerCrossingPulseResult()
        for pair in p.targets:
            r, ctx = results[pair], context[pair]
            old = ctx["old_idle"]
            fit: dict = {
                "old_coupler_idle_flux": old,
                "ec_hz_used": ctx["ec"],
                "center_kind": r["center_kind"],
                "center_from_idle_v": r["center_from_idle_v"],
                "center_from_idle_stderr_v": r["center_from_idle_stderr_v"],
            }
            for role in ("high", "low"):
                for side in ("lower", "upper"):
                    for key in _CROSSING_KEYS:
                        name = f"crossing_{role}_{side}_{key}"
                        fit[name] = r[name]
            fit.update({flag: int(r[flag]) for flag in _FLAG_KEYS})
            if r["apex_identified"]:
                fit["flux_offset"] = old + r["apex_from_idle_v"]
                fit["flux_offset_stderr"] = r["apex_from_idle_stderr_v"]
            if not r["arch_unsolved"]:
                fit.update(flux_per_phi0=r["period_v"], flux_per_phi0_stderr=r["period_stderr_v"],
                           f_q_max_hz=r["f_c_max_hz"], f_q_max_stderr_hz=r["f_c_max_stderr_hz"],
                           f_c_at_idle_hz=r["f_c_at_idle_hz"],
                           f_c_at_idle_stderr_hz=r["f_c_at_idle_stderr_hz"])
            result.fit[pair] = fit
            result.outcomes[pair] = Outcome.SUCCESSFUL if r["success"] else Outcome.FAILED
        return result

    def update(self) -> None:
        """One SUCCESSFUL pair: the coupler apex (``flux_offset``, absolute) when the
        center is known to be one, and the arch (``flux_per_phi0`` on the coupler's
        flux channel, ``f_q_max_hz`` on the coupler) when it solved. Never the
        coupler's ``idle_flux``."""
        if self.result is None or len(self.result.fit) != 1:
            return
        for pair, fit in self.result.fit.items():
            if self.result.outcomes[pair] is not Outcome.SUCCESSFUL:
                continue
            coupler = pair_coupler(self.device.roster, pair)
            flux = self.device.channel(coupler, "flux")
            if "flux_offset" in fit:
                flux.flux_offset = fit["flux_offset"]
            if "flux_per_phi0" in fit:
                flux.flux_per_phi0 = fit["flux_per_phi0"]
                self.device.component(coupler).f_q_max_hz = fit["f_q_max_hz"]

    @classmethod
    def validate_targets(cls, roster, targets):
        """See :func:`one_coupled_pair_problems`."""
        return one_coupled_pair_problems(roster, targets)

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
