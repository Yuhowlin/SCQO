"""Swept time axes are whole nanoseconds — the neutral instrument-grid promise.

chipA, 2026-07-26: a Qblox `qubit_relaxation` died at compile time on a wait of
2004649.68 ns. `np.linspace(16, 100000, 51)` steps by 1999.68 ns, so 48 of its 51
points were fractional; `qblox_scheduler` refuses anything off its 1 ns grid. QM
had been hiding the defect for every experiment by re-rounding each axis to clock
cycles.

The axis must be uniform AND integral: the Qblox probes rebuild it from its
endpoints (`linspace(first, last, size)`), so a merely per-point-rounded axis is
no longer evenly spaced and gets re-expanded straight back off the grid.
"""

from __future__ import annotations

import numpy as np
import pytest

from scqo.cli._backends import ensure_demo_experiments
from scqo.experiments import get
from scqo.experiments._time_grid import log_time_axis_ns, time_axis_ns
from scqo.testing import SimulatedBackend, demo_device

#: qblox_scheduler's GRID_TIME_TOLERANCE_TIME. Compared ABSOLUTELY — np.allclose's
#: default rtol=1e-5 would call 40012.8 ns "equal to" 40013 and pass everything.
TOL_NS = 1.1e-3


def off_grid_ns(values, grid_ns: int = 1) -> float:
    values = np.asarray(values, dtype=float) / grid_ns
    return float(np.max(np.abs(values - np.round(values)))) * grid_ns


# --------------------------------------------------------------- the helper

def test_axis_is_uniform_and_integral():
    axis = time_axis_ns(16, 100000, 51)  # the chipA window
    assert off_grid_ns(axis) <= TOL_NS
    step = np.diff(axis)
    assert np.all(step == step[0]), "non-uniform: Qblox re-expands from endpoints"
    assert axis[0] == 16 and axis[-1] <= 100000


def test_window_is_a_bound_not_a_target():
    """Start rounds UP, step rounds DOWN, so no point ever leaves the window —
    waiting longer than the caller's maximum is worse than losing < 1 step."""
    axis = time_axis_ns(16.4, 1000.9, 11)
    assert axis[0] >= 16.4 and axis[-1] <= 1000.9
    assert off_grid_ns(axis) <= TOL_NS


def test_echo_grid_keeps_both_arms_whole():
    """grid_ns=2: the echo probes schedule two `tau / 2` arms, so an odd total
    would hand the instrument half a nanosecond."""
    axis = time_axis_ns(33, 100001, 21, grid_ns=2)
    assert off_grid_ns(axis, grid_ns=2) <= TOL_NS
    assert not np.any(axis.astype(int) % 2)
    assert off_grid_ns(axis / 2) <= TOL_NS  # each arm is whole ns too


def test_over_dense_window_is_refused_with_a_fix():
    """Already degenerate today (QM collapses it into duplicate clock counts and
    the fit sees repeated x-values) — say so instead of returning a broken axis."""
    with pytest.raises(ValueError, match="cannot hold 51 points"):
        time_axis_ns(16, 40, 51)
    with pytest.raises(ValueError, match="reduce num_points"):
        time_axis_ns(0, 10, 51)


def test_single_point_axis():
    assert time_axis_ns(17.2, 500, 1).tolist() == [18]


# ------------------------------------------------------- the six experiments

#: (experiment, params) — windows whose NAIVE linspace step was fractional, so
#: each case genuinely exercises the fix (asserted below).
CASES = [
    ("qubit_relaxation", {"min_wait_ns": 16.0, "max_wait_ns": 100000.0, "num_points": 51}),
    ("qubit_relaxation", {}),
    ("qubit_echo", {}),
    ("qubit_ramsey", {}),
    ("qubit_relaxation_flux_pulse", {}),
    ("qubit_echo_flux_pulse", {}),
    ("pair_zz_coupler", {}),
    # explicit window: the chevron's own defaults (1, 100, 100) step by exactly
    # 1.0 ns under the naive linspace, so they would pass with or without the
    # fix — widen to 200 ns to keep this case honest.
    ("pair_swap_chevron", {"min_swap_time_ns": 1.0, "max_swap_time_ns": 200.0,
                           "num_time_points": 100}),
]
#: pair_swap_flux_map is deliberately ABSENT: its duration is a fixed Parameter,
#: not a swept axis, so it declares no *_ns axis for these rows to check.


