"""Per-(cooldown, setup) SCQO folders — path convention, registry guards, isolation.

Every setup of every cooldown gets its own ``<device>/<cooldown>/<setup>/scqo/``
folder holding ``scqo_state.json`` (knobs + monitors) and ``physical.json``
(measured facts), plus the shared ``history.sqlite`` change-history database
(scqo.changes — one per context, both stores). SCQO never writes into a setup's
vendor-config ``instrument_config`` folder, so the QM backend's QUAM load never
sweeps up SCQO files. Two users on two setups of ONE device never share a file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scqo import RecordingDevice, physical_store, state_store
from scqo.changes import HISTORY_FILE, ChangeDB
from scqo.datastore import load_cooldowns, setup_scqo_dir, setup_state_path
from scqo.testing import demo_device


def _db_rows(scqo_dir, store: str) -> list[dict]:
    """The context database's committed rows of one store, ascending."""
    return ChangeDB(Path(scqo_dir) / HISTORY_FILE).context_history(store)


def _demo():
    """A one-qubit fixed-frequency demo device: (roster, design, vendor)."""
    return demo_device(("q0",), pair=False)


def _roster():
    return _demo()[0]


def _recorder(scqo_dir=None, *, setup: str = "", on_load: str = "pull"):
    """A RecordingDevice over a FRESH vendor tree bound to one scqo/ folder."""
    roster, _design, vendor = _demo()
    return RecordingDevice(vendor, roster,
                           state_store(scqo_dir, roster, setup=setup),
                           on_load=on_load)


#: The one-per-device roster file build_session requires post-cutover.
_COMPONENTS_TOML = """\
schema = 3

[modes.q0]
kind = "transmon"

[lines.fl]
readout = ["q0"]
[lines.xy0]
drive = ["q0"]
"""

#: The datasheet that gives the bring-up anchors (readout_freq_hz hops to
#: the resonator's design f_dress0_hz).
_DESIGN_TOML = """\
schema = 1

[q0]
f_01_hz = 3.87e9
[q0_res]
f_dress0_hz = 5.95e9
"""


def _write_cooldowns(data_root: Path, device: str, text: str) -> Path:
    ddir = data_root / device
    ddir.mkdir(parents=True, exist_ok=True)
    path = ddir / "cooldowns.toml"
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------- path helpers

def test_scqo_dir_is_per_cooldown_setup(tmp_path):
    """<data_root>/<device>/<cooldown>/<setup>/scqo/ — uniform for every backend,
    never inside a vendor-config folder."""
    d = setup_scqo_dir(tmp_path / "data", "chipA", "cd1", "qm_main")
    assert d == tmp_path / "data" / "chipA" / "cd1" / "qm_main" / "scqo"
    assert setup_state_path(tmp_path / "data", "chipA", "cd1", "qm_main") == d / "scqo_state.json"


def test_scqo_dir_requires_filename_safe_parts(tmp_path):
    for cooldown, setup in (("", "s"), ("cd 1", "s"), ("cd1", ""), ("cd1", "a/b")):
        with pytest.raises(ValueError, match="letters/digits"):
            setup_scqo_dir(tmp_path, "chipA", cooldown, setup)


# ------------------------------------------------------------ registry guards

def test_registry_derives_the_vendor_folder_from_the_keys(tmp_path):
    """A real setup carries only backend (+ note); load_cooldowns DERIVES the vendor
    folder — <device>/<cooldown>/<setup>/backend_config — and injects it as
    setup['instrument_config'] (absolute), so no path can ever dangle. Simulated
    setups get no key (no vendor folder)."""
    _write_cooldowns(
        tmp_path, "chipA",
        "[cd1]\nstart = 2026-07-01\n"
        "[cd1.setup.qblox_main]\nbackend = 'qblox'\nnote = 'cluster A'\n"
        "[cd1.setup.sim]\nbackend = 'simulated'\n")
    cycles = load_cooldowns(tmp_path, "chipA")
    folder = cycles["cd1"]["setup"]["qblox_main"]["instrument_config"]
    expected = (tmp_path / "chipA" / "cd1" / "qblox_main" / "backend_config").resolve()
    assert Path(folder) == expected and Path(folder).is_absolute()
    assert "instrument_config" not in cycles["cd1"]["setup"]["sim"]


