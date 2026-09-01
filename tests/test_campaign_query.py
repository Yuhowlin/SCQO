"""Tests for the high-level Campaign Query Engine (scqo.campaign_query)."""

from __future__ import annotations

import pytest

from scqo.campaign import CampaignPlan
from scqo.campaign_query import get_latest_metric_stat, normalize_target_name, query_campaign_statistics
from scqo.testing import (
    InMemoryDevice,
    SimulatedBackend,
    demo_components,
    demo_design,
    demo_vendor_state,
)
from scqo.session import Session


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


def test_normalize_target_name():
    """Rider suffixes strip; a COMPOSITE does not — q1_q2 is a pair, and
    folding it into q1 files two-qubit numbers under one qubit."""
    assert normalize_target_name("q1") == "q1"
    assert normalize_target_name("q1_xy") == "q1"
    assert normalize_target_name("q1_ro") == "q1"
    assert normalize_target_name("q1_z") == "q1"
    assert normalize_target_name("Q2_RES") == "q2"
    assert normalize_target_name("") == ""
    # composites normalise to THEMSELVES — the pair experiments (qc_n_swap_amp,
    # qc_n_stark_amp, pair_zz_coupler) all target names of this shape
    assert normalize_target_name("q1_q2") == "q1_q2"
    assert normalize_target_name("coupler_q1_q2") == "coupler_q1_q2"


def test_query_campaign_statistics_multi_campaign(session):
    store = session.datastore

    # Run Campaign 1 on q0: qubit_relaxation
    camp1 = session.run_campaign(CampaignPlan(
        label="q0_t1", repeat=3, skip_artifacts=True,
        defaults={"targets": ["q0"]},
        steps=[{"experiment": "qubit_relaxation"}],
        tags=["baseline", "cd1"],
    ))

    # Run Campaign 2 on q1: qubit_relaxation
    camp2 = session.run_campaign(CampaignPlan(
        label="q1_t1", repeat=3, skip_artifacts=True,
        defaults={"targets": ["q1"]},
        steps=[{"experiment": "qubit_relaxation"}],
        tags=["baseline", "cd1"],
    ))

    # Query statistics across device
    stats = query_campaign_statistics(store, "chipT", min_repeats=2)

    assert "q0" in stats
    assert "q1" in stats
    assert "t1_s" in stats["q0"]
    assert "t1_s" in stats["q1"]

    assert stats["q0"]["t1_s"]["n"] == 3
    assert stats["q0"]["t1_s"]["campaign_id"] == camp1["campaign_id"]
    assert stats["q1"]["t1_s"]["campaign_id"] == camp2["campaign_id"]


def test_query_campaign_statistics_quality_gate(session):
    store = session.datastore

    # Run Campaign with repeat=2
    session.run_campaign(CampaignPlan(
        label="short_camp", repeat=2, skip_artifacts=True,
        defaults={"targets": ["q0"]},
        steps=[{"experiment": "qubit_relaxation"}],
    ))

    # min_repeats=5 should filter it out
    stats_strict = query_campaign_statistics(store, "chipT", min_repeats=5)
    assert "q0" not in stats_strict or "t1_s" not in stats_strict["q0"]

    # min_repeats=2 should include it
    stats_loose = query_campaign_statistics(store, "chipT", min_repeats=2)
    assert "t1_s" in stats_loose["q0"]


def test_get_latest_metric_stat(session):
    store = session.datastore

    session.run_campaign(CampaignPlan(
        label="t1_watch", repeat=3, skip_artifacts=True,
        defaults={"targets": ["q0"]},
        steps=[{"experiment": "qubit_relaxation"}],
    ))

    stat = get_latest_metric_stat(
        store, "chipT", target="q0_xy", quantity="t1_s",
        experiment="qubit_relaxation", min_repeats=2,
    )

    assert stat is not None
    assert stat["n"] == 3
    assert stat["mean"] is not None
    assert stat["experiment"] == "qubit_relaxation"



def test_composite_statistics_do_not_shadow_a_qubits_own(session):
    """A pair's quantities are the PAIR's. Filed under q1 they would also block
    q1's own value for the same quantity, since newest-first keeps the first
    occurrence."""
    store = session.datastore
    session.run_campaign(CampaignPlan(
        label="q0_t1", repeat=3, skip_artifacts=True,
        defaults={"targets": ["q0"]},
        steps=[{"experiment": "qubit_relaxation"}],
    ))
    stats = query_campaign_statistics(store, "chipT", min_repeats=2)
    assert not any("_" in target for target in stats), (
        f"a composite leaked into the per-qubit map: {sorted(stats)}")


def test_median_estimator_reports_the_median(session):
    """The estimator argument must change the numbers. 'mad_sigma' used to be
    offered here and silently produced the mean."""
    store = session.datastore
    session.run_campaign(CampaignPlan(
        label="q0_t1", repeat=3, skip_artifacts=True,
        defaults={"targets": ["q0"]},
        steps=[{"experiment": "qubit_relaxation"}],
    ))
    mean = query_campaign_statistics(store, "chipT", min_repeats=2)["q0"]["t1_s"]
    median = query_campaign_statistics(
        store, "chipT", min_repeats=2, estimator="median")["q0"]["t1_s"]
    assert mean["value"] == mean["mean"]
    assert median["value"] == median["median"]


def test_experiment_filter_is_pushed_into_the_query(session):
    """Asking for an experiment that recorded nothing must return None, and
    asking for the one that did must find it — the filter runs in SQL, not as a
    comparison against whichever campaign happened to be newest."""
    store = session.datastore
    session.run_campaign(CampaignPlan(
        label="q0_t1", repeat=3, skip_artifacts=True,
        defaults={"targets": ["q0"]},
        steps=[{"experiment": "qubit_relaxation"}],
    ))
    assert get_latest_metric_stat(
        store, "chipT", "q0", "t1_s", experiment="qubit_relaxation") is not None
    assert get_latest_metric_stat(
        store, "chipT", "q0", "t1_s", experiment="qubit_ramsey") is None


def test_a_broken_store_raises_rather_than_looking_empty(session):
    """{} from a failed query is indistinguishable from an unmeasured chip."""
    class Broken:
        def find_campaigns(self, **kw):
            raise OSError("index unreadable")

    with pytest.raises(OSError):
        query_campaign_statistics(Broken(), "chipT")
