"""The Stark-tone timing arithmetic (``scqo.experiments._stark_tone``).

THE one place both driver probes of ``qubit_resonator_stark`` take their timing
from, so a wrong number here is wrong on QM *and* Qblox in the same way — which is
the point, but only if the arithmetic and the refusals are pinned. Everything is in
ns.

The rule under test: the Stark tone starts one depletion wait (the ring-up) before
the saturation drive and ends with it, and the readout starts one more depletion
wait after that. The wait is ONE number per run — the longest target's, rounded UP
onto the grid both backends can play — and a wait nobody governed is refused.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from scqo import Session
from scqo.cli._backends import ensure_demo_experiments
from scqo.experiments import get
from scqo.experiments._stark_tone import (
    MIN_WAIT_NS,
    playable_wait_ns,
    stark_windows,
)
from scqo.testing import SimulatedBackend, demo_device


@pytest.fixture
def experiment():
    """A ``qubit_resonator_stark`` bound to the two-qubit demo device, with both
    readout channels' depletion knob settable per test."""
    ensure_demo_experiments()
    cls = get("qubit_resonator_stark")
    roster, design, vendor = demo_device()
    sess = Session(SimulatedBackend(vendor), roster, design=design)

    def build(depletion_s=None, targets=("q0",), **params):
        for target, value in (depletion_s or {}).items():
            sess.device.channel(target, "readout").readout_depletion_s = value
        exp = cls(sess.backend, cls.Parameters(targets=list(targets), **params))
        exp.device = sess.device  # what Session.run does before define_sweep()
        return exp

    return build


def test_ring_up_and_depletion_are_the_one_governed_wait(experiment):
    w = stark_windows(experiment({"q0": 1e-6}, drive_len_ns=2000.0))
    assert w.ring_up_ns == w.depletion_ns == pytest.approx(1000.0)
    assert w.drive_len_ns == pytest.approx(2000.0)
    # the drive ends WITH the tone, so the tone is the ring-up plus the drive
    assert w.tone_len_ns == pytest.approx(w.ring_up_ns + w.drive_len_ns)


def test_the_per_run_override_wins_over_the_knob(experiment):
    w = stark_windows(experiment({"q0": 1e-6}, readout_depletion_ns=800.0))
    assert w.ring_up_ns == w.depletion_ns == pytest.approx(800.0)


def test_one_multiplexed_run_waits_for_the_slowest_resonator(experiment):
    exp = experiment({"q0": 1e-6, "q1": 2.5e-6}, targets=("q0", "q1"))
    assert stark_windows(exp).depletion_ns == pytest.approx(2500.0)


@pytest.mark.parametrize("requested,played", [
    (1000.0, 1000.0),    # on the grid: untouched
    (1001.0, 1004.0),    # rounded UP — a shorter wait would leave photons behind
    (1592.0 + 1e-7, 1592.0),  # float noise on an on-grid value is not a step
    (5.0, MIN_WAIT_NS),  # QM's wait() floor is 4 clock cycles
    (0.0, 0.0),          # "no wait" is a legitimate governed value
])
def test_the_wait_lands_on_a_grid_both_backends_can_play(requested, played):
    assert playable_wait_ns(requested) == pytest.approx(played)


def test_a_zero_wait_starts_the_drive_with_the_tone(experiment):
    w = stark_windows(experiment({"q0": 1e-6}, readout_depletion_ns=0.0,
                                 drive_len_ns=400.0))
    assert w.ring_up_ns == w.depletion_ns == 0.0
    assert w.tone_len_ns == pytest.approx(400.0)


class _Readouts:
    """Readout views holding a given depletion knob per target. Both spellings a
    view can hand back for "never calibrated" are covered — None (no stored value)
    and NaN (the Qblox vendor default); the store itself refuses to PERSIST
    either, so this branch is only reachable from a read, which is why it is
    stubbed (the shape of test_overlap_windows' _UncalibratedReadout)."""

    def __init__(self, knobs):
        self._knobs = knobs

    def channel(self, target, kind):
        assert kind == "readout"
        return SimpleNamespace(readout_depletion_s=self._knobs[target])


@pytest.mark.parametrize("blank", [None, math.nan], ids=["none", "nan"])
def test_an_ungoverned_depletion_is_refused_by_name(experiment, blank):
    """Refusing names every ungoverned target and the fix; silently defaulting
    would read out with the Stark photons still in the resonator."""
    exp = experiment(targets=("q0", "q1"))
    exp.device = _Readouts({"q0": 1e-6, "q1": blank})
    with pytest.raises(ValueError, match=r"no governed depletion wait for q1\.") as err:
        stark_windows(exp)
    assert "readout_depletion_ns=" in str(err.value)


def test_a_knob_the_vendor_never_seeded_is_refused_too(experiment):
    """The recording view raises KeyError for a field the vendor never carried —
    the same 'nobody governed it', so the same refusal, not a crash."""
    exp = experiment()  # the demo device ships no readout_depletion_s at all
    with pytest.raises(ValueError, match=r"no governed depletion wait for q0"):
        stark_windows(exp)


def test_define_sweep_refuses_before_any_acquisition(experiment):
    """define_sweep is where the probes' timing is resolved, so the refusal lands
    there — before the drive-power boundary and before a probe is built."""
    exp = experiment()
    with pytest.raises(ValueError, match=r"no governed depletion wait"):
        exp.define_sweep()
    ok = experiment({"q0": 1e-6})
    axes = ok.define_sweep()
    assert list(axes) == ["amp_prefactor", "detuning_hz"]  # amplitude outer
    assert ok.resolved_windows().depletion_ns == pytest.approx(1000.0)


def test_a_probe_cannot_read_timing_that_was_never_resolved(experiment):
    with pytest.raises(RuntimeError, match=r"define_sweep\(\)"):
        experiment({"q0": 1e-6}).resolved_windows()
