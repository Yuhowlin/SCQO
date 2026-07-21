"""Suggest -> review -> accept: capture semantics, the selection grammar, deferred
apply by run_id (guards included), and the audit trail on the run record.

All offline (SimulatedBackend, tmp_path)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from scqo import Outcome, PhysicalStore, RecordingDevice, Session, register
from scqo.experiments import QubitRamsey, QubitRelaxation, ResonatorSpectroscopy
from scqo.suggestions import SuggestionCapture, select_suggestions
from scqo.cli._review import format_table, parse_selection
from scqo.testing import InMemoryDevice, SimulatedBackend, demo_roster


@register
class _SgResonatorSpectroscopy(ResonatorSpectroscopy):
    def probe(self):
        return None


@register
class _SgQubitRamsey(QubitRamsey):
    def probe(self):
        return None


@register
class _SgQubitRelaxation(QubitRelaxation):
    def probe(self):
        return None


def _device() -> InMemoryDevice:
    return InMemoryDevice(
        {
            "q0": {"readout_freq": 5.95e9, "drive_freq": 3.87e9, "pi_amp": 0.2, "readout_amp": 0.25},
            "q1": {"readout_freq": 6.05e9, "drive_freq": 4.01e9, "pi_amp": 0.18, "readout_amp": 0.22},
        }
    )


def _session(tmp_path, **kwargs) -> Session:
    return Session(SimulatedBackend(_device()), demo_roster(),
                   data_root=tmp_path / "data", device_name="devA", **kwargs)


RAMSEY_PARAMS = {"targets": ["q1"], "frequency_detuning_hz": 1.0e6, "max_idle_time_ns": 4000, "num_points": 201}

#: Ramsey update() writes the knob + its measured twin + T2* (capture order).
RAMSEY_FIELDS = ["drive_freq", "f_01_hz", "t2_star_s"]
#: Resonator-spectroscopy update(): readout_freq on the target, the fit's sample
#: physics on the target's Resonator component.
RESONATOR_FIELDS = ["readout_freq", "f_r_hz", "kappa_tot_hz"]


# ------------------------------------------------------------------ capture shim


def test_capture_routes_fields_and_records_before_values():
    """One component-view surface, two stores: instrument-category fields ->
    instrument, physical-category fields -> physical; before = current value (None
    for never-measured); capture order kept; the vendor is never touched."""
    inner = _device()
    roster = demo_roster()
    device = RecordingDevice(inner, roster)
    vendor_before = inner.snapshot()
    capture = SuggestionCapture(device, PhysicalStore(), roster)

    view = capture.component("q0")
    view.drive_freq = 3.9e9        # pushed instrument knob
    view.readout_fidelity = 0.97   # record-only instrument value
    view.t1_s = 25e-6              # physical (sample) parameter

    got = [(s.component, s.field, s.store, s.before, s.after, s.status) for s in capture.suggestions]
    assert got == [
        ("q0", "drive_freq", "instrument", 3.87e9, 3.9e9, "pending"),
        ("q0", "readout_fidelity", "instrument", None, 0.97, "pending"),
        ("q0", "t1_s", "physical", None, 25e-6, "pending"),
    ]
    assert inner.snapshot() == vendor_before  # capture never writes through
    assert device.history() == []
    capture.save()  # explicitly a no-op
    assert inner.snapshot() == vendor_before


def test_capture_refuses_non_finite_and_unknown_fields():
    roster = demo_roster()
    capture = SuggestionCapture(RecordingDevice(_device(), roster), PhysicalStore(), roster)
    with pytest.raises(ValueError, match="non-finite"):
        capture.component("q0").pi_amp = float("nan")
    with pytest.raises(AttributeError, match="has no field 'pi_ampp'"):
        capture.component("q0").pi_ampp = 0.3  # the typo must not vanish silently
    assert capture.suggestions == []


def test_select_suggestions_filters_pending_only():
    from scqo.suggestions import Suggestion

    items = [
        Suggestion(component="q0", field="readout_freq", store="instrument", before=1.0, after=2.0),
        Suggestion(component="q0", field="t1_s", store="physical", before=None, after=3.0,
                   status="accepted"),
        Suggestion(component="q1", field="readout_freq", store="instrument", before=1.0, after=2.5),
    ]
    assert select_suggestions(items) == [0, 2]  # pending only
    assert select_suggestions(items, components=["q1"]) == [2]
    assert select_suggestions(items, fields=["readout_freq"]) == [0, 2]
    assert select_suggestions(items, indices=[1, 2]) == [2]  # decided index not selected
    assert select_suggestions(items, components=["q0"], fields=["t1_s"]) == []


# ------------------------------------------------------------------ selection grammar


def test_parse_selection_matrix():
    suggestions = [
        {"component": "q0", "field": "readout_freq", "store": "instrument", "before": 1.0, "after": 2.0,
         "status": "pending"},
        {"component": "q0", "field": "t1_s", "store": "physical", "before": None, "after": 3.0,
         "status": "accepted"},
        {"component": "q1", "field": "readout_freq", "store": "instrument", "before": 1.0, "after": 2.5,
         "status": "pending"},
    ]
    assert parse_selection("", suggestions) == []
    assert parse_selection("n", suggestions) == []
    assert parse_selection("NONE", suggestions) == []
    assert parse_selection("a", suggestions) == [0, 2]
    assert parse_selection("all", suggestions) == [0, 2]
    assert parse_selection("1", suggestions) == [0]  # displayed rows are 1-based
    assert parse_selection("1, 3", suggestions) == [0, 2]
    assert parse_selection("q1", suggestions) == [2]
    assert parse_selection("readout_freq", suggestions) == [0, 2]
    assert parse_selection("q0.readout_freq", suggestions) == [0]
    with pytest.raises(ValueError, match="already decided"):
        parse_selection("2", suggestions)
    with pytest.raises(ValueError, match="no row"):
        parse_selection("9", suggestions)
    with pytest.raises(ValueError, match="nothing pending"):
        parse_selection("q0.t1_s", suggestions)  # only an accepted item matches
    with pytest.raises(ValueError, match="nothing pending"):
        parse_selection("bogus", suggestions)
    # the table renders every row (smoke: numbering + unmeasured marker)
    table = format_table(suggestions)
    assert "(unmeasured)" in table and " 3 " in table


# ------------------------------------------------------------------ apply mode


def test_apply_mode_equals_old_behavior_with_audit_trail(tmp_path):
    """update="apply" applies immediately through the same path: vendor updated,
    ChangeRecords stamped with the run_id, and the record carries accepted items."""
    sess = _session(tmp_path)
    result = sess.run("resonator_spectroscopy", {"targets": ["q0"]}, update="apply")
    assert result["outcomes"]["q0"] == Outcome.SUCCESSFUL.value

    assert np.isclose(sess.device_state()["q0"]["readout_freq"], result["fit"]["q0"]["readout_freq"])
    (h,) = sess.history()
    assert h["run_id"] == result["run_id"] and h["experiment"] == "resonator_spectroscopy"
    assert [s["field"] for s in result["suggestions"]] == RESONATOR_FIELDS
    # the sample physics lands on the qubit's Resonator component
    assert [s["component"] for s in result["suggestions"]] == ["q0", "q0_res", "q0_res"]
    assert all(s["status"] == "accepted" and s["decided_by"] for s in result["suggestions"])
    # the sample physics landed in the physical store, stamped with the same run
    assert {h["field"] for h in sess.history(store="physical")} == {"f_r_hz", "kappa_tot_hz"}
    row = sess.find_runs()[0]
    assert row["updated_device"] is True and row["suggestions_pending"] == 0


def test_legacy_bool_updates_still_work(tmp_path):
    sess = _session(tmp_path)
    r_false = sess.run("resonator_spectroscopy", {"targets": ["q0"]}, update=False)
    assert r_false["suggestions"] == []  # "none": not even captured
    before = sess.device_state()["q0"]["readout_freq"]
    r_true = sess.run("resonator_spectroscopy", {"targets": ["q0"]}, update=True)
    assert sess.device_state()["q0"]["readout_freq"] != before  # "apply"
    assert {s["status"] for s in r_true["suggestions"]} == {"accepted"}
    with pytest.raises(ValueError, match="update must be"):
        sess.run("resonator_spectroscopy", {"targets": ["q0"]}, update="bogus")


# ------------------------------------------------------------------ accept later


def test_accept_all_pending_by_run_id(tmp_path):
    sess = _session(tmp_path)
    result = sess.run("qubit_ramsey", RAMSEY_PARAMS)  # default: suggest
    run_id = result["run_id"]
    state_before = sess.device_state()

    summary = sess.accept(run_id, comment="looks right")
    assert [a["field"] for a in summary["applied"]] == RAMSEY_FIELDS
    assert summary["stale"] == [] and summary["errors"] == []
    assert summary["pending_left"] == 0

    # applied through the real stores, stamped with the ORIGINATING run_id
    assert sess.device_state()["q1"]["drive_freq"] == result["fit"]["q1"]["drive_freq"]
    assert sess.device_state()["q1"]["drive_freq"] != state_before["q1"]["drive_freq"]
    assert sess.physical_state()["q1"]["t2_star_s"] == result["fit"]["q1"]["t2_star_s"]
    (h,) = sess.history()
    assert h["run_id"] == run_id and h["experiment"] == "qubit_ramsey"
    for hp in sess.history(store="physical"):  # f_01_hz + t2_star_s
        assert hp["run_id"] == run_id
    assert [hp["field"] for hp in sess.history(store="physical")] == ["f_01_hz", "t2_star_s"]

    # the decision is on the record (truth) + index, and flips updated_device
    record = sess.load_run(run_id)["record"]
    assert {s["status"] for s in record["suggestions"]} == {"accepted"}
    assert {s["comment"] for s in record["suggestions"]} == {"looks right"}
    assert record["updated_device"] is True
    assert sess.find_runs(pending=True) == []

    # double-accept is a no-op (nothing pending anymore)
    again = sess.accept(run_id)
    assert again["applied"] == [] and again["pending_left"] == 0
    assert len(sess.history()) == 1


def test_partial_accept_and_reject_by_field(tmp_path):
    sess = _session(tmp_path)
    drive_before = sess.device_state()["q1"]["drive_freq"]
    result = sess.run("qubit_ramsey", RAMSEY_PARAMS)
    run_id = result["run_id"]

    summary = sess.accept(run_id, fields=["t2_star_s"])
    assert [a["field"] for a in summary["applied"]] == ["t2_star_s"]
    assert summary["pending_left"] == 2
    assert sess.physical_state()["q1"]["t2_star_s"] == result["fit"]["q1"]["t2_star_s"]
    assert sess.device_state()["q1"]["drive_freq"] == drive_before  # knob NOT applied
    assert sess.history() == []  # ... so no instrument history either

    rejected = sess.reject(run_id, comment="fit chased a noise spike")
    assert rejected["rejected"] == [{"component": "q1", "field": "drive_freq"},
                                    {"component": "q1", "field": "f_01_hz"}]
    assert rejected["pending_left"] == 0
    record = sess.load_run(run_id)["record"]
    by_field = {s["field"]: s for s in record["suggestions"]}
    assert by_field["t2_star_s"]["status"] == "accepted"
    assert by_field["drive_freq"]["status"] == "rejected"
    assert by_field["drive_freq"]["comment"] == "fit chased a noise spike"
    assert sess.history() == []  # reject touched no store

    # decisions survive a full index rebuild (record.json is the truth)
    assert sess.datastore.reindex() == 1
    row = sess.find_runs()[0]
    assert row["suggestions_pending"] == 0
    assert {s["status"] for s in row["suggestions"]} == {"accepted", "rejected"}


def test_accept_in_fresh_session_days_later(tmp_path):
    """The run's Session is long gone: a brand-new Session applies by run_id."""
    result = _session(tmp_path).run("resonator_spectroscopy", {"targets": ["q0"]})

    sess2 = _session(tmp_path)  # fresh session, same lab
    summary = sess2.accept(result["run_id"])
    assert [a["field"] for a in summary["applied"]] == RESONATOR_FIELDS
    assert sess2.device_state()["q0"]["readout_freq"] == result["fit"]["q0"]["readout_freq"]
    (h,) = sess2.history()
    assert h["run_id"] == result["run_id"]


