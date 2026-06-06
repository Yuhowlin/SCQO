"""End-to-end test of the abstraction with no instrument installed.

A throwaway concrete protocol (build = no-op) is enough because the SimulatedBackend
drives ``simulate`` instead of ``build``.
"""

from __future__ import annotations

import numpy as np

from scqo import Outcome, Session, register
from scqo.protocols import PowerRabi, Ramsey, ResonatorSpectroscopy
from scqo.testing import InMemoryDevice, SimulatedBackend


@register
class DemoResonatorSpectroscopy(ResonatorSpectroscopy):
    """Concrete protocol for tests/demos; no real instrument program."""

    def build(self):  # never called by SimulatedBackend
        return None


@register
class DemoRamsey(Ramsey):
    """Concrete Ramsey for tests/demos; no real instrument program."""

    def build(self):  # never called by SimulatedBackend
        return None


@register
class DemoPowerRabi(PowerRabi):
    """Concrete power Rabi for tests/demos; no real instrument program."""

    def build(self):  # never called by SimulatedBackend
        return None


def _device() -> InMemoryDevice:
    return InMemoryDevice(
        {
            "q0": {"readout_freq": 5.95e9, "drive_freq": 3.87e9, "pi_amp": 0.2},
            "q1": {"readout_freq": 6.05e9, "drive_freq": 4.01e9, "pi_amp": 0.18},
        }
    )


def test_protocol_runs_and_fits_dip():
    backend = SimulatedBackend(_device())
    proto = DemoResonatorSpectroscopy(
        backend, DemoResonatorSpectroscopy.Parameters(qubits=["q0", "q1"], frequency_span_hz=15e6, num_points=201)
    )
    result = proto.run()
    assert result.success
    # recovered dip lies within the swept window for each qubit
    for qubit in ["q0", "q1"]:
        assert abs(result.fit[qubit]["dip_detuning_hz"]) < 15e6 / 2


def test_session_catalog_and_run_are_json():
    sess = Session(SimulatedBackend(_device()))

    catalog = sess.catalog()
    names = {entry["name"] for entry in catalog}
    assert "resonator_spectroscopy" in names
    # schema is real JSON-schema with the declared knobs
    schema = next(e for e in catalog if e["name"] == "resonator_spectroscopy")["parameters_schema"]
    assert "frequency_span_hz" in schema["properties"]

    before = sess.device_state()["q0"]["readout_freq"]
    result = sess.run("resonator_spectroscopy", {"qubits": ["q0"], "frequency_span_hz": 15e6})
    assert result["outcomes"]["q0"] == Outcome.SUCCESSFUL.value
    # update=True wrote the fitted frequency back into the device
    after = sess.device_state()["q0"]["readout_freq"]
    assert after != before
    assert np.isclose(after, result["fit"]["q0"]["readout_freq"])


def test_ramsey_generalizes_pattern():
    """Same lifecycle, different sweep/fit/field: time sweep -> T2* + drive_freq update."""
    sess = Session(SimulatedBackend(_device()))

    # both protocols share one catalog/registry
    assert {"resonator_spectroscopy", "ramsey"} <= {e["name"] for e in sess.catalog()}

    before = sess.device_state()["q1"]["drive_freq"]
    result = sess.run(
        "ramsey",
        {"qubits": ["q1"], "frequency_detuning_hz": 1.0e6, "max_idle_time_ns": 4000, "num_points": 201},
    )
    assert result["outcomes"]["q1"] == Outcome.SUCCESSFUL.value
    # recovered residual detuning is small (|err| <= 0.2 * applied) and T2* is physical
    assert abs(result["fit"]["q1"]["detuning_error_hz"]) < 0.3e6
    assert 1e-6 < result["fit"]["q1"]["t2_star_s"] < 50e-6
    # update=True wrote the corrected drive_freq back (a different field than resonator spec)
    after = sess.device_state()["q1"]["drive_freq"]
    assert after != before
    assert np.isclose(after, result["fit"]["q1"]["drive_freq"])


def test_power_rabi_generalizes_pattern():
    """Amplitude sweep -> cosine fit -> updates a third device field: pi_amp."""
    sess = Session(SimulatedBackend(_device()))

    assert "power_rabi" in {e["name"] for e in sess.catalog()}

    before = sess.device_state()["q0"]["pi_amp"]
    result = sess.run("power_rabi", {"qubits": ["q0"], "max_amp_factor": 2.0, "num_points": 201})
    assert result["outcomes"]["q0"] == Outcome.SUCCESSFUL.value
    # recovered pi factor is near 1 (simulated miscalibration was within +-15%)
    assert 0.8 < result["fit"]["q0"]["pi_amp_factor"] < 1.2
    after = sess.device_state()["q0"]["pi_amp"]
    assert after != before
    assert np.isclose(after, result["fit"]["q0"]["pi_amp"])
