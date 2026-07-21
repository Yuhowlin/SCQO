"""Datastore: every run saved to a self-describing folder + a rebuildable SQLite index.

All offline (SimulatedBackend, tmp_path). The run folder is the truth; the index is a
disposable cache — several tests delete/rebuild it to prove that.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from scqo import Outcome, Session, register, reindex
from scqo.experiments import QubitRamsey, ResonatorSpectroscopy
from scqo.testing import InMemoryDevice, SimulatedBackend, demo_roster


# Concrete demo experiments (probe is a no-op under SimulatedBackend); registering under
# the canonical names is idempotent across test modules.
@register
class _DsResonatorSpectroscopy(ResonatorSpectroscopy):
    def probe(self):
        return None


@register
class _DsQubitRamsey(QubitRamsey):
    def probe(self):
        return None


@register
class _BrokenResonatorSpectroscopy(ResonatorSpectroscopy):
    """Test-only: violates its own contract (drops Q) to exercise failed-run persistence."""

    name = "broken_resonator_spectroscopy"
    description = "test-only contract violation"

    def probe(self):
        return None

    def simulate(self, coords):
        data = super().simulate(coords)
        del data["Q"]
        return data


def _device() -> InMemoryDevice:
    return InMemoryDevice(
        {
            "q0": {"readout_freq": 5.95e9, "drive_freq": 3.87e9, "pi_amp": 0.2, "readout_amp": 0.25},
            "q1": {"readout_freq": 6.05e9, "drive_freq": 4.01e9, "pi_amp": 0.18, "readout_amp": 0.22},
        }
    )


def _session(tmp_path, **kwargs) -> Session:
    return Session(SimulatedBackend(_device()), demo_roster(), data_root=tmp_path / "data",
               device_name="devA", **kwargs)


RAMSEY_PARAMS = {"targets": ["q1"], "frequency_detuning_hz": 1.0e6, "max_idle_time_ns": 4000, "num_points": 201}


def test_run_persists_full_layout(tmp_path):
    sess = _session(tmp_path)
    result = sess.run("resonator_spectroscopy", {"targets": ["q0", "q1"]}, update="apply")
    assert result["outcomes"]["q0"] == Outcome.SUCCESSFUL.value
    assert "datastore_error" not in result

    run_dir = Path(result["data_path"])
    assert run_dir.name == result["run_id"]
    assert run_dir.parent.parent.name == "devA"  # <data_root>/devA/<date>/<run_id>
    for fname in (
        "record.json", "dataset.nc", "parameters.json", "result.json",
        "device_before.json", "device_after.json",
    ):
        assert (run_dir / fname).is_file(), fname

    # scqat artifacts land per qubit: metadata is mandatory, figures come with it
    for q in ("q0", "q1"):
        qdir = run_dir / "analysis" / q
        assert list(qdir.glob("*_metadata.json"))
        assert list(qdir.glob("*.png"))

    record = json.loads((run_dir / "record.json").read_text(encoding="utf-8"))
    assert record["run_id"] == result["run_id"]
    assert record["outcome"] == "successful"
    assert record["updated_device"] is True
    assert record["targets"] == ["q0", "q1"]
    # the applied updates carry their audit trail on the record (the truth)
    assert {s["status"] for s in record["suggestions"]} == {"accepted"}

    # device_before/after snapshots bracket the (applied) writeback
    before = json.loads((run_dir / "device_before.json").read_text(encoding="utf-8"))
    after = json.loads((run_dir / "device_after.json").read_text(encoding="utf-8"))
    assert after["q0"]["readout_freq"] != before["q0"]["readout_freq"]


def test_default_run_stores_pending_suggestions(tmp_path):
    """The v0.6 default: nothing applied, the proposal is stored on the record and
    findable via the pending filter — and the decision store survives a reindex."""
    sess = _session(tmp_path)
    result = sess.run("resonator_spectroscopy", {"targets": ["q0"]})
    run_dir = Path(result["data_path"])

    record = json.loads((run_dir / "record.json").read_text(encoding="utf-8"))
    assert record["updated_device"] is False
    assert [s["field"] for s in record["suggestions"]] == ["readout_freq", "f_r_hz", "kappa_tot_hz"]
    assert {s["status"] for s in record["suggestions"]} == {"pending"}

    before = json.loads((run_dir / "device_before.json").read_text(encoding="utf-8"))
    after = json.loads((run_dir / "device_after.json").read_text(encoding="utf-8"))
    assert before == after  # nothing was applied at run time

    assert [r["run_id"] for r in sess.find_runs(pending=True)] == [result["run_id"]]
    assert sess.find_runs(pending=False) == []
    assert sess.datastore.reindex() == 1  # pending state lives in record.json
    assert [r["run_id"] for r in sess.find_runs(pending=True)] == [result["run_id"]]


def test_update_suggestions_append_recomputes_pending(tmp_path):
    """The contract Session.suggest relies on: update_suggestions accepts a LONGER
    list (append) and recomputes the index's pending counter from it."""
    sess = _session(tmp_path)
    result = sess.run("resonator_spectroscopy", {"targets": ["q0"]})
    run_id = result["run_id"]
    sess.accept(run_id)  # everything decided
    assert sess.find_runs(pending=True) == []

    record = sess.load_run(run_id)["record"]
    appended = record["suggestions"] + [{
        "component": "q0", "field": "pi_amp", "store": "instrument",
        "before": 0.2, "after": 0.21, "status": "pending",
        "origin": "operator", "proposed_by": "alice",
    }]
    sess.datastore.update_suggestions(run_id, appended)

    assert [r["run_id"] for r in sess.find_runs(pending=True)] == [run_id]
    row = sess.find_runs()[0]
    assert row["suggestions_pending"] == 1 and len(row["suggestions"]) == 4
    assert sess.datastore.reindex() == 1  # record.json carries the appended item
    assert [r["run_id"] for r in sess.find_runs(pending=True)] == [run_id]