def test_accept_staleness_guard(tmp_path):
    """If the field changed since the run measured it, the item is skipped as stale
    (a newer calibration must not be silently clobbered); --force overrides."""
    sess = _session(tmp_path)
    result = sess.run("resonator_spectroscopy", {"targets": ["q0"]})
    run_id = result["run_id"]

    sess.device.component("q0").readout_freq = 6.2e9  # someone recalibrated in between

    summary = sess.accept(run_id)
    # only the recalibrated knob is stale; the (untouched) physical items apply
    assert [a["field"] for a in summary["applied"]] == ["f_r_hz", "kappa_tot_hz"]
    assert len(summary["stale"]) == 1 and summary["stale"][0]["field"] == "readout_freq"
    assert summary["stale"][0]["current"] == 6.2e9
    assert summary["pending_left"] == 1  # still decidable
    assert sess.device_state()["q0"]["readout_freq"] == 6.2e9

    forced = sess.accept(run_id, force=True)
    assert [a["field"] for a in forced["applied"]] == ["readout_freq"]
    assert sess.device_state()["q0"]["readout_freq"] == result["fit"]["q0"]["readout_freq"]


def test_accept_cooldown_era_guard(tmp_path):
    """A value measured under another cooldown/setup era may not transfer: refuse
    without force."""
    sess = _session(tmp_path)
    result = sess.run("resonator_spectroscopy", {"targets": ["q0"]})  # stamps ("", "")

    ddir = tmp_path / "data" / "devA"
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "cooldowns.toml").write_text(
        '[cd1]\nstart = 2026-01-01\n[cd1.setup.sim]\nbackend = "simulated"\n',
        encoding="utf-8",
    )  # the device moved to a declared cycle -> era mismatch with the run

    with pytest.raises(RuntimeError, match="cooldown/setup"):
        sess.accept(result["run_id"])
    assert sess.history() == []

    forced = sess.accept(result["run_id"], force=True)
    assert [a["field"] for a in forced["applied"]] == RESONATOR_FIELDS