def test_registry_refuses_explicit_instrument_config(tmp_path):
    """Typing the path is retired (it was a second source of truth that could
    dangle) — refused loudly, naming the derived folder as the fix."""
    _write_cooldowns(
        tmp_path, "chipA",
        "[cd1]\nstart = 2026-07-01\n"
        "[cd1.setup.main]\nbackend = 'qblox'\ninstrument_config = 'qblox'\n")
    with pytest.raises(ValueError, match="retired in v0.9") as ei:
        load_cooldowns(tmp_path, "chipA")
    assert "backend_config" in str(ei.value)  # the message names the derived folder


def test_registry_refuses_casefold_twin_setup_names(tmp_path):
    """'Main' vs 'main': one <cooldown>/<name>/ folder on a case-insensitive FS."""
    _write_cooldowns(
        tmp_path, "chipA",
        "[cd1]\nstart = 2026-07-01\n"
        "[cd1.setup.Main]\nbackend = 'simulated'\n"
        "[cd1.setup.main]\nbackend = 'simulated'\n")
    with pytest.raises(ValueError, match="letter case"):
        load_cooldowns(tmp_path, "chipA")


def test_registry_refuses_bad_cooldown_id(tmp_path):
    """The cooldown id is a folder segment too — a non-filename-safe one is refused
    LOUDLY at load (not left to crash a later setup_scqo_dir, e.g. in doctor)."""
    _write_cooldowns(
        tmp_path, "chipA",
        '["cd 1"]\nstart = 2026-07-01\n'
        '["cd 1".setup.sim]\nbackend = "simulated"\n')  # space in the cooldown id
    with pytest.raises(ValueError, match="cooldown id"):
        load_cooldowns(tmp_path, "chipA")


def test_registry_refuses_casefold_twin_cooldown_ids(tmp_path):
    """[cd2] (ended) + [CD2] (open): on Windows their derived folder trees ALIAS —
    the new cycle would silently inherit and overwrite the ended cycle's state
    files and vendor snapshot. Refused loudly, like casefold-twin setup names."""
    _write_cooldowns(
        tmp_path, "chipA",
        "[cd2]\nstart = 2026-06-01\nend = 2026-07-01\n"
        "[cd2.setup.main]\nbackend = 'simulated'\n"
        "[CD2]\nstart = 2026-07-02\n"
        "[CD2.setup.main]\nbackend = 'simulated'\n")
    with pytest.raises(ValueError, match="letter case"):
        load_cooldowns(tmp_path, "chipA")


def test_derived_folder_uses_the_device_argument_verbatim(tmp_path):
    """The injected vendor folder must join data_root/device exactly like the
    scqo-sibling helpers do — for ANY device string, backend_config/ and scqo/
    stay siblings (the QUAM-safety guarantee)."""
    device = "nested/chipB"  # a device string containing a separator
    _write_cooldowns(
        tmp_path, device,
        "[cd1]\nstart = 2026-07-01\n"
        "[cd1.setup.qblox_main]\nbackend = 'qblox'\n")
    cycles = load_cooldowns(tmp_path, device)
    injected = Path(cycles["cd1"]["setup"]["qblox_main"]["instrument_config"])
    scqo_dir = setup_scqo_dir(tmp_path, device, "cd1", "qblox_main").resolve()
    assert injected.parent == scqo_dir.parent  # siblings under the SAME setup folder


# ------------------------------------------------- ChangeRecord setup stamping

def test_change_records_carry_the_setup(tmp_path):
    """Every write — run-driven or manual — is stamped with the session's setup,
    and the stamp round-trips through the state file."""
    dev = _recorder(tmp_path, setup="alpha")
    dev.component("q0_xy").pi_amp = 0.3  # a manual write, no run context
    assert [r.setup for r in dev.history()] == ["alpha"]
    dev.save()

    again = _recorder(tmp_path, setup="beta", on_load="push")
    assert [r.setup for r in again.history()] == ["alpha"]  # loaded rows keep theirs
    again.component("q0_xy").pi_amp = 0.4
    assert [r.setup for r in again.history()] == ["alpha", "beta"]


def test_setupless_device_stamps_none(tmp_path):
    """Direct-API sessions without a setup still record — with setup=None."""
    dev = _recorder()
    dev.component("q0_xy").pi_amp = 0.3
    assert dev.history()[0].setup is None


