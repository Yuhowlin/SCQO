"""Unit tests for CrosstalkCompensatedSQRB experiment in SCQO."""

import numpy as np
import pytest

from scqo import Session
from scqo import experiments as registry
from scqo.experiments.crosstalk_compensated_sqrb import (
    CrosstalkCompensatedSQRB,
    CrosstalkCompensatedSQRBParameters,
    compute_theoretical_phase_rate,
)
from scqo.testing import (
    InMemoryDevice,
    SimulatedBackend,
    demo_components,
    demo_design,
    demo_vendor_state,
)


@pytest.fixture()
def session(tmp_path):
    roster = demo_components()
    design = demo_design(roster)
    vendor = InMemoryDevice(roster, demo_vendor_state(roster, design))
    return Session(
        SimulatedBackend(vendor), roster, design=design,
        scqo_dir=tmp_path / "scqo", data_root=tmp_path / "data",
        device_name="chipT", backend_label="simulated",
        setup_name="sim", cooldown_id="cd1"
    )


def test_theoretical_phase_rate():
    # 85 MHz detuning, 20 ns slot
    f_d = 5.235e9
    f_p = 5.150e9
    tau = 20.0
    dphi = compute_theoretical_phase_rate(f_d, f_p, tau)
    # Expected: 2 * pi * 85e6 * 20e-9 = 3.4 * pi rad
    assert np.isclose(dphi, 2 * np.pi * 85e6 * 20e-9)


def test_parameter_validation():
    # Auto-derives targets from probe_qubit and drive_qubit
    params = CrosstalkCompensatedSQRBParameters(probe_qubit="q1", drive_qubit="q2")
    assert params.targets == ["q1", "q2"]
    assert params.probe_qubit == "q1"
    assert params.drive_qubit == "q2"

    # Auto-derives probe_qubit and drive_qubit from targets
    params2 = CrosstalkCompensatedSQRBParameters(targets=["q3", "q4"])
    assert params2.probe_qubit == "q3"
    assert params2.drive_qubit == "q4"


def test_get_depths():
    p_log = CrosstalkCompensatedSQRBParameters(max_circuit_depth=128, log_scale=True)
    np.testing.assert_array_equal(p_log.get_depths(), [1, 2, 4, 8, 16, 32, 64, 128])

    p_lin = CrosstalkCompensatedSQRBParameters(max_circuit_depth=60, delta_clifford=20, log_scale=False)
    np.testing.assert_array_equal(p_lin.get_depths(), [1, 20, 40, 60])

    p_custom = CrosstalkCompensatedSQRBParameters(depths=[1, 5, 10])
    np.testing.assert_array_equal(p_custom.get_depths(), [1, 5, 10])


def test_get_cancel_amps_and_init_phases():
    p_def = CrosstalkCompensatedSQRBParameters(
        min_cancel_amp=0.0, max_cancel_amp=0.1, num_cancel_amps=11,
        min_init_phase=-1.0, max_init_phase=1.0, num_init_phases=9,
    )
    np.testing.assert_allclose(p_def.get_cancel_amps(), np.linspace(0.0, 0.1, 11))
    np.testing.assert_allclose(p_def.get_init_phases(), np.linspace(-1.0, 1.0, 9))

    p_explicit = CrosstalkCompensatedSQRBParameters(
        cancel_amps=[0.02, 0.04, 0.06],
        init_phases=[0.1, 0.5],
    )
    np.testing.assert_allclose(p_explicit.get_cancel_amps(), [0.02, 0.04, 0.06])
    np.testing.assert_allclose(p_explicit.get_init_phases(), [0.1, 0.5])


def test_end_to_end_simulated_benchmark(session):
    out = session.run(
        "crosstalk_compensated_sqrb",
        {
            "probe_qubit": "q0",
            "drive_qubit": "q1",
            "mode": "benchmark",
            "depths": [1, 2, 4, 8, 16],
            "num_random_sequences": 5,
            "cancel_amp": 0.035,
            "init_phase": 0.85,
        },
        update="none",
    )
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]
    assert fit["mode"] == "benchmark"
    assert "r_isolated" in fit
    assert "r_simultaneous" in fit
    assert "r_compensated" in fit
    assert fit["r_isolated"] < fit["r_simultaneous"]


def test_end_to_end_simulated_calibrate(session):
    out = session.run(
        "crosstalk_compensated_sqrb",
        {
            "probe_qubit": "q0",
            "drive_qubit": "q1",
            "mode": "calibrate",
            "cal_depth": 10,
            "num_random_sequences": 5,
            "cancel_amps": [0.01, 0.035, 0.06],
            "init_phases": [-0.5, 0.85, 1.5],
        },
        update="none",
    )
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]
    assert fit["mode"] == "calibrate"
    assert "optimal_cancel_amp" in fit
    assert "optimal_init_phase_rad" in fit


def test_contract_validation_with_population():
    import xarray as xr
    ds_cal = xr.Dataset(
        {"population": (("target", "cancel_amp", "init_phase", "sequence_idx"), np.ones((1, 5, 5, 2)))},
        coords={
            "target": ["q0"],
            "cancel_amp": np.linspace(0, 0.05, 5),
            "init_phase": np.linspace(-np.pi, np.pi, 5),
            "sequence_idx": np.arange(2),
        },
    )
    CrosstalkCompensatedSQRB.Contract.validate(ds_cal)

    ds_bench = xr.Dataset(
        {"population": (("target", "condition", "sequence_idx", "depth"), np.ones((1, 3, 2, 4)))},
        coords={
            "target": ["q0"],
            "condition": ["isolated", "simultaneous", "compensated"],
            "sequence_idx": np.arange(2),
            "depth": [1, 2, 4, 8],
        },
    )
    CrosstalkCompensatedSQRB.Contract.validate(ds_bench)