def test_accept_refuses_wrong_device(tmp_path):
    result = _session(tmp_path).run("resonator_spectroscopy", {"targets": ["q0"]})
    sess_b = Session(SimulatedBackend(_device()), demo_roster(),
                     data_root=tmp_path / "data", device_name="devB")
    with pytest.raises(RuntimeError, match="devA"):
        sess_b.accept(result["run_id"])


class _VendorRejectsDriveFreq:
    """Vendor device whose drive_freq knob rejects writes (per-component atomicity probe)."""

    def __init__(self, inner):
        self._inner = inner

    def component(self, name):
        view = self._inner.component(name)

        class _V:
            def __getattr__(self, attr):
                return getattr(view, attr)

            def __setattr__(self, attr, value):
                if attr == "drive_freq":
                    raise ValueError("vendor rejected drive_freq")
                setattr(view, attr, value)

        return _V()

    def snapshot(self):
        return self._inner.snapshot()

    def save(self):
        pass


def test_reapply_rolls_back_to_older_run(tmp_path):
    """The regret flow: accept run A, then run B; --reapply on A restores A's value,
    with a fresh ChangeRecord linked to A (old = the live value it overwrote)."""
    sess = _session(tmp_path)
    run_a = sess.run("resonator_spectroscopy", {"targets": ["q0"]})
    sess.accept(run_a["run_id"])
    value_a = sess.device_state()["q0"]["readout_freq"]

    run_b = sess.run("resonator_spectroscopy", {"targets": ["q0"]})
    sess.accept(run_b["run_id"])
    value_b = sess.device_state()["q0"]["readout_freq"]
    assert value_b != value_a

    # without the flag, A's decided item stays dead (existing behavior pinned)
    plain = sess.accept(run_a["run_id"])
    assert plain["applied"] == [] and sess.device_state()["q0"]["readout_freq"] == value_b

    rollback = sess.accept(run_a["run_id"], reapply=True, comment="B's fit chased a spike")
    assert [a["field"] for a in rollback["applied"]] == RESONATOR_FIELDS
    (item,) = [a for a in rollback["applied"] if a["field"] == "readout_freq"]
    assert item["current"] == value_b  # what the rollback overwrote — shown, not blocked
    assert rollback["stale"] == []  # staleness guard is OFF in reapply mode
    assert sess.device_state()["q0"]["readout_freq"] == value_a

    # full provenance: three history entries, the last linked to run A again
    hist = sess.history()
    assert [h["run_id"] for h in hist] == [run_a["run_id"], run_b["run_id"], run_a["run_id"]]
    assert hist[-1]["old"] == value_b and hist[-1]["new"] == value_a
    record = sess.load_run(run_a["run_id"])["record"]
    assert {s["status"] for s in record["suggestions"]} == {"accepted"}
    assert {s["comment"] for s in record["suggestions"]} == {"B's fit chased a spike"}