# ---------------------------------- physical.json: flat per-context + merging

def test_physical_flat_values_round_trip(tmp_path):
    roster = _roster()
    path = tmp_path / "physical.json"
    store = physical_store(tmp_path, roster, setup="qm_main")
    store.record("q0", "t1_s", 25e-6, run_id="run-a")
    store.record("q0", "t1_s", 26e-6, run_id="run-b")
    store.save()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["values"]["q0"]["t1_s"] == 26e-6  # FLAT — one context per file
    assert "history" not in data  # values-only: history lives in the database
    assert [r["setup"] for r in _db_rows(tmp_path, "physical")] == [
        "qm_main", "qm_main"]

    reloaded = physical_store(tmp_path, roster)
    assert reloaded.values() == {"q0": {"t1_s": 26e-6}}
    assert reloaded.get("q0", "t1_s") == 26e-6


def test_physical_same_context_concurrent_save_no_clobber(tmp_path):
    """Two sessions on the SAME (cooldown, setup) file (two terminals): merge-on-save
    keeps both writers' value keys and both history row-sets."""
    roster = _roster()
    a = physical_store(tmp_path, roster, setup="qm_main")
    b = physical_store(tmp_path, roster, setup="qm_main")  # both loaded the (empty) file

    a.record("q0", "t1_s", 25e-6, run_id="run-a")
    a.save()
    b.record("q0", "t2_echo_s", 12e-6, run_id="run-b")
    b.save()  # must NOT erase a's t1_s row or value

    final = physical_store(tmp_path, roster)
    assert final.values()["q0"] == {"t1_s": 25e-6, "t2_echo_s": 12e-6}
    assert {(r.run_id) for r in final.history()} == {"run-a", "run-b"}


def test_physical_same_field_concurrent_newest_wins(tmp_path, monkeypatch):
    """Two same-context sessions record the SAME (entity, field): the later
    measurement wins on merge (not older-save-wins), and the persisted value matches
    its crediting record so provenance never shows it as 'external'."""
    from scqo import stores
    from scqo.report import live_sources

    roster = _roster()
    a = physical_store(tmp_path, roster, setup="qm")
    b = physical_store(tmp_path, roster, setup="qm")  # both loaded the empty file

    monkeypatch.setattr(stores, "_now", lambda: "2026-07-15T10:00:00+08:00")
    a.record("q0", "t1_s", 25e-6, run_id="run-a")  # earlier
    monkeypatch.setattr(stores, "_now", lambda: "2026-07-15T10:00:01+08:00")
    b.record("q0", "t1_s", 26e-6, run_id="run-b")  # later

    b.save()  # persists 26e-6
    a.save()  # must KEEP 26e-6 (the newer record), not revert to its own 25e-6

    final = physical_store(tmp_path, roster)
    assert final.get("q0", "t1_s") == 26e-6
    src = live_sources(final.values(), [r.as_dict() for r in final.history()])
    info = src["q0"]["t1_s"]
    assert info["status"] == "run" and info["run_id"] == "run-b"  # credited, not external


def test_physical_pre_cutover_file_is_archived_aside(tmp_path):
    """Fresh start: a physical.json without the "schema": 3 stamp is pre-cutover —
    archived as *.v2.bak on first contact (values and any sidecar both) and never
    read; the store starts empty and the next save writes a clean v3 file."""
    roster = _roster()
    path = tmp_path / "physical.json"
    path.write_text(json.dumps({
        "schema": 2,
        "values": {"q0": {"t1_s": 25e-6}},
        "history": [{"timestamp": "2026-01-01T00:00:00+08:00", "component": "q0",
                     "field": "t1_s", "old": None, "new": 25e-6}],
    }), encoding="utf-8")
    store = physical_store(tmp_path, roster, setup="alpha")
    assert (tmp_path / "physical.json.v2.bak").is_file()  # old bytes preserved...
    assert not path.exists()                              # ...but never read
    assert store.get("q0", "t1_s") is None
    assert store.values() == {} and store.history() == ()

    store.record("q0", "t2_echo_s", 12e-6)
    store.save()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema"] == 3 and "history" not in data
    assert [r["new"] for r in _db_rows(tmp_path, "physical")] == [
        12e-6]  # v2 rows never merged