def test_find_runs_filters(tmp_path):
    sess = _session(tmp_path)
    r1 = sess.run("resonator_spectroscopy", {"targets": ["q0"]})
    r2 = sess.run("qubit_ramsey", RAMSEY_PARAMS, tags=["special"])
    assert r1.get("error") is None, r1["error"]
    assert r2.get("error") is None, r2["error"]

    runs = sess.find_runs()
    assert [r["run_id"] for r in runs] == [r2["run_id"], r1["run_id"]]  # newest first

    assert [r["run_id"] for r in sess.find_runs(experiment="qubit_ramsey")] == [r2["run_id"]]
    assert [r["run_id"] for r in sess.find_runs(target="q0")] == [r1["run_id"]]
    assert [r["run_id"] for r in sess.find_runs(tag="special")] == [r2["run_id"]]
    assert sess.find_runs(outcome="successful", experiment="resonator_spectroscopy")
    assert sess.find_runs(since="2000-01-01") and not sess.find_runs(until="2000-01-01")
    assert sess.find_runs(device="devA") and not sess.find_runs(device="other")

    # key fit values are queryable straight from the index ("what T2* did q1 get?")
    assert "t2_star_s" in sess.find_runs(experiment="qubit_ramsey")[0]["fit"]["q1"]


def test_tags_default_and_retroactive(tmp_path):
    sess = _session(tmp_path, default_tags=["cooldown7"])
    r = sess.run("resonator_spectroscopy", {"targets": ["q0"]}, tags=["extra"], note="after wiring fix")

    row = sess.find_runs(tag="cooldown7")[0]
    assert row["run_id"] == r["run_id"]
    assert row["tags"] == ["cooldown7", "extra"]
    assert row["note"] == "after wiring fix"

    sess.tag_run(r["run_id"], add=["thesis-fig3"], remove=["extra"])
    assert sess.find_runs(tag="thesis-fig3")
    assert not sess.find_runs(tag="extra")

    # record.json is the truth: a full rebuild keeps the retro-tag
    assert sess.datastore.reindex() == 1
    assert sess.find_runs(tag="thesis-fig3")


