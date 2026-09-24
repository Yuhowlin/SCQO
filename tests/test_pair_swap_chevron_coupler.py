"""The chevron's optional COUPLER pulse — the phase-free QCQ resonance survey.

``pair_swap_chevron`` sweeps one member's flux amplitude against the pulse
DURATION, so it is a single-pulse measurement: nothing accumulates a phase
between swaps the way the repeated-swap maps (``qc_n_swap_amp``,
``pair_swap_angle``) do, and their transfer peak moves with that phase by
roughly ``phi / t_pulse`` — comparable to the resonance linewidth on a 40 ns
swap. Setting ``coupler_flux_v`` makes that phase-free survey usable on a QCQ
pair, where the swap only exists while the coupler pulse plays.

The price is the duration grid: a coupler pulse can only be STRETCHED (4 ns
clock), never baked sub-clock alongside the member's, so the coupled path drops
to a 4 ns axis from 16 ns up. That promise is pinned here rather than in
``test_time_grid.py``'s ``COARSE_GRID`` table, whose fixture builds every axis
from an EMPTY parameter set and so cannot reach a path that a parameter selects.
"""

from __future__ import annotations

import numpy as np
import pytest

from scqo import Session
from scqo.experiments import get
from scqo.roster import parse_components
from scqo.testing import (
    InMemoryDevice,
    SimulatedBackend,
    demo_components,
    demo_design,
    demo_vendor_state,
)

#: qblox_scheduler's grid tolerance, compared ABSOLUTELY (test_time_grid.py's).
TOL_NS = 1.1e-3

WINDOW = {"min_swap_time_ns": 1.0, "max_swap_time_ns": 200.0, "num_time_points": 20}


#: a flux-tunable pair with NO coupler declared — the directly-coupled chip this
#: experiment was written for. demo_components has no such variant: its `tunable`
#: flag mints the qubit z lines and the coupler together, so a pair without a
#: coupler there has no member flux either and refuses for that reason instead.
_NO_COUPLER = """schema = 3
[modes.q0]
kind = "flux_transmon"
[modes.q1]
kind = "flux_transmon"
[composites.q0_q1]
kind       = "qubit_pair"
high       = "q1"
low        = "q0"
operations = ["iswap"]
[lines.fl]
readout = ["q0", "q1"]
[lines.q0_xyl]
drive = ["q0"]
[lines.q1_xyl]
drive = ["q1"]
[lines.q0_zl]
flux = ["q0"]
[lines.q1_zl]
flux = ["q1"]
"""


def _session(tmp_path, *, coupler: bool = True):
    """A Session, because ``define_sweep``'s roster gate reads
    ``self.device.roster`` — the RecordingDevice surface a Session wires up, not
    the bare vendor double a backend carries on its own."""
    roster = demo_components(tunable=True) if coupler else parse_components(_NO_COUPLER)
    design = demo_design(roster)
    vendor = InMemoryDevice(roster, demo_vendor_state(roster, design))
    return Session(SimulatedBackend(vendor), roster, design=design,
                   scqo_dir=tmp_path / "scqo", device_name="chipT",
                   setup_name="sim", cooldown_id="cd1")


def _sweep(tmp_path, **params):
    cls = get("pair_swap_chevron")
    session = _session(tmp_path)
    exp = cls(session.backend, cls.Parameters(targets=["q0_q1"], **params))
    exp.device = session.device      # what Session.run does before define_sweep
    return exp.define_sweep()


# ------------------------------------------------------------- the time axis

def test_the_coupler_free_axis_keeps_its_one_nanosecond_grid(tmp_path):
    """The default path bakes sub-clock segments, so nothing coarsens: this is
    the axis every chevron has had, and the coupler knob must not touch it."""
    axis = _sweep(tmp_path, **WINDOW)["swap_time_ns"]
    assert axis.min() < 16.0, "the baked path reaches below the 4 ns floor"
    assert np.any(np.round(axis).astype(int) % 4), "would pass on a 4 ns grid too"


def test_engaging_the_coupler_moves_the_axis_onto_the_four_nanosecond_grid(tmp_path):
    axis = _sweep(tmp_path, coupler_flux_v=0.05, **WINDOW)["swap_time_ns"]
    off = np.abs(axis / 4 - np.round(axis / 4)).max() * 4
    assert off <= TOL_NS, f"{axis} is off the 4 ns clock"
    assert axis.min() >= 16.0, "16 ns is the shortest stretched pulse"
    step = np.diff(axis)
    assert float(np.max(np.abs(step - step[0]))) <= TOL_NS, "must stay uniform"


def test_the_amplitude_axis_is_untouched_by_the_coupler(tmp_path):
    """Only the DURATION axis pays for the coupler; the swept member amplitude
    is a QUA amplitude_scale either way."""
    free = _sweep(tmp_path, **WINDOW)["flux_amp_v"]
    coupled = _sweep(tmp_path, coupler_flux_v=0.05, **WINDOW)["flux_amp_v"]
    assert np.array_equal(free, coupled)


# ------------------------------------------------------------- the roster gate

def test_a_coupler_less_pair_still_runs_the_plain_chevron(tmp_path):
    """The gate is conditional: the historical use of this experiment is a
    directly-coupled pair, which HAS no coupler to declare."""
    out = _session(tmp_path, coupler=False).run(
        "pair_swap_chevron", {"targets": ["q0_q1"], **WINDOW})
    assert out["error"] is None, out["error"]


def test_asking_for_a_coupler_the_pair_does_not_have_is_refused(tmp_path):
    out = _session(tmp_path, coupler=False).run(
        "pair_swap_chevron",
        {"targets": ["q0_q1"], "coupler_flux_v": 0.05, **WINDOW})
    assert "declares no coupler role" in out["error"]


# ------------------------------------------------------------- the run record

@pytest.mark.parametrize("coupler_flux_v", [None, 0.05])
def test_the_coupler_setting_lands_in_the_fit(tmp_path, coupler_flux_v):
    """A campaign reads its theta(coupler) curve off the run records, so the
    setting the map was taken AT has to be in result.fit and not only in
    parameters.json."""
    out = _session(tmp_path).run(
        "pair_swap_chevron",
        {"targets": ["q0_q1"], "coupler_flux_v": coupler_flux_v, **WINDOW})
    assert out["error"] is None, out["error"]
    fit = out["fit"]["q0_q1"]
    assert fit["coupler_flux_v"] == coupler_flux_v
    # the arch itself still lands, so the record answers both questions at once
    assert np.isfinite(fit["best_flux_amp_v"])
    assert np.isfinite(fit["best_swap_time_ns"])
