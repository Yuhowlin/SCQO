"""Unit tests for CrosstalkCompensatedSQRB experiment in SCQO."""

import numpy as np
import pytest

from scqo import Session
from scqo import experiments as registry
from scqo.experiments.crosstalk_compensated_sqrb import (
    CrosstalkCompensatedSQRB,
    CrosstalkCompensatedSQRBParameters,
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


def test_end_to_end_simulated_benchmark(session):
    out = session.run(
        "crosstalk_compensated_sqrb",
        {
            "probe_qubit": "q0",
            "drive_qubit": "q1",
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


def test_contract_validation_with_population():
    import xarray as xr
    from scqo.contract import ContractError

    # the old calibrate-mode grid (cancel_amp x init_phase) is no longer a valid dataset
    ds_cal = xr.Dataset(
        {"population": (("target", "cancel_amp", "init_phase", "sequence_idx"), np.ones((1, 5, 5, 2)))},
        coords={
            "target": ["q0"],
            "cancel_amp": np.linspace(0, 0.05, 5),
            "init_phase": np.linspace(-np.pi, np.pi, 5),
            "sequence_idx": np.arange(2),
        },
    )
    with pytest.raises(ContractError, match="condition"):
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


def test_end_to_end_simulated_bidirectional(session):
    out = session.run(
        "crosstalk_compensated_sqrb",
        {
            "probe_qubit": "q0",
            "drive_qubit": "q1",
            "bidirectional": True,
            "depths": [1, 2, 4, 8, 16, 32],
            "num_random_sequences": 5,
            "cancel_amp": 0.035,
            "init_phase": 0.85,
            "drive_cancel_amp": 0.028,
            "drive_init_phase": -0.6,
        },
        update="none",
    )
    assert out.get("error") is None, out.get("error")
    assert set(out["fit"]) == {"q0", "q1"}
    for victim, aggressor in (("q0", "q1"), ("q1", "q0")):
        fit = out["fit"][victim]
        assert fit["mode"] == "benchmark"
        assert fit["victim_qubit"] == victim
        assert fit["aggressor_qubit"] == aggressor
        assert fit["r_isolated"] < fit["r_simultaneous"]
        assert fit["r_compensated"] < fit["r_simultaneous"]


def test_bidirectional_reverse_compensation_is_independent(session):
    # Forward direction tuned, reverse far off its optimum: only q0 recovers. (Not amp=0: the
    # simulator's compensation-quality window is wide enough that 0 still recovers ~half.)
    out = session.run(
        "crosstalk_compensated_sqrb",
        {
            "probe_qubit": "q0",
            "drive_qubit": "q1",
            "bidirectional": True,
            "depths": [1, 2, 4, 8, 16, 32],
            "num_random_sequences": 5,
            "cancel_amp": 0.035,
            "init_phase": 0.85,
            "drive_cancel_amp": 0.2,
        },
        update="none",
    )
    assert out.get("error") is None, out.get("error")
    assert out["fit"]["q0"]["mitigation_ratio"] > 0.6
    assert out["fit"]["q1"]["mitigation_ratio"] < 0.3


@pytest.mark.parametrize("removed", [
    {"mode": "calibrate"}, {"cal_stage_repetitions": [10, 20]}, {"min_cancel_amp": 0.01},
    {"readout_scheme": "separate_passes"}, {"sequence_mode": "unrolled"},
    {"stark_frame_rad": 0.01}, {"phase_reset": "if_only"}, {"fpga_dispatch": "merged"},
    {"reverse_track": False}, {"strict_timing": True}, {"target_gate": "x180"},
])
def test_removed_options_are_refused(removed):
    """One way to run it: the calibrate scan, the unrolled path and the diagnostics are gone."""
    with pytest.raises(ValueError):
        CrosstalkCompensatedSQRBParameters(probe_qubit="q1", drive_qubit="q3", **removed)


def test_conditions_and_depths_are_checked():
    p = CrosstalkCompensatedSQRBParameters(conditions=["compensated", "isolated"])
    assert p.get_conditions() == ["compensated", "isolated"]
    assert CrosstalkCompensatedSQRBParameters().get_conditions() == ["isolated", "simultaneous", "compensated"]
    with pytest.raises(ValueError, match="conditions"):
        CrosstalkCompensatedSQRBParameters(conditions=["calibrate"])
    with pytest.raises(ValueError, match="depths"):
        CrosstalkCompensatedSQRBParameters(depths=[0, 4])
    assert CrosstalkCompensatedSQRBParameters().sequences_per_program == 7


def test_cancel_tone_is_aggressor_or_victim():
    assert CrosstalkCompensatedSQRBParameters().cancel_tone == "aggressor"
    assert CrosstalkCompensatedSQRBParameters(cancel_tone="victim").cancel_tone == "victim"
    with pytest.raises(ValueError):
        CrosstalkCompensatedSQRBParameters(cancel_tone="drive")
