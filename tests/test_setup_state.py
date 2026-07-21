"""Per-(cooldown, setup) SCQO folders — path convention, registry guards, isolation.

Every setup of every cooldown gets its own ``<device>/<cooldown>/<setup>/scqo/``
folder holding ``scqo_state.json`` (calibration values) and ``physical.json``
(measured physics), each with its append-only ``.history.jsonl`` change-history
sidecar (scqo._state_io). SCQO never writes into a setup's vendor-config
``instrument_config`` folder, so the QM backend's QUAM load never sweeps up SCQO
files. Two users on two setups of ONE device never share a file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scqo._state_io import read_history
from scqo.config import RecordingDevice
from scqo.datastore import load_cooldowns, setup_scqo_dir, setup_state_path
from scqo.testing import InMemoryDevice, demo_roster


def _vendor() -> InMemoryDevice:
    return InMemoryDevice(
        {"q0": {"readout_freq": 5.95e9, "drive_freq": 3.87e9, "pi_amp": 0.2, "readout_amp": 0.25}}
    )


def _roster():
    return demo_roster(qubits=("q0",))


#: The one-per-device roster file build_session requires post-cutover.
_COMPONENTS_TOML = """\
schema = 1
[components.q0]
physical   = "FixedTransmon"
instrument = "ReadableTransmon"
operations = ["rx", "readout"]
[components.q0_res]
physical = "Resonator"
[components.q0_ro]
physical = "ReadoutLine"
members  = { transmon = "q0", resonator = "q0_res" }
[components.q0_xy]
physical = "XYControl"
members  = { transmon = "q0" }
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
    path = str(tmp_path / "scqo_state.json")
    dev = RecordingDevice(_vendor(), _roster(), state_path=path, setup="alpha")
    dev.component("q0").pi_amp = 0.3  # a manual write, no run context
    assert [r.setup for r in dev.history()] == ["alpha"]
    dev.save()

    again = RecordingDevice(_vendor(), _roster(), state_path=path, on_load="push", setup="beta")
    assert [r.setup for r in again.history()] == ["alpha"]  # loaded rows keep theirs
    again.component("q0").pi_amp = 0.4
    assert [r.setup for r in again.history()] == ["alpha", "beta"]


def test_setupless_device_stamps_none(tmp_path):
    """Direct-API sessions without a setup still record — with setup=None."""
    dev = RecordingDevice(_vendor(), _roster())
    dev.component("q0").pi_amp = 0.3
    assert dev.history()[0].setup is None


# ---------------------------------- physical.json: flat per-context + merging

def test_physical_flat_values_round_trip(tmp_path):
    from scqo.physical import PhysicalStore

    path = tmp_path / "physical.json"
    store = PhysicalStore(path, setup="qm_main")
    store.record("q0", "t1_s", 25e-6, run_id="run-a")
    store.record("q0", "t1_s", 26e-6, run_id="run-b")
    store.save()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["values"]["q0"]["t1_s"] == 26e-6  # FLAT — one context per file
    assert "history" not in data  # values-only: history lives in the sidecar
    assert [r["setup"] for r in read_history(path)] == ["qm_main", "qm_main"]

    reloaded = PhysicalStore(path)
    assert reloaded.snapshot() == {"q0": {"t1_s": 26e-6}}
    assert reloaded.get("q0", "t1_s") == 26e-6


def test_physical_same_context_concurrent_save_no_clobber(tmp_path):
    """Two sessions on the SAME (cooldown, setup) file (two terminals): merge-on-save
    keeps both writers' value keys and both history row-sets."""
    from scqo.physical import PhysicalStore

    path = tmp_path / "physical.json"
    a = PhysicalStore(path, setup="qm_main")
    b = PhysicalStore(path, setup="qm_main")  # both loaded the (empty) file

    a.record("q0", "t1_s", 25e-6, run_id="run-a")
    a.save()
    b.record("q0", "t2_echo_s", 12e-6, run_id="run-b")
    b.save()  # must NOT erase a's t1_s row or value

    final = PhysicalStore(path)
    assert final.snapshot()["q0"] == {"t1_s": 25e-6, "t2_echo_s": 12e-6}
    assert {(r.run_id) for r in final.history()} == {"run-a", "run-b"}