def test_suggest_during_tag_window_survives(tmp_path, monkeypatch):
    """REGRESSION (record.json race): an operator suggestion landing INSIDE a
    concurrent tag_run's read->write window must survive — tag_run rewrites the WHOLE
    record, so it re-reads under the record lock rather than storing a snapshot taken
    before the suggestion existed. Mirrors the accept-vs-suggest pair in
    tests/test_suggestions.py, with the tagger as the whole-record writer."""
    from scqo import datastore as datastore_mod

    sess_a = _session(tmp_path)
    run_id = sess_a.run("resonator_spectroscopy", {"targets": ["q0"]})["run_id"]
    sess_b = _session(tmp_path)  # a second terminal on the same lab

    errors: list[Exception] = []

    def suggest_concurrently():
        try:
            sess_b.suggest(run_id, {"q0.pi_amp": 0.21}, comment="operator, mid-tag")
        except Exception as err:  # pragma: no cover - the assertion below reports it
            errors.append(err)

    suggester = threading.Thread(target=suggest_concurrently)

    at_store = threading.Event()  # b has read the record and is about to store its item
    real_edit = sess_b.datastore.edit_suggestions

    def edit_announcing_entry(*args, **kwargs):
        at_store.set()
        return real_edit(*args, **kwargs)

    sess_b.datastore.edit_suggestions = edit_announcing_entry

    real_write = datastore_mod._write_json
    tagging = []

    def write_with_concurrent_suggest(path, payload):
        # Fires ONCE, on tag_run's own record write — b then races that very write.
        if not tagging and Path(path).name == "record.json":
            tagging.append(True)
            suggester.start()
            at_store.wait(5)
            # Unlocked, b's item lands here and the write below erases it; locked, b
            # is parked on the record lock and only proceeds once we release it.
            suggester.join(1.0)
        return real_write(path, payload)

    monkeypatch.setattr(datastore_mod, "_write_json", write_with_concurrent_suggest)
    sess_a.tag_run(run_id, add=["thesis-fig3"], note="tagged mid-suggest")
    suggester.join(10)
    assert not errors and not suggester.is_alive()

    record = sess_a.load_run(run_id)["record"]
    assert record["tags"] == ["thesis-fig3"] and record["note"] == "tagged mid-suggest"
    assert [s["field"] for s in record["suggestions"]] == [  # nothing clobbered
        "readout_freq", "f_r_hz", "kappa_tot_hz", "pi_amp"]
    assert record["suggestions"][-1]["origin"] == "operator"

    # record.json is the truth: both writers' edits survive a full rebuild
    assert sess_a.datastore.reindex() == 1
    assert [r["run_id"] for r in sess_a.find_runs(tag="thesis-fig3", pending=True)] == [run_id]
    assert sess_a.find_runs()[0]["suggestions_pending"] == 4


def test_tag_during_suggest_window_survives(tmp_path):
    """REGRESSION (record.json race, reverse order): a retro-tag completing inside
    suggest's load->write window must keep its tags — suggest edits the FRESH stored
    list under the record lock, and tag_run holds that lock only across its own
    read-write (a lock held across the nested writer would deadlock, not clobber)."""
    sess_a = _session(tmp_path)
    run_id = sess_a.run("resonator_spectroscopy", {"targets": ["q0"]})["run_id"]
    sess_b = _session(tmp_path)

    real_load = sess_b._load_run_record

    def load_then_concurrent_tag(rid):
        out = real_load(rid)
        sess_a.tag_run(run_id, add=["thesis-fig3"], note="tagged mid-suggest")
        return out

    sess_b._load_run_record = load_then_concurrent_tag
    summary = sess_b.suggest(run_id, {"q0.pi_amp": 0.21})
    assert summary["pending_total"] == 4  # 3 estimator rows + the operator's

    record = sess_b.load_run(run_id)["record"]
    assert record["tags"] == ["thesis-fig3"]  # NOT reverted by the suggestion write
    assert record["note"] == "tagged mid-suggest"
    assert [s["field"] for s in record["suggestions"]] == [
        "readout_freq", "f_r_hz", "kappa_tot_hz", "pi_amp"]
    assert [r["run_id"] for r in sess_b.find_runs(tag="thesis-fig3")] == [run_id]


def test_reindex_rebuilds_deleted_index(tmp_path):
    sess = _session(tmp_path)
    r1 = sess.run("resonator_spectroscopy", {"targets": ["q0"]})
    r2 = sess.run("qubit_ramsey", RAMSEY_PARAMS)
    before = [(r["run_id"], r["outcome"], r["tags"]) for r in sess.find_runs()]

    data_root = tmp_path / "data"
    for f in data_root.glob("index.sqlite*"):  # main db + -wal/-shm
        f.unlink()
    assert reindex(data_root) == 2

    sess2 = _session(tmp_path)  # same data_root, fresh Session
    after = [(r["run_id"], r["outcome"], r["tags"]) for r in sess2.find_runs()]
    assert after == before
    assert {r1["run_id"], r2["run_id"]} == {r[0] for r in after}


def test_failed_run_is_persisted_and_findable(tmp_path):
    sess = _session(tmp_path)
    result = sess.run("broken_resonator_spectroscopy", {"targets": ["q0"]})
    assert result["error"]
    assert result["outcomes"]["q0"] == Outcome.NO_DATA.value
    assert "run_id" in result  # failed runs are saved too — that's the debugging story

    run_dir = Path(result["data_path"])
    assert (run_dir / "record.json").is_file()
    assert (run_dir / "dataset.nc").is_file()  # the nonconforming dataset is kept
    row = sess.find_runs(outcome="no_data")[0]
    assert row["run_id"] == result["run_id"]
    assert row["updated_device"] is False