def test_physical_save_takes_over_stale_lock_then_times_out_on_fresh(tmp_path, monkeypatch):
    from scqo import _state_io  # the lock's constants live in the shared module now

    path = tmp_path / "physical.json"
    lock = tmp_path / "physical.json.lock"
    store = physical_store(tmp_path, _roster(), setup="alpha")
    store.record("q0", "t1_s", 25e-6)

    lock.touch()  # a crashed writer's leftover
    monkeypatch.setattr(_state_io, "_LOCK_STALE_S", 0.0)  # instantly stale
    store.save()  # takes the lock over instead of hanging
    assert not lock.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["values"]["q0"]["t1_s"] == 25e-6

    lock.touch()  # now a FRESH lock that never goes away
    monkeypatch.setattr(_state_io, "_LOCK_STALE_S", 60.0)
    monkeypatch.setattr(_state_io, "_LOCK_TIMEOUT_S", 0.2)
    store.record("q0", "t1_s", 26e-6)
    with pytest.raises(TimeoutError, match="physical.json.lock"):
        store.save()


def test_physical_history_commit_failure_keeps_rows_for_retry(
        tmp_path, monkeypatch):
    """A failed history transaction (Write 1) must NOT drop the just-recorded
    rows: the buffer clears only after the commit, so the next save()
    re-inserts them — and the rollback leaves zero rows behind."""
    import sqlite3

    from scqo import changes as changes_mod

    path = tmp_path / "physical.json"
    store = physical_store(tmp_path, _roster(), setup="alpha")
    store.record("q0", "t1_s", 25e-6, run_id="run-a")

    boom = {"n": 1}
    real_insert = changes_mod.ChangeDB.insert

    def flaky_insert(db, records, *, store):
        if boom["n"]:
            boom["n"] -= 1
            raise sqlite3.OperationalError("database is locked")
        return real_insert(db, records, store=store)

    monkeypatch.setattr(changes_mod.ChangeDB, "insert",
                        staticmethod(flaky_insert))
    with pytest.raises(sqlite3.OperationalError):
        store.save()  # the transaction rolls back before the values write
    assert not path.exists()
    assert _db_rows(tmp_path, "physical") == []      # rollback: zero rows
    assert list(tmp_path.glob("*.tmp")) == []        # no orphan temp

    store.save()  # healthy retry re-persists with provenance
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["values"]["q0"]["t1_s"] == 25e-6
    assert [(r["run_id"], r["setup"]) for r in _db_rows(
        tmp_path, "physical")] == [("run-a", "alpha")]


def test_physical_values_write_failure_self_heals_on_retry(tmp_path, monkeypatch):
    """Write 2 (values) fails AFTER the history transaction committed: the
    buffer is already cleared (no duplicate rows on retry) and the dirty keys
    stay, so the retry rebuilds the values file FROM the database."""
    from scqo import stores as stores_mod

    path = tmp_path / "physical.json"
    store = physical_store(tmp_path, _roster(), setup="alpha")
    store.record("q0", "t1_s", 25e-6, run_id="run-a")

    real_replace = stores_mod.os.replace

    def failing_replace(src, dst):
        raise PermissionError("file momentarily locked")

    monkeypatch.setattr(stores_mod.os, "replace", failing_replace)
    with pytest.raises(PermissionError):
        store.save()
    monkeypatch.setattr(stores_mod.os, "replace", real_replace)
    assert not path.exists()                          # values write failed...
    assert [r["run_id"] for r in _db_rows(tmp_path, "physical")] == [
        "run-a"]                                      # ...history is durable

    store.save()  # heals: values rebuilt FROM the database, rows NOT duplicated
    assert json.loads(path.read_text(encoding="utf-8"))["values"]["q0"]["t1_s"] == 25e-6
    assert [r["run_id"] for r in _db_rows(tmp_path, "physical")] == ["run-a"]


def test_physical_lock_is_released_only_by_its_owner(tmp_path):
    """Token ownership: if our lock is taken over (deemed stale) while we pause, our
    release must NOT delete the new owner's lock file."""
    from scqo._state_io import _file_lock

    lock = tmp_path / "physical.json.lock"
    cm = _file_lock(tmp_path / "physical.json")
    cm.__enter__()  # we hold it, with our token
    assert lock.is_file()
    lock.write_bytes(b"another-owner-token")  # a takeover replaced our lock
    cm.__exit__(None, None, None)  # release: the token mismatch must spare it
    assert lock.read_bytes() == b"another-owner-token"