def test_physical_same_field_concurrent_newest_wins(tmp_path, monkeypatch):
    """Two same-context sessions record the SAME (component, field): the later
    measurement wins on merge (not older-save-wins), and the persisted value matches
    its crediting record so provenance never shows it as 'external'."""
    from scqo import physical
    from scqo.physical import PhysicalStore
    from scqo.provenance import live_sources

    path = tmp_path / "physical.json"
    a = PhysicalStore(path, setup="qm")
    b = PhysicalStore(path, setup="qm")  # both loaded the empty file

    monkeypatch.setattr(physical, "_now", lambda: "2026-07-15T10:00:00+08:00")
    a.record("q0", "t1_s", 25e-6, run_id="run-a")  # earlier
    monkeypatch.setattr(physical, "_now", lambda: "2026-07-15T10:00:01+08:00")
    b.record("q0", "t1_s", 26e-6, run_id="run-b")  # later

    b.save()  # persists 26e-6
    a.save()  # must KEEP 26e-6 (the newer record), not revert to its own 25e-6

    final = PhysicalStore(path)
    assert final.get("q0", "t1_s") == 26e-6
    src = live_sources(final.snapshot(), [r.as_dict() for r in final.history()])
    info = src["q0"]["t1_s"]
    assert info["status"] == "run" and info["run_id"] == "run-b"  # credited, not external


def test_physical_pre_cutover_file_is_archived_aside(tmp_path):
    """Fresh start: a physical.json without the "schema": 2 stamp is pre-cutover —
    archived as *.v1.bak on first contact (values and any sidecar both) and never
    read; the store starts empty and the next save writes a clean v2 file."""
    from scqo.physical import PhysicalStore

    path = tmp_path / "physical.json"
    path.write_text(json.dumps({
        "values": {"q0": {"t1_s": 25e-6}},
        "history": [{"timestamp": "2026-01-01T00:00:00+08:00", "qubit": "q0",
                     "field": "t1_s", "old": None, "new": 25e-6}],
    }), encoding="utf-8")
    store = PhysicalStore(path, setup="alpha")
    assert (tmp_path / "physical.json.v1.bak").is_file()  # old bytes preserved...
    assert not path.exists()                              # ...but never read
    assert store.get("q0", "t1_s") is None
    assert store.snapshot() == {} and store.history() == []

    store.record("q0", "t2_echo_s", 12e-6)
    store.save()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema"] == 2 and "history" not in data
    assert [r["new"] for r in read_history(path)] == [12e-6]  # v1 rows never merged


def test_physical_save_takes_over_stale_lock_then_times_out_on_fresh(tmp_path, monkeypatch):
    from scqo import _state_io  # the lock's constants live in the shared module now
    from scqo.physical import PhysicalStore

    path = tmp_path / "physical.json"
    lock = tmp_path / "physical.json.lock"
    store = PhysicalStore(path, setup="alpha")
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


def test_physical_save_failure_keeps_rows_for_retry(tmp_path, monkeypatch):
    """A failed history write (Write 1) must NOT drop the just-recorded rows: the
    in-memory merge commits only after the sidecar lands, so the next save()
    re-persists them."""
    from scqo import _state_io
    from scqo.physical import PhysicalStore

    path = tmp_path / "physical.json"
    store = PhysicalStore(path, setup="alpha")
    store.record("q0", "t1_s", 25e-6, run_id="run-a")

    boom = {"n": 1}
    real_replace = _state_io.os.replace  # capture before patching (same module object)

    def flaky_replace(src, dst):
        if boom["n"]:
            boom["n"] -= 1
            raise PermissionError("file momentarily locked")
        return real_replace(src, dst)

    monkeypatch.setattr(_state_io.os, "replace", flaky_replace)
    with pytest.raises(PermissionError):
        store.save()  # the FIRST replace is the history sidecar's
    assert not path.exists() and not _state_io.history_path(path).exists()
    assert list(tmp_path.glob("*.tmp")) == []  # and no orphan temp left behind

    store.save()  # healthy retry re-persists with provenance
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["values"]["q0"]["t1_s"] == 25e-6
    assert [(r["run_id"], r["setup"]) for r in read_history(path)] == [("run-a", "alpha")]


