"""Per-context history.sqlite contracts (scqo.changes) — the change-history
TRUTH file: append-only rows, indexed reads, read-side never creates,
folder-copy mergeability, and survival of index.sqlite version bumps."""

import json
import shutil
import sqlite3

import pytest

from scqo import parse_components, physical_store, state_store
from scqo.changes import (
    HISTORY_FILE,
    ChangeDB,
    ChangeRecord,
    ChangesError,
    collect_fact_matrix,
    collect_fact_series,
    record_from_row,
)
from scqo.stores import PHYSICAL_FILE, Store
from tests.test_model_roster import EXAMPLE


@pytest.fixture()
def roster():
    return parse_components(EXAMPLE)


def _rec(ts, entity, field, new, old=None, **kw):
    return ChangeRecord(timestamp=ts, entity=entity, field=field,
                        old=old, new=new, **kw)


def _db(tmp_path) -> ChangeDB:
    return ChangeDB(tmp_path / HISTORY_FILE)


# --------------------------------------------------------------- roundtrip

def test_rows_round_trip_including_cooldown_stamp_and_waveforms(tmp_path):
    db = _db(tmp_path)
    rows = [
        _rec("2026-08-11T10:00:00+08:00", "q1_xy", "pi_amp", 0.209,
             kind="drive", experiment="qubit_power_rabi", run_id="r1",
             operator="shiau", setup="qm_a", cooldown="cd1"),
        _rec("2026-08-11T10:00:01+08:00", "q1_q2", "iswap_waveform",
             [0.0, 0.4, 0.0], old=[0.0, 0.2, 0.0], setup="qm_a",
             cooldown="cd1", coupled_to="iswap_amp"),
    ]
    with db.transaction() as con:
        ChangeDB.insert(con, rows, store="state")
    got = db.context_history("state")
    assert [r["new"] for r in got] == [0.209, [0.0, 0.4, 0.0]]
    assert got[0]["old"] is None and got[1]["old"] == [0.0, 0.2, 0.0]
    assert got[0]["cooldown"] == "cd1" and got[0]["setup"] == "qm_a"
    assert got[1]["coupled_to"] == "iswap_amp"
    # rehydration inverts the insert exactly ('' -> None, seq/store drop)
    back = record_from_row(got[0])
    assert back == rows[0]


def test_record_from_row_maps_empty_context_stamps_to_none(tmp_path):
    db = _db(tmp_path)
    with db.transaction() as con:
        ChangeDB.insert(con, [_rec("t1", "q1", "t1_s", 3e-5)],
                        store="physical")
    row = db.context_history("physical")[0]
    rec = record_from_row(row)
    assert rec.setup is None and rec.cooldown is None


# ------------------------------------------------------- ordering contract

def test_latest_wins_by_timestamp_then_insertion_order(tmp_path):
    db = _db(tmp_path)
    with db.transaction() as con:
        ChangeDB.insert(con, [_rec("t2", "q1_xy", "pi_amp", 0.222)],
                        store="state")
    with db.transaction() as con:  # same timestamp, later insertion
        ChangeDB.insert(con, [_rec("t2", "q1_xy", "pi_amp", 0.333)],
                        store="state")
    with db.transaction() as con:  # older timestamp, latest insertion
        ChangeDB.insert(con, [_rec("t1", "q1_xy", "pi_amp", 0.111)],
                        store="state")
        latest = ChangeDB.latest_new(con, store="state",
                                     keys=[("q1_xy", "pi_amp")])
    assert latest == {("q1_xy", "pi_amp"): 0.333}
    series = db.param_series("q1_xy", "pi_amp")
    assert [r["new"] for r in series] == [0.333, 0.222, 0.111]  # newest first
    asc = db.context_history("state")
    assert [r["new"] for r in asc] == [0.111, 0.222, 0.333]


def test_latest_new_sees_own_uncommitted_rows(tmp_path):
    db = _db(tmp_path)
    with db.transaction() as con:
        ChangeDB.insert(con, [_rec("t1", "q1_xy", "pi_amp", 0.1)],
                        store="state")
        assert ChangeDB.latest_new(con, store="state",
                                   keys=[("q1_xy", "pi_amp")]) == {
            ("q1_xy", "pi_amp"): 0.1}


