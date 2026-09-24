"""The drive/readout anchor arithmetic (``scqo.experiments._overlap``).

THE one place both driver probes derive their timing from, so a wrong number
here is wrong on QM *and* Qblox in the same way — which is the point, but only
if the arithmetic and the refusals are pinned. Everything is in ns.

The rule under test: the drive ENDS at the readout tone's start (sequential) or
its end (overlap), and begins ``drive_len_ns`` earlier. Nothing bounds the drive
against the tone, so what has to be pinned is the pair of LEADS — the offset the
element that starts SECOND spends — because each backend hands one of them to a
``wait()`` or a ``rel_time`` that may not go negative.
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
    """A ``qubit_spectroscopy`` in overlap mode, bound to the demo device with
    the two readout duration knobs seeded on the 4 ns grid."""
    ensure_demo_experiments()
    cls = get("qubit_spectroscopy")
    roster, design, vendor = demo_device()
    backend = SimulatedBackend(vendor)
    sess = Session(backend, roster, design=design)
    ro = sess.device.channel("q0", "readout")
    ro.readout_duration_s = 2_000e-9
    ro.readout_integration_s = 1_600e-9

    def build(**params):
        params.setdefault("readout_overlap", True)
        exp = cls(backend, cls.Parameters(targets=["q0"], **params))
        exp.device = sess.device  # what Session.run does before probe()
        return exp

    return build


def test_the_tone_is_the_knob_and_the_drive_is_the_parameter(experiment):
    """Nothing derives the drive length from the tone any more: the tone is the
    readout knob (plus the ADC lead) and the drive is whatever was asked for."""
    w = overlap_windows(experiment(drive_len_ns=800.0), "q0")
    assert w.acq_start_ns == 0.0
    assert w.tone_len_ns == pytest.approx(2_000.0)
    assert w.drive_len_ns == pytest.approx(800.0)
    assert w.integration_ns == pytest.approx(1_600.0)


def test_acq_start_lengthens_the_tone_and_leaves_the_drive_alone(experiment):
    """The readout PULSE grows by acq_start_ns so the standing integration
    window still fits inside it. The drive is not part of that arithmetic."""
    w = overlap_windows(experiment(acq_start_ns=600.0, drive_len_ns=800.0), "q0")
    assert w.tone_len_ns == pytest.approx(2_600.0)  # 600 + the 2000 ns knob
    assert w.drive_len_ns == pytest.approx(800.0)  # untouched
    # the whole point: the ADC opens after the lead, and still closes inside
    assert w.acq_start_ns > 0
    assert w.acq_start_ns + w.integration_ns <= w.tone_len_ns


def test_a_short_drive_makes_the_drive_start_second(experiment):
    """Drive shorter than the tone: both END together, so the drive starts
    ``tone - drive`` late and the readout starts first (lead 0)."""
    w = overlap_windows(experiment(drive_len_ns=800.0), "q0")
    assert w.readout_lead_ns == pytest.approx(1_200.0)  # 2000 - 800
    assert w.drive_lead_ns == 0.0


def test_a_long_drive_makes_the_readout_start_second(experiment):
    """A drive LONGER than the tone is legal and is the normal case (a 20 us
    saturation against a 2 us tone): it simply starts before the tone and runs
    through it, so the readout is the element that waits."""
    w = overlap_windows(experiment(drive_len_ns=20_000.0), "q0")
    assert w.drive_lead_ns == pytest.approx(18_000.0)  # 20000 - 2000
    assert w.readout_lead_ns == 0.0


def test_equal_lengths_need_no_lead_at_all(experiment):
    """The boundary between the two branches: both start and end together, so
    neither backend spends an offset. Exactly one lead is non-zero elsewhere."""
    w = overlap_windows(experiment(drive_len_ns=2_000.0), "q0")
    assert w.drive_lead_ns == 0.0
    assert w.readout_lead_ns == 0.0


@pytest.mark.parametrize("field,value", [("acq_start_ns", 6.0), ("drive_len_ns", 998.0)])
def test_off_grid_times_are_refused_by_the_schema(experiment, field, value):
    """Refused, because QM (4 ns clock cycles) and Qblox (1 ns) would round the
    same Parameters differently and realize different timings. The front door is
    the Parameters schema, so an off-grid value never reaches a probe."""
    with pytest.raises(ValueError, match=r"multiple of 4"):
        experiment(**{field: value})


class _OffGridParams:
    """Params built past the schema — what a probe driven directly in a test
    would hand over. ``_on_grid`` is the invariant behind the schema, not a
    duplicate of it, and its message has to name the legal value or the refusal
    is useless at the bench."""

    acq_start_ns = 0.0
    drive_len_ns = 998.0


def test_the_grid_invariant_still_refuses_and_names_the_legal_value(experiment):
    exp = experiment()
    exp.params = _OffGridParams()
    with pytest.raises(ValueError, match=r"off the 4 ns instrument time grid"):
        overlap_windows(exp, "q0")
    with pytest.raises(ValueError, match=r"Use 1000 ns"):
        overlap_windows(exp, "q0")


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
    params = get("qubit_spectroscopy").Parameters.model_fields
    for field, text in OVERLAP_FIELD_DESCS.items():
        assert params[field].description == text


def test_acq_start_without_the_overlap_is_refused_by_name():
    """Sequential mode has no steady state to wait for, so a non-zero lead would
    just push the ADC deeper into the readout pulse. Refused at the schema, with
    the fix named."""
    cls = get("qubit_spectroscopy")
    with pytest.raises(ValueError, match=r"only meaningful with readout_overlap=true"):
        cls.Parameters(targets=["q0"], acq_start_ns=400.0)
    # ... and it is fine the moment the overlap is asked for
    p = cls.Parameters(targets=["q0"], acq_start_ns=400.0, readout_overlap=True)
    assert p.acq_start_ns == 400.0
