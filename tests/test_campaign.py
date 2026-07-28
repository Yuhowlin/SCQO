"""Campaigns: repeat an experiment (or a bundle) N times and get statistics.

Two halves, deliberately separate:

* the PURE aggregator (``scqo.campaign``) over hand-built value lists — this is
  where the arithmetic is actually proven, because an end-to-end run over N
  simulated fits proves far less about mean/std/slope than a planted answer does;
* the ORCHESTRATION (``Session.run_campaign``) — counts, stamps, ordering,
  persistence and the refusal policies.

Note the pinned ``std == 0.0`` in the orchestration tests: the simulated backend
is deterministic in (experiment, targets) — ``scqo.experiments._sim.stable_seed``
— so a noiseless virtual chip reports exactly zero scatter. That is the correct
answer here and it is what lets these tests assert exact values.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from scqo import CampaignPlan, CampaignStep, Session, load_campaign_plan
from scqo.campaign import STAT_KEYS, aggregate, robust_summary, stderr_twin, summarize
from scqo.testing import (
    InMemoryDevice,
    SimulatedBackend,
    demo_components,
    demo_design,
    demo_vendor_state,
)

PLAN_TOML = """\
label = "t1_stability"
repeat = 2
skip_artifacts = true
tags = ["stability", "overnight"]

[defaults]
targets = ["q0"]

[[steps]]
experiment = "qubit_relaxation"