# ----------------------------------------------------------------- queries

def test_context_history_entity_filter_and_last_n_limit(tmp_path):
    db = _db(tmp_path)
    with db.transaction() as con:
        ChangeDB.insert(con, [
            _rec("t1", "q1_xy", "pi_amp", 0.1),
            _rec("t2", "q2_xy", "pi_amp", 0.2),
            _rec("t3", "q1_xy", "pi_amp", 0.3),
        ], store="state")
    only_q1 = db.context_history("state", entity="q1_xy")
    assert [r["new"] for r in only_q1] == [0.1, 0.3]
    last_two = db.context_history("state", limit=2)
    assert [r["new"] for r in last_two] == [0.2, 0.3]  # last N, ascending


def test_param_series_limit_and_store_filter(tmp_path):
    db = _db(tmp_path)
    with db.transaction() as con:
        ChangeDB.insert(con, [_rec(f"t{i}", "q1_xy", "pi_amp", i / 10)
                              for i in range(1, 8)], store="state")
        ChangeDB.insert(con, [_rec("t9", "q1", "t1_s", 3e-5)],
                        store="physical")
    assert len(db.param_series("q1_xy", "pi_amp", limit=3)) == 3
    # store=None reaches both stores; explicit store narrows
    assert db.param_series("q1", "t1_s")[0]["new"] == 3e-5
    assert db.param_series("q1", "t1_s", store="state") == []


def test_latest_two_single_double_and_campaign_only_records(tmp_path):
    db = _db(tmp_path)
    with db.transaction() as con:
        ChangeDB.insert(con, [
            _rec("t1", "q1_xy", "pi_amp", 0.1, run_id="r1"),
            _rec("t2", "q1_xy", "pi_amp", 0.2, old=0.1,
                 campaign_id="c1"),                      # campaign accept
            _rec("t3", "q1_xy", "drag_beta", -1.0, run_id="r2"),
        ], store="state")
    pairs = db.latest_two("state")
    latest, prev = pairs[("q1_xy", "pi_amp")]
    assert latest["new"] == 0.2 and latest["campaign_id"] == "c1"
    assert latest["run_id"] is None
    assert prev["new"] == 0.1 and prev["run_id"] == "r1"
    single, none = pairs[("q1_xy", "drag_beta")]
    assert single["new"] == -1.0 and none is None


def test_context_facts_and_fact_series_are_physical_only(tmp_path):
    db = _db(tmp_path)
    with db.transaction() as con:
        ChangeDB.insert(con, [
            _rec("t1", "q1", "t1_s", 3.0e-5),
            _rec("t2", "q1", "t1_s", 3.1e-5, old=3.0e-5),
            _rec("t3", "q1_res", "f_r_hz", 5.9e9),
        ], store="physical")
        ChangeDB.insert(con, [_rec("t4", "q1_xy", "pi_amp", 0.2)],
                        store="state")
    facts = db.context_facts()
    assert {(r["entity"], r["field"]): r["new"] for r in facts} == {
        ("q1", "t1_s"): 3.1e-5, ("q1_res", "f_r_hz"): 5.9e9}
    series = db.fact_series("q1", "t1_s")
    assert [r["new"] for r in series] == [3.0e-5, 3.1e-5]
    assert db.fact_series("q1_xy", "pi_amp") == []       # knob: not a fact


# ------------------------------------------------- cross-context aggregation

def test_collect_helpers_attach_the_folder_context_not_row_stamps(tmp_path):
    a, b = _db(tmp_path / "a"), _db(tmp_path / "b")
    with a.transaction() as con:  # row stamped with a LYING context
        ChangeDB.insert(con, [_rec("t1", "q1", "t1_s", 3.0e-5,
                                   cooldown="bogus", setup="bogus")],
                        store="physical")
    with b.transaction() as con:
        ChangeDB.insert(con, [_rec("t2", "q1", "t1_s", 2.8e-5)],
                        store="physical")
    contexts = [("cd1", "qm_a", a), ("cd2", "qb_a", b)]
    cells = collect_fact_matrix(contexts)
    assert {(c["cooldown"], c["setup"]): c["new"] for c in cells} == {
        ("cd1", "qm_a"): 3.0e-5, ("cd2", "qb_a"): 2.8e-5}
    series = collect_fact_series(contexts, "q1", "t1_s")
    assert [(r["timestamp"], r["cooldown"]) for r in series] == [
        ("t1", "cd1"), ("t2", "cd2")]                   # chronological