def test_load_run_and_open_dataset(tmp_path):
    sess = _session(tmp_path)
    r = sess.run("resonator_spectroscopy", {"targets": ["q0", "q1"], "num_points": 51})

    loaded = sess.load_run(r["run_id"])
    assert loaded["record"]["experiment"] == "resonator_spectroscopy"
    assert loaded["parameters"]["num_points"] == 51
    assert loaded["result"]["outcomes"] == r["outcomes"]
    assert loaded["figures"]  # PNG paths, ready for a viewer

    ds = sess.datastore.open_dataset(r["run_id"])
    assert set(ds.data_vars) >= {"I", "Q"}
    assert list(ds["target"].values) == ["q0", "q1"]
    assert ds.sizes["detuning_hz"] == 51


def test_history_links_to_run_id(tmp_path):
    sess = _session(tmp_path)
    r = sess.run("resonator_spectroscopy", {"targets": ["q0"]}, update="apply")
    hist = sess.history()
    assert hist
    assert all(h["run_id"] == r["run_id"] for h in hist)


@register
class _UpdateExplodes(ResonatorSpectroscopy):
    """Test-only: fit succeeds but the device rejects the writeback."""

    name = "update_explodes"
    description = "test-only update failure"

    def probe(self):
        return None

    def update(self):
        raise RuntimeError("vendor rejected value")


def test_update_failure_is_structured_and_run_still_persisted(tmp_path):
    """A raising update() must not raise across the boundary, and must not lose the
    measurement — the capture phase reports it as a structured error."""
    sess = _session(tmp_path)
    result = sess.run("update_explodes", {"targets": ["q0"]})
    assert result["outcomes"]["q0"] == Outcome.SUCCESSFUL.value  # the measurement itself
    assert "suggestion capture failed" in result["error"]
    assert result["suggestions"] == []  # nothing proposable came out of it
    assert "run_id" in result  # persisted despite the failed capture
    assert (Path(result["data_path"]) / "record.json").is_file()
    assert sess.find_runs(experiment="update_explodes")[0]["updated_device"] is False


def test_dates_are_local_and_until_is_day_inclusive(tmp_path):
    """Folder dates, run_id and started_at all use local time, so the date a user
    sees is the date the filter matches; a bare-date `until` includes that day."""
    from datetime import date

    sess = _session(tmp_path)
    r = sess.run("resonator_spectroscopy", {"targets": ["q0"]})
    today = date.today().isoformat()
    assert Path(r["data_path"]).parent.name == today  # folder date == local date
    assert sess.find_runs(since=today)  # a run made "today" is found with since=today
    assert sess.find_runs(until=today)  # ...and with until=today (inclusive)
    row = sess.find_runs()[0]
    assert row["started_at"][:10] == today  # index timestamp matches the folder date


def test_old_index_schema_triggers_rebuild(tmp_path):
    import sqlite3

    sess = _session(tmp_path)
    r = sess.run("resonator_spectroscopy", {"targets": ["q0"]})
    con = sqlite3.connect(sess.datastore._db_path)
    con.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    con.commit()
    con.close()

    sess2 = _session(tmp_path)  # version mismatch -> automatic rebuild from folders
    assert [x["run_id"] for x in sess2.find_runs()] == [r["run_id"]]


def test_without_data_root_behaves_as_before(tmp_path):
    sess = Session(SimulatedBackend(_device()), demo_roster())
    result = sess.run("resonator_spectroscopy", {"targets": ["q0"]}, update="apply")
    assert result["outcomes"]["q0"] == Outcome.SUCCESSFUL.value
    assert "run_id" not in result and "data_path" not in result
    assert result["suggestions"]  # returned even without a datastore (the AI loop)
    assert sess.find_runs() == []
    assert sess.history()[0]["run_id"] is None


def test_multi_device_one_index(tmp_path):
    """Several samples share ONE data_root + ONE index; device = the sample name.
    Qubit names repeat across chips, so fit_trend must be scopeable by device."""
    from scqo import DataStore

    ra = _session(tmp_path).run("resonator_spectroscopy", {"targets": ["q0"]})  # devA
    sess_b = Session(SimulatedBackend(_device()), demo_roster(),
                     data_root=tmp_path / "data", device_name="devB")
    rb = sess_b.run("resonator_spectroscopy", {"targets": ["q0"]})

    store = DataStore(tmp_path / "data")
    assert store.distinct_devices() == ["devA", "devB"]
    assert [r["run_id"] for r in store.find_runs(device="devB")] == [rb["run_id"]]

    both = store.fit_trend("q0", "readout_freq")
    assert {r["run_id"] for r in both} == {ra["run_id"], rb["run_id"]}
    scoped = store.fit_trend("q0", "readout_freq", device="devB")
    assert [r["run_id"] for r in scoped] == [rb["run_id"]]