[[steps]]
experiment = "qubit_ramsey"
params = { frequency_detuning_hz = 2.0e5 }
"""


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


def _row(repeat_idx, elapsed_s, *steps):
    return {"repeat_idx": repeat_idx, "elapsed_s": elapsed_s, "steps": list(steps)}


def _step(experiment, fit, outcome="successful", step_idx=0):
    targets = sorted(set(fit) | {"q0"})
    return {"step_idx": step_idx, "experiment": experiment, "fit": fit,
            "outcomes": {t: outcome for t in targets}}


# --------------------------------------------------------------- the aggregator

def test_summarize_basic_statistics():
    stat = summarize([1.0, 2.0, 3.0, 4.0])
    assert stat["n"] == 4 and stat["n_missing"] == 0
    assert stat["mean"] == 2.5
    assert stat["std"] == pytest.approx(math.sqrt(5 / 3))  # ddof=1, not the population std
    assert stat["sem"] == pytest.approx(stat["std"] / 2)
    assert (stat["median"], stat["min"], stat["max"]) == (2.5, 1.0, 4.0)
    assert (stat["first"], stat["last"]) == (1.0, 4.0)
    assert stat["mad"] == pytest.approx(1.0)
    assert stat["mad_sigma"] == pytest.approx(1.4826)
    assert set(stat) == set(STAT_KEYS)


def test_summarize_needs_two_points_for_a_spread():
    stat = summarize([2.5])
    assert stat["n"] == 1 and stat["mean"] == 2.5
    assert stat["std"] is None and stat["sem"] is None and stat["scatter_ratio"] is None


def test_summarize_counts_none_and_nan_alike_as_missing():
    """Both roads reach here: in-memory fits carry NaN, persisted JSON carries null."""
    stat = summarize([1.0, None, float("nan"), 3.0, float("inf")])
    assert stat["n"] == 2 and stat["n_missing"] == 3 and stat["n_nonscalar"] == 0
    assert stat["mean"] == 2.0


def test_summarize_separates_array_valued_fits_from_failures():
    """The record-only flux diagnostics publish per-point ARRAYS in `fit`. Those fits
    SUCCEEDED — there is just no scalar to average — so counting them as missing
    would report a healthy measurement as a failure."""
    stat = summarize([[1.0, 2.0], [3.0, 4.0], None])
    assert (stat["n"], stat["n_missing"], stat["n_nonscalar"]) == (0, 1, 2)
    assert stat["mean"] is None


def test_the_three_counters_always_add_up():
    values = [1.0, None, [1.0, 2.0], float("nan"), 5.0]
    stat = summarize(values)
    assert stat["n"] + stat["n_missing"] + stat["n_nonscalar"] == len(values)


_COUNTERS = ("n", "n_missing", "n_nonscalar")


def test_summarize_all_missing_is_empty_not_a_crash():
    stat = summarize([None, float("nan")])
    assert stat["n"] == 0 and stat["n_missing"] == 2
    assert all(stat[key] is None for key in STAT_KEYS if key not in _COUNTERS)


def test_summarize_recovers_a_planted_drift():
    values = [10.0 + 0.5 * k for k in range(6)]  # +0.5 per 100 s
    stat = summarize(values, elapsed_s=[100.0 * k for k in range(6)])
    assert stat["slope_per_s"] == pytest.approx(0.005)


def test_slope_needs_three_points_and_a_real_time_axis():
    assert summarize([1.0, 2.0], elapsed_s=[0.0, 10.0])["slope_per_s"] is None
    assert summarize([1.0, 2.0, 3.0])["slope_per_s"] is None  # no elapsed at all
    assert summarize([1.0, 2.0, 3.0], elapsed_s=[5.0, 5.0, 5.0])["slope_per_s"] is None


def test_scatter_ratio_is_the_drift_vs_fit_noise_question():
    values = [10.0, 12.0, 14.0]          # std = 2.0
    stat = summarize(values, stderrs=[0.5, 0.5, 0.5], stderr_key="x_stderr")
    assert stat["mean_stderr"] == pytest.approx(0.5)
    assert stat["scatter_ratio"] == pytest.approx(stat["std"] / 0.5)
    assert stat["stderr_key"] == "x_stderr"


def test_stderr_twin_keeps_the_unit_suffix_last():
    assert stderr_twin("t1_s") == "t1_stderr_s"
    assert stderr_twin("t2_echo_s") == "t2_echo_stderr_s"
    assert stderr_twin("f_01_hz") == "f_01_stderr_hz"
    assert stderr_twin("fidelity_g") == "fidelity_g_stderr"


def test_aggregate_pairs_a_quantity_with_its_stderr_twin():
    rows = [_row(k, 100.0 * k, _step("qubit_relaxation",
                                     {"q0": {"t1_s": 4.0e-5 + 1e-6 * k, "t1_stderr_s": 8e-7}}))
            for k in range(4)]
    stats = aggregate(rows)["qubit_relaxation"]["q0"]
    assert stats["t1_s"]["n"] == 4
    assert stats["t1_s"]["stderr_key"] == "t1_stderr_s"
    assert stats["t1_s"]["mean_stderr"] == pytest.approx(8e-7)
    assert stats["t1_s"]["scatter_ratio"] == pytest.approx(stats["t1_s"]["std"] / 8e-7)
    assert stats["t1_s"]["slope_per_s"] == pytest.approx(1e-8)


def test_aggregate_leaves_scatter_ratio_none_without_a_twin():
    """t2_star_s is real: qubit_ramsey publishes no standard error for it."""
    rows = [_row(k, 0.0, _step("qubit_ramsey", {"q0": {"t2_star_s": 8e-6 + 1e-7 * k}}))
            for k in range(3)]
    stat = aggregate(rows)["qubit_ramsey"]["q0"]["t2_star_s"]
    assert stat["n"] == 3 and stat["std"] > 0
    assert stat["stderr_key"] is None and stat["scatter_ratio"] is None


def test_aggregate_counts_a_failed_repeat_as_missing_not_skipped():
    rows = [
        _row(0, 0.0, _step("qubit_relaxation", {"q0": {"t1_s": 4.0e-5}})),
        _row(1, 1.0, _step("qubit_relaxation", {}, outcome="failed")),
        _row(2, 2.0, _step("qubit_relaxation", {"q0": {"t1_s": 4.2e-5}})),
    ]
    stat = aggregate(rows)["qubit_relaxation"]["q0"]["t1_s"]
    assert (stat["n"], stat["n_missing"]) == (2, 1)
    assert stat["mean"] == pytest.approx(4.1e-5)


def test_aggregate_takes_the_union_of_quantity_keys():
    """A quantity that only sometimes resolves still gets a row."""
    rows = [
        _row(0, 0.0, _step("qubit_ramsey", {"q0": {"t2_star_s": 8e-6}})),
        _row(1, 1.0, _step("qubit_ramsey", {"q0": {"t2_star_s": 9e-6, "beat_hz": 1e5}})),
    ]
    stats = aggregate(rows)["qubit_ramsey"]["q0"]
    assert set(stats) == {"t2_star_s", "beat_hz"}
    assert (stats["beat_hz"]["n"], stats["beat_hz"]["n_missing"]) == (1, 1)


def test_aggregate_keeps_experiments_and_targets_apart():
    rows = [_row(0, 0.0,
                 _step("qubit_relaxation", {"q0": {"t1_s": 4e-5}, "q1": {"t1_s": 5e-5}}),
                 _step("qubit_echo", {"q0": {"t2_echo_s": 3e-5}}, step_idx=1))]
    stats = aggregate(rows)
    assert set(stats) == {"qubit_relaxation", "qubit_echo"}
    assert set(stats["qubit_relaxation"]) == {"q0", "q1"}
    assert stats["qubit_relaxation"]["q1"]["t1_s"]["mean"] == pytest.approx(5e-5)


def test_robust_summary_flags_a_planted_outlier():
    out = robust_summary([1.0, 1.01, 0.99, 1.02, 5.0])
    assert out["n_outliers"] == 1 and out["outlier_indices"] == [4]
    assert out["median"] == pytest.approx(1.01)
    assert out["mean"] == pytest.approx(1.005)  # the outlier is excluded


def test_robust_summary_flags_nothing_below_four_points():
    """scqat's mad_outliers contract, not a choice made here."""
    assert robust_summary([1.0, 1.0, 9.0])["n_outliers"] == 0


# ---------------------------------------------------------------------- the plan

