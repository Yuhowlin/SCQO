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
    assert normalize_target_name("q1") == "q1"
    assert normalize_target_name("q1_xy") == "q1"
    assert normalize_target_name("q1_ro") == "q1"
    assert normalize_target_name("q1_z") == "q1"
    assert normalize_target_name("Q2_RES") == "q2"
    assert normalize_target_name("") == ""


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

