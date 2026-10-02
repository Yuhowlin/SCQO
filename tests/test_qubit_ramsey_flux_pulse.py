"""qubit_ramsey_flux_pulse: the Ramsey-vs-flux-pulse park (BACKLOG F23).

Offline, on the tunable demo device. Pins the writeback (idle_flux re-referenced to
ABSOLUTE, drive_freq_hz with its f_01_hz twin, the apex facts only when bracketed),
the record-only cases, the pre-probe refusals, and where the virtual detuning's
SIGN comes from (the arch facts when present, the apex default otherwise).
"""

from __future__ import annotations

import warnings

import pytest

from scqo import experiments as registry
from scqo.session import Session
from scqo.testing import (
    InMemoryDevice,
    SimulatedBackend,
    demo_components,
    demo_design,
    demo_vendor_state,
)

NAME = "qubit_ramsey_flux_pulse"
#: the re-park lands on q0's flux LINE, the apex offset on its flux channel
APEX_WRITES = {("z_q0", "idle_flux"), ("xy_q0.q0", "drive_freq_hz"), ("q0", "f_01_hz"),
               ("z_q0.q0", "flux_offset"), ("q0", "f_q_max_hz")}


@pytest.fixture()
def session(tmp_path):
    roster = demo_components(tunable=True)
    design = demo_design(roster)
    vendor = InMemoryDevice(roster, demo_vendor_state(roster, design))
    s = Session(SimulatedBackend(vendor), roster, design=design,
                scqo_dir=tmp_path / "scqo", data_root=tmp_path / "data",
                device_name="chipT", setup_name="sim", cooldown_id="cd1")
    s.set_values({"z_q0.idle_flux": 0.05})
    return s


def _run(session, **params):
    params.setdefault("targets", ["q0"])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return session.run(NAME, params)


def _writes(out):
    return {(x["entity"], x["field"]) for x in out["suggestions"]}


def test_apex_run_reparks_in_the_absolute_frame(session):
    out = _run(session)
    assert out["error"] is None
    fit = out["fit"]["q0"]
    assert fit["question"] == "apex"
    assert _writes(out) == APEX_WRITES
    assert fit["old_idle_flux"] == pytest.approx(0.05)
    # the swept frame is idle-relative; everything written is absolute
    assert fit["idle_flux"] == pytest.approx(0.05 + fit["flux_offset_from_idle"])
    assert fit["flux_offset"] == pytest.approx(fit["idle_flux"])
    assert fit["drive_freq_hz"] == pytest.approx(fit["f_01_hz"])
    # no arch facts on the demo device -> the apex default sign, said so
    assert fit["ramp_sign_from"] == "default"
    assert fit["ramp_detuning_hz"] == pytest.approx(-4.0e6)


def test_park_run_finds_the_root_and_retunes_the_drive(session):
    drive = session.device_state()["xy_q0.q0"]["drive_freq_hz"]
    out = _run(session, park_frequency_hz=drive - 0.3e6, flux_side="upper")
    assert out["error"] is None, out["error"]
    fit = out["fit"]["q0"]
    assert fit["question"] == "park" and fit["park_out_of_window"] == 0
    assert fit["idle_flux"] == pytest.approx(0.05 + fit["park_excursion_v"])
    assert fit["f_01_hz"] == pytest.approx(drive - 0.3e6)
    assert {("z_q0", "idle_flux"), ("xy_q0.q0", "drive_freq_hz"),
            ("q0", "f_01_hz")} <= _writes(out)


def test_several_targets_are_record_only(session):
    out = _run(session, targets=["q0", "q1"])
    assert out["error"] is None
    assert out["suggestions"] == []
    assert all(f["multi_target_context"] == 1 for f in out["fit"].values())


@pytest.mark.parametrize("extra,match", [
    ({"targets": ["q0", "q1"], "park_frequency_hz": 3.7e9}, "exactly ONE target"),
    ({"flux_component": "q1", "park_frequency_hz": 3.7e9}, "record-only"),
    ({"park_frequency_hz": 9.9e9}, "unreachable"),
])
def test_park_requests_that_cannot_park_are_refused(session, extra, match):
    out = _run(session, **extra)
    assert out["error"] is not None and match in out["error"]


def _with_arch(session, apex_above_drive_hz=0.0):
    """Arch facts with the apex AT the standing idle, apex_above_drive_hz over the drive."""
    session.set_values({"z_q0.q0.flux_offset": 0.05,
                        "z_q0.q0.flux_per_phi0": 0.9,
                        "q0.f_q_max_hz": session.device_state()["xy_q0.q0"]["drive_freq_hz"]
                        + apex_above_drive_hz})


def test_arch_facts_orient_the_detuning(session):
    _with_arch(session)
    fit = _run(session)["fit"]["q0"]
    assert fit["ramp_sign_from"] == "facts"
    assert fit["ramp_detuning_hz"] == pytest.approx(-4.0e6)  # the qubit only drops


def test_a_folding_window_is_refused_before_the_probe(session):
    # apex 1 MHz above the drive: the +-12 mV window swings the qubit to BOTH sides of
    # the drive (+1 MHz at the apex, ~-0.75 MHz at the edges), so 0.3 MHz folds either way
    _with_arch(session, apex_above_drive_hz=1e6)
    out = _run(session, frequency_detuning_hz=0.3e6)
    assert out["error"] is not None and "folding_risk" in out["error"]


def test_an_undersampled_window_is_refused(session):
    _with_arch(session)
    out = _run(session, frequency_detuning_hz=20e6, num_idle_points=11)
    assert out["error"] is not None and "undersampled" in out["error"]


@pytest.mark.parametrize("bad", [4, 12, 18])
def test_flux_buffer_must_be_playable(bad):
    with pytest.raises(ValueError, match="flux_buffer_ns"):
        registry.get(NAME).Parameters(targets=["q0"], flux_buffer_ns=bad)
    registry.get(NAME).Parameters(targets=["q0"], flux_buffer_ns=0)
    registry.get(NAME).Parameters(targets=["q0"], flux_buffer_ns=24)
