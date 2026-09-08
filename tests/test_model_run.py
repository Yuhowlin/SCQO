"""End-to-end run flow on the greenfield stack: catalog -> run -> suggest ->
accept, with the real DataStore, the simulated backend, and the ported
reference experiment. The offline twin of the bench workflow."""

import pytest

from scqo import Session
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
        setup_name="sim", cooldown_id="cd1")


def test_catalog_lists_the_ported_experiment(session):
    entries = {e["name"]: e for e in session.catalog()}
    entry = entries["resonator_spectroscopy"]
    assert entry["required_operations"] == ["readout"]
    assert "start_readout_detuning_hz" in entry["parameters_schema"]["properties"]


def test_run_suggest_accept_roundtrip(session):
    out = session.run("resonator_spectroscopy",
                      {"targets": ["q0", "q1"], "num_readout_freq_points": 61})
    assert out.get("error") is None
    assert out["run_id"] and out["data_path"]
    proposed = {(s["entity"], s["field"], s["role"])
                for s in out["suggestions"]}
    # ONE fit, THREE homes, and the role is what routes each: the operating
    # choice and the wait derived from the linewidth are channel KNOBS (pushed),
    # the linewidth and dip position are resonator FACTS (never pushed).
    assert proposed == {
        ("q0_ro", "readout_freq_hz", "knob"), ("q0_ro", "readout_depletion_s", "knob"),
        ("q0_res", "f_dress0_hz", "fact"), ("q0_res", "kappa_tot_hz", "fact"),
        ("q1_ro", "readout_freq_hz", "knob"), ("q1_ro", "readout_depletion_s", "knob"),
        ("q1_res", "f_dress0_hz", "fact"), ("q1_res", "kappa_tot_hz", "fact"),
    }
    # nothing applied yet
    assert session.physical_state() == {}
    summary = session.accept(out["run_id"])
    assert not summary["errors"] and summary["pending_left"] == 0
    assert session.physical_state()["q0_res"]["kappa_tot_hz"] > 0
    fitted = session.device_state()["q0_ro"]["readout_freq_hz"]
    assert abs(fitted - 5.95e9) < 5e6                 # near the design f_r
    assert session.history()[-1]["run_id"] == out["run_id"]


def test_run_apply_mode_is_immediate(session):
    out = session.run("resonator_spectroscopy", {"targets": ["q0"]},
                      update="apply")
    assert out.get("error") is None
    assert session.physical_state()["q0_res"]["f_dress0_hz"] == pytest.approx(
        session.device_state()["q0_ro"]["readout_freq_hz"])
    record = session.load_run(out["run_id"])["record"]
    assert record["updated_device"] is True


def test_run_none_mode_captures_nothing(session):
    out = session.run("resonator_spectroscopy", {"targets": ["q0"]},
                      update="none")
    assert out["suggestions"] == []
    assert session.physical_state() == {}


def test_target_gate_refuses_before_hardware(session):
    out = session.run("resonator_spectroscopy", {"targets": ["q0_q1"]})
    assert "target validation refused" in out["error"]
    assert "is kind 'qubit_pair'" in out["error"]
    out = session.run("resonator_spectroscopy", {"targets": ["ghost"]})
    assert "not in this device's roster" in out["error"]


def test_invalid_params_return_structured_not_raise(session):
    out = session.run("resonator_spectroscopy",
                      {"targets": ["q0"], "num_readout_freq_points": 1})
    assert "invalid parameters" in out["error"]
    assert out.get("run_id") is None                  # nothing persisted
    # EVERY return of run() carries "suggestions", so a caller in a loop
    # (run_campaign) can read it blind — this early return used to omit it.
    assert out["suggestions"] == []


def test_run_campaign_repeats_and_summarizes(session):
    """The run-flow twin of the campaign suite: N runs, one manifest, statistics."""
    out = session.run_campaign({
        "label": "t1_stability", "repeat": 3, "defaults": {"targets": ["q0"]},
        "steps": [{"experiment": "resonator_spectroscopy", "params": {"num_readout_freq_points": 61}}],
    })
    assert out["status"] == "complete" and out["repeat_done"] == 3
    assert out["statistics"]["resonator_spectroscopy"]["q0"]["f_dress0_hz"]["n"] == 3
    children = session.campaign_runs(out["campaign_id"])
    assert [c["repeat_idx"] for c in children] == [0, 1, 2]
    # update defaults to "none" for a campaign: no pending suggestions to go stale
    assert session.physical_state() == {}
    assert all(not c["suggestions"] for c in children)


def test_design_seeded_anchor_tags_the_run(tmp_path):
    roster = demo_components()
    design = demo_design(roster)
    state = demo_vendor_state(roster, design)
    del state["q0_ro"]["readout_freq_hz"]             # fresh chip: no standing
    vendor = InMemoryDevice(roster, state)
    session = Session(SimulatedBackend(vendor), roster, design=design,
                      scqo_dir=tmp_path / "scqo", data_root=tmp_path / "d",
                      device_name="chipT", setup_name="sim",
                      cooldown_id="cd1")
    out = session.run("resonator_spectroscopy", {"targets": ["q0"]})
    assert out.get("error") is None
    record = session.load_run(out["run_id"])["record"]
    assert "seeded:q0_res.f_dress0_hz" in record["tags"]


def test_runs_are_findable(session):
    out = session.run("resonator_spectroscopy", {"targets": ["q0"]})
    found = session.find_runs(experiment="resonator_spectroscopy")
    assert found and found[0]["run_id"] == out["run_id"]
    assert session.find_runs(pending=True)            # undecided suggestions
