"""scqo restore: a run's setup snapshot becomes a NEW named setup (folders + registry).

In-process (the command touches no instrument): a persisted session with a fake
vendor-config hook produces the snapshot, then `restore.main` is driven the way
the CLI dispatcher drives it. Every refusal is a SystemExit naming the fix.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scqo.cli import restore
from scqo.datastore import load_cooldowns
from scqo.testing import SimulatedBackend, demo_device
from tests.test_datastore import _QM_REGISTRY, _snapshot_session


def _config(tmp_path) -> str:
    config = tmp_path / "config.toml"
    config.write_text(f'[lab]\ndevice = "devA"\ndata_root = \'{(tmp_path / "data").as_posix()}\'\n',
                      encoding="utf-8")
    return str(config)


def _snapshot_run(tmp_path):
    sess = _snapshot_session(tmp_path)
    res = sess.run("resonator_spectroscopy", {"targets": ["q0"]}, update="none")
    return sess, res["run_id"], sess.load_run(res["run_id"])["record"]["setup_snapshot"]


def test_restore_recreates_the_setup_and_registers_it(tmp_path, capsys):
    sess, run_id, snap = _snapshot_run(tmp_path)
    config = _config(tmp_path)

    assert restore.main([run_id, "--setup", "replay", "--yes", "--config", config]) == 0

    root = tmp_path / "data"
    new = root / "devA" / "cd1" / "replay"
    src = root / snap["path"]
    for rel in ("backend_config/state.json", "backend_config/wiring.json",
                "backend_config/extra.json", "scqo/scqo_state.json", "scqo/physical.json"):
        assert (new / rel).read_bytes() == (src / rel).read_bytes(), rel
    assert not (new / "scqo" / "history.sqlite").exists()  # a fresh change history
    cycles = load_cooldowns(root, "devA")
    setup = cycles["cd1"]["setup"]["replay"]
    assert setup["backend"] == "qm"
    assert run_id in setup["note"] and snap["hash"] in setup["note"]
    assert cycles["cd1"]["setup"]["main"]["backend"] == "qm"  # the original block is untouched
    out, err = capsys.readouterr()
    summary = json.loads(out)
    assert summary["setup"] == "replay" and summary["snapshot"] == snap["hash"]
    assert "scqo user --setup replay" in err and "--params" in err

    # the new setup is a real context: a session binds to it and runs
    sess2 = _snapshot_session(tmp_path)
    assert sess2.find_runs(experiment="resonator_spectroscopy")


def test_restore_refuses_the_cases_that_would_corrupt_the_registry(tmp_path, capsys):
    sess, run_id, snap = _snapshot_run(tmp_path)
    config = _config(tmp_path)
    root = tmp_path / "data"

    with pytest.raises(SystemExit, match="unknown run_id"):
        restore.main(["nope", "--setup", "replay", "--yes", "--config", config])
    with pytest.raises(SystemExit, match="already exists"):
        restore.main([run_id, "--setup", "MAIN", "--yes", "--config", config])  # casefold twin
    with pytest.raises(SystemExit, match="letters/digits"):
        restore.main([run_id, "--setup", "re play", "--yes", "--config", config])
    # nothing was created for the refused names (Windows folders are case-insensitive,
    # so the twin check is the only thing standing between MAIN and main)
    assert sorted(p.name for p in (root / "devA" / "cd1").iterdir()) == ["main"]

    # a simulated run carries no snapshot
    roster, design, vendor = demo_device()
    from scqo import Session

    plain = Session(SimulatedBackend(vendor), roster, design=design, data_root=root,
                    device_name="devA", scqo_dir=root / "devA" / "cd1" / "main" / "scqo",
                    setup_name="main", cooldown_id="cd1", backend_label="simulated")
    res = plain.run("resonator_spectroscopy", {"targets": ["q0"]}, update="none")
    with pytest.raises(SystemExit, match="no setup snapshot"):
        restore.main([res["run_id"], "--setup", "replay", "--yes", "--config", config])

    # a missing snapshot folder (not mirrored) is named, not tracebacked
    import shutil

    shutil.move(root / snap["path"], root / "devA" / "moved_away")
    with pytest.raises(SystemExit, match="snapshot folder missing"):
        restore.main([run_id, "--setup", "replay", "--yes", "--config", config])
    shutil.move(root / "devA" / "moved_away", root / snap["path"])

    # not a terminal and no --yes: nothing written, exit 1
    assert restore.main([run_id, "--setup", "replay", "--config", config]) == 1
    assert not (root / "devA" / "cd1" / "replay").exists()
    assert "replay" not in load_cooldowns(root, "devA")["cd1"]["setup"]
    capsys.readouterr()


def test_restore_guards_the_cooldown_era_and_warns_on_versions(tmp_path, capsys):
    sess, run_id, snap = _snapshot_run(tmp_path)
    config = _config(tmp_path)
    root = tmp_path / "data"

    # the run's cycle is closed and a new one is active: refused without --force
    (root / "devA" / "cooldowns.toml").write_text(
        _QM_REGISTRY.replace("start = 2026-07-01\n", "start = 2026-07-01\nend = 2026-07-10\n")
        + "\n[cd2]\nstart = 2026-07-11\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="cooldown"):
        restore.main([run_id, "--setup", "replay", "--yes", "--config", config])
    assert not (root / "devA" / "cd2" / "replay").exists()

    # the manifest was written by another driver version: warned, not refused
    manifest_path = root / snap["path"] / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["versions"]["scqo"] = "0.0.1"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert restore.main([run_id, "--setup", "replay", "--yes", "--force", "--config", config]) == 0
    assert (root / "devA" / "cd2" / "replay" / "backend_config" / "state.json").is_file()
    assert load_cooldowns(root, "devA")["cd2"]["setup"]["replay"]["backend"] == "qm"
    out, err = capsys.readouterr()
    assert "WARNING: scqo was 0.0.1" in err
    assert json.loads(out)["cooldown"] == "cd2"


#: demo_device()'s roster as the device's components.toml - what restore reads
#: to re-address a 3.x snapshot (rider names q0_ro/q0_xy, op knob <op>_<suffix>).
_DEMO_COMPONENTS = """schema = 3
[modes.q0]
kind = "transmon"
[modes.q1]
kind = "transmon"
[composites.q0_q1]
kind       = "qubit_pair"
high       = "q1"
low        = "q0"
operations = ["iswap"]
[lines.fl]
readout = ["q0", "q1"]
[lines.xy_q0]
drive = ["q0"]
[lines.xy_q1]
drive = ["q1"]
"""


def test_restore_readdresses_a_3x_snapshot(tmp_path, capsys):
    """A snapshot taken before 4.0.0 keeps its 3.x store files forever (run
    data is immutable); restoring it re-addresses them through the device's
    roster, so the new setup starts on schema 4 - and a value the roster no
    longer has is named, not carried."""
    sess, run_id, snap = _snapshot_run(tmp_path)
    root = tmp_path / "data"
    (root / "devA" / "components.toml").write_text(_DEMO_COMPONENTS,
                                                   encoding="utf-8")
    old = root / snap["path"] / "scqo" / "scqo_state.json"
    old.write_text(json.dumps({"schema": 3, "values": {
        "q0_ro": {"readout_freq_hz": 6.0e9},
        "q0_xy": {"pi_amp": 0.2},
        "q0_q1": {"iswap_coupler_flux": 0.1},
        "ghost_xy": {"pi_amp": 0.5}}}), encoding="utf-8")

    assert restore.main([run_id, "--setup", "replay", "--yes",
                         "--config", _config(tmp_path)]) == 0
    new = json.loads((root / "devA" / "cd1" / "replay" / "scqo" /
                      "scqo_state.json").read_text(encoding="utf-8"))
    assert new == {"schema": 4, "values": {
        "fl": {"q0": {"readout_freq_hz": 6.0e9}},
        "xy_q0": {"q0": {"pi_amp": 0.2}},
        "q0_q1": {"iswap": {"coupler_flux": 0.1}}}}
    err = capsys.readouterr().err
    assert "re-addressed for 4.0.0" in err
    assert "not carried (no longer in the roster): ghost_xy: pi_amp" in err