def test_reapply_accepts_previously_rejected(tmp_path):
    sess = _session(tmp_path)
    result = sess.run("qubit_relaxation", {"targets": ["q0"]})
    sess.reject(result["run_id"], comment="not sure yet")
    assert sess.physical_state() == {}

    summary = sess.accept(result["run_id"], reapply=True, comment="it was fine after all")
    assert [a["field"] for a in summary["applied"]] == ["t1_s"]
    assert sess.physical_state()["q0"]["t1_s"] == result["fit"]["q0"]["t1_s"]
    (s,) = sess.load_run(result["run_id"])["record"]["suggestions"]
    assert s["status"] == "accepted" and s["comment"] == "it was fine after all"


def test_reapply_still_respects_era_guard(tmp_path):
    sess = _session(tmp_path)
    result = sess.run("resonator_spectroscopy", {"targets": ["q0"]})
    sess.accept(result["run_id"])

    ddir = tmp_path / "data" / "devA"
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "cooldowns.toml").write_text(
        '[cd1]\nstart = 2026-01-01\n[cd1.setup.sim]\nbackend = "simulated"\n',
        encoding="utf-8",
    )  # device moved to a declared cycle: values may not transfer

    with pytest.raises(RuntimeError, match="cooldown/setup"):
        sess.accept(result["run_id"], reapply=True)


def test_parse_selection_allow_decided():
    suggestions = [
        {"component": "q0", "field": "readout_freq", "store": "instrument", "before": 1.0,
         "after": 2.0, "status": "accepted"},
        {"component": "q0", "field": "t1_s", "store": "physical", "before": None,
         "after": 3.0, "status": "rejected"},
    ]
    with pytest.raises(ValueError, match="--reapply"):
        parse_selection("1", suggestions)
    assert parse_selection("1", suggestions, allow_decided=True) == [0]
    assert parse_selection("a", suggestions, allow_decided=True) == [0, 1]
    assert parse_selection("t1_s", suggestions, allow_decided=True) == [1]
    assert parse_selection("", suggestions, allow_decided=True) == []  # Enter still = nothing