def test_operator_is_stamped_and_survives_reindex(tmp_path):
    """Multi-user provenance: every run records the OS login of whoever ran it."""
    import getpass

    from scqo import DataStore

    sess = _session(tmp_path)
    r = sess.run("resonator_spectroscopy", {"targets": ["q0"]})
    me = getpass.getuser()

    store = DataStore(tmp_path / "data")
    assert store.find_runs()[0]["operator"] == me
    assert store.find_runs(operator=me)[0]["run_id"] == r["run_id"]
    assert store.find_runs(operator="somebody-else") == []

    (tmp_path / "data" / "index.sqlite").unlink()  # operator lives in record.json too
    reindex(tmp_path / "data")
    assert DataStore(tmp_path / "data").find_runs(operator=me)[0]["run_id"] == r["run_id"]


def test_run_ids_unique_across_devices_same_second(tmp_path):
    """Two samples allocating in the same wall-clock second must not share a run_id
    (run_ids embed the device name — /run/{id} and tag_run stay unambiguous)."""
    from scqo import DataStore

    a = DataStore(tmp_path / "data", device_name="devA")
    b = DataStore(tmp_path / "data", device_name="devB")
    id_a, _ = a.new_run_dir("resonator_spectroscopy")  # same second, same experiment
    id_b, _ = b.new_run_dir("resonator_spectroscopy")
    assert id_a != id_b
    assert "devA" in id_a and "devB" in id_b


def test_device_registry_loader(tmp_path):
    """devices.toml is optional, instrument-independent, and a typo never raises."""
    from scqo.datastore import load_device_registry

    assert load_device_registry(tmp_path) == {}
    (tmp_path / "devices.toml").write_text(
        '[chipA]\ndescription = "demo"\nmounted_on = "qblox"\n[chipA.design]\nEC_MHz = 200\n',
        encoding="utf-8",
    )
    reg = load_device_registry(tmp_path)
    assert reg["chipA"]["description"] == "demo"
    assert reg["chipA"]["design"]["EC_MHz"] == 200

    (tmp_path / "devices.toml").write_text("not [valid toml", encoding="utf-8")
    assert load_device_registry(tmp_path) == {}  # broken hand-edit -> warn, not crash


def _write_cooldowns(data_root: Path, device: str, text: str) -> Path:
    ddir = data_root / device
    ddir.mkdir(parents=True, exist_ok=True)
    path = ddir / "cooldowns.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_cooldown_registry_loader(tmp_path):
    """device -> cycle (packaging fixed) -> NAMED setup tables; the name is the
    setup's identity — one when the cycle has exactly one, by name otherwise."""
    from scqo.datastore import active_cooldown, load_cooldowns, resolve_setup

    assert load_cooldowns(tmp_path, "devA") == {}  # absent -> no cycles, no stamps

    _write_cooldowns(
        tmp_path, "devA",
        "[cd1]\nstart = 2026-01-01\nend = 2026-02-01\n"
        '[cd1.setup.sim]\nbackend = "simulated"\n\n'
        '[cd2]\nstart = 2026-07-01\nfridge = "BlueforsA"\npackaging = "PCB v3"\n\n'
        '[cd2.setup.qblox_main]\nbackend = "qblox"\n\n'
        '[cd2.setup.qblox_spare]\nnote = "out0 dead"\nbackend = "qblox"\n',
    )
    cycles = load_cooldowns(tmp_path, "devA")
    cid, cycle = active_cooldown(cycles)
    assert cid == "cd2"
    assert cycle["packaging"] == "PCB v3"
    assert isinstance(cycle["setup"], dict)  # setups keyed by NAME
    assert set(cycle["setup"]) == {"qblox_main", "qblox_spare"}

    # single-setup cycle: resolve_setup picks the only one without a selection
    name, setup = resolve_setup(cycles["cd1"])
    assert name == "sim" and setup["backend"] == "simulated"

    # multi-setup cycle: selection by name returns THAT setup; the vendor folder is
    # DERIVED from the keys and injected (absolute) — never typed in the registry
    name, setup = resolve_setup(cycle, "qblox_spare")
    assert name == "qblox_spare"
    assert setup["backend"] == "qblox"
    assert setup["note"] == "out0 dead"
    from pathlib import Path as _P
    assert _P(setup["instrument_config"]) == (
        tmp_path / "devA" / "cd2" / "qblox_spare" / "backend_config").resolve()

    # structured failures (reason + available drive the CLI's exact-fix messages)
    import pytest

    from scqo.datastore import SetupResolutionError

    with pytest.raises(SetupResolutionError) as ei:
        resolve_setup(cycle)  # several setups, no selection
    assert ei.value.reason == "ambiguous"
    assert ei.value.available == ["qblox_main", "qblox_spare"]
    with pytest.raises(SetupResolutionError) as ei:
        resolve_setup(cycle, "nope")
    assert ei.value.reason == "unknown"
    assert ei.value.available == ["qblox_main", "qblox_spare"]
    with pytest.raises(SetupResolutionError) as ei:
        resolve_setup({"setup": {}})  # empty cycle
    assert ei.value.reason == "none" and ei.value.available == []


