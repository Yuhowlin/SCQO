"""Live-source provenance: which run does each CURRENT value trace to?

Pure-function tests for scqo.provenance (the strict-match rule shared by the
viewer, the CLI and the report layer). The generic utility keys its history
rows on ``entity`` — the model's one vocabulary — so the viewer, the CLI and
the report layer all credit values by the same rule."""

from __future__ import annotations

from scqo.provenance import live_run_map, live_sources, summarize_live


def _rec(entity, field, new, run_id=None, **extra):
    return {"timestamp": "2026-07-12T10:00:00+08:00", "entity": entity, "field": field,
            "old": None, "new": new, "experiment": "resonator_spectroscopy",
            "run_id": run_id, "operator": "shiau", **extra}


def test_last_record_wins_and_strict_match_credits_the_run():
    values = {"q0_ro": {"readout_freq_hz": 5.95e9}}
    history = [
        _rec("q0_ro", "readout_freq_hz", 5.90e9, run_id="run-old"),
        _rec("q0_ro", "readout_freq_hz", 5.95e9, run_id="run-new"),
    ]
    (info,) = [live_sources(values, history)["q0_ro"]["readout_freq_hz"]]
    assert info["status"] == "run"
    assert info["run_id"] == "run-new"  # last record wins
    assert info["value"] == info["recorded"] == 5.95e9
    assert info["operator"] == "shiau"


def test_drifted_value_is_external_and_credits_no_run():
    """Strict match: the vendor reseeded (or another tool wrote) — the last record
    carries a run_id, but the value no longer matches, so NO run is credited."""
    values = {"q0_ro": {"readout_freq_hz": 6.2e9}}
    history = [_rec("q0_ro", "readout_freq_hz", 5.95e9, run_id="run-a")]
    info = live_sources(values, history)["q0_ro"]["readout_freq_hz"]
    assert info["status"] == "external"
    assert info["run_id"] is None  # never a false credit
    assert info["recorded"] == 5.95e9 and info["value"] == 6.2e9
    assert info["timestamp"]  # the last SCQO write is still reported (debug value)


def test_manual_and_unrecorded_and_none_values():
    values = {"q0_xy": {"pi_amp": 0.31},
              "q0_ro": {"readout_freq_hz": 5.95e9, "fidelity_g": None}}
    history = [_rec("q0_xy", "pi_amp", 0.31, run_id=None)]  # notebook write
    sources = live_sources(values, history)
    assert sources["q0_xy"]["pi_amp"]["status"] == "manual"
    assert sources["q0_ro"]["readout_freq_hz"]["status"] == "unrecorded"  # vendor pull-seed
    assert sources["q0_ro"]["readout_freq_hz"]["timestamp"] is None
    assert "fidelity_g" not in sources["q0_ro"]  # None values are skipped


def test_live_sources_handles_records_missing_entity_or_field():
    values = {"q0_xy": {"pi_amp": 0.31}}
    history = [
        {"timestamp": "2026-07-12T10:00:00+08:00", "invalid_key": "val"},  # no entity/field
        {"timestamp": "2026-07-12T10:00:00+08:00", "entity": "q0_xy"},      # no field
        _rec("q0_xy", "pi_amp", 0.31, run_id="run-1"),
    ]
    sources = live_sources(values, history)
    assert sources["q0_xy"]["pi_amp"]["status"] == "run"
    assert sources["q0_xy"]["pi_amp"]["run_id"] == "run-1"


def test_operator_suggested_value_credits_the_run(tmp_path):
    """End-to-end crediting chain: a value suggested by a HUMAN against a run and
    then accepted traces back to that run — status "run", not "manual" (the run's
    record carries the operator's proposal, so the credit is truthful)."""
    from scqo import Session
    from scqo.report import live_sources as model_live_sources
    from scqo.testing import SimulatedBackend, demo_device

    roster, design, vendor = demo_device()
    sess = Session(SimulatedBackend(vendor), roster, design=design,
                   data_root=tmp_path / "data", device_name="devA")
    result = sess.run("resonator_spectroscopy", {"targets": ["q0"]}, update="none")
    assert result.get("error") is None
    sess.suggest(result["run_id"], {"q0.t1_s": 2.5e-5}, comment="read off the decay")
    sess.accept(result["run_id"])

    info = model_live_sources(sess.physical_state(),
                              sess.history(store="physical"))["q0"]["t1_s"]
    assert info["status"] == "run"
    assert info["run_id"] == result["run_id"]


def test_live_run_map_merges_stores_and_keeps_runs_only():
    state = live_sources(
        {"q0_ro": {"readout_freq_hz": 1.0},
         "q1_ro": {"readout_freq_hz": 2.0}, "q1_xy": {"pi_amp": 0.2}},
        [_rec("q0_ro", "readout_freq_hz", 1.0, run_id="run-x"),
         _rec("q1_ro", "readout_freq_hz", 2.0, run_id="run-x"),
         _rec("q1_xy", "pi_amp", 0.2, run_id=None)],  # manual: not in the map
    )
    phys = live_sources(
        {"q1": {"t1_s": 3.0}},
        [_rec("q1", "t1_s", 3.0, run_id="run-y")],
    )
    merged = live_run_map(state, phys)
    assert merged == {"run-x": [("q0_ro", "readout_freq_hz"),
                                ("q1_ro", "readout_freq_hz")],
                      "run-y": [("q1", "t1_s")]}


def test_summarize_live_groups_by_field():
    pairs = [("q0_ro", "readout_freq_hz"), ("q1_ro", "readout_freq_hz"), ("q1", "t1_s")]
    assert summarize_live(pairs) == "readout_freq_hz (q0_ro,q1_ro), t1_s (q1)"
