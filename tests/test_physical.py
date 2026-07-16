"""PhysicalStore: the sample's instrument-independent measured-physics ledger.

Round-trips, provenance stamping, in-memory mode, and the flux experiments'
physics landing here through the suggest/accept flow. All offline."""

from __future__ import annotations

import json

import pytest

from scqo import PHYSICAL_FIELDS, PhysicalStore, Session, register
from scqo.config import FIELDS
from scqo.experiments import QubitSpectroscopyFlux, ResonatorSpectroscopyFlux
from scqo.testing import InMemoryDevice, SimulatedBackend


@register
class _PhQubitSpectroscopyFlux(QubitSpectroscopyFlux):
    def probe(self):
        return None


@register
class _PhResonatorSpectroscopyFlux(ResonatorSpectroscopyFlux):
    def probe(self):
        return None


def _device() -> InMemoryDevice:
    return InMemoryDevice(
        {
            "q0": {"readout_freq": 5.95e9, "drive_freq": 3.87e9, "pi_amp": 0.2, "readout_amp": 0.25},
        }
    )


def test_field_tables_are_disjoint():
    """Routing depends on it: a field name resolves to exactly one store."""
    assert not set(PHYSICAL_FIELDS) & set(FIELDS)


def test_round_trip_and_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr("scqo.config._current_operator", lambda: "alice")
    path = tmp_path / "physical.json"

    store = PhysicalStore(path, setup="qm_main")
    assert store.get("q0", "t1_s") is None  # never measured -> None, no KeyError
    store.record("q0", "t1_s", 25e-6, experiment="qubit_relaxation", run_id="run-01")
    store.record("q0", "t1_s", 26e-6, experiment="qubit_relaxation", run_id="run-02")
    store.save()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["values"]["q0"]["t1_s"] == 26e-6  # FLAT — the file is one context
    assert [h["old"] for h in data["history"]] == [None, 25e-6]  # old -> new chain

    reloaded = PhysicalStore(path, setup="qm_main")
    assert reloaded.snapshot() == {"q0": {"t1_s": 26e-6}}
    (h1, h2) = reloaded.history()
    assert (h1.experiment, h1.run_id, h1.operator) == ("qubit_relaxation", "run-01", "alice")
    assert h1.setup == "qm_main"  # stamped for self-describing rows
    assert h2.old == 25e-6 and h2.new == 26e-6


def test_in_memory_mode_and_non_finite_guard(tmp_path):
    store = PhysicalStore(None)  # no path: usable, nothing persists
    store.record("q0", "g_hz", 80e6)
    store.save()  # no-op, must not raise
    assert store.snapshot() == {"q0": {"g_hz": 80e6}}
    with pytest.raises(ValueError, match="non-finite"):
        store.record("q0", "g_hz", float("inf"))
    assert len(store.history()) == 1


def test_flux_experiments_physics_lands_on_accept(tmp_path):
    """The former "physics with nowhere to live": the flux maps' arch/dispersive
    quantities are suggested (store="physical") and land in physical.json on accept,
    stamped with the originating run_id. The instrument config never changes."""
    sess = Session(SimulatedBackend(_device()), data_root=tmp_path / "data", device_name="devA")
    state_before = sess.device_state()

    result = sess.run("qubit_spectroscopy_flux", {"qubits": ["q0"]})
    assert {s["store"] for s in result["suggestions"]} == {"physical"}
    assert {s["field"] for s in result["suggestions"]} == {
        "sweet_spot_flux_v", "dv_phi0_v", "ej_sum_ghz", "f_q_max_hz",
    }
    assert sess.physical_state() == {}  # pending until accepted

    summary = sess.accept(result["run_id"])
    assert len(summary["applied"]) == 4 and summary["errors"] == []
    physical = sess.physical_state()["q0"]
    assert physical["ej_sum_ghz"] == result["fit"]["q0"]["ej_sum_ghz"]
    assert physical["dv_phi0_v"] == result["fit"]["q0"]["flux_period_v"]  # unified name
    assert all(h.run_id == result["run_id"] for h in sess.physical.history())
    assert sess.device_state() == state_before  # no instrument knob involved
    assert sess.history() == []

    # persisted on disk at the device-level fallback (a setup-less direct-API
    # session with a data_root but no cooldown/setup), flat values
    payload = json.loads((tmp_path / "data" / "devA" / "physical.json").read_text(encoding="utf-8"))
    assert payload["values"]["q0"]["ej_sum_ghz"] == physical["ej_sum_ghz"]

    # the resonator flux map adds the dispersive quantities to the same ledger —
    # f_r0/g only because f_q_max_hz constrains the fit (else they are assumption-
    # conditional and not proposed), and the measured f_q_max_hz from the qubit
    # arch fit above must NOT be overwritten by this experiment's input value
    f_q_max_measured = physical["f_q_max_hz"]
    result2 = sess.run("resonator_spectroscopy_flux",
                       {"qubits": ["q0"], "f_q_max_hz": 3.87e9}, update="apply")
    for field in ("f_r0_hz", "g_hz"):
        assert sess.physical_state()["q0"][field] == result2["fit"]["q0"][field]
    assert sess.physical_state()["q0"]["f_q_max_hz"] == f_q_max_measured
