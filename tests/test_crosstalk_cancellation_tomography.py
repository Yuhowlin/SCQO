"""Unit tests for CrosstalkCancellationTomography experiment in SCQO."""

import numpy as np
import pytest

from scqo import Session
from scqo import experiments as registry
from scqo.experiments.crosstalk_cancellation_tomography import (
    CrosstalkCancellationTomography,
    CrosstalkCancellationTomographyParameters,
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
    params = CrosstalkCancellationTomographyParameters(probe_qubit="q1", drive_qubit="q2")
    assert params.targets == ["q1", "q2"]
    assert params.probe_qubit == "q1"
    assert params.drive_qubit == "q2"

    params2 = CrosstalkCancellationTomographyParameters(targets=["q3", "q4"])
    assert params2.probe_qubit == "q3"
    assert params2.drive_qubit == "q4"


def test_get_repetitions():
    p = CrosstalkCancellationTomographyParameters(min_repetitions=0, max_repetitions=10, step_repetitions=2)
    reps = p.get_repetitions()
    assert reps.tolist() == [0, 2, 4, 6, 8, 10]


def test_simulation_and_estimation(session):
    exp = CrosstalkCancellationTomography(
        session.backend,
        params=CrosstalkCancellationTomographyParameters(
            probe_qubit="q0",
            drive_qubit="q1",
            min_repetitions=0,
            max_repetitions=5,
            cancel_amp=0.035,
            init_phase=0.85,
        ),
    )
    coords = exp.define_sweep()
    sim_data = exp.simulate(coords)
    assert "I" in sim_data or "state" in sim_data

    # Run through session simulated backend
    out = session.run(
        "crosstalk_cancellation_tomography",
        {
            "probe_qubit": "q0",
            "drive_qubit": "q1",
            "min_repetitions": 0,
            "max_repetitions": 5,
            "cancel_amp": 0.035,
            "init_phase": 0.85,
        },
        update="none",
    )
    assert out.get("error") is None, out.get("error")
    assert "q0" in out["fit"]
    fit = out["fit"]["q0"]
    assert fit["success"]
    assert "per_condition" in fit


def test_estimate_suggests_the_simulated_optimum(session):
    # The simulator's leakage sits at (cancel_amp 0.035, init_phase 0.85); start detuned from it
    out = session.run(
        "crosstalk_cancellation_tomography",
        {
            "probe_qubit": "q0",
            "drive_qubit": "q1",
            "repetitions": "0:40:2",
            "cancel_amp": 0.028,
            "init_phase": 0.6,
            "use_state_discrimination": True,
        },
        update="none",
    )
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]
    assert fit["suggested_cancel_amp"] == pytest.approx(0.035, rel=0.05)
    assert fit["suggested_init_phase"] == pytest.approx(0.85, abs=0.05)
    assert fit["suggested_updates"]["cancel_amp"] == fit["suggested_cancel_amp"]


@pytest.mark.parametrize("removed", [
    {"cancel_tone": "victim"}, {"phase_reset": "if_only"}, {"stark_frame_rad": 0.01},
    {"drive_cancel_amp": 0.0}, {"drive_init_phase": 0.1},
])
def test_removed_tomography_options_are_refused(removed):
    """The cancel tone is always the aggressor's; the unrolled-era knobs are gone."""
    from scqo.experiments.crosstalk_cancellation_tomography import CrosstalkCancellationTomographyParameters

    with pytest.raises(ValueError):
        CrosstalkCancellationTomographyParameters(probe_qubit="q1", drive_qubit="q3", **removed)
