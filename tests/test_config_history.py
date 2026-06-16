"""SCQO-native config + change history: runs are recorded, device_state() is the
authoritative SCQO config, and the state round-trips through JSON (load is authoritative
— it pushes the SCQO values back into the vendor device)."""

from __future__ import annotations

import json

import numpy as np

from scqo import Outcome, Session, register
from scqo.experiments import PowerRabi, ResonatorSpectroscopy
from scqo.testing import InMemoryDevice, SimulatedBackend


# Concrete demo experiments (probe is a no-op under SimulatedBackend). Registering under
# the canonical names is idempotent across test modules (last registration wins; all are
# equivalent no-op-probe subclasses).
@register
class _DemoRes(ResonatorSpectroscopy):
    def probe(self):
        return None


@register
class _DemoRabi(PowerRabi):
    def probe(self):
        return None


def _device() -> InMemoryDevice:
    return InMemoryDevice(
        {
            "q0": {"readout_freq": 5.95e9, "drive_freq": 3.87e9, "pi_amp": 0.2},
            "q1": {"readout_freq": 6.05e9, "drive_freq": 4.01e9, "pi_amp": 0.18},
        }
    )


def test_run_records_history_and_updates_config():
    sess = Session(SimulatedBackend(_device()))
    before = sess.device_state()["q0"]["readout_freq"]

    result = sess.run("resonator_spectroscopy", {"qubits": ["q0"]})
    assert result["outcomes"]["q0"] == Outcome.SUCCESSFUL.value

    # device_state() is the authoritative SCQO config and reflects the update
    after = sess.device_state()["q0"]["readout_freq"]
    assert after != before
    assert np.isclose(after, result["fit"]["q0"]["readout_freq"])

    # the change is recorded with provenance (which experiment, old -> new)
    hist = sess.history()
    assert len(hist) == 1
    rec = hist[0]
    assert (rec["qubit"], rec["field"], rec["experiment"]) == ("q0", "readout_freq", "resonator_spectroscopy")
    assert rec["old"] == before
    assert np.isclose(rec["new"], after)
    assert rec["timestamp"]  # non-empty ISO timestamp


def test_state_round_trips(tmp_path):
    path = str(tmp_path / "scqo_state.json")

    sess = Session(SimulatedBackend(_device()), state_path=path)
    sess.run("power_rabi", {"qubits": ["q0"]})
    saved_pi = sess.device_state()["q0"]["pi_amp"]
    saved_history = sess.history()
    assert saved_history  # recorded and (since state_path is set) persisted

    # A fresh session loads the SCQO state instead of re-seeding from the vendor.
    sess2 = Session(SimulatedBackend(_device()), state_path=path)
    assert np.isclose(sess2.device_state()["q0"]["pi_amp"], saved_pi)
    assert len(sess2.history()) == len(saved_history)


def test_load_is_authoritative(tmp_path):
    """A saved SCQO config wins on load: its values are pushed into the vendor device,
    even where the vendor started at a different (un-calibrated) value."""
    path = tmp_path / "scqo_state.json"
    path.write_text(
        json.dumps(
            {
                "config": {"q0": {"readout_freq": 7.0e9, "drive_freq": 4.0e9, "pi_amp": 0.5}},
                "history": [],
            }
        ),
        encoding="utf-8",
    )

    backend = SimulatedBackend(_device())  # vendor q0.readout_freq starts at 5.95e9
    sess = Session(backend, state_path=str(path))

    # SCQO config reports the loaded value...
    assert sess.device_state()["q0"]["readout_freq"] == 7.0e9
    # ...and it was pushed into the vendor device (authoritative on load).
    assert backend.device.snapshot()["q0"]["readout_freq"] == 7.0e9