def test_accept_dry_run_reports_guards_without_applying(tmp_path):
    """dry_run=True returns the plan (era reported not raised, staleness flagged,
    decided items included) and mutates NOTHING."""
    sess = _session(tmp_path)
    result = sess.run("qubit_ramsey", RAMSEY_PARAMS)
    run_id = result["run_id"]
    sess.device.component("q1").drive_freq = 4.1e9  # someone recalibrated -> stale

    ddir = tmp_path / "data" / "devA"
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "cooldowns.toml").write_text(
        '[cd1]\nstart = 2026-01-01\n[cd1.setup.sim]\nbackend = "simulated"\n',
        encoding="utf-8",
    )  # era mismatch with the run's ("", "") stamps

    plan = sess.accept(run_id, dry_run=True)  # must NOT raise despite the mismatch
    assert plan["era"]["match"] is False
    assert plan["era"]["run"] == ["", ""]
    assert plan["era"]["current"] == ["cd1", "sim"]  # the single setup's NAME, auto-resolved
    by_field = {item["field"]: item for item in plan["items"]}
    assert by_field["drive_freq"]["stale"] is True
    assert by_field["drive_freq"]["current"] == 4.1e9
    assert by_field["t2_star_s"]["stale"] is False  # physical value untouched
    assert by_field["f_01_hz"]["stale"] is False
    assert {item["status"] for item in plan["items"]} == {"pending"}

    # nothing was applied, decided, or recorded
    assert sess.physical_state() == {}
    record = sess.load_run(run_id)["record"]
    assert {s["status"] for s in record["suggestions"]} == {"pending"}


def test_accept_dry_run_includes_decided_items(tmp_path):
    sess = _session(tmp_path)
    result = sess.run("resonator_spectroscopy", {"targets": ["q0"]})
    sess.accept(result["run_id"], comment="first")

    plan = sess.accept(result["run_id"], dry_run=True)  # no reapply flag needed
    assert [item["field"] for item in plan["items"]] == RESONATOR_FIELDS
    for item in plan["items"]:
        assert item["status"] == "accepted" and item["decided_by"]
        assert item["current"] == item["after"]  # its value IS the current one
    # ("stale" compares against the capture-time BEFORE, so a decided item is
    # stale by construction — the confirmation flow never stale-checks those.)


class _Tty:
    """stderr/stdin stand-in: writable, capturable, and isatty() -> True."""

    def __init__(self):
        import io

        self._buf = io.StringIO()

    def write(self, s):
        return self._buf.write(s)

    def flush(self):
        pass

    def isatty(self):
        return True

    def getvalue(self):
        return self._buf.getvalue()


def _interactive(monkeypatch, answers):
    import sys

    tty = _Tty()
    monkeypatch.setattr(sys, "stderr", tty)
    monkeypatch.setattr(sys, "stdin", tty)
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda: next(it))
    return tty


def test_review_confirms_stale_overwrite(tmp_path, monkeypatch):
    """A stale row warns with the before/current diff; 'y' applies it."""
    from scqo.cli import _review

    sess = _session(tmp_path)
    result = sess.run("resonator_spectroscopy", {"targets": ["q0"]})
    sess.device.component("q0").readout_freq = 6.2e9  # recalibrated since the run

    tty = _interactive(monkeypatch, ["a", "y", "checked the trace"])
    summary = _review.review_interactively(sess, result["run_id"], result["suggestions"])

    assert [a["field"] for a in summary["applied"]] == RESONATOR_FIELDS
    assert summary["stale"] == []  # confirmed, not blocked
    assert sess.device_state()["q0"]["readout_freq"] == result["fit"]["q0"]["readout_freq"]
    prompted = tty.getvalue()
    assert "current value is 6.2e+09" in prompted and "overwrite" in prompted


def test_review_declines_stale_stays_pending(tmp_path, monkeypatch):
    """Enter at the stale confirmation = No: that item stays pending and the knob is
    unchanged; the other (non-stale) selected items still apply."""
    from scqo.cli import _review

    sess = _session(tmp_path)
    result = sess.run("resonator_spectroscopy", {"targets": ["q0"]})
    sess.device.component("q0").readout_freq = 6.2e9

    # select all, Enter declines the stale readout_freq overwrite, empty comment
    _interactive(monkeypatch, ["a", "", ""])
    summary = _review.review_interactively(sess, result["run_id"], result["suggestions"])

    assert [a["field"] for a in summary["applied"]] == ["f_r_hz", "kappa_tot_hz"]
    assert sess.device_state()["q0"]["readout_freq"] == 6.2e9
    record = sess.load_run(result["run_id"])["record"]
    assert record["suggestions"][0]["status"] == "pending"


def test_review_confirms_rollback_without_reapply_flag(tmp_path, monkeypatch):
    """The picker lets you select a DECIDED row; a confirmation replaces the old
    refusal — no --reapply knowledge required."""
    from scqo.cli import _review

    sess = _session(tmp_path)
    run_a = sess.run("resonator_spectroscopy", {"targets": ["q0"]})
    sess.accept(run_a["run_id"])
    value_a = sess.device_state()["q0"]["readout_freq"]
    run_b = sess.run("resonator_spectroscopy", {"targets": ["q0"]})
    sess.accept(run_b["run_id"])
    assert sess.device_state()["q0"]["readout_freq"] != value_a

    record_a = sess.load_run(run_a["run_id"])["record"]
    tty = _interactive(monkeypatch, ["1", "y", "rollback"])
    summary = _review.review_interactively(sess, run_a["run_id"], record_a["suggestions"])

    assert [a["field"] for a in summary["applied"]] == ["readout_freq"]
    assert sess.device_state()["q0"]["readout_freq"] == value_a
    assert sess.history()[-1]["run_id"] == run_a["run_id"]  # fresh entry linked to A
    assert "re-apply (rollback" in tty.getvalue()


