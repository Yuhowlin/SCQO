"""Live-source provenance: which run does each CURRENT value trace to?

Pure-function tests for scqo.provenance (the strict-match rule shared by the
viewer, the CLI and Session.live_sources)."""

from __future__ import annotations

from scqo.provenance import live_run_map, live_sources, summarize_live


def _rec(component, field, new, run_id=None, **extra):
    return {"timestamp": "2026-07-12T10:00:00+08:00", "component": component, "field": field,
            "old": None, "new": new, "experiment": "resonator_spectroscopy",
            "run_id": run_id, "operator": "shiau", **extra}


def test_last_record_wins_and_strict_match_credits_the_run():
    values = {"q0": {"readout_freq": 5.95e9}}
    history = [
        _rec("q0", "readout_freq", 5.90e9, run_id="run-old"),
        _rec("q0", "readout_freq", 5.95e9, run_id="run-new"),
    ]
    (info,) = [live_sources(values, history)["q0"]["readout_freq"]]
    assert info["status"] == "run"
    assert info["run_id"] == "run-new"  # last record wins
    assert info["value"] == info["recorded"] == 5.95e9
    assert info["operator"] == "shiau"


def test_drifted_value_is_external_and_credits_no_run():
    """Strict match: the vendor reseeded (or another tool wrote) — the last record
    carries a run_id, but the value no longer matches, so NO run is credited."""
    values = {"q0": {"readout_freq": 6.2e9}}
    history = [_rec("q0", "readout_freq", 5.95e9, run_id="run-a")]
    info = live_sources(values, history)["q0"]["readout_freq"]
    assert info["status"] == "external"
    assert info["run_id"] is None  # never a false credit
    assert info["recorded"] == 5.95e9 and info["value"] == 6.2e9
    assert info["timestamp"]  # the last SCQO write is still reported (debug value)


def test_manual_and_unrecorded_and_none_values():
    values = {"q0": {"pi_amp": 0.31, "readout_freq": 5.95e9, "readout_fidelity": None}}
    history = [_rec("q0", "pi_amp", 0.31, run_id=None)]  # notebook write
    sources = live_sources(values, history)
    assert sources["q0"]["pi_amp"]["status"] == "manual"
    assert sources["q0"]["readout_freq"]["status"] == "unrecorded"  # vendor pull-seed
    assert sources["q0"]["readout_freq"]["timestamp"] is None
    assert "readout_fidelity" not in sources["q0"]  # None values are skipped


def test_operator_suggested_value_credits_the_run(tmp_path):
    """End-to-end crediting chain: a value suggested by a HUMAN against a run and
    then accepted traces back to that run — status "run", not "manual" (the run's
    record carries the operator's proposal, so the credit is truthful)."""
    from scqo import Session, register
    from scqo.experiments import ResonatorSpectroscopy
    from scqo.testing import InMemoryDevice, SimulatedBackend, demo_roster

    @register
    class _PvResonatorSpectroscopy(ResonatorSpectroscopy):  # idempotent re-register
        def probe(self):
            return None

    sess = Session(SimulatedBackend(InMemoryDevice({"q0": {"readout_freq": 5.95e9}})),
                   demo_roster(), data_root=tmp_path / "data", device_name="devA")
    result = sess.run("resonator_spectroscopy", {"targets": ["q0"]}, update="none")
    sess.suggest(result["run_id"], {"q0.t1_s": 2.5e-5}, comment="read off the decay")
    sess.accept(result["run_id"])

    info = live_sources(sess.physical_state(), sess.history(store="physical"))["q0"]["t1_s"]
    assert info["status"] == "run"
    assert info["run_id"] == result["run_id"]


def test_live_run_map_merges_stores_and_keeps_runs_only():
    inst = live_sources(
        {"q0": {"readout_freq": 1.0}, "q1": {"readout_freq": 2.0, "pi_amp": 0.2}},
        [_rec("q0", "readout_freq", 1.0, run_id="run-x"),
         _rec("q1", "readout_freq", 2.0, run_id="run-x"),
         _rec("q1", "pi_amp", 0.2, run_id=None)],  # manual: not in the map
    )
    phys = live_sources(
        {"q1": {"t1_s": 3.0}},
        [_rec("q1", "t1_s", 3.0, run_id="run-y")],
    )
    merged = live_run_map(inst, phys)
    assert merged == {"run-x": [("q0", "readout_freq"), ("q1", "readout_freq")],
                      "run-y": [("q1", "t1_s")]}


def test_summarize_live_groups_by_field():
    pairs = [("q0", "readout_freq"), ("q1", "readout_freq"), ("q1", "t1_s")]
    assert summarize_live(pairs) == "readout_freq (q0,q1), t1_s (q1)"
