"""Unit tests for CrosstalkCompensatedBenchmark experiment in SCQO."""

import numpy as np
import pytest

from scqo import Session
from scqo import experiments as registry
from scqo.experiments.crosstalk_compensated_benchmark import (
    CrosstalkCompensatedBenchmark,
    CrosstalkCompensatedBenchmarkParameters,
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
    f_d = 5.235e9
    f_p = 5.150e9
    tau = 16.0
    dphi = compute_theoretical_phase_rate(f_d, f_p, tau)
    assert np.isclose(dphi, 2 * np.pi * 85e6 * 16e-9)


def test_parameter_validation():
    # Auto-derives targets from probe_qubit and drive_qubit
    params = CrosstalkCompensatedBenchmarkParameters(probe_qubit="q1", drive_qubit="q2")
    assert params.targets == ["q1", "q2"]
    assert params.probe_qubit == "q1"
    assert params.drive_qubit == "q2"

    # Auto-derives probe_qubit and drive_qubit from targets
    params2 = CrosstalkCompensatedBenchmarkParameters(targets=["q3", "q4"])
    assert params2.probe_qubit == "q3"
    assert params2.drive_qubit == "q4"


def test_get_repetitions():
    p_def = CrosstalkCompensatedBenchmarkParameters(max_repetitions=64)
    reps = p_def.get_repetitions()
    assert 64 in reps
    assert np.all(reps[:-1] <= reps[1:])

    p_log = CrosstalkCompensatedBenchmarkParameters(min_repetitions=2, max_repetitions=100, num_repetitions=8)
    reps_log = p_log.get_repetitions()
    assert len(reps_log) == 8
    assert reps_log[0] == 2
    assert reps_log[-1] == 100
    assert np.all(reps_log % 2 == 0)

    p_with_zero = CrosstalkCompensatedBenchmarkParameters(min_repetitions=0, max_repetitions=64)
    reps_zero = p_with_zero.get_repetitions()
    assert 0 in reps_zero
    assert 64 in reps_zero

    p_alias = CrosstalkCompensatedBenchmarkParameters(**{"max_repeat": 50, "num_repeat": 6})
    reps_alias = p_alias.get_repetitions()
    assert reps_alias[-1] == 50

    p_explicit = CrosstalkCompensatedBenchmarkParameters(repetitions=[0, 4, 8, 16])
    np.testing.assert_array_equal(p_explicit.get_repetitions(), [0, 4, 8, 16])


def test_get_cancel_amps_and_init_phases():
    p_def = CrosstalkCompensatedBenchmarkParameters(
        min_cancel_amp=0.0, max_cancel_amp=0.1, num_cancel_amps=11,
        min_init_phase=-1.0, max_init_phase=1.0, num_init_phases=9,
    )
    np.testing.assert_allclose(p_def.get_cancel_amps(), np.linspace(0.0, 0.1, 11))
    np.testing.assert_allclose(p_def.get_init_phases(), np.linspace(-1.0, 1.0, 9))

    p_explicit = CrosstalkCompensatedBenchmarkParameters(
        cancel_amps=[0.02, 0.04, 0.06],
        init_phases=[0.1, 0.5],
    )
    np.testing.assert_allclose(p_explicit.get_cancel_amps(), [0.02, 0.04, 0.06])
    np.testing.assert_allclose(p_explicit.get_init_phases(), [0.1, 0.5])


def test_get_cal_repetitions():
    # 1. Default (min/max/num)
    p_def = CrosstalkCompensatedBenchmarkParameters(min_cal_repetitions=10, max_cal_repetitions=30, num_cal_repetitions=5)
    assert p_def.get_cal_repetitions() == [10, 15, 20, 25, 30]

    # 2. Slice string: "10:30:5"
    p_slice = CrosstalkCompensatedBenchmarkParameters(cal_repetitions="10:30:5")
    assert p_slice.get_cal_repetitions() == [10, 15, 20, 25, 30]

    # 3. Single int: 20 -> centered points
    p_int = CrosstalkCompensatedBenchmarkParameters(cal_repetitions=20)
    reps_int = p_int.get_cal_repetitions()
    assert 20 in reps_int
    assert len(reps_int) == 5

    # 4. List of ints: [10, 20]
    p_list = CrosstalkCompensatedBenchmarkParameters(cal_repetitions=[10, 20])
    assert p_list.get_cal_repetitions() == [10, 20]



def test_end_to_end_simulated_benchmark(session):
    out = session.run(
        "crosstalk_compensated_benchmark",
        {
            "probe_qubit": "q0",
            "drive_qubit": "q1",
            "mode": "benchmark",
            "repetitions": [0, 4, 8, 16, 32],
            "cancel_amp": 0.035,
            "init_phase": 0.85,
        },
        update="none",
    )
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]
    assert fit["mode"] == "benchmark"
    assert "mean_error_isolated" in fit
    assert "mean_error_simultaneous" in fit
    assert "mean_error_compensated" in fit
    assert fit["mean_error_isolated"] <= fit["mean_error_simultaneous"]


def test_end_to_end_simulated_calibrate(session):
    out = session.run(
        "crosstalk_compensated_benchmark",
        {
            "probe_qubit": "q0",
            "drive_qubit": "q1",
            "mode": "calibrate",
            "cal_repetitions": 20,
            "min_cancel_amp": 0.0,
            "max_cancel_amp": 0.06,
            "num_cancel_amps": 7,
            "min_init_phase": 0.0,
            "max_init_phase": 1.5,
            "num_init_phases": 7,
        },
        update="none",
    )
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]
    assert fit["mode"] == "calibrate"
    assert "optimal_cancel_amp" in fit
    assert "optimal_init_phase" in fit


def test_alternating_parameters_and_aliases():
    # Test default values
    p_def = CrosstalkCompensatedBenchmarkParameters()
    assert p_def.alternate_probe is False
    assert p_def.alternate_target is False

    # Test explicit values
    p_custom = CrosstalkCompensatedBenchmarkParameters(alternate_probe=True, alternate_target=False)
    assert p_custom.alternate_probe is True
    assert p_custom.alternate_target is False

    # Test aliases
    p_alias1 = CrosstalkCompensatedBenchmarkParameters(**{"alter_probe": True, "alter_target": True})
    assert p_alias1.alternate_probe is True
    assert p_alias1.alternate_target is True

    # Test shorthand "alternating"
    p_alias2 = CrosstalkCompensatedBenchmarkParameters(**{"alternating": True})
    assert p_alias2.alternate_probe is True
    assert p_alias2.alternate_target is True


def test_end_to_end_simulated_benchmark_alternating(session):
    out = session.run(
        "crosstalk_compensated_benchmark",
        {
            "probe_qubit": "q0",
            "drive_qubit": "q1",
            "mode": "benchmark",
            "repetitions": [2, 4, 8],
            "cancel_amp": 0.035,
            "init_phase": 0.85,
            "alternate_probe": True,
            "alternate_target": True,
        },
        update="none",
    )
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]
    assert fit["mode"] == "benchmark"
    assert "mean_error_isolated" in fit
    assert "mean_error_simultaneous" in fit
    assert "mean_error_compensated" in fit
    # Isolated baseline error should be suppressed due to alternating positive/negative pulses
    assert fit["mean_error_isolated"] < 0.02