def test_persist_is_atomic_and_leaves_no_temp(tmp_path):
    scqo_dir = tmp_path / "sub"  # parent created on first save
    path = scqo_dir / "scqo_state.json"
    dev = _recorder(scqo_dir, setup="alpha")
    dev.component("q0_xy").pi_amp = 0.3
    dev.save()
    assert path.is_file()
    assert list(path.parent.glob("*.tmp")) == []
    assert list(path.parent.glob("*.lock")) == []  # released after the save
    # short-lived connections checkpoint WAL on close: an idle context folder
    # holds no -wal/-shm side files, so a folder copy is always clean
    assert list(path.parent.glob("*.sqlite-*")) == []
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema"] == 3  # the model-cutover stamp
    assert "history" not in data  # values-only: history lives in the database
    assert _db_rows(scqo_dir, "state")[0]["setup"] == "alpha"


def test_device_history_merges_same_setup_sessions(tmp_path):
    """NEW with the sidecar split: two same-setup sessions no longer clobber each
    other's history rows — saves merge under the lock (values stay last-writer-wins,
    reseeded from the vendor in pull mode)."""
    path = tmp_path / "scqo_state.json"
    a = _recorder(tmp_path, setup="alpha")
    b = _recorder(tmp_path, setup="alpha")  # both pre-save

    a.component("q0_xy").pi_amp = 0.3
    a.save()
    b.component("q0_xy").drive_freq_hz = 3.9e9
    b.save()  # must NOT erase a's pi_amp row

    assert path.is_file()
    rows = {(r["field"], r["new"]) for r in _db_rows(tmp_path, "state")}
    assert rows == {("pi_amp", 0.3), ("drive_freq_hz", 3.9e9)}


def test_pre_cutover_state_file_is_archived_on_save_path_too(tmp_path):
    """The v3 gate applies at the device's store as well: a pre-cutover
    scqo_state.json (schema 2, "config" block, embedded "history") is archived
    aside on first contact and its rows never leak into the v3 sidecar."""
    path = tmp_path / "scqo_state.json"
    path.write_text(json.dumps({
        "schema": 2,
        "config": {"q0": {"readout_freq": 5.9e9, "drive_freq": 3.87e9,
                          "pi_amp": 0.3, "readout_amp": 0.25}},
        "history": [{"timestamp": "2026-07-01T10:00:00+08:00", "component": "q0",
                     "field": "pi_amp", "old": 0.2, "new": 0.3, "setup": "alpha"}],
    }), encoding="utf-8")

    dev = _recorder(tmp_path, setup="alpha")
    assert (tmp_path / "scqo_state.json.v2.bak").is_file()  # archived, not read
    assert dev.history() == ()
    assert dev.component("q0_xy").pi_amp == 0.1  # reseeded from the vendor
    dev.component("q0_xy").pi_amp = 0.4
    dev.save()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema"] == 3 and "history" not in data
    assert [r["new"] for r in _db_rows(tmp_path, "state")] == [
        0.4]  # v2 rows never resurrect


def test_values_only_reset_keeps_history(tmp_path):
    """The documented reset (delete scqo_state.json) reseeds calibration from
    the vendor but never silently drops provenance: the database still holds
    every row."""
    path = tmp_path / "scqo_state.json"
    dev = _recorder(tmp_path, setup="alpha")
    dev.component("q0_xy").pi_amp = 0.3
    dev.save()

    path.unlink()  # the reset: values gone, history.sqlite stays
    fresh = _recorder(tmp_path, setup="alpha")
    assert fresh.component("q0_xy").pi_amp == 0.1  # reseeded from the vendor
    assert [r.new for r in fresh.history()] == [0.3]  # provenance continuous


# ------------------------------------------- the two-users-two-setups scenario