def test_plan_refuses_a_non_slug_label():
    with pytest.raises(ValidationError, match="letters, digits"):
        CampaignPlan(label="t1 stability!", repeat=1,
                     steps=[CampaignStep(experiment="qubit_relaxation")])


def test_plan_refuses_an_open_ended_campaign_with_no_budget():
    with pytest.raises(ValidationError, match="max_duration_s"):
        CampaignPlan(label="forever", steps=[CampaignStep(experiment="qubit_relaxation")])
    # ... and accepts it once a budget is given
    assert CampaignPlan(label="overnight", max_duration_s=60.0,
                        steps=[CampaignStep(experiment="qubit_relaxation")]).repeat is None


@pytest.mark.parametrize("kwargs", [
    {"steps": []},                                  # a campaign with nothing to do
    {"repeat": 0},                                  # gt=0
    {"typo_field": 1},                              # extra="forbid"
])
def test_plan_rejects_malformed_input(kwargs):
    base = {"label": "x", "repeat": 1, "steps": [{"experiment": "qubit_relaxation"}]}
    with pytest.raises(ValidationError):
        CampaignPlan(**{**base, **kwargs})


def test_plan_merges_defaults_under_step_params():
    plan = CampaignPlan(label="x", repeat=1, defaults={"targets": ["q0"], "num_averages": 10},
                        steps=[{"experiment": "qubit_relaxation", "params": {"num_averages": 99}}])
    assert plan.step_params(plan.steps[0]) == {"targets": ["q0"], "num_averages": 99}
    assert plan.targets() == ["q0"]


def test_load_campaign_plan_reads_toml_with_a_bom(tmp_path):
    path = tmp_path / "plan.toml"
    path.write_text(PLAN_TOML, encoding="utf-8-sig")  # what PowerShell 5.1 writes
    plan = load_campaign_plan(path)
    assert plan.label == "t1_stability" and plan.repeat == 2
    assert plan.experiments == ["qubit_relaxation", "qubit_ramsey"]
    assert plan.step_params(plan.steps[1])["frequency_detuning_hz"] == 2.0e5


# ------------------------------------------------------------- the orchestration

def test_one_step_campaign_collects_a_fit_per_repeat(session):
    out = session.run_campaign(CampaignPlan(
        label="t1_stability", repeat=3, defaults={"targets": ["q0"]},
        steps=[{"experiment": "qubit_relaxation"}]))

    assert out["status"] == "complete" and out["stop_reason"] == "repeat count reached"
    assert out["repeat_done"] == 3 and out["repeats_failed"] == 0
    stat = out["statistics"]["qubit_relaxation"]["q0"]["t1_s"]
    assert (stat["n"], stat["n_missing"]) == (3, 0)
    assert stat["mean"] > 0
    # The simulated chip is noiseless and deterministic in (experiment, targets),
    # so zero scatter is the CORRECT answer — see the module docstring.
    assert stat["std"] == 0.0

    children = session.campaign_runs(out["campaign_id"])
    assert len(children) == 3
    assert len({c["run_id"] for c in children}) == 3
    assert [(c["repeat_idx"], c["step_idx"]) for c in children] == [(0, 0), (1, 0), (2, 0)]
    assert {c["campaign"] for c in children} == {out["campaign_id"]}
    assert len({c["started_at"] for c in children}) == 3  # distinct timestamps


def test_bundle_campaign_is_interleaved_not_grouped(session):
    """The outer loop MUST be the repeat: T1[k] and T2*[k] have to share an epoch."""
    steps = ["qubit_relaxation", "qubit_ramsey", "qubit_echo", "single_shot_readout"]
    out = session.run_campaign(CampaignPlan(
        label="bundle", repeat=3, defaults={"targets": ["q0"]}, skip_artifacts=True,
        steps=[{"experiment": name} for name in steps]))

    assert out["repeat_done"] == 3
    order = [c["experiment"] for c in session.campaign_runs(out["campaign_id"])]
    assert order == steps * 3

    stats = out["statistics"]
    # the four quantities the feature exists to produce
    assert stats["qubit_relaxation"]["q0"]["t1_s"]["n"] == 3
    assert stats["qubit_ramsey"]["q0"]["t2_star_s"]["n"] == 3
    assert stats["qubit_echo"]["q0"]["t2_echo_s"]["n"] == 3
    assert stats["single_shot_readout"]["q0"]["p_e_given_g"]["n"] == 3