def test_physical_values_write_failure_self_heals_on_retry(tmp_path, monkeypatch):
    """Write 2 (values) fails AFTER the sidecar landed: the merge is already
    committed (no duplicate rows on retry) and the dirty keys stay, so the retry
    rebuilds the values file from the durable history."""
    from scqo import _state_io
    from scqo.physical import PhysicalStore

    path = tmp_path / "physical.json"
    store = PhysicalStore(path, setup="alpha")
    store.record("q0", "t1_s", 25e-6, run_id="run-a")

    boom = {"n": 1}
    real_replace = _state_io.os.replace

    def fail_second_replace(src, dst):  # sidecar lands, values write fails
        if boom["n"] == 0:
            raise PermissionError("file momentarily locked")
        boom["n"] -= 1
        return real_replace(src, dst)

    monkeypatch.setattr(_state_io.os, "replace", fail_second_replace)
    with pytest.raises(PermissionError):
        store.save()
    assert _state_io.history_path(path).is_file() and not path.exists()
    monkeypatch.setattr(_state_io.os, "replace", real_replace)

    store.save()  # heals: values rebuilt, history NOT duplicated
    assert json.loads(path.read_text(encoding="utf-8"))["values"]["q0"]["t1_s"] == 25e-6
    assert [r["run_id"] for r in read_history(path)] == ["run-a"]


def test_physical_lock_is_released_only_by_its_owner(tmp_path):
    """Token ownership: if our lock is taken over (deemed stale) while we pause, our
    release must NOT delete the new owner's lock file."""
    from scqo.physical import _file_lock

    lock = tmp_path / "physical.json.lock"
    cm = _file_lock(tmp_path / "physical.json")
    cm.__enter__()  # we hold it, with our token
    assert lock.is_file()
    lock.write_bytes(b"another-owner-token")  # a takeover replaced our lock
    cm.__exit__(None, None, None)  # release: the token mismatch must spare it
    assert lock.read_bytes() == b"another-owner-token"


def test_persist_is_atomic_and_leaves_no_temp(tmp_path):
    path = tmp_path / "sub" / "scqo_state.json"  # parent created on first save
    dev = RecordingDevice(_vendor(), _roster(), state_path=str(path), setup="alpha")
    dev.component("q0").pi_amp = 0.3
    dev.save()
    assert path.is_file()
    assert list(path.parent.glob("*.tmp")) == []
    assert list(path.parent.glob("*.lock")) == []  # released after the save
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema"] == 2  # the component-cutover stamp
    assert "history" not in data  # values-only: history lives in the sidecar
    assert read_history(path)[0]["setup"] == "alpha"


def test_device_history_merges_same_setup_sessions(tmp_path):
    """NEW with the sidecar split: two same-setup sessions no longer clobber each
    other's history rows — saves merge under the lock (values stay last-writer-wins,
    reseeded from the vendor in pull mode)."""
    path = str(tmp_path / "scqo_state.json")
    a = RecordingDevice(_vendor(), _roster(), state_path=path, setup="alpha")
    b = RecordingDevice(_vendor(), _roster(), state_path=path, setup="alpha")  # both pre-save

    a.component("q0").pi_amp = 0.3
    a.save()
    b.component("q0").drive_freq = 3.9e9
    b.save()  # must NOT erase a's pi_amp row

    rows = {(r["field"], r["new"]) for r in read_history(path)}
    assert rows == {("pi_amp", 0.3), ("drive_freq", 3.9e9)}


def test_pre_cutover_state_file_is_archived_on_save_path_too(tmp_path):
    """The v2 gate applies at the SAVE-merge site as well: a pre-cutover
    scqo_state.json (no schema stamp, embedded "history") is archived aside on
    first contact and its rows never leak into the v2 sidecar."""
    path = tmp_path / "scqo_state.json"
    path.write_text(json.dumps({
        "config": {"q0": {"readout_freq": 5.9e9, "drive_freq": 3.87e9,
                          "pi_amp": 0.3, "readout_amp": 0.25}},
        "history": [{"timestamp": "2026-07-01T10:00:00+08:00", "qubit": "q0",
                     "field": "pi_amp", "old": 0.2, "new": 0.3, "setup": "alpha"}],
    }), encoding="utf-8")

    dev = RecordingDevice(_vendor(), _roster(), state_path=str(path), setup="alpha")
    assert (tmp_path / "scqo_state.json.v1.bak").is_file()  # archived, not read
    assert dev.history() == []
    assert dev.component("q0").pi_amp == 0.2  # reseeded from the vendor
    dev.component("q0").pi_amp = 0.4
    dev.save()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema"] == 2 and "history" not in data
    assert [r["new"] for r in read_history(path)] == [0.4]  # v1 rows never resurrect


