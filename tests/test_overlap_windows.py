"""The concurrent drive+readout window arithmetic (``scqo.experiments._overlap``).

THE one place both driver probes derive their timing from, so a wrong number
here is wrong on QM *and* Qblox in the same way — which is the point, but only
if the arithmetic and the refusals are pinned. Everything is in ns from the
shared tone onset; the two tones are co-started by construction, so there is no
offset to test, only lengths and the ADC onset.
"""

from __future__ import annotations

import math

import pytest

from scqo import Session
from scqo.cli._backends import ensure_demo_experiments
from scqo.experiments import get
from scqo.experiments._overlap import GRID_NS, OVERLAP_FIELD_DESCS, overlap_windows
from scqo.testing import SimulatedBackend, demo_device


@pytest.fixture
def experiment():
    """A ``qubit_spectroscopy_overlap`` bound to the demo device, with the two
    readout duration knobs seeded on the 4 ns grid."""
    ensure_demo_experiments()
    cls = get("qubit_spectroscopy_overlap")
    roster, design, vendor = demo_device()
    backend = SimulatedBackend(vendor)
    sess = Session(backend, roster, design=design)
    ro = sess.device.channel("q0", "readout")
    ro.readout_duration_s = 2_000e-9
    ro.readout_integration_s = 1_600e-9

    def build(**params):
        exp = cls(backend, cls.Parameters(targets=["q0"], **params))
        exp.device = sess.device  # what Session.run does before probe()
        return exp

    return build


def test_default_is_todays_timing_with_a_full_overlap(experiment):
    """acq_start_ns=0 + drive_len_ns=None: the ADC opens with the readout pulse
    (what both probes do today) and the drive spans the whole tone."""
    w = overlap_windows(experiment(), "q0")
    assert w.acq_start_ns == 0.0
    assert w.tone_len_ns == pytest.approx(2_000.0)
    assert w.drive_len_ns == pytest.approx(2_000.0)  # the whole tone
    assert w.integration_ns == pytest.approx(1_600.0)


def test_acq_start_lengthens_the_tone_and_the_default_drive(experiment):
    """The readout PULSE grows by acq_start_ns so the standing integration
    window still fits inside it, and the full-overlap drive grows with it."""
    w = overlap_windows(experiment(acq_start_ns=600.0), "q0")
    assert w.tone_len_ns == pytest.approx(2_600.0)  # 600 + the 2000 ns knob
    assert w.drive_len_ns == pytest.approx(2_600.0)
    # the whole point: the ADC opens after the tones, and still closes inside
    assert w.acq_start_ns > 0
    assert w.acq_start_ns + w.integration_ns <= w.tone_len_ns


def test_explicit_drive_len_bounds_the_concurrent_window(experiment):
    w = overlap_windows(experiment(acq_start_ns=400.0, drive_len_ns=800.0), "q0")
    assert w.drive_len_ns == pytest.approx(800.0)
    assert w.tone_len_ns == pytest.approx(2_400.0)  # unchanged by the drive


@pytest.mark.parametrize("field,value", [("acq_start_ns", 6.0), ("drive_len_ns", 998.0)])
def test_off_grid_times_are_refused_not_snapped(experiment, field, value):
    """Refused, because QM (4 ns clock cycles) and Qblox (1 ns) would round the
    same Parameters differently and realize different timings. The message has
    to name the legal value or the refusal is useless at the bench."""
    with pytest.raises(ValueError, match=r"off the 4 ns instrument time grid"):
        overlap_windows(experiment(**{field: value}), "q0")
    with pytest.raises(ValueError, match=r"Use (8|1000) ns"):
        overlap_windows(experiment(**{field: value}), "q0")


def test_a_drive_that_outlasts_the_tone_is_refused(experiment):
    """The drive has to end inside the tone it overlaps — a longer one would be
    driving into a dark resonator, which is a different experiment."""
    with pytest.raises(ValueError, match=r"outlasts the 2000 ns readout tone"):
        overlap_windows(experiment(drive_len_ns=2_400.0), "q0")
    # ... and it becomes legal once acq_start_ns has stretched the tone past it
    w = overlap_windows(experiment(acq_start_ns=400.0, drive_len_ns=2_400.0), "q0")
    assert w.drive_len_ns == pytest.approx(2_400.0)


class _UncalibratedReadout:
    """A readout view that has never been calibrated. Both spellings a view can
    hand back for that are covered — None (no stored value) and NaN (a vendor
    default that means 'unknown'); the store itself refuses to PERSIST NaN, so
    this branch is only reachable from a read, which is why it is stubbed."""

    def __init__(self, field, blank):
        self.readout_duration_s = 2_000e-9
        self.readout_integration_s = 1_600e-9
        setattr(self, field, blank)

    def channel(self, target, kind):
        return self


@pytest.mark.parametrize("field", ["readout_duration_s", "readout_integration_s"])
@pytest.mark.parametrize("blank", [None, math.nan], ids=["none", "nan"])
def test_an_uncalibrated_readout_knob_is_refused_by_name(experiment, field, blank):
    """Refusing names the field and the fix; silently defaulting would put an
    unknown window on air, and the fit would look perfectly healthy."""
    exp = experiment()
    exp.device = _UncalibratedReadout(field, blank)
    with pytest.raises(ValueError, match=rf"q0: {field} has never been set"):
        overlap_windows(exp, "q0")


def test_the_grid_and_the_field_texts_are_the_shared_ones():
    """Both driver probes and the experiment's catalog descriptions read these,
    so they live here once — the shape of ``_depletion.READOUT_DEPLETION_NS_DESC``."""
    assert GRID_NS == 4
    assert set(OVERLAP_FIELD_DESCS) == {"acq_start_ns", "drive_len_ns"}
    params = get("qubit_spectroscopy_overlap").Parameters.model_fields
    for field, text in OVERLAP_FIELD_DESCS.items():
        assert params[field].description == text