def test_cooldown_registry_validation_is_loud(tmp_path):
    """This file stamps and DRIVES runs — a broken registry must fail loudly."""
    import pytest

    from scqo.datastore import load_cooldowns

    sim = '[%s.setup.sim]\nbackend = "simulated"\n'
    _write_cooldowns(tmp_path, "devA",
                     "[cd1]\nstart = 2026-01-01\n" + sim % "cd1"
                     + "\n[cd2]\nstart = 2026-07-01\n" + sim % "cd2")
    with pytest.raises(ValueError, match="more than one open cycle"):
        load_cooldowns(tmp_path, "devA")

    # ZERO setups is legal at LOAD time since v0.7.0 (the manager hand-adds blocks
    # later; runs refuse at session-build time, not here).
    _write_cooldowns(tmp_path, "devA", "[cd1]\nstart = 2026-01-01\n")
    cycles = load_cooldowns(tmp_path, "devA")
    assert cycles["cd1"].get("setup", {}) == {}

    # the retired v0.6 [[<id>.setup]] ARRAY form must fail loudly, naming the fix
    _write_cooldowns(tmp_path, "devA",
                     '[cd1]\nstart = 2026-01-01\n[[cd1.setup]]\nbackend = "simulated"\n')
    with pytest.raises(ValueError, match=r"retired \[\[cd1\.setup\]\] array form"):
        load_cooldowns(tmp_path, "devA")

    # 'since' dates are retired (the setup NAME is its identity)
    _write_cooldowns(tmp_path, "devA",
                     '[cd1]\nstart = 2026-01-01\n[cd1.setup.sim]\n'
                     'since = 2026-01-01\nbackend = "simulated"\n')
    with pytest.raises(ValueError, match=r"unknown key\(s\): since"):
        load_cooldowns(tmp_path, "devA")

    # port-map pairs are retired (wiring lives in the vendor config folder)
    _write_cooldowns(tmp_path, "devA",
                     '[cd1]\nstart = 2026-01-01\n[cd1.setup.sim]\nbackend = "simulated"\n'
                     '"q0.drive" = "cluster0.module2.out0"\n')
    with pytest.raises(ValueError, match=r"unknown key\(s\): q0\.drive"):
        load_cooldowns(tmp_path, "devA")

    # setup names travel as CLI args / index values / URL params — keep them plain
    _write_cooldowns(tmp_path, "devA",
                     '[cd1]\nstart = 2026-01-01\n[cd1.setup."my setup"]\nbackend = "simulated"\n')
    with pytest.raises(ValueError, match="letters/digits"):
        load_cooldowns(tmp_path, "devA")

    _write_cooldowns(tmp_path, "devA",
                     '[cd1]\nstart = 2026-01-01\n[cd1.setup.main]\nbackend = "opx"\n')
    with pytest.raises(ValueError, match="'backend' must be one of"):
        load_cooldowns(tmp_path, "devA")

    # 'instrument_config' is retired in v0.9 — the vendor folder is DERIVED from the
    # keys; a typed path (any backend) is refused naming the derived folder.
    _write_cooldowns(tmp_path, "devA",
                     '[cd1]\nstart = 2026-01-01\n[cd1.setup.main]\n'
                     'backend = "qblox"\ninstrument_config = "somewhere"\n')
    with pytest.raises(ValueError, match="retired in v0.9"):
        load_cooldowns(tmp_path, "devA")

    path = _write_cooldowns(tmp_path, "devA", "not [valid toml")
    with pytest.raises(ValueError, match="cooldowns.toml"):
        load_cooldowns(tmp_path, "devA")
    assert path.is_file()  # never silently repaired