def _sweep(name, params):
    ensure_demo_experiments()
    _roster, _design, vendor = demo_device(tunable=True)
    cls = get(name)
    target = "q0_q1" if cls.target_kinds == ("qubit_pair",) else "q0"
    exp = cls(SimulatedBackend(vendor), cls.Parameters(targets=[target], **params))
    return exp.define_sweep()


@pytest.mark.parametrize("name,params", CASES, ids=[f"{n}{i}" for i, (n, _) in enumerate(CASES)])
def test_every_time_axis_is_whole_nanoseconds(name, params):
    axes = {k: v for k, v in _sweep(name, params).items() if k.endswith("_ns")}
    assert axes, f"{name} declares no *_ns axis"
    for axis, values in axes.items():
        assert off_grid_ns(values) <= TOL_NS, f"{name}.{axis} is fractional"
        step = np.diff(values)
        assert float(np.max(np.abs(step - step[0]))) <= TOL_NS, f"{name}.{axis} is not uniform"
        if "echo" in name or name == "pair_zz_coupler":
            assert not np.any(np.round(values).astype(int) % 2), (
                f"{name}.{axis} must be even — the echo splits it into two arms")


@pytest.mark.parametrize("name,params", CASES, ids=[f"{n}{i}" for i, (n, _) in enumerate(CASES)])
def test_cases_would_have_failed_before_the_fix(name, params):
    """Self-check: rebuild each axis the pre-fix way and assert it WAS off-grid.
    Without this a case can silently stop exercising the fix — trimming a sweep
    to 7 points makes linspace(16, 100000, 7) land on an integer 16664 ns step."""
    cls = get(name)
    target = "q0_q1" if cls.target_kinds == ("qubit_pair",) else "q0"
    # rebuild from the raw Parameters window — the pre-fix code handed exactly
    # these three numbers straight to np.linspace
    lo, hi, n = _raw_window(name, cls.Parameters(targets=[target], **params))
    assert off_grid_ns(np.linspace(lo, hi, n)) > TOL_NS, (
        f"{name}: linspace({lo}, {hi}, {n}) is already integral — this case "
        f"would pass with or without the fix")


def _raw_window(name, p):
    """The (min, max, n) each experiment used to hand straight to np.linspace."""
    if name == "qubit_ramsey":
        return p.min_idle_time_ns, p.max_idle_time_ns, p.num_points
    if name == "pair_zz_coupler":
        return 16, p.max_idle_time_ns, p.num_time_points
    if name == "pair_swap_chevron":
        return p.min_swap_time_ns, p.max_swap_time_ns, p.num_time_points
    # the flux-swept coherence pair, in EITHER frame: they name their time axis
    # num_wait_points, unlike the plain coherence experiments' num_points
    if name.endswith(("_flux", "_flux_pulse")):
        return p.min_wait_ns, p.max_wait_ns, p.num_wait_points
    return p.min_wait_ns, p.max_wait_ns, p.num_points


# --- log_time_axis_ns -------------------------------------------------------
# The log axis is for estimators that do NOT differentiate along it (the phasor
# Ramsey reads a decay envelope; the Ramsey cryoscope's Savitzky-Golay step is
# the counter-example that still needs the uniform axis above).


def test_log_axis_is_whole_nanoseconds_on_its_grid():
    axis = log_time_axis_ns(16, 200_000, 60)
    assert off_grid_ns(axis, 4) < TOL_NS
    assert np.all(axis == np.round(axis))


def test_log_axis_is_strictly_increasing_after_dedup():
    axis = log_time_axis_ns(16, 4000, 101)
    assert np.all(np.diff(axis) > 0)


def test_log_axis_shrinks_and_callers_must_read_the_realized_size():
    """Snapping collides neighbouring log points at the dense end, so the
    realized length is <= num_points. A caller that trusted num_points would
    hand the fit repeated x-values."""
    axis = log_time_axis_ns(16, 4000, 101)
    assert axis.size < 101
    assert axis.size == np.unique(axis).size


def test_log_axis_honours_the_instrument_floor():
    axis = log_time_axis_ns(1, 10_000, 40)
    assert axis[0] >= 16


def test_log_axis_spans_decades():
    axis = log_time_axis_ns(16, 200_000, 60)
    assert axis[0] == 16
    assert axis[-1] == pytest.approx(200_000, rel=1e-3)
    # log-spaced, not linear: the last step dwarfs the first
    assert np.diff(axis)[-1] > 100 * np.diff(axis)[0]


def test_log_axis_refuses_a_window_under_the_floor():
    with pytest.raises(ValueError, match="instrument floor"):
        log_time_axis_ns(1, 8, 10)