def test_values_only_reset_keeps_history_sidecar(tmp_path):
    """The documented reset (delete scqo_state.json) reseeds calibration from the
    vendor but never silently drops provenance: the sidecar still loads."""
    path = tmp_path / "scqo_state.json"
    dev = RecordingDevice(_vendor(), _roster(), state_path=str(path), setup="alpha")
    dev.component("q0").pi_amp = 0.3
    dev.save()

    path.unlink()  # the reset: values gone, sidecar stays
    fresh = RecordingDevice(_vendor(), _roster(), state_path=str(path), setup="alpha")
    assert fresh.component("q0").pi_amp == 0.2  # reseeded from the vendor
    assert [r.new for r in fresh.history()] == [0.3]  # provenance continuous


def test_read_history_stripped_values_without_sidecar_is_empty(tmp_path):
    """The fallback loop terminates: a post-split values file (no embedded key)
    whose sidecar was hand-deleted reads as an empty history, not an error."""
    path = tmp_path / "physical.json"
    path.write_text('{"values": {}}', encoding="utf-8")
    assert read_history(path) == []


def test_read_history_skips_torn_trailing_line(tmp_path, capsys):
    """A torn/hand-mangled sidecar line is skipped with a warning — one bad line
    must not take the whole store down."""
    path = tmp_path / "physical.json"
    from scqo._state_io import history_path

    history_path(path).write_text(
        '{"timestamp": "2026-07-01T10:00:00+08:00", "component": "q0", '
        '"field": "t1_s", "old": null, "new": 2.5e-05}\n'
        '{"timestamp": "2026-07-01T10:01:00+08:00", "component": "q0", "fi',  # torn
        encoding="utf-8")
    rows = read_history(path)
    assert [r["new"] for r in rows] == [2.5e-05]
    assert "unparseable history line skipped" in capsys.readouterr().err


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
    # independent state stores, each history sidecar purely its own setup's
    file_a = json.loads((scqo_a / "scqo_state.json").read_text(encoding="utf-8"))
    file_b = json.loads((scqo_b / "scqo_state.json").read_text(encoding="utf-8"))
    hist_a = read_history(scqo_a / "scqo_state.json")
    hist_b = read_history(scqo_b / "scqo_state.json")
    assert "history" not in file_a and "history" not in file_b  # values-only files
    assert {(r["run_id"], r["setup"]) for r in hist_a} == {(res_a["run_id"], "alpha")}
    assert {(r["run_id"], r["setup"]) for r in hist_b} == {(res_b["run_id"], "beta")}
    assert file_a["config"]["q0"]["readout_freq"] == res_a["fit"]["q0"]["readout_freq"]
    assert file_b["config"]["q0"]["readout_freq"] == res_b["fit"]["q0"]["readout_freq"]
    assert not (ddir / "scqo_state.json").exists()  # no retired per-device file

    # independent physical stores, each FLAT with only its own setup's measurements
    # (the resonator run also proposes f_r/kappa sample physics on q0_res)
    phys_a = json.loads((scqo_a / "physical.json").read_text(encoding="utf-8"))
    phys_b = json.loads((scqo_b / "physical.json").read_text(encoding="utf-8"))
    assert isinstance(phys_a["values"]["q0"]["t1_s"], float)
    assert {r["run_id"] for r in read_history(scqo_a / "physical.json")} == {res_a["run_id"], t1_a["run_id"]}
    assert {r["run_id"] for r in read_history(scqo_b / "physical.json")} == {res_b["run_id"], t1_b["run_id"]}
    assert not (ddir / "physical.json").exists()  # no device-level ledger

    # the era guard refuses transferring alpha's values into a beta session
    with pytest.raises(Exception, match="alpha"):
        sess_b.accept(res_a["run_id"], reapply=True)