def test_runs_stamp_cooldown_and_setup_name(tmp_path):
    """Every run carries its full environment provenance: cycle id + the NAME of
    the setup in effect. A single-setup cycle auto-stamps its only setup; once a
    second setup appears, an UNBOUND session stamps "" and a bound one its name."""
    from scqo import DataStore, reindex

    data_root = tmp_path / "data"
    _write_cooldowns(
        data_root, "devA",
        '[cd7]\nstart = 2026-01-05\npackaging = "PCB v2"\n\n'
        '[cd7.setup.simA]\nbackend = "simulated"\n',
    )
    # (a) single-setup cycle: a Session with NO setup_name auto-resolves the only one
    sess = _session(tmp_path)
    r1 = sess.run("resonator_spectroscopy", {"targets": ["q0"]})
    rec1 = json.loads((Path(r1["data_path"]) / "record.json").read_text(encoding="utf-8"))
    assert rec1["cooldown"] == "cd7"
    assert rec1["setup"] == "simA"

    # (b) a second setup appears mid-cycle (broken channel scenario)
    path = data_root / "devA" / "cooldowns.toml"
    path.write_text(
        path.read_text(encoding="utf-8")
        + '\n[cd7.setup.simB]\nnote = "out0 dead"\nbackend = "simulated"\n',
        encoding="utf-8",
    )
    # ...an UNBOUND session can no longer auto-pick: it stamps "" (tolerant; the
    # CLI chain is the loud enforcer)
    r2 = sess.run("resonator_spectroscopy", {"targets": ["q0"]})
    rec2 = json.loads((Path(r2["data_path"]) / "record.json").read_text(encoding="utf-8"))
    assert rec2["cooldown"] == "cd7"
    assert rec2["setup"] == ""

    # ...a BOUND store/session stamps its name (bound once, not re-validated)
    assert DataStore(data_root, device_name="devA", setup="simB").run_stamps() == ("cd7", "simB")
    sess_b = _session(tmp_path, setup_name="simB")
    r3 = sess_b.run("resonator_spectroscopy", {"targets": ["q0"]})
    rec3 = json.loads((Path(r3["data_path"]) / "record.json").read_text(encoding="utf-8"))
    assert rec3["cooldown"] == "cd7"
    assert rec3["setup"] == "simB"

    # (c) index filters + reindex survival (record.json is the truth; column 'setup')
    all_ids = {r1["run_id"], r2["run_id"], r3["run_id"]}
    assert {r["run_id"] for r in sess.find_runs(cooldown="cd7")} == all_ids
    assert sess.find_runs(cooldown="nope") == []
    assert [r["run_id"] for r in sess.find_runs(setup="simB")] == [r3["run_id"]]
    assert [r["run_id"] for r in sess.find_runs(setup="simA")] == [r1["run_id"]]
    (data_root / "index.sqlite").unlink()
    reindex(data_root)
    assert len(sess.find_runs(cooldown="cd7")) == 3
    assert [r["run_id"] for r in sess.find_runs(setup="simB")] == [r3["run_id"]]
    assert sess.find_runs(setup="simB")[0]["setup"] == "simB"


def test_run_without_registry_stamps_empty(tmp_path):
    """Library Sessions stay tolerant (the CLI chain is the loud enforcer)."""
    sess = _session(tmp_path)
    r = sess.run("resonator_spectroscopy", {"targets": ["q0"]})
    rec = json.loads((Path(r["data_path"]) / "record.json").read_text(encoding="utf-8"))
    assert rec["cooldown"] == "" and rec["setup"] == ""


def test_broken_registry_fails_at_run_start(tmp_path):
    """A corrupt cooldowns.toml must surface BEFORE any instrument time is spent —
    not after the measurement as a datastore_error that discards the data."""
    import pytest

    data_root = tmp_path / "data"
    _write_cooldowns(data_root, "devA", "not [valid toml")
    sess = _session(tmp_path)
    with pytest.raises(ValueError, match="cooldowns.toml"):
        sess.run("resonator_spectroscopy", {"targets": ["q0"]})


def test_old_index_auto_reindexes_to_v8(tmp_path):
    """Pre-cutover records carry 'qubits' (and v0.6 ones 'setup_since'/no 'setup');
    opening a v8 DataStore over an old index rebuilds from the folders: the old
    fields are dropped, 'qubits' maps to the 'targets' column and meta says 8."""
    import sqlite3

    from scqo import DataStore

    sess = _session(tmp_path)
    r = sess.run("resonator_spectroscopy", {"targets": ["q0"]})

    # rewrite the record as an old one: qubits + setup_since present, setup absent
    rec_path = Path(r["data_path"]) / "record.json"
    record = json.loads(rec_path.read_text(encoding="utf-8"))
    del record["setup"]
    record["qubits"] = record.pop("targets")
    record["setup_since"] = "2026-01-05"
    record["schema_version"] = 6
    rec_path.write_text(json.dumps(record), encoding="utf-8")

    db_path = tmp_path / "data" / "index.sqlite"
    con = sqlite3.connect(db_path)
    con.execute("UPDATE meta SET value = '6' WHERE key = 'schema_version'")
    con.commit()
    con.close()

    store = DataStore(tmp_path / "data")  # version mismatch -> automatic rebuild
    (row,) = store.find_runs()
    assert row["run_id"] == r["run_id"]
    assert row["targets"] == ["q0"]  # the OLD 'qubits' key stays findable
    assert row["setup"] == ""  # renamed column; the old date value is not carried over
    assert "setup_since" not in row
    con = sqlite3.connect(db_path)
    (version,) = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    con.close()
    assert version == "8"


