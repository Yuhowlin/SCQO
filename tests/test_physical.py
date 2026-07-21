"""PhysicalStore: the sample's instrument-independent measured-physics ledger.

Round-trips, provenance stamping, in-memory mode, and the flux experiments'
physics landing here through the suggest/accept flow. All offline."""

from __future__ import annotations

import json

import pytest

from scqo import CATEGORIES, Component, PhysicalStore, Roster, Session, register
from scqo.experiments import QubitSpectroscopyFlux, ResonatorSpectroscopyFlux
from scqo.testing import InMemoryDevice, SimulatedBackend, demo_roster


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


def _flux_roster() -> Roster:
    """A flux-capable single-qubit roster: FluxTunableTransmon + the ZControl
    interaction term the flux experiments' proposals land on."""
    return Roster({
        "q0": Component(name="q0", physical="FluxTunableTransmon",
                        instrument="ReadableTransmon",
                        operations=("rx", "readout", "flux_bias")),
        "q0_res": Component(name="q0_res", physical="Resonator"),
        "q0_ro": Component(name="q0_ro", physical="ReadoutLine",
                           members={"transmon": "q0", "resonator": "q0_res"}),
        "q0_z": Component(name="q0_z", physical="ZControl",
                          members={"transmon": "q0"}),
    })


def test_per_name_field_routing_is_unambiguous():
    """Routing depends on it: for every roster name, the two category slots must
    not declare the same field — a field resolves to exactly one store."""
    for roster in (demo_roster(), _flux_roster()):
        for name, comp in roster.components.items():
            if comp.physical and comp.instrument:
                clash = (set(CATEGORIES[comp.physical].fields)
                         & set(CATEGORIES[comp.instrument].fields))
                assert not clash, f"{name}: ambiguous field(s) {sorted(clash)}"
            for field, (side, _spec) in roster.fields_of(name).items():
                assert roster.resolve(name, field)[0] == side


def test_round_trip_and_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr("scqo.config._current_operator", lambda: "alice")
    path = tmp_path / "physical.json"

    store = PhysicalStore(path, setup="qm_main")
    assert store.get("q0", "t1_s") is None  # never measured -> None, no KeyError
    store.record("q0", "t1_s", 25e-6, experiment="qubit_relaxation", run_id="run-01")
    store.record("q0", "t1_s", 26e-6, experiment="qubit_relaxation", run_id="run-02")
    store.save()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema"] == 2  # the component-cutover stamp
    assert data["values"]["q0"]["t1_s"] == 26e-6  # FLAT — the file is one context
    assert "history" not in data  # values-only; the sidecar holds the history
    from scqo._state_io import read_history

    assert [h["old"] for h in read_history(path)] == [None, 25e-6]  # old -> new chain

    reloaded = PhysicalStore(path, setup="qm_main")
    assert reloaded.snapshot() == {"q0": {"t1_s": 26e-6}}
    (h1, h2) = reloaded.history()
    assert (h1.experiment, h1.run_id, h1.operator) == ("qubit_relaxation", "run-01", "alice")
    assert h1.setup == "qm_main"  # stamped for self-describing rows
    assert h2.old == 25e-6 and h2.new == 26e-6


def test_in_memory_mode_and_non_finite_guard(tmp_path):
    store = PhysicalStore(None)  # no path: usable, nothing persists
    store.record("q0_ro", "g_hz", 80e6)
    store.save()  # no-op, must not raise
    assert store.snapshot() == {"q0_ro": {"g_hz": 80e6}}
    with pytest.raises(ValueError, match="non-finite"):
        store.record("q0_ro", "g_hz", float("inf"))
    assert len(store.history()) == 1


def test_record_rejects_unknown_field():
    """A typo'd field name must not silently become ledger truth (roster-validated)."""
    store = PhysicalStore(None, roster=demo_roster())
    with pytest.raises(KeyError, match="has no field 't1_sec'"):
        store.record("q0", "t1_sec", 1e-6)
    # an INSTRUMENT field may not be smuggled into the physics ledger either
    with pytest.raises(ValueError, match="INSTRUMENT field"):
        store.record("q0", "pi_amp", 0.2)
    assert store.history() == [] and store.snapshot() == {}


def test_flux_experiments_physics_lands_on_accept(tmp_path):
    """The former "physics with nowhere to live": the flux maps' arch/dispersive
    quantities are suggested (store="physical") and land in physical.json on accept,
    stamped with the originating run_id — the arch facts on the transmon, the
    volts-to-flux transfer on its ZControl component. The instrument config never
    changes."""
    sess = Session(SimulatedBackend(_device()), _flux_roster(),
                   data_root=tmp_path / "data", device_name="devA")
    state_before = sess.device_state()

    result = sess.run("qubit_spectroscopy_flux", {"targets": ["q0"]})
    assert {s["store"] for s in result["suggestions"]} == {"physical"}
    assert {(s["component"], s["field"]) for s in result["suggestions"]} == {
        ("q0", "ej_sum_hz"), ("q0", "f_q_max_hz"),
        ("q0_z", "v_offset_v"), ("q0_z", "v_per_phi0_v"),
    }
    assert sess.physical_state() == {}  # pending until accepted

    summary = sess.accept(result["run_id"])
    assert len(summary["applied"]) == 4 and summary["errors"] == []
    physical = sess.physical_state()
    assert physical["q0"]["ej_sum_hz"] == result["fit"]["q0"]["ej_sum_hz"]
    assert physical["q0_z"]["v_per_phi0_v"] == result["fit"]["q0"]["v_per_phi0_v"]
    assert all(h.run_id == result["run_id"] for h in sess.physical.history())
    assert sess.device_state() == state_before  # no instrument knob involved
    assert sess.history() == []

    # persisted on disk at the device-level fallback (a setup-less direct-API
    # session with a data_root but no cooldown/setup), flat values
    payload = json.loads((tmp_path / "data" / "devA" / "physical.json").read_text(encoding="utf-8"))
    assert payload["values"]["q0"]["ej_sum_hz"] == physical["q0"]["ej_sum_hz"]

    # the resonator flux map adds the dispersive quantities to the same ledger —
    # f_r0 (Resonator) / g (ReadoutLine) only because f_q_max_hz constrains the
    # fit (else they are assumption-conditional and not proposed), and the
    # measured f_q_max_hz from the qubit arch fit above must NOT be overwritten
    # by this experiment's input value
    f_q_max_measured = physical["q0"]["f_q_max_hz"]
    result2 = sess.run("resonator_spectroscopy_flux",
                       {"targets": ["q0"], "f_q_max_hz": 3.87e9}, update="apply")
    physical = sess.physical_state()
    assert physical["q0_res"]["f_r0_hz"] == result2["fit"]["q0"]["f_r0_hz"]
    assert physical["q0_ro"]["g_hz"] == result2["fit"]["q0"]["g_hz"]
    assert physical["q0"]["f_q_max_hz"] == f_q_max_measured
