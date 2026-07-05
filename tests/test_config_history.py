"""SCQO-native config + change history: runs are recorded, device_state() is the
authoritative SCQO config, and the state round-trips through JSON (load is authoritative
— it pushes the SCQO values back into the vendor device)."""

from __future__ import annotations

import json

import numpy as np

from scqo import Outcome, Session, register
from scqo.experiments import QubitPowerRabi, ResonatorSpectroscopy
from scqo.testing import InMemoryDevice, SimulatedBackend


# Concrete demo experiments (probe is a no-op under SimulatedBackend). Registering under
# the canonical names is idempotent across test modules (last registration wins; all are
# equivalent no-op-probe subclasses).
@register
class _DemoRes(ResonatorSpectroscopy):
    def probe(self):
        return None


@register
class _DemoRabi(QubitPowerRabi):
    def probe(self):
        return None


def _device() -> InMemoryDevice:
    return InMemoryDevice(
        {
            "q0": {"readout_freq": 5.95e9, "drive_freq": 3.87e9, "pi_amp": 0.2, "readout_amp": 0.25},
            "q1": {"readout_freq": 6.05e9, "drive_freq": 4.01e9, "pi_amp": 0.18, "readout_amp": 0.22},
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

    sess = Session(SimulatedBackend(_device()), state_path=path, state_sync="push")
    sess.run("qubit_power_rabi", {"qubits": ["q0"]})
    saved_pi = sess.device_state()["q0"]["pi_amp"]
    saved_history = sess.history()
    assert saved_history  # recorded and (since state_path is set) persisted

    # A fresh push-mode session loads the SCQO state instead of re-seeding from the vendor.
    sess2 = Session(SimulatedBackend(_device()), state_path=path, state_sync="push")
    assert np.isclose(sess2.device_state()["q0"]["pi_amp"], saved_pi)
    assert len(sess2.history()) == len(saved_history)


def _stale_state(tmp_path):
    path = tmp_path / "scqo_state.json"
    path.write_text(
        json.dumps(
            {
                "config": {"q0": {"readout_freq": 7.0e9, "drive_freq": 4.0e9, "pi_amp": 0.5, "readout_amp": 0.25}},
                "history": [
                    {
                        "timestamp": "2026-01-01T00:00:00+00:00", "qubit": "q0",
                        "field": "readout_freq", "old": 5.9e9, "new": 7.0e9,
                        "experiment": "resonator_spectroscopy",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def test_push_load_is_authoritative(tmp_path):
    """state_sync="push" (a device SCQO fully owns): the saved SCQO config wins on load —
    its values are pushed into the vendor device, even where the vendor differs."""
    backend = SimulatedBackend(_device())  # vendor q0.readout_freq starts at 5.95e9
    sess = Session(backend, state_path=_stale_state(tmp_path), state_sync="push")

    # SCQO config reports the loaded value...
    assert sess.device_state()["q0"]["readout_freq"] == 7.0e9
    # ...and it was pushed into the vendor device (authoritative on load).
    assert backend.device.snapshot()["q0"]["readout_freq"] == 7.0e9


def test_pull_load_does_not_clobber_vendor(tmp_path):
    """Default state_sync="pull" (another tool may also calibrate the vendor, e.g.
    qualibrate on QM): a stale SCQO state file must NOT overwrite vendor values at
    startup — but the saved history is kept, so provenance is continuous."""
    backend = SimulatedBackend(_device())
    sess = Session(backend, state_path=_stale_state(tmp_path))  # default pull

    # vendor wins at startup: nothing was pushed, SCQO config mirrors the vendor
    assert backend.device.snapshot()["q0"]["readout_freq"] == 5.95e9
    assert sess.device_state()["q0"]["readout_freq"] == 5.95e9
    # provenance survives the pull
    assert len(sess.history()) == 1
    assert sess.history()[0]["experiment"] == "resonator_spectroscopy"

    # a fresh measurement still writes back + pushes (writes are always legitimate)
    sess.run("resonator_spectroscopy", {"qubits": ["q0"]})
    assert backend.device.snapshot()["q0"]["readout_freq"] == sess.device_state()["q0"]["readout_freq"]
    assert len(sess.history()) == 2


def test_vendor_rejection_leaves_no_phantom_history():
    """If the instrument rejects a value, neither the SCQO config nor the history may
    claim a change that never reached the hardware (vendor push happens first)."""
    import pytest

    from scqo import RecordingDevice

    inner = _device()

    class _RejectingQubit:
        def __init__(self, view):
            object.__setattr__(self, "_view", view)

        def __getattr__(self, name):
            return getattr(object.__getattribute__(self, "_view"), name)

        def __setattr__(self, name, value):
            raise ValueError("vendor rejected value")

    class _RejectingDevice:
        def qubit(self, name):
            return _RejectingQubit(inner.qubit(name))

        def snapshot(self):
            return inner.snapshot()

        def save(self):
            pass

    device = RecordingDevice(_RejectingDevice())
    before = device.snapshot()["q0"]["pi_amp"]
    with pytest.raises(ValueError):
        device.qubit("q0").pi_amp = 0.5
    assert device.history() == []  # no phantom record
    assert device.snapshot()["q0"]["pi_amp"] == before  # config untouched