def test_two_users_two_setups_end_to_end(tmp_path, monkeypatch):
    """The pin: two users on two setups of ONE device in ONE cooldown get fully
    independent state + physics files under <device>/<cooldown>/<setup>/scqo/, and
    the era guard refuses cross-setup accepts."""
    from scqo import labconfig
    from scqo.cli import _backends

    ddir = tmp_path / "data" / "chipT"
    ddir.mkdir(parents=True)
    (ddir / "cooldowns.toml").write_text(
        '[cd1]\nstart = 2026-07-01\n'
        '[cd1.setup.alpha]\nbackend = "simulated"\n'
        '[cd1.setup.beta]\nbackend = "simulated"\n', encoding="utf-8")
    (ddir / "components.toml").write_text(_COMPONENTS_TOML, encoding="utf-8")
    (ddir / "design.toml").write_text(_DESIGN_TOML, encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text(
        f"[lab]\ndevice = \"chipT\"\ndata_root = '{(tmp_path / 'data').as_posix()}'\n",
        encoding="utf-8")
    user = tmp_path / "user.toml"
    monkeypatch.setenv(labconfig.USER_ENV_VAR, str(user))

    user.write_text('setup = "alpha"\n', encoding="utf-8")
    sess_a, _ = _backends.build_session(str(config))
    res_a = sess_a.run("resonator_spectroscopy", {"targets": ["q0"]}, update="apply")
    t1_a = sess_a.run("qubit_relaxation", {"targets": ["q0"]}, update="apply")

    user.write_text('setup = "beta"\n', encoding="utf-8")
    sess_b, _ = _backends.build_session(str(config))
    res_b = sess_b.run("resonator_spectroscopy", {"targets": ["q0"]}, update="apply")
    t1_b = sess_b.run("qubit_relaxation", {"targets": ["q0"]}, update="apply")

    scqo_a = ddir / "cd1" / "alpha" / "scqo"
    scqo_b = ddir / "cd1" / "beta" / "scqo"
    # independent state stores, each history database purely its own setup's
    file_a = json.loads((scqo_a / "scqo_state.json").read_text(encoding="utf-8"))
    file_b = json.loads((scqo_b / "scqo_state.json").read_text(encoding="utf-8"))
    assert (scqo_a / HISTORY_FILE).is_file() and (scqo_b / HISTORY_FILE).is_file()
    hist_a = _db_rows(scqo_a, "state")
    hist_b = _db_rows(scqo_b, "state")
    assert "history" not in file_a and "history" not in file_b  # values-only files
    # two knob-writing runs per setup: the resonator sets readout_freq_hz, the
    # T1 sets the drive channel's thermalization_time_s (10 x the fitted T1)
    assert {(r["run_id"], r["setup"]) for r in hist_a} == {
        (res_a["run_id"], "alpha"), (t1_a["run_id"], "alpha")}
    assert {(r["run_id"], r["setup"]) for r in hist_b} == {
        (res_b["run_id"], "beta"), (t1_b["run_id"], "beta")}
    assert (file_a["values"]["q0_ro"]["readout_freq_hz"]
            == res_a["fit"]["q0"]["readout_freq_hz"])
    assert (file_b["values"]["q0_ro"]["readout_freq_hz"]
            == res_b["fit"]["q0"]["readout_freq_hz"])
    assert not (ddir / "scqo_state.json").exists()  # no retired per-device file

    # independent physical stores, each FLAT with only its own setup's measurements
    # (the resonator run also proposes f_r/kappa sample physics on q0_res)
    phys_a = json.loads((scqo_a / "physical.json").read_text(encoding="utf-8"))
    phys_b = json.loads((scqo_b / "physical.json").read_text(encoding="utf-8"))
    assert isinstance(phys_a["values"]["q0"]["t1_s"], float)
    assert isinstance(phys_b["values"]["q0"]["t1_s"], float)
    assert {r["run_id"] for r in _db_rows(scqo_a, "physical")} == {res_a["run_id"], t1_a["run_id"]}
    assert {r["run_id"] for r in _db_rows(scqo_b, "physical")} == {res_b["run_id"], t1_b["run_id"]}
    assert not (ddir / "physical.json").exists()  # no device-level ledger
    assert not (ddir / HISTORY_FILE).exists()     # no device-level database

    # the era guard refuses transferring alpha's values into a beta session
    with pytest.raises(Exception, match="alpha"):
        sess_b.accept(res_a["run_id"], reapply=True)