def test_setup_validation_rejects_any_typed_instrument_config(tmp_path):
    """The key is retired — even a non-string (unquoted TOML date) value must hit
    the LOUD ValueError contract, never a TypeError that escapes `except ValueError`
    consumers (CLI refusal text, viewer device page, scqo doctor)."""
    import pytest

    from scqo.datastore import load_cooldowns

    _write_cooldowns(tmp_path, "devT",
                     "[cd1]\nstart = 2026-07-01\n"
                     '[cd1.setup.qm_main]\nbackend = "qm"\ninstrument_config = 2026-07-12\n')
    with pytest.raises(ValueError, match="retired in v0.9"):
        load_cooldowns(tmp_path, "devT")


def test_resolve_setup_selected_name_on_empty_cycle_is_reason_none():
    """A stale selection on a ZERO-setup cycle must prescribe the hand-add fix
    (reason 'none'), not 'unknown' — `scqo user --setup` cannot select anything."""
    import pytest

    from scqo.datastore import SetupResolutionError, resolve_setup

    with pytest.raises(SetupResolutionError) as ei:
        resolve_setup({"setup": {}}, "gone")
    assert ei.value.reason == "none"


def test_bound_era_pair_survives_a_mid_session_cycle_change(tmp_path):
    """A store bound to (cd1, a) keeps stamping THAT pair even after the manager
    ends cd1 and opens cd2 — mixing cd2 with setup 'a' would stamp an era that
    never existed (and ("", "") would erase the bound setup)."""
    from scqo.datastore import DataStore

    _write_cooldowns(tmp_path, "devE",
                     "[cd1]\nstart = 2026-07-01\n"
                     '[cd1.setup.a]\nbackend = "simulated"\n')
    store = DataStore(tmp_path, device_name="devE", setup="a", cooldown="cd1")
    assert store.run_stamps() == ("cd1", "a")

    _write_cooldowns(tmp_path, "devE",
                     "[cd1]\nstart = 2026-07-01\nend = 2026-07-10\n"
                     '[cd1.setup.a]\nbackend = "simulated"\n\n'
                     "[cd2]\nstart = 2026-07-11\n")
    assert store.run_stamps() == ("cd1", "a")  # the bound era, verbatim


def test_power_context_persists_and_reindexes(tmp_path):
    """power_context (v0.8) lands in record.json, survives reindex, and a pre-v0.8
    record without the key loads as {} — no index schema bump."""
    import json as _json

    from scqo.testing import InMemoryDevice, SimulatedBackend

    device = InMemoryDevice(
        {"q0": {"readout_freq": 5.95e9, "drive_freq": 3.87e9, "pi_amp": 0.2,
                "readout_amp": 0.25, "readout_power_dbm": -25.0}}
    )

    class _PowerBackend(SimulatedBackend):
        def power_context(self, qubits):
            return {q: {"output_att_db": 20, "pulse_amp": 0.5,
                        "readout_power_dbm": float("nan")} for q in qubits}

    from scqo import Session

    sess = Session(_PowerBackend(device), demo_roster(), data_root=tmp_path / "data",
                   device_name="devA")
    result = sess.run("resonator_spectroscopy", {"targets": ["q0"]}, update="none")
    run_id = result["run_id"]

    loaded = sess.load_run(run_id)
    record = loaded["record"]
    assert record["power_context"]["q0"]["output_att_db"] == 20
    assert record["power_context"]["q0"]["readout_power_dbm"] is None  # NaN scrubbed

    # a pre-v0.8 record (key absent) still parses and reindexes (Pydantic default {})
    from pathlib import Path as _Path

    from scqo.datastore import RunRecord

    del record["power_context"]
    assert RunRecord(**record).power_context == {}
    rec_path = _Path(loaded["path"]) / "record.json"
    rec_path.write_text(_json.dumps(record), encoding="utf-8")
    assert sess.datastore.reindex() >= 1


def test_cooldown_registry_tolerates_powershell_bom(tmp_path):
    """PowerShell 5.1's `Set-Content -Encoding utf8` writes a UTF-8 BOM; the
    cooldown registry is exactly the file operators write that way — it must
    parse identically (the BOM used to fail as 'Invalid statement at line 1')."""
    from scqo.datastore import load_cooldowns

    dev = tmp_path / "bomdev"
    dev.mkdir()
    body = '[cd1]\nstart = 2026-07-20\n\n[cd1.setup.practice]\nbackend = "simulated"\n'
    (dev / "cooldowns.toml").write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
    cycles = load_cooldowns(tmp_path, "bomdev")
    assert cycles["cd1"]["setup"]["practice"]["backend"] == "simulated"