def test_campaign_persists_a_manifest_and_a_repeat_log(session, tmp_path):
    out = session.run_campaign(CampaignPlan(
        label="t1_stability", repeat=2, defaults={"targets": ["q0"]},
        steps=[{"experiment": "qubit_relaxation"}]))

    campaign_dir = Path(out["data_path"])
    # a sibling of the day folders, NOT inside one: a campaign can cross midnight
    assert campaign_dir.parent.name == "campaigns"
    assert campaign_dir.parent.parent.name == "chipT"
    manifest = json.loads((campaign_dir / "campaign.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "complete" and manifest["repeat_done"] == 2
    assert manifest["cooldown"] == "cd1" and manifest["setup"] == "sim"
    assert manifest["statistics"]["qubit_relaxation"]["q0"]["t1_s"]["n"] == 2

    repeats = [json.loads(line) for line in
               (campaign_dir / "repeats.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["repeat_idx"] for r in repeats] == [0, 1]
    # the skeleton carries run_ids and outcomes but NEVER fit values: the child's
    # result.json stays the one truth for every number.
    assert all("fit" not in step for r in repeats for step in r["steps"])
    assert all(step["run_id"] for r in repeats for step in r["steps"])

    assert [c["label"] for c in session.find_campaigns()] == ["t1_stability"]
    assert len(session.load_campaign(out["campaign_id"])["repeats"]) == 2


def test_campaign_survives_an_index_rebuild(session):
    out = session.run_campaign(CampaignPlan(
        label="t1_stability", repeat=2, defaults={"targets": ["q0"]},
        steps=[{"experiment": "qubit_relaxation"}]))
    store = session.datastore
    (store.data_root / "index.sqlite").unlink()

    from scqo import DataStore

    rebuilt = DataStore(store.data_root, device_name="chipT")
    assert rebuilt.reindex() == 2  # the children
    found = rebuilt.find_campaigns()
    assert [c["campaign_id"] for c in found] == [out["campaign_id"]]
    assert found[0]["statistics"]["qubit_relaxation"]["q0"]["t1_s"]["n"] == 2
    children = rebuilt.campaign_runs(out["campaign_id"])
    assert [(c["repeat_idx"], c["step_idx"]) for c in children] == [(0, 0), (1, 0)]


def test_campaign_runs_is_not_truncated_like_find_runs(session):
    """find_runs defaults to limit=50 and newest-first — wrong on both counts here."""
    out = session.run_campaign(CampaignPlan(
        label="long", repeat=55, defaults={"targets": ["q0"]}, skip_artifacts=True,
        steps=[{"experiment": "qubit_relaxation"}]))
    assert out["repeat_done"] == 55
    assert len(session.campaign_runs(out["campaign_id"])) == 55
    assert len(session.find_runs(campaign=out["campaign_id"])) == 50  # the documented cap


def test_default_update_mode_touches_nothing(session):
    out = session.run_campaign(CampaignPlan(
        label="t1_stability", repeat=2, defaults={"targets": ["q0"]},
        steps=[{"experiment": "qubit_relaxation"}]))
    assert session.physical_state() == {}
    assert all(not c["suggestions"] for c in session.campaign_runs(out["campaign_id"]))


def test_suggest_mode_is_refused_for_multiple_repeats(session):
    plan = CampaignPlan(label="t1_stability", repeat=3, defaults={"targets": ["q0"]},
                        steps=[{"experiment": "qubit_relaxation"}])
    with pytest.raises(ValueError, match="stale"):
        session.run_campaign(plan, update="suggest")


def test_preflight_refuses_a_bad_plan_before_touching_anything(session, tmp_path):
    out = session.run_campaign(CampaignPlan(
        label="typo", repeat=100, defaults={"targets": ["q0"]},
        steps=[{"experiment": "qubit_relaxation", "params": {"num_points": "many"}},
               {"experiment": "qubit_echo", "params": {"targets": ["not_a_qubit"]}}]))

    assert out["status"] == "failed" and out["campaign_id"] is None
    assert len(out["problems"]) == 2
    assert "step 0 (qubit_relaxation)" in out["problems"][0]
    assert "step 1 (qubit_echo)" in out["problems"][1]
    assert "not in this device's roster" in out["problems"][1]
    # nothing was created: no run folders, no campaign folder
    assert not list((tmp_path / "data" / "chipT").glob("*"))


def test_check_campaign_is_the_same_gate_without_running(session, tmp_path):
    plan = CampaignPlan(label="x", repeat=1, defaults={"targets": ["nope"]},
                        steps=[{"experiment": "qubit_relaxation"}])
    assert session.check_campaign(plan)
    assert not list((tmp_path / "data" / "chipT").glob("*"))
    good = CampaignPlan(label="x", repeat=1, defaults={"targets": ["q0"]},
                        steps=[{"experiment": "qubit_relaxation"}])
    assert session.check_campaign(good) == []


def test_skip_artifacts_drops_the_analysis_folder_only(session):
    out = session.run_campaign(CampaignPlan(
        label="lean", repeat=1, defaults={"targets": ["q0"]}, skip_artifacts=True,
        steps=[{"experiment": "qubit_relaxation"}]))
    run_dir = Path(session.load_run(
        session.campaign_runs(out["campaign_id"])[0]["run_id"])["path"])
    assert not (run_dir / "analysis").exists()
    assert (run_dir / "dataset.nc").is_file() and (run_dir / "result.json").is_file()


def test_on_error_stop_halts_after_a_washout(session, monkeypatch):
    calls = {"n": 0}
    real_run = Session.run

    def flaky(self, experiment, params, update="suggest", **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            return {"outcomes": {"q0": "failed"}, "fit": {}, "error": "boom",
                    "suggestions": []}
        return real_run(self, experiment, params, update, **kwargs)

    monkeypatch.setattr(Session, "run", flaky)
    out = session.run_campaign(CampaignPlan(
        label="flaky", repeat=5, defaults={"targets": ["q0"]}, on_error="stop",
        steps=[{"experiment": "qubit_relaxation"}]))
    assert out["status"] == "stopped" and out["repeat_done"] == 2
    assert out["repeats_failed"] == 1
    assert "on_error" in out["stop_reason"]


def test_consecutive_failure_breaker_trips(session, monkeypatch):
    monkeypatch.setattr(Session, "run", lambda *a, **k: {
        "outcomes": {"q0": "failed"}, "fit": {}, "error": "instrument gone",
        "suggestions": []})
    out = session.run_campaign(CampaignPlan(
        label="storm", repeat=100, defaults={"targets": ["q0"]},
        max_consecutive_failures=3, steps=[{"experiment": "qubit_relaxation"}]))
    assert out["status"] == "stopped" and out["repeat_done"] == 3
    assert "max_consecutive_failures" in out["stop_reason"]


def test_a_partial_failure_is_not_a_failed_repeat(session, monkeypatch):
    """One flaky step among several must not trip on_error on a healthy run."""
    real_run = Session.run

    def flaky(self, experiment, params, update="suggest", **kwargs):
        if experiment == "qubit_echo":
            return {"outcomes": {"q0": "failed"}, "fit": {}, "error": "bad fit",
                    "suggestions": []}
        return real_run(self, experiment, params, update, **kwargs)

    monkeypatch.setattr(Session, "run", flaky)
    out = session.run_campaign(CampaignPlan(
        label="mixed", repeat=3, defaults={"targets": ["q0"]}, on_error="stop",
        steps=[{"experiment": "qubit_relaxation"}, {"experiment": "qubit_echo"}]))
    assert out["status"] == "complete" and out["repeat_done"] == 3
    assert out["repeats_failed"] == 0
    # the flaky step shows up as missing values, which is what the operator needs
    assert out["statistics"]["qubit_echo"]["q0"] == {}
    assert out["statistics"]["qubit_relaxation"]["q0"]["t1_s"]["n"] == 3


def test_interrupt_during_the_cadence_wait_still_finalizes(session, monkeypatch):
    """The regression for the escape hatch: `time.sleep` sat outside every guard,
    so Ctrl-C there propagated out of run_campaign and the manifest was left
    status="running" with no ended_at — FOREVER. And with period_s set, the sleep
    is where a campaign spends most of its wall clock, so it is the likely Ctrl-C."""
    def interrupt(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", interrupt)
    out = session.run_campaign(CampaignPlan(
        label="sleepirq", repeat=4, period_s=5, defaults={"targets": ["q0"]},
        skip_artifacts=True, steps=[{"experiment": "qubit_relaxation"}]))

    assert out["status"] == "stopped" and out["stop_reason"] == "interrupted (Ctrl-C)"
    assert out["repeat_done"] == 1  # repeat 0 ran; the wait before repeat 1 was cut
    manifest = json.loads(
        (Path(out["data_path"]) / "campaign.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "stopped" and manifest["ended_at"]


def test_interrupt_mid_repeat_keeps_the_steps_that_ran(session, monkeypatch):
    """A half-walked repeat's completed steps already persisted their own run
    folders and are already campaign-stamped; dropping them from the manifest would
    leave campaign_runs() returning more children than repeat_done accounts for."""
    real_run = Session.run
    calls = {"n": 0}

    def interrupt_on_the_fifth(self, experiment, params, update="suggest", **kwargs):
        calls["n"] += 1
        if calls["n"] == 5:  # repeat 1, step 1 of a 3-step plan
            raise KeyboardInterrupt
        return real_run(self, experiment, params, update, **kwargs)

    monkeypatch.setattr(Session, "run", interrupt_on_the_fifth)
    steps = ["qubit_relaxation", "qubit_ramsey", "qubit_echo"]
    out = session.run_campaign(CampaignPlan(
        label="midirq", repeat=4, defaults={"targets": ["q0"]}, skip_artifacts=True,
        steps=[{"experiment": name} for name in steps]))

    assert out["status"] == "stopped"
    assert out["repeat_done"] == 1 and out["repeats_partial"] == 1
    assert [(r["repeat_idx"], len(r["steps"]), r.get("partial", False))
            for r in out["repeats"]] == [(0, 3, False), (1, 1, True)]
    # the partial repeat's T1 IS in the statistics — nothing measured is dropped
    assert out["statistics"]["qubit_relaxation"]["q0"]["t1_s"]["n"] == 2
    assert out["statistics"]["qubit_ramsey"]["q0"]["t2_star_s"]["n"] == 1
    # ...and the child count reconciles: one whole repeat (3) + one partial step
    assert len(session.campaign_runs(out["campaign_id"])) == 4


def test_an_unexpected_error_still_leaves_a_readable_manifest(session, monkeypatch):
    """A crash during an overnight run must not also lose the record of it."""
    import scqo.campaign as module

    def boom(_rows):
        raise RuntimeError("aggregate exploded")

    # run_campaign imports aggregate INSIDE the function, so the import resolves
    # the module attribute on every call and patching the source module lands.
    monkeypatch.setattr(module, "aggregate", boom)
    out = session.run_campaign(CampaignPlan(
        label="crash", repeat=3, defaults={"targets": ["q0"]}, skip_artifacts=True,
        steps=[{"experiment": "qubit_relaxation"}]))

    assert out["status"] == "failed"
    assert "RuntimeError: aggregate exploded" in out["stop_reason"]
    manifest = json.loads(
        (Path(out["data_path"]) / "campaign.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed" and manifest["ended_at"]


def test_keyboard_interrupt_finalizes_instead_of_raising(session, monkeypatch):
    real_run = Session.run
    seen = {"n": 0}

    def interrupt_on_second(self, experiment, params, update="suggest", **kwargs):
        seen["n"] += 1
        if seen["n"] == 2:
            raise KeyboardInterrupt
        return real_run(self, experiment, params, update, **kwargs)

    monkeypatch.setattr(Session, "run", interrupt_on_second)
    out = session.run_campaign(CampaignPlan(
        label="stopme", repeat=5, defaults={"targets": ["q0"]},
        steps=[{"experiment": "qubit_relaxation"}]))
    assert out["status"] == "stopped" and out["stop_reason"] == "interrupted (Ctrl-C)"
    assert out["repeat_done"] == 1
    manifest = json.loads(
        (Path(out["data_path"]) / "campaign.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "stopped" and manifest["ended_at"]


def test_max_duration_stops_an_open_ended_campaign(session):
    """The budget gates CONTINUING: repeat 0 always runs, however small it is."""
    out = session.run_campaign(CampaignPlan(
        label="overnight", max_duration_s=0.001, defaults={"targets": ["q0"]},
        steps=[{"experiment": "qubit_relaxation"}]))
    assert out["status"] == "stopped" and "max_duration_s" in out["stop_reason"]
    assert out["repeat_planned"] is None and out["repeat_done"] == 1
    assert out["statistics"]["qubit_relaxation"]["q0"]["t1_s"]["n"] == 1


def test_period_gates_the_repeat_start(session):
    out = session.run_campaign(CampaignPlan(
        label="cadence", repeat=3, period_s=0.25, defaults={"targets": ["q0"]},
        skip_artifacts=True, steps=[{"experiment": "qubit_relaxation"}]))
    assert out["repeat_done"] == 3
    starts = [r["elapsed_s"] for r in out["repeats"]]
    assert starts[1] >= 0.25 and starts[2] >= 0.5


def test_a_single_repeat_reproduces_a_standalone_run(session):
    plain = session.run("qubit_relaxation", {"targets": ["q0"]}, update="none")
    out = session.run_campaign(CampaignPlan(
        label="one", repeat=1, defaults={"targets": ["q0"]},
        steps=[{"experiment": "qubit_relaxation"}]))
    assert out["statistics"]["qubit_relaxation"]["q0"]["t1_s"]["mean"] == pytest.approx(
        plain["fit"]["q0"]["t1_s"])


# ------------------------------------------------------------------- progress

def _plan_obj(**kwargs) -> CampaignPlan:
    base = {"label": "p", "repeat": 3, "defaults": {"targets": ["q0"]},
            "steps": [{"experiment": "qubit_relaxation"}]}
    return CampaignPlan(**{**base, **kwargs})


def test_progress_events_fire_in_order(session):
    events: list[dict] = []
    session.run_campaign(
        CampaignPlan(label="ev", repeat=2, defaults={"targets": ["q0"]},
                     skip_artifacts=True,
                     steps=[{"experiment": "qubit_relaxation"},
                            {"experiment": "qubit_echo"}]),
        on_progress=events.append)

    assert [e["kind"] for e in events] == [
        "repeat_start", "step_done", "step_done", "repeat_done",
        "repeat_start", "step_done", "step_done", "repeat_done",
    ]
    assert [e["repeat_idx"] for e in events] == [0, 0, 0, 0, 1, 1, 1, 1]
    steps = [e for e in events if e["kind"] == "step_done"]
    assert [e["step_idx"] for e in steps] == [0, 1, 0, 1]
    assert all(e["duration_s"] >= 0 for e in steps)
    assert all(e["ok"] for e in steps)
    # repeat_start must carry what an ETA needs, and repeat 0 has no history yet
    assert events[0]["durations_s"] == [] and events[0]["repeat_planned"] == 2
    assert len(events[4]["durations_s"]) == 1


def test_progress_stop_is_honoured_only_at_a_repeat_boundary(session):
    """Stopping mid-repeat would leave a half-walked bundle whose quantities no
    longer share a drift epoch — the invariant interleaving exists to protect."""
    out = session.run_campaign(
        _plan_obj(label="stopdone", repeat=5, skip_artifacts=True),
        on_progress=lambda e: "stop" if e["kind"] == "repeat_done" else None)
    assert out["status"] == "stopped" and out["repeat_done"] == 1
    assert "on_progress" in out["stop_reason"]

    out = session.run_campaign(
        _plan_obj(label="stopstep", repeat=2, skip_artifacts=True),
        on_progress=lambda e: "stop" if e["kind"] == "step_done" else None)
    assert out["status"] == "complete" and out["repeat_done"] == 2


def test_a_broken_progress_callback_never_kills_the_campaign(session, capsys):
    def boom(event):
        raise RuntimeError("boom")

    out = session.run_campaign(
        _plan_obj(label="broken", repeat=2, skip_artifacts=True), on_progress=boom)
    assert out["status"] == "complete" and out["repeat_done"] == 2
    # reported ONCE, then dropped
    assert capsys.readouterr().err.count("progress callback failed") == 1


def test_cadence_wait_is_announced_before_sleeping(session):
    events: list[dict] = []
    session.run_campaign(
        _plan_obj(label="cad", repeat=2, period_s=0.4, skip_artifacts=True),
        on_progress=events.append)
    waits = [e for e in events if e["kind"] == "cadence_wait"]
    assert waits and waits[0]["wait_s"] > 0
    # ...and it precedes the repeat_start whose timestamp it delays
    assert events.index(waits[0]) < [e["kind"] for e in events][4:].index("repeat_start") + 4


def test_progress_line_for_a_successful_step():
    from scqo.cli._campaign import progress_lines

    lines = progress_lines(
        {"kind": "step_done", "repeat_idx": 0, "step_idx": 0, "n_steps": 1,
         "experiment": "qubit_relaxation", "run_id": "r1", "duration_s": 2.4,
         "outcomes": {"q0": "successful"}, "error": None, "ok": True,
         "fit": {"q0": {"t1_s": 4.131e-5, "t1_stderr_s": 8e-7,
                        "amplitude": -2.2, "offset": 0.15}}},
        _plan_obj())
    assert len(lines) == 1
    line = lines[0]
    assert "qubit_relaxation" in line and "q0" in line and "ok" in line
    assert "t1_s=4.131e-05" in line and line.endswith("2.4s")
    # uncatalogued curve-fit bookkeeping must not eat the width
    assert "amplitude" not in line and "offset" not in line


def test_progress_line_headline_drops_knobs_and_keeps_physics():
    """A knob in a fit dict is the run's standing setting: in an update="none"
    campaign it is constant by construction, so it cannot drift."""
    from scqo.cli._campaign import _headline

    ramsey = _headline({"t2_star_s": 8.9e-6, "f_01_hz": 3.8e9,
                        "drive_freq_hz": 3.8e9, "old_drive_freq_hz": 3.8e9,
                        "detuning_error_hz": -6754.0})
    assert "t2_star_s=" in ramsey and "f_01_hz=" in ramsey
    assert "drive_freq_hz" not in ramsey and "detuning_error_hz" not in ramsey

    ssro = _headline({"mean_e_i": 4.9, "mean_g_q": -0.1, "p_e_given_g": 0.0425,
                      "readout_fidelity": 0.936})
    assert "p_e_given_g=0.0425" in ssro and "mean_e_i" not in ssro


def test_progress_line_falls_back_for_an_uncatalogued_fit():
    """A brand-new experiment writing nothing catalogued still gets a line."""
    from scqo.cli._campaign import _headline

    out = _headline({"widget_gain": 1.25, "widget_gain_stderr": 0.01})
    assert "widget_gain=1.25" in out
    assert "widget_gain_stderr" not in out  # the stderr twin is not its own column


def test_progress_line_for_a_failed_and_a_multi_target_step():
    from scqo.cli._campaign import progress_lines

    failed = progress_lines(
        {"kind": "step_done", "repeat_idx": 0, "step_idx": 0, "n_steps": 1,
         "experiment": "qubit_ramsey", "run_id": None, "duration_s": 0.2,
         "outcomes": {"q0": "failed"}, "fit": {}, "ok": False,
         "error": "ValueError: fit did not converge\nsecond line dropped"},
        _plan_obj())
    assert len(failed) == 1 and "FAILED" in failed[0]
    assert "fit did not converge" in failed[0] and "second line" not in failed[0]

    both = progress_lines(
        {"kind": "step_done", "repeat_idx": 0, "step_idx": 0, "n_steps": 1,
         "experiment": "qubit_relaxation", "run_id": "r1", "duration_s": 1.0,
         "outcomes": {"q0": "successful", "q1": "failed"}, "ok": True,
         "error": "q1 fit failed", "fit": {"q0": {"t1_s": 4e-5}}},
        _plan_obj())
    assert len(both) == 2
    assert "q0" in both[0] and "ok" in both[0]
    assert "q1" in both[1] and "FAILED" in both[1]

    # a hard failure that never reached a target still reports
    hard = progress_lines(
        {"kind": "step_done", "repeat_idx": 0, "step_idx": 0, "n_steps": 1,
         "experiment": "qubit_echo", "run_id": None, "duration_s": 0.0,
         "outcomes": {}, "fit": {}, "ok": False, "error": "OSError: disk full"},
        _plan_obj())
    assert len(hard) == 1 and "FAILED" in hard[0] and "disk full" in hard[0]


def test_progress_repeat_start_and_cadence_lines():
    from scqo.cli._campaign import progress_lines

    first = progress_lines(
        {"kind": "repeat_start", "repeat_idx": 0, "repeat_planned": 100,
         "n_steps": 1, "started_at": "2026-07-27T20:15:03.472+08:00",
         "elapsed_s": 0.0, "overran_by_s": 0.0, "durations_s": []}, _plan_obj())
    assert first == ["# repeat    1/100  started 20:15:03"]  # no ETA without history

    later = progress_lines(
        {"kind": "repeat_start", "repeat_idx": 1, "repeat_planned": 100,
         "n_steps": 1, "started_at": "2026-07-27T20:20:03.000+08:00",
         "elapsed_s": 300.0, "overran_by_s": 0.0, "durations_s": [10.0]},
        _plan_obj())
    assert "eta " in later[0] and "repeat    2/100" in later[0]

    late = progress_lines(
        {"kind": "repeat_start", "repeat_idx": 1, "repeat_planned": 3, "n_steps": 1,
         "started_at": "2026-07-27T20:20:03.000+08:00", "elapsed_s": 1.0,
         "overran_by_s": 3.5, "durations_s": [10.0]}, _plan_obj())
    assert "late by 3.5s" in late[0]

    assert progress_lines({"kind": "cadence_wait", "repeat_idx": 1, "wait_s": 297.0},
                          _plan_obj(period_s=300.0)) == [
        "# waiting 4m57s for the next repeat (period_s=300)"]

    # repeat_done adds nothing the next repeat_start does not already carry
    assert progress_lines({"kind": "repeat_done", "repeat_idx": 0}, _plan_obj()) == []


def test_open_ended_campaign_gets_an_eta_from_its_budget():
    from scqo.cli._campaign import progress_lines

    lines = progress_lines(
        {"kind": "repeat_start", "repeat_idx": 3, "repeat_planned": None,
         "n_steps": 1, "started_at": "2026-07-27T20:15:03+08:00",
         "elapsed_s": 600.0, "overran_by_s": 0.0, "durations_s": [10.0, 10.0]},
        _plan_obj(repeat=None, max_duration_s=3600.0))
    assert "eta " in lines[0] and "+50m00s" in lines[0]


def test_duration_formatting_is_ascii_and_unitful():
    from scqo.cli._campaign import _fmt_duration

    assert _fmt_duration(2.44) == "2.4s"
    assert _fmt_duration(252) == "4m12s"
    assert _fmt_duration(29220) == "8h07m"
    assert _fmt_duration(-1) == "0.0s"


@pytest.mark.parametrize("event", [
    {"kind": "repeat_start", "repeat_idx": 1, "repeat_planned": 9, "n_steps": 1,
     "started_at": "2026-07-27T20:15:03+08:00", "elapsed_s": 5.0,
     "overran_by_s": 1.0, "durations_s": [4.0]},
    {"kind": "cadence_wait", "repeat_idx": 1, "wait_s": 12.0},
    {"kind": "step_done", "repeat_idx": 0, "step_idx": 0, "n_steps": 1,
     "experiment": "qubit_relaxation", "run_id": "r", "duration_s": 1.0,
     "outcomes": {"q0": "successful"}, "fit": {"q0": {"t1_s": 4e-5}},
     "error": None, "ok": True},
])
def test_every_progress_line_is_ascii(event):
    """These reach lab consoles in whatever codepage Windows picked."""
    from scqo.cli._campaign import progress_lines

    for line in progress_lines(event, _plan_obj(period_s=30.0)):
        assert line.isascii(), line
        assert "\r" not in line  # QM's vendor bar already owns \r on stdout


def test_campaign_without_a_datastore_still_returns_statistics(tmp_path):
    roster = demo_components()
    design = demo_design(roster)
    vendor = InMemoryDevice(roster, demo_vendor_state(roster, design))
    sess = Session(SimulatedBackend(vendor), roster, design=design,
                   scqo_dir=tmp_path / "scqo")  # no data_root
    out = sess.run_campaign(CampaignPlan(
        label="nowhere", repeat=2, defaults={"targets": ["q0"]},
        steps=[{"experiment": "qubit_relaxation"}]))
    assert out["campaign_id"] is None and out["repeat_done"] == 2
    assert out["statistics"]["qubit_relaxation"]["q0"]["t1_s"]["n"] == 2
    assert sess.find_campaigns() == []