def test_review_era_mismatch_asks_once_and_no_aborts(tmp_path, monkeypatch):
    from scqo.cli import _review

    sess = _session(tmp_path)
    result = sess.run("resonator_spectroscopy", {"targets": ["q0"]})
    ddir = tmp_path / "data" / "devA"
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "cooldowns.toml").write_text(
        '[cd1]\nstart = 2026-01-01\n[cd1.setup.sim]\nbackend = "simulated"\n',
        encoding="utf-8",
    )
    state_before = sess.device_state()

    tty = _interactive(monkeypatch, ["a", "n"])  # decline the era warning
    assert _review.review_interactively(sess, result["run_id"], result["suggestions"]) is None
    assert sess.device_state() == state_before
    assert "WARNING" in tty.getvalue() and "may not transfer" in tty.getvalue()

    _interactive(monkeypatch, ["a", "y", ""])  # confirm it this time
    summary = _review.review_interactively(sess, result["run_id"], result["suggestions"])
    assert [a["field"] for a in summary["applied"]] == RESONATOR_FIELDS


def test_review_interactively_applies_selection(tmp_path, monkeypatch):
    """The `scqo run` prompt path: table -> bad token re-asks -> field selection ->
    comment -> applied via Session.accept. (Non-TTY behavior is covered by the
    subprocess CLI tests, which are non-TTY by construction.)"""
    import io
    import sys

    from scqo.cli import _review

    class _Tty(io.StringIO):
        def isatty(self):
            return True

    sess = _session(tmp_path)
    result = sess.run("qubit_ramsey", RAMSEY_PARAMS)
    answers = iter(["bogus", "t2_star_s", "from the prompt"])
    monkeypatch.setattr(sys, "stderr", _Tty())
    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr("builtins.input", lambda: next(answers))

    summary = _review.review_interactively(sess, result["run_id"], result["suggestions"])

    assert [a["field"] for a in summary["applied"]] == ["t2_star_s"]
    prompted = sys.stderr.getvalue()
    assert "nothing selectable matches 'bogus'" in prompted  # re-asked after the typo
    record = sess.load_run(result["run_id"])["record"]
    by_field = {s["field"]: s for s in record["suggestions"]}
    assert by_field["t2_star_s"]["status"] == "accepted"
    assert by_field["t2_star_s"]["comment"] == "from the prompt"
    assert by_field["drive_freq"]["status"] == "pending"
    assert by_field["f_01_hz"]["status"] == "pending"


# ------------------------------------------------ operator-authored (scqo suggest)


def test_suggest_on_run_without_suggestions(tmp_path, monkeypatch):
    """The estimator-failed-but-the-figure-didn't flow: attach manually-read values
    to the run; they land on the record as pending operator rows, findable via the
    pending filter — surviving a reindex (record.json is the truth)."""
    monkeypatch.setattr("scqo.config._current_operator", lambda: "alice")
    sess = _session(tmp_path)
    result = sess.run("resonator_spectroscopy", {"targets": ["q0"]}, update="none")
    run_id = result["run_id"]
    assert result["suggestions"] == []

    summary = sess.suggest(run_id, {"q0.readout_freq": 5.912e9, "q0_res.f_r_hz": 5.912e9},
                           comment="read off the dip")
    assert summary["run_id"] == run_id and summary["pending_total"] == 2
    assert summary["added"] == [
        {"component": "q0", "field": "readout_freq", "store": "instrument",
         "before": 5.95e9, "after": 5.912e9},
        {"component": "q0_res", "field": "f_r_hz", "store": "physical",
         "before": None, "after": 5.912e9},
    ]

    record = sess.load_run(run_id)["record"]
    for s in record["suggestions"]:
        assert s["origin"] == "operator" and s["proposed_by"] == "alice"
        assert s["proposed_at"] and s["status"] == "pending"
        assert s["comment"] == "read off the dip"
    assert [r["run_id"] for r in sess.find_runs(pending=True)] == [run_id]
    assert sess.datastore.reindex() == 1
    assert [r["run_id"] for r in sess.find_runs(pending=True)] == [run_id]
    # nothing was applied anywhere — proposing touches no store
    assert sess.history() == [] and sess.physical_state() == {}


