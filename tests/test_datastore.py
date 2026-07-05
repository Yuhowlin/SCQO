"""Datastore: every run saved to a self-describing folder + a rebuildable SQLite index.

All offline (SimulatedBackend, tmp_path). The run folder is the truth; the index is a
disposable cache — several tests delete/rebuild it to prove that.
"""

from __future__ import annotations

import json
from pathlib import Path

from scqo import Outcome, Session, register, reindex
from scqo.experiments import QubitRamsey, ResonatorSpectroscopy
from scqo.testing import InMemoryDevice, SimulatedBackend


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
    return Session(SimulatedBackend(_device()), data_root=tmp_path / "data", device_name="devA", **kwargs)


RAMSEY_PARAMS = {"qubits": ["q1"], "frequency_detuning_hz": 1.0e6, "max_idle_time_ns": 4000, "num_points": 201}


def test_run_persists_full_layout(tmp_path):
    sess = _session(tmp_path)
    result = sess.run("resonator_spectroscopy", {"qubits": ["q0", "q1"]})
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
    assert record["qubits"] == ["q0", "q1"]

    # device_before/after snapshots bracket the writeback
    before = json.loads((run_dir / "device_before.json").read_text(encoding="utf-8"))
    after = json.loads((run_dir / "device_after.json").read_text(encoding="utf-8"))
    assert after["q0"]["readout_freq"] != before["q0"]["readout_freq"]


def test_find_runs_filters(tmp_path):
    sess = _session(tmp_path)
    r1 = sess.run("resonator_spectroscopy", {"qubits": ["q0"]})
    r2 = sess.run("qubit_ramsey", RAMSEY_PARAMS, tags=["special"])
    assert r1.get("error") is None, r1["error"]
    assert r2.get("error") is None, r2["error"]

    runs = sess.find_runs()
    assert [r["run_id"] for r in runs] == [r2["run_id"], r1["run_id"]]  # newest first

    assert [r["run_id"] for r in sess.find_runs(experiment="qubit_ramsey")] == [r2["run_id"]]
    assert [r["run_id"] for r in sess.find_runs(qubit="q0")] == [r1["run_id"]]
    assert [r["run_id"] for r in sess.find_runs(tag="special")] == [r2["run_id"]]
    assert sess.find_runs(outcome="successful", experiment="resonator_spectroscopy")
    assert sess.find_runs(since="2000-01-01") and not sess.find_runs(until="2000-01-01")
    assert sess.find_runs(device="devA") and not sess.find_runs(device="other")

    # key fit values are queryable straight from the index ("what T2* did q1 get?")
    assert "t2_star_s" in sess.find_runs(experiment="qubit_ramsey")[0]["fit"]["q1"]


def test_tags_default_and_retroactive(tmp_path):
    sess = _session(tmp_path, default_tags=["cooldown7"])
    r = sess.run("resonator_spectroscopy", {"qubits": ["q0"]}, tags=["extra"], note="after wiring fix")

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


def test_reindex_rebuilds_deleted_index(tmp_path):
    sess = _session(tmp_path)
    r1 = sess.run("resonator_spectroscopy", {"qubits": ["q0"]})
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
    result = sess.run("broken_resonator_spectroscopy", {"qubits": ["q0"]})
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
    r = sess.run("resonator_spectroscopy", {"qubits": ["q0", "q1"], "num_points": 51})

    loaded = sess.load_run(r["run_id"])
    assert loaded["record"]["experiment"] == "resonator_spectroscopy"
    assert loaded["parameters"]["num_points"] == 51
    assert loaded["result"]["outcomes"] == r["outcomes"]
    assert loaded["figures"]  # PNG paths, ready for a viewer

    ds = sess.datastore.open_dataset(r["run_id"])
    assert set(ds.data_vars) >= {"I", "Q"}
    assert list(ds["qubit"].values) == ["q0", "q1"]
    assert ds.sizes["detuning_hz"] == 51


def test_history_links_to_run_id(tmp_path):
    sess = _session(tmp_path)
    r = sess.run("resonator_spectroscopy", {"qubits": ["q0"]})
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
    """A writeback failure must not raise, and must not lose the measurement."""
    sess = _session(tmp_path)
    result = sess.run("update_explodes", {"qubits": ["q0"]})
    assert result["outcomes"]["q0"] == Outcome.SUCCESSFUL.value  # the measurement itself
    assert "update/save failed" in result["error"]
    assert "run_id" in result  # persisted despite the failed writeback
    assert (Path(result["data_path"]) / "record.json").is_file()
    assert sess.find_runs(experiment="update_explodes")[0]["updated_device"] is False


def test_dates_are_local_and_until_is_day_inclusive(tmp_path):
    """Folder dates, run_id and started_at all use local time, so the date a user
    sees is the date the filter matches; a bare-date `until` includes that day."""
    from datetime import date

    sess = _session(tmp_path)
    r = sess.run("resonator_spectroscopy", {"qubits": ["q0"]})
    today = date.today().isoformat()
    assert Path(r["data_path"]).parent.name == today  # folder date == local date
    assert sess.find_runs(since=today)  # a run made "today" is found with since=today
    assert sess.find_runs(until=today)  # ...and with until=today (inclusive)
    row = sess.find_runs()[0]
    assert row["started_at"][:10] == today  # index timestamp matches the folder date


def test_old_index_schema_triggers_rebuild(tmp_path):
    import sqlite3

    sess = _session(tmp_path)
    r = sess.run("resonator_spectroscopy", {"qubits": ["q0"]})
    con = sqlite3.connect(sess.datastore._db_path)
    con.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    con.commit()
    con.close()

    sess2 = _session(tmp_path)  # version mismatch -> automatic rebuild from folders
    assert [x["run_id"] for x in sess2.find_runs()] == [r["run_id"]]


def test_without_data_root_behaves_as_before(tmp_path):
    sess = Session(SimulatedBackend(_device()))
    result = sess.run("resonator_spectroscopy", {"qubits": ["q0"]})
    assert result["outcomes"]["q0"] == Outcome.SUCCESSFUL.value
    assert "run_id" not in result and "data_path" not in result
    assert sess.find_runs() == []
    assert sess.history()[0]["run_id"] is None


def test_multi_device_one_index(tmp_path):
    """Several samples share ONE data_root + ONE index; device = the sample name.
    Qubit names repeat across chips, so fit_trend must be scopeable by device."""
    from scqo import DataStore

    ra = _session(tmp_path).run("resonator_spectroscopy", {"qubits": ["q0"]})  # devA
    sess_b = Session(SimulatedBackend(_device()), data_root=tmp_path / "data", device_name="devB")
    rb = sess_b.run("resonator_spectroscopy", {"qubits": ["q0"]})

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
    r = sess.run("resonator_spectroscopy", {"qubits": ["q0"]})
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