# ------------------------------------------------------------ store weaving
# (state_store / physical_store integration — the Store derives its own DB)

def test_both_stores_share_one_context_db_no_sidecars(tmp_path, roster):
    physical = physical_store(tmp_path, roster, setup="qm_a", cooldown="cd1")
    state = state_store(tmp_path, roster, setup="qm_a", cooldown="cd1")
    physical.record("q1", "t1_s", 3e-5)
    state.record("q1_xy", "pi_amp", 0.209)
    physical.save()
    state.save()
    assert (tmp_path / HISTORY_FILE).is_file()
    assert not list(tmp_path.glob("*.history.jsonl"))    # sidecars retired
    rows = physical.history()
    assert len(rows) == 1 and rows[0].cooldown == "cd1"
    assert rows[0].setup == "qm_a" and rows[0].new == 3e-5
    assert [r.new for r in state.history()] == [0.209]   # store-separated


def test_two_sessions_no_lost_rows_latest_timestamp_wins(
        tmp_path, roster, monkeypatch):
    import scqo.stores as stores_mod
    clock = iter(["2026-08-11T10:00:01+08:00", "2026-08-11T10:00:02+08:00",
                  "2026-08-11T10:00:03+08:00"])
    monkeypatch.setattr(stores_mod, "_now", lambda: next(clock))
    a = state_store(tmp_path, roster)
    b = state_store(tmp_path, roster)
    a.record("q1_xy", "pi_amp", 0.111)                   # t1
    b.record("q1_xy", "pi_amp", 0.222)                   # t2
    b.record("q2_xy", "pi_amp", 0.333)                   # t3
    b.save()
    a.save()                                             # saves LAST, older
    fresh = state_store(tmp_path, roster)
    assert fresh.get("q1_xy", "pi_amp") == 0.222         # newest record wins
    assert fresh.get("q2_xy", "pi_amp") == 0.333
    assert len(fresh.history()) == 3                     # nothing lost


def test_history_pushdown_filters(tmp_path, roster):
    state = state_store(tmp_path, roster)
    state.record("q1_xy", "pi_amp", 0.1)
    state.record("q2_xy", "pi_amp", 0.2)
    state.record("q1_xy", "pi_amp", 0.3)
    state.save()
    again = state_store(tmp_path, roster)
    assert [r.new for r in again.history(entity="q1_xy")] == [0.1, 0.3]
    assert [r.new for r in again.history(limit=1)] == [0.3]
    # unsaved buffer rows are visible too
    again.record("q1_xy", "drag_beta", -1.0)
    assert again.history()[-1].new == -1.0


def test_in_memory_store_keeps_history_in_buffer_only(roster):
    state = state_store(None, roster)
    state.record("q1_xy", "pi_amp", 0.209)
    state.save()                                         # no-op, no file
    assert [r.new for r in state.history()] == [0.209]


def test_device_level_escape_hatch_db_lands_next_to_physical_json(
        tmp_path, roster):
    device_dir = tmp_path / "chipA"
    physical = Store(device_dir / PHYSICAL_FILE, roster,
                     roles=frozenset({"fact"}))
    physical.record("q1", "t1_s", 3e-5)
    physical.save()
    db = ChangeDB(device_dir / HISTORY_FILE)
    rows = db.context_history("physical")
    assert len(rows) == 1
    assert rows[0]["cooldown"] == "" and rows[0]["setup"] == ""


# ----------------------------------------------------------- merge-by-copy

