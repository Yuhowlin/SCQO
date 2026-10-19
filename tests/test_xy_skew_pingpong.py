"""xy_skew_pingpong (physics half): parameters, waveform math, and offline end-to-end runs with figures."""

import numpy as np
import pytest

from scqo import Session
from scqo.experiments.xy_skew_pingpong import (
    SIM_C_AMP,
    SIM_C_AXIS,
    SIM_SKEW_NS,
    XySkewPingpongParameters,
    _sim_pulses,
    axis_factor,
    fractional_shift,
    null_waveforms,
    scan_waveforms,
)
from scqo.testing import InMemoryDevice, SimulatedBackend, demo_components, demo_design, demo_vendor_state


@pytest.fixture()
def session(tmp_path):
    roster = demo_components()
    design = demo_design(roster)
    vendor = InMemoryDevice(roster, demo_vendor_state(roster, design))
    return Session(
        SimulatedBackend(vendor), roster, design=design,
        scqo_dir=tmp_path / "scqo", data_root=tmp_path / "data",
        device_name="chipT", backend_label="simulated", setup_name="sim", cooldown_id="cd1",
    )


def _pulses():
    x180, y180 = _sim_pulses()
    scale = 0.2 / np.max(np.abs(x180))
    return scale * x180, scale * y180


def _true_via_phase():
    return float((np.pi / 2 - SIM_C_AXIS + np.pi) % (2 * np.pi) - np.pi)


def test_parameters_parse_and_guard():
    p = XySkewPingpongParameters(targets=["q1", "q3"], tau_sw_ns="-1:1:0.5", repetitions="2,5")
    assert (p.sensor_qubit, p.source_qubit) == ("q1", "q3")
    assert p.tau_sw_ns == [-1.0, -0.5, 0.0, 0.5, 1.0]
    assert p.repetitions == [2, 5]
    assert p.kflips() == [1, -1, 0]
    assert p.flip_tau_index() == 2
    p = XySkewPingpongParameters(targets=["q1", "q3"], include_null=True, include_flip_check=True)
    assert p.cal_conditions() == ["own_x", "own_y", "via_0", "via_90", "null", "l1_flip", "l2_flip"]
    with pytest.raises(ValueError, match="pad_ns"):
        XySkewPingpongParameters(targets=["q1", "q3"], tau_sw_ns=[-3.5, 3.5], pad_ns=4)


def test_fractional_shift_is_exact_for_integers_and_moves_the_centroid():
    t = np.arange(32)
    x = np.where((t >= 8) & (t < 24), 0.5 * (1 - np.cos(2 * np.pi * (t - 8) / 16)), 0.0)
    assert np.allclose(fractional_shift(x, 2), np.roll(x, 2), atol=1e-12)
    for tau in (-1.3, 0.25, 2.7):
        y = fractional_shift(x, tau).real
        assert np.sum(t * y) / np.sum(y) == pytest.approx(np.sum(t * x) / np.sum(x) + tau, abs=1e-4)


def test_y_parts_cancel_at_the_alignment_point():
    x180, y180 = _pulses()
    assert axis_factor(x180, y180) == pytest.approx(1j)
    c_amp, via_phase, k = 0.09, 0.4, 0.1
    leak = c_amp * axis_factor(x180, y180) * np.exp(-1j * via_phase)
    for kflip in (1, -1):
        for sign in (1, -1):
            l1, l2 = scan_waveforms(x180, y180, k=k, c_amp=c_amp, via_phase=via_phase, tau_ns=0.0, pad=4,
                                    kflip=kflip, sign=sign)
            assert len(l1) == len(l2) == 24
            assert np.allclose(l1 + leak * l2, sign * np.pad(x180, 4), atol=1e-9)
    n1, n2 = null_waveforms(x180, y180, k=0.03, c_amp=c_amp, via_phase=via_phase)
    assert np.allclose(n1 + leak * n2, 0, atol=1e-12)


def _figures(out, session_root):
    run_dir = next(session_root.rglob(out["run_id"]))
    return sorted(p.name for p in run_dir.rglob("*.png"))


def test_offline_scan_recovers_the_simulated_skew_and_draws(session, tmp_path):
    out = session.run("xy_skew_pingpong", {
        "targets": ["q0", "q1"], "mode": "scan", "k": 0.1, "repetitions": [2], "tau_sw_ns": "-1.5:1.5:0.25",
        "use_state_discrimination": True,
    }, update="none")
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]
    assert fit["success"] and fit["mode"] == "scan"
    assert fit["tau_sw_star_ns"] == pytest.approx(-SIM_SKEW_NS, abs=0.05)
    assert _figures(out, tmp_path / "data") == ["xy_skew_pingpong.png", "xy_skew_pingpong_detail.png"]


def test_offline_calibration_with_null_and_flip_checks(session, tmp_path):
    out = session.run("xy_skew_pingpong", {
        "targets": ["q0", "q1"], "mode": "calibrate", "repetitions": "0:12:2", "include_null": True,
        "include_flip_check": True, "c_amp": SIM_C_AMP, "via_phase": _true_via_phase(), "k": 0.05,
        "use_state_discrimination": True,
    }, update="none")
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]
    assert fit["success"], fit
    assert fit["c_amp"] == pytest.approx(SIM_C_AMP, rel=0.05)
    assert fit["null_ratio"] < 0.1
    # in the model both lines flip their sign: the alternating trains net ~0
    assert fit["flip_ratio_l1"] < 0.1 and fit["flip_ratio_l2"] < 0.1
    assert _figures(out, tmp_path / "data") == ["xy_skew_pingpong.png", "xy_skew_pingpong_detail.png"]
