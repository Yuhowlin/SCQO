"""A sweep window's start/end is a TRAVERSAL ORDER, and the order is not a result.

Decided 2026-09-26: on the flux, drive-detuning, readout-detuning,
amplitude and coupler-flux windows the probe walks ``start`` -> ``end`` in either
direction, the dataset keeps the order it walked, and no estimator may be able to
tell which way the sweep went. Three properties, checked on EVERY carrier of those
five windows (the carrier list is derived from the Parameters mixins, so a new carrier
is covered the day it lands):

1. ``define_sweep`` emits the reversed window as the reversed axis — never
   re-sorted, and nothing else in the sweep changes;
2. a descending run goes end to end through the simulated backend and the stored
   dataset keeps the descending order (the simulators used to build their hidden
   truth from ``axis[-1] - axis[0]``: one raised on it, three mirrored the dip);
3. the SAME acquired data, reversed along the window and re-fitted as the
   descending run it would have been, gives the same fit. Two simulated runs
   cannot be compared directly — the simulators draw noise by index, so a
   reversed axis is a different noise realization — hence the one dataset,
   reversed.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from scqo import experiments as registry
from scqo.experiment import Experiment
from scqo.experiments._capabilities import (
    AMP_AXIS,
    COUPLER_FLUX_AXIS,
    DETUNING_AXIS,
    FLUX_AXIS,
    AmplitudeSweepParameters,
    CouplerFluxPulseSweepParameters,
    DriveDetuningSweepParameters,
    FluxSweepParameters,
    ReadoutDetuningSweepParameters,
)
from tests.test_model_experiments import _targets_for, session  # noqa: F401 - fixture

#: window mixin -> (start field, end field, the axis it sweeps)
WINDOWS = {
    FluxSweepParameters: ("start_flux_v", "end_flux_v", FLUX_AXIS),
    DriveDetuningSweepParameters: (
        "start_drive_detuning_hz", "end_drive_detuning_hz", DETUNING_AXIS),
    ReadoutDetuningSweepParameters: (
        "start_readout_detuning_hz", "end_readout_detuning_hz", DETUNING_AXIS),
    AmplitudeSweepParameters: ("start_amp_factor", "end_amp_factor", AMP_AXIS),
    CouplerFluxPulseSweepParameters: (
        "start_coupler_flux_v", "end_coupler_flux_v", COUPLER_FLUX_AXIS),
}

#: per-carrier params the window needs to be a window at all: benchmarking
#: defaults to ONE point (the current amplitude), which has no direction.
EXTRA = {"qubit_deterministic_benchmarking": {"num_amp_points": 5}}

#: a fit whose answer already wobbles between two calls on the SAME data -
#: readout_power's Gaussian-mixture discrimination (~1e-5); the order check must
#: not mistake that for the order.
RTOL = {"readout_power": 1e-3}


def _cases():
    """(experiment, start field, end field, axis) for every core carrier."""
    core = sorted(
        (obj for obj in (getattr(registry, n) for n in registry.__all__)
         if isinstance(obj, type) and issubclass(obj, Experiment)),
        key=lambda cls: cls.name)
    return [
        pytest.param(cls.name, *fields, id=f"{cls.name}-{fields[0]}")
        for cls in core
        for mixin, fields in WINDOWS.items()
        if issubclass(cls.Parameters, mixin)
    ]


CASES = _cases()


def test_every_window_capability_has_carriers():
    """Guard against the derivation silently finding nothing."""
    covered = {start for _, start, _, _ in (c.values for c in CASES)}
    assert covered == {fields[0] for fields in WINDOWS.values()}


def _params(name, **overrides):
    cls = registry.get(name)
    return cls.Parameters(targets=_targets_for(name),
                          **{**EXTRA.get(name, {}), **overrides})


def _reversed_params(name, start, end):
    """The carrier's own default window, walked the other way."""
    default = _params(name)
    return _params(name, **{start: getattr(default, end), end: getattr(default, start)})


def _experiment(session, name, params):
    cls = registry.get(name)
    exp = cls(session.backend, params)
    exp.device, exp.design, exp.physical = session.device, session.design, session.physical
    return exp


@pytest.mark.parametrize("name,start,end,axis", CASES)
def test_define_sweep_walks_start_to_end(session, name, start, end, axis):
    up = _experiment(session, name, _params(name)).define_sweep()
    down = _experiment(session, name, _reversed_params(name, start, end)).define_sweep()
    assert list(down) == list(up)  # same axes, same contract order
    assert down[axis] == pytest.approx(np.asarray(up[axis])[::-1])
    assert down[axis][0] > down[axis][-1]
    for other in up:
        if other != axis:
            np.testing.assert_array_equal(down[other], up[other])


@pytest.mark.parametrize("name,start,end,axis", CASES)
def test_a_descending_run_keeps_its_order(session, name, start, end, axis):
    params = _reversed_params(name, start, end).model_dump(exclude_unset=True)
    out = session.run(name, params, update="none")
    assert out.get("error") is None, out.get("error")
    ds = session.datastore.open_dataset(out["run_id"])
    values = np.asarray(ds.coords[axis].values)
    assert values[0] == pytest.approx(getattr(_reversed_params(name, start, end), start))
    assert values[0] > values[-1], "the stored dataset was re-sorted"


def _same(a, b, rtol, path):
    if isinstance(a, dict) and isinstance(b, dict):
        assert a.keys() == b.keys(), f"{path}: keys differ"
        for key in a:
            _same(a[key], b[key], rtol, f"{path}.{key}")
    elif isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        assert len(a) == len(b), f"{path}: lengths differ"
        for i, (x, y) in enumerate(zip(a, b)):
            _same(x, y, rtol, f"{path}[{i}]")
    elif isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) or math.isnan(b):
            assert math.isnan(a) and math.isnan(b), f"{path}: {a} != {b}"
        else:
            assert math.isclose(a, b, rel_tol=rtol, abs_tol=1e-12), f"{path}: {a} != {b}"
    else:
        assert a == b, f"{path}: {a!r} != {b!r}"


@pytest.mark.parametrize("name,start,end,axis", CASES)
def test_the_order_never_changes_the_fit(session, name, start, end, axis):
    out = session.run(name, _params(name).model_dump(exclude_unset=True), update="none")
    assert out.get("error") is None, out.get("error")
    acquired = session.datastore.open_dataset(out["run_id"]).load()

    def refit(params, dataset):
        exp = _experiment(session, name, params)
        exp.sweep_axes = exp.define_sweep()
        exp.dataset = dataset
        return exp.run_estimate(frozen=True)

    up = refit(_params(name), acquired)
    down = refit(_reversed_params(name, start, end),
                 acquired.isel({axis: slice(None, None, -1)}))
    assert up.error is None and down.error is None, (up.error, down.error)
    assert down.outcomes == up.outcomes
    _same(up.fit, down.fit, RTOL.get(name, 1e-9), "fit")