def test_folder_copy_merges_two_data_roots(tmp_path, roster):
    """The collaborator scenario: same device + cooldown, different setups,
    separate data_roots — a plain folder copy (robocopy semantics) merges
    the histories because a context has exactly one writing machine."""
    root_a, root_b = tmp_path / "rootA", tmp_path / "rootB"
    ctx_a = root_a / "chip" / "cd1" / "qm_main" / "scqo"
    ctx_b = root_b / "chip" / "cd1" / "qblox_main" / "scqo"
    pa = physical_store(ctx_a, roster, setup="qm_main", cooldown="cd1")
    pa.record("q1", "t1_s", 3.0e-5)
    pa.save()
    pb = physical_store(ctx_b, roster, setup="qblox_main", cooldown="cd1")
    pb.record("q1", "t1_s", 2.8e-5)
    pb.save()
    shutil.copytree(root_b / "chip", root_a / "chip", dirs_exist_ok=True)
    contexts = [
        ("cd1", name,
         ChangeDB(root_a / "chip" / "cd1" / name / "scqo" / HISTORY_FILE))
        for name in ("qm_main", "qblox_main")]
    series = collect_fact_series(contexts, "q1", "t1_s")
    assert {(r["setup"], r["new"]) for r in series} == {
        ("qm_main", 3.0e-5), ("qblox_main", 2.8e-5)}


# --------------------------------------------------------------- guarantees

def test_read_side_never_creates_the_file(tmp_path):
    db = ChangeDB(tmp_path / "scqo" / HISTORY_FILE)
    assert db.context_history("state") == []
    assert db.param_series("q1_xy", "pi_amp") == []
    assert db.latest_two("state") == {}
    assert db.context_facts() == []
    assert db.fact_series("q1", "t1_s") == []
    assert not (tmp_path / "scqo").exists()


def test_version_gate_refuses_newer_on_write_still_reads(tmp_path):
    db = _db(tmp_path)
    with db.transaction() as con:
        ChangeDB.insert(con, [_rec("t1", "q1_xy", "pi_amp", 0.2)],
                        store="state")
    con = sqlite3.connect(tmp_path / HISTORY_FILE)
    con.execute("UPDATE meta SET value = '99' "
                "WHERE key = 'changes_schema_version'")
    con.commit()
    con.close()
    with pytest.raises(ChangesError, match="newer scqo"):
        with db.transaction():
            pass
    assert db.context_history("state")[0]["new"] == 0.2  # reads degrade fine


def test_vetoed_transaction_commits_nothing(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(RuntimeError, match="veto"):
        with db.transaction() as con:
            ChangeDB.insert(con, [_rec("t1", "q1_xy", "pi_amp", 0.2)],
                            store="state")
            raise RuntimeError("veto")
    assert db.context_history("state") == []


def test_history_db_survives_index_schema_bump(tmp_path, roster, monkeypatch):
    """index.sqlite is a disposable cache that gets dropped on version
    bumps; history.sqlite is TRUTH and must be untouched by that."""
    import scqo.datastore as datastore_mod
    from scqo.datastore import DataStore

    ctx = tmp_path / "chip" / "cd1" / "qm_main" / "scqo"
    state = state_store(ctx, roster, setup="qm_main", cooldown="cd1")
    state.record("q1_xy", "pi_amp", 0.209)
    state.save()
    DataStore(tmp_path)                                  # index at version N
    monkeypatch.setattr(datastore_mod, "SCHEMA_VERSION",
                        datastore_mod.SCHEMA_VERSION + 1)
    DataStore(tmp_path)                                  # stale -> drop+rebuild
    rows = ChangeDB(ctx / HISTORY_FILE).context_history("state")
    assert [r["new"] for r in rows] == [0.209]


def test_old_and_new_json_encode_losslessly(tmp_path):
    db = _db(tmp_path)
    wave = [0.0, 0.4000000000000001, -0.2]
    with db.transaction() as con:
        ChangeDB.insert(con, [_rec("t1", "q1_z", "distortion_amp", wave,
                                   old=[0.1])], store="physical")
    raw = sqlite3.connect(tmp_path / HISTORY_FILE).execute(
        "SELECT old, new FROM changes").fetchone()
    assert json.loads(raw[0]) == [0.1] and json.loads(raw[1]) == wave
