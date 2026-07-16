"""Per-(cooldown, setup) SCQO folders — path convention, registry guards, isolation.

Every setup of every cooldown gets its own ``<device>/<cooldown>/<setup>/scqo/``
folder holding ``scqo_state.json`` (calibration + history) and ``physical.json``
(measured physics + history). SCQO never writes into a setup's vendor-config
``instrument_config`` folder, so the QM backend's QUAM load never sweeps up SCQO
files. Two users on two setups of ONE device never share a file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scqo.config import RecordingDevice
from scqo.datastore import load_cooldowns, setup_scqo_dir, setup_state_path
from scqo.testing import InMemoryDevice


def _vendor() -> InMemoryDevice:
    return InMemoryDevice(
        {"q0": {"readout_freq": 5.95e9, "drive_freq": 3.87e9, "pi_amp": 0.2, "readout_amp": 0.25}}
    )


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
    dev = RecordingDevice(_vendor(), state_path=path, setup="alpha")
    dev.qubit("q0").pi_amp = 0.3  # a manual write, no run context
    assert [r.setup for r in dev.history()] == ["alpha"]
    dev.save()

    again = RecordingDevice(_vendor(), state_path=path, on_load="push", setup="beta")
    assert [r.setup for r in again.history()] == ["alpha"]  # loaded rows keep theirs
    again.qubit("q0").pi_amp = 0.4
    assert [r.setup for r in again.history()] == ["alpha", "beta"]


def test_setupless_device_stamps_none(tmp_path):
    """Direct-API sessions without a setup still record — with setup=None."""
    dev = RecordingDevice(_vendor())
    dev.qubit("q0").pi_amp = 0.3
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
    assert [r["setup"] for r in data["history"]] == ["qm_main", "qm_main"]

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
    """Two same-context sessions record the SAME (qubit, field): the later
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


def test_physical_non_float_legacy_values_dropped(tmp_path):
    """Fresh start: a stray non-numeric (e.g. old nested) value is not read; history
    is never rewritten."""
    from scqo.physical import PhysicalStore

    path = tmp_path / "physical.json"
    path.write_text(json.dumps({
        "values": {"q0": {"t1_s": {"alpha": 25e-6}}},  # old nested shape — a dict, not a float
        "history": [{"timestamp": "2026-01-01T00:00:00+08:00", "qubit": "q0",
                     "field": "t1_s", "old": None, "new": 25e-6}],
    }), encoding="utf-8")
    store = PhysicalStore(path, setup="alpha")
    assert store.get("q0", "t1_s") is None
    assert store.snapshot() == {"q0": {}}
    assert len(store.history()) == 1


def test_physical_save_takes_over_stale_lock_then_times_out_on_fresh(tmp_path, monkeypatch):
    from scqo import physical
    from scqo.physical import PhysicalStore

    path = tmp_path / "physical.json"
    lock = tmp_path / "physical.json.lock"
    store = PhysicalStore(path, setup="alpha")
    store.record("q0", "t1_s", 25e-6)

    lock.touch()  # a crashed writer's leftover
    monkeypatch.setattr(physical, "_LOCK_STALE_S", 0.0)  # instantly stale
    store.save()  # takes the lock over instead of hanging
    assert not lock.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["values"]["q0"]["t1_s"] == 25e-6

    lock.touch()  # now a FRESH lock that never goes away
    monkeypatch.setattr(physical, "_LOCK_STALE_S", 60.0)
    monkeypatch.setattr(physical, "_LOCK_TIMEOUT_S", 0.2)
    store.record("q0", "t1_s", 26e-6)
    with pytest.raises(TimeoutError, match="physical.json.lock"):
        store.save()