def test_suggest_appends_without_disturbing_decided(tmp_path):
    """Suggest APPENDS: existing (even decided) rows keep their status, comment and
    position — displayed row numbers stay stable for the review grammar."""
    sess = _session(tmp_path)
    result = sess.run("resonator_spectroscopy", {"targets": ["q0"]})
    run_id = result["run_id"]
    sess.accept(run_id, comment="first decision")

    sess.suggest(run_id, {"q0.pi_amp": 0.21}, comment="tweak")
    record = sess.load_run(run_id)["record"]
    assert [s["field"] for s in record["suggestions"]] == [
        "readout_freq", "f_r_hz", "kappa_tot_hz", "pi_amp",  # appended at the END
    ]
    decided, added = record["suggestions"][:3], record["suggestions"][3]
    assert all(s["status"] == "accepted" and s["comment"] == "first decision"
               and s["origin"] == "estimator" for s in decided)
    assert added["status"] == "pending" and added["origin"] == "operator"


def test_accept_applies_operator_suggestion_with_provenance(tmp_path):
    """An accepted operator suggestion goes through the exact same apply path:
    vendor pushed / physical recorded, ChangeRecords credit the suggested-on run."""
    sess = _session(tmp_path)
    result = sess.run("resonator_spectroscopy", {"targets": ["q0"]}, update="none")
    run_id = result["run_id"]
    sess.suggest(run_id, {"q0.readout_freq": 5.912e9, "q0.t1_s": 2.5e-5})

    summary = sess.accept(run_id)
    assert [a["field"] for a in summary["applied"]] == ["readout_freq", "t1_s"]
    assert summary["errors"] == [] and summary["pending_left"] == 0

    assert sess.device_state()["q0"]["readout_freq"] == 5.912e9
    assert sess.physical_state()["q0"]["t1_s"] == 2.5e-5
    (h,) = sess.history()
    assert h["run_id"] == run_id and h["experiment"] == "resonator_spectroscopy"
    (hp,) = sess.history(store="physical")
    assert hp["run_id"] == run_id
    record = sess.load_run(run_id)["record"]
    assert {s["status"] for s in record["suggestions"]} == {"accepted"}
    assert all(s["decided_by"] for s in record["suggestions"])
    assert record["updated_device"] is True


def test_suggest_staleness_and_era_guards_still_apply(tmp_path):
    """Operator items get no special treatment at accept time: a recalibrated field
    is stale, a cooldown-era mismatch refuses without force."""
    sess = _session(tmp_path)
    result = sess.run("resonator_spectroscopy", {"targets": ["q0"]}, update="none")
    run_id = result["run_id"]
    sess.suggest(run_id, {"q0.readout_freq": 5.912e9})

    sess.device.component("q0").readout_freq = 6.2e9  # someone recalibrated in between
    summary = sess.accept(run_id)
    assert summary["applied"] == []
    assert len(summary["stale"]) == 1 and summary["stale"][0]["field"] == "readout_freq"
    assert sess.device_state()["q0"]["readout_freq"] == 6.2e9

    ddir = tmp_path / "data" / "devA"
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "cooldowns.toml").write_text(
        '[cd1]\nstart = 2026-01-01\n[cd1.setup.sim]\nbackend = "simulated"\n',
        encoding="utf-8",
    )  # the device moved to a declared cycle -> era mismatch with the run
    with pytest.raises(RuntimeError, match="cooldown/setup"):
        sess.accept(run_id)


def test_suggest_validation(tmp_path):
    sess = _session(tmp_path)
    result = sess.run("resonator_spectroscopy", {"targets": ["q0"]}, update="none")
    run_id = result["run_id"]

    with pytest.raises(ValueError, match="has no field 't1_sec'"):
        sess.suggest(run_id, {"q0.t1_sec": 1e-6})
    # category-aware: a field that exists on ANOTHER component names the carrier
    with pytest.raises(ValueError) as err:
        sess.suggest(run_id, {"q0.f_r_hz": 5.9e9})
    assert "Resonator field" in str(err.value) and "q0_res.f_r_hz" in str(err.value)
    with pytest.raises(ValueError, match="unknown component 'q9'"):
        sess.suggest(run_id, {"q9.readout_freq": 5.9e9})
    with pytest.raises(ValueError, match="non-finite"):
        sess.suggest(run_id, {"q0.readout_freq": float("nan")})
    with pytest.raises(ValueError, match="must be a number"):
        sess.suggest(run_id, {"q0.readout_freq": True})  # bool is not a value
    with pytest.raises(ValueError, match="must be a number"):
        sess.suggest(run_id, {"q0.readout_freq": "5.9e9"})
    with pytest.raises(ValueError, match="'component.field'"):
        sess.suggest(run_id, {"readout_freq": 5.9e9})  # no component part
    with pytest.raises(ValueError, match="no assignments"):
        sess.suggest(run_id, {})
    # nothing was stored by any failed attempt
    assert sess.load_run(run_id)["record"]["suggestions"] == []

    sess_b = Session(SimulatedBackend(_device()), demo_roster(),
                     data_root=tmp_path / "data", device_name="devB")
    with pytest.raises(RuntimeError, match="devA"):
        sess_b.suggest(run_id, {"q0.readout_freq": 5.9e9})
    with pytest.raises(RuntimeError, match="no data_root"):
        Session(SimulatedBackend(_device()), demo_roster()).suggest(
            run_id, {"q0.readout_freq": 5.9e9})


def test_suggest_during_accept_window_survives(tmp_path):
    """REGRESSION (record.json race): an operator suggestion landing INSIDE a
    concurrent accept's load->write window must survive — accept persists its
    decisions via the index-targeted editor, never a stale whole-list snapshot."""
    sess_a = _session(tmp_path)
    result = sess_a.run("resonator_spectroscopy", {"targets": ["q0"]})
    run_id = result["run_id"]
    sess_b = _session(tmp_path)  # a second terminal on the same lab

    real_apply = sess_a._apply

    def apply_with_concurrent_suggest(*args, **kwargs):
        sess_b.suggest(run_id, {"q0.pi_amp": 0.21}, comment="operator, mid-accept")
        return real_apply(*args, **kwargs)

    sess_a._apply = apply_with_concurrent_suggest
    summary = sess_a.accept(run_id)
    assert [a["field"] for a in summary["applied"]] == RESONATOR_FIELDS
    assert summary["pending_left"] == 1  # counted from the FRESH stored list

    record = sess_a.load_run(run_id)["record"]
    assert len(record["suggestions"]) == 4  # nothing clobbered
    by_field = {s["field"]: s for s in record["suggestions"]}
    assert by_field["pi_amp"]["origin"] == "operator"
    assert by_field["pi_amp"]["status"] == "pending"
    for field in RESONATOR_FIELDS:
        assert by_field[field]["status"] == "accepted" and by_field[field]["decided_by"]
    assert [r["run_id"] for r in sess_a.find_runs(pending=True)] == [run_id]


def test_accept_during_suggest_window_survives(tmp_path):
    """REGRESSION (record.json race, reverse order): an accept completing inside
    suggest's load->write window must keep its decisions — suggest appends to the
    FRESH stored list under the record lock, so statuses never revert to pending."""
    sess_a = _session(tmp_path)
    result = sess_a.run("resonator_spectroscopy", {"targets": ["q0"]})
    run_id = result["run_id"]
    sess_b = _session(tmp_path)

    real_load = sess_b._load_run_record

    def load_then_concurrent_accept(rid):
        out = real_load(rid)
        sess_a.accept(run_id, comment="decided mid-suggest")
        return out

    sess_b._load_run_record = load_then_concurrent_accept
    summary = sess_b.suggest(run_id, {"q0.pi_amp": 0.21})
    assert summary["pending_total"] == 1  # only the operator row is pending

    record = sess_b.load_run(run_id)["record"]
    assert len(record["suggestions"]) == 4
    by_field = {s["field"]: s for s in record["suggestions"]}
    for field in RESONATOR_FIELDS:  # NOT reverted to pending
        assert by_field[field]["status"] == "accepted" and by_field[field]["decided_by"]
    assert by_field["pi_amp"]["status"] == "pending"


def test_suggestion_dicts_without_origin_default_to_estimator():
    """Records written before the origin field existed stay parseable and truthful."""
    from scqo.suggestions import Suggestion

    s = Suggestion(**{"component": "q0", "field": "readout_freq", "store": "instrument",
                      "before": 1.0, "after": 2.0, "status": "accepted",
                      "decided_at": "2026-07-01T10:00:00+08:00", "decided_by": "bob",
                      "comment": ""})
    assert s.origin == "estimator" and s.proposed_by is None and s.proposed_at is None


def test_format_table_marks_operator_rows():
    rows = [
        {"component": "q0", "field": "readout_freq", "store": "instrument", "before": 1.0,
         "after": 2.0, "status": "pending", "origin": "operator", "proposed_by": "alice"},
        {"component": "q0", "field": "t1_s", "store": "physical", "before": None,
         "after": 3.0, "status": "pending"},  # estimator row: no marker
    ]
    table = format_table(rows)
    first, second = table.splitlines()[1:3]
    assert "[operator: alice]" in first
    assert "operator" not in second


def test_accept_vendor_rejection_skips_rest_of_component(tmp_path):
    """Ramsey proposes drive_freq THEN f_01_hz/t2_star_s: if the vendor rejects the
    knob, the component's remaining items stay pending too — no half-applied qubit."""
    sess = Session(
        SimulatedBackend(_VendorRejectsDriveFreq(_device())), demo_roster(),
        data_root=tmp_path / "data", device_name="devA",
    )
    result = sess.run("qubit_ramsey", RAMSEY_PARAMS)
    summary = sess.accept(result["run_id"])

    assert summary["applied"] == []
    assert len(summary["errors"]) == 1 and "vendor rejected" in summary["errors"][0]
    assert summary["pending_left"] == 3  # every item remains decidable
    assert sess.history() == [] and sess.physical_state() == {}
    record = sess.load_run(result["run_id"])["record"]
    by_field = {s["field"]: s for s in record["suggestions"]}
    assert by_field["drive_freq"]["status"] == "pending"
    assert "apply failed" in by_field["drive_freq"]["comment"]
    assert by_field["f_01_hz"]["status"] == "pending"
    assert by_field["t2_star_s"]["status"] == "pending"