def test_physical_save_failure_keeps_rows_for_retry(tmp_path, monkeypatch):
    """A failed replace must NOT drop the just-recorded rows: the in-memory merge
    commits only after the write lands, so the next save() re-persists them."""
    from scqo import physical
    from scqo.physical import PhysicalStore

    path = tmp_path / "physical.json"
    store = PhysicalStore(path, setup="alpha")
    store.record("q0", "t1_s", 25e-6, run_id="run-a")

    boom = {"n": 1}
    real_replace = physical.os.replace  # capture before patching (same module object)

    def flaky_replace(src, dst):
        if boom["n"]:
            boom["n"] -= 1
            raise PermissionError("file momentarily locked")
        return real_replace(src, dst)

    monkeypatch.setattr(physical.os, "replace", flaky_replace)
    with pytest.raises(PermissionError):
        store.save()
    assert not path.exists()  # nothing was written
    assert list(tmp_path.glob("*.tmp")) == []  # and no orphan temp left behind

    store.save()  # healthy retry re-persists with provenance
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["values"]["q0"]["t1_s"] == 25e-6
    assert [(r["run_id"], r["setup"]) for r in saved["history"]] == [("run-a", "alpha")]


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
    dev = RecordingDevice(_vendor(), state_path=str(path), setup="alpha")
    dev.qubit("q0").pi_amp = 0.3
    dev.save()
    assert path.is_file()
    assert list(path.parent.glob("*.tmp")) == []
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["history"][0]["setup"] == "alpha"


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
    config = tmp_path / "config.toml"
    config.write_text(
        f"[lab]\ndevice = \"chipT\"\ndata_root = '{(tmp_path / 'data').as_posix()}'\n",
        encoding="utf-8")
    user = tmp_path / "user.toml"
    monkeypatch.setenv(labconfig.USER_ENV_VAR, str(user))

    user.write_text('setup = "alpha"\n', encoding="utf-8")
    sess_a, _ = _backends.build_session(str(config))
    res_a = sess_a.run("resonator_spectroscopy", {"qubits": ["q0"]}, update="apply")
    t1_a = sess_a.run("qubit_relaxation", {"qubits": ["q0"]}, update="apply")

    user.write_text('setup = "beta"\n', encoding="utf-8")
    sess_b, _ = _backends.build_session(str(config))
    res_b = sess_b.run("resonator_spectroscopy", {"qubits": ["q0"]}, update="apply")
    t1_b = sess_b.run("qubit_relaxation", {"qubits": ["q0"]}, update="apply")

    scqo_a = ddir / "cd1" / "alpha" / "scqo"
    scqo_b = ddir / "cd1" / "beta" / "scqo"
    # independent state files, each history purely its own setup's
    file_a = json.loads((scqo_a / "scqo_state.json").read_text(encoding="utf-8"))
    file_b = json.loads((scqo_b / "scqo_state.json").read_text(encoding="utf-8"))
    assert {(r["run_id"], r["setup"]) for r in file_a["history"]} == {(res_a["run_id"], "alpha")}
    assert {(r["run_id"], r["setup"]) for r in file_b["history"]} == {(res_b["run_id"], "beta")}
    assert file_a["config"]["q0"]["readout_freq"] == res_a["fit"]["q0"]["readout_freq"]
    assert file_b["config"]["q0"]["readout_freq"] == res_b["fit"]["q0"]["readout_freq"]
    assert not (ddir / "scqo_state.json").exists()  # no retired per-device file

    # independent physical files, each FLAT with only its own T1
    phys_a = json.loads((scqo_a / "physical.json").read_text(encoding="utf-8"))
    phys_b = json.loads((scqo_b / "physical.json").read_text(encoding="utf-8"))
    assert isinstance(phys_a["values"]["q0"]["t1_s"], float)
    assert {r["run_id"] for r in phys_a["history"]} == {t1_a["run_id"]}
    assert {r["run_id"] for r in phys_b["history"]} == {t1_b["run_id"]}
    assert not (ddir / "physical.json").exists()  # no device-level ledger

    # the era guard refuses transferring alpha's values into a beta session
    with pytest.raises(Exception, match="alpha"):
        sess_b.accept(res_a["run_id"], reapply=True)
