"""Cooldown-cycle lifecycle: `scqo device cooldown` / `scqo run` / `scqo user` / `scqo find`.

device -> cycle -> NAMED setups -> runs (v0.7.0): the manager starts an EMPTY cycle,
runs refuse until a [<cycle>.setup.<name>] block is hand-added, a lone setup
auto-selects, several demand `scqo user --setup <name>`, and ending the cycle makes
runs refuse again. Absorbs LCHQBDriver/tests/test_cooldown_lifecycle.py (the
pre-v0.7 `scqo cooldown` command it exercised is now `scqo device cooldown`).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _env(tmp_path: Path, user_config: str = "none") -> dict:
    """Hermetic subprocess env: SCQO_CONFIG -> a tmp lab config; the user overlay
    defaults to disabled ('none') — step 5 points it at a tmp user.toml instead."""
    config = tmp_path / "config.toml"
    config.write_text(
        "\n".join(
            [
                "[lab]",
                'device = "simdev"',
                f"data_root = '{(tmp_path / 'data').as_posix()}'",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return {**os.environ, "SCQO_CONFIG": str(config), "SCQO_USER_CONFIG": user_config}


def _cli(env: dict, tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "scqo.cli", *args],
        capture_output=True, text=True, env=env, cwd=tmp_path,
    )


def _components(tmp_path: Path) -> None:
    """A configured device needs its roster (components.toml) to build sessions."""
    device_dir = tmp_path / "data" / "simdev"
    device_dir.mkdir(parents=True, exist_ok=True)
    (device_dir / "components.toml").write_text(
        'schema = 1\n'
        '[components.q0]\n'
        'physical   = "FixedTransmon"\n'
        'instrument = "ReadableTransmon"\n'
        'operations = ["rx", "readout"]\n'
        '[components.q0_res]\n'
        'physical = "Resonator"\n'
        '[components.q0_ro]\n'
        'members  = { transmon = "q0", resonator = "q0_res" }\n'
        'physical = "ReadoutLine"\n'
        '[components.q0_xy]\n'
        'physical = "XYControl"\n'
        'members  = { transmon = "q0" }\n',
        encoding="utf-8",
    )


def _run_record(proc: subprocess.CompletedProcess) -> tuple[dict, dict]:
    """(the result JSON `scqo run` prints, the persisted record.json)."""
    result = json.loads(proc.stdout.split("\nsaved:")[0])
    record = json.loads((Path(result["data_path"]) / "record.json").read_text(encoding="utf-8"))
    return result, record


def test_cooldown_lifecycle(tmp_path):
    env = _env(tmp_path)
    _components(tmp_path)

    # 1. `start` records an EMPTY cycle. The v0.6 --backend/--instrument-config
    #    flags are RETIRED (setups are hand-added named blocks now).
    assert _cli(env, tmp_path, "device", "cooldown", "start", "cd1",
                "--backend", "simulated").returncode != 0
    proc = _cli(env, tmp_path, "device", "cooldown", "start", "cd1",
                "--fridge", "TestFridge", "--packaging", "PCB v1")
    assert proc.returncode == 0, proc.stderr
    reg = tmp_path / "data" / "simdev" / "cooldowns.toml"
    text = reg.read_text(encoding="utf-8")
    assert "[cd1]" in text and 'fridge = "TestFridge"' in text and 'packaging = "PCB v1"' in text
    assert "#   [cd1.setup.<name>]" in text  # the commented setup skeleton
    # a second start refuses while cd1 is still open
    assert _cli(env, tmp_path, "device", "cooldown", "start", "cd2").returncode != 0
    # bare `device cooldown` validates + shows the cycle (empty setups table)
    show = _cli(env, tmp_path, "device", "cooldown")
    assert show.returncode == 0, show.stderr
    assert "ACTIVE" in show.stdout and "setups=0" in show.stdout
    assert "has no setups yet" in show.stdout

    # 2. a run REFUSES while the cycle has zero setups (paste-ready skeleton shown)
    proc = _cli(env, tmp_path, "run", "resonator_spectroscopy", "--targets", "q0")
    assert proc.returncode != 0
    assert "has no setups yet" in proc.stderr
    assert "[cd1.setup.<name>]" in proc.stderr

    # 3. hand-add ONE named setup (the documented workflow) -> it auto-selects,
    #    and the run record carries the cycle + setup NAME + backend provenance
    reg.write_text(reg.read_text(encoding="utf-8")
                   + '\n[cd1.setup.practice]\nbackend = "simulated"\n', encoding="utf-8")
    proc = _cli(env, tmp_path, "run", "resonator_spectroscopy", "--targets", "q0")
    assert proc.returncode == 0, proc.stderr
    r1, rec1 = _run_record(proc)
    assert rec1["cooldown"] == "cd1" and rec1["setup"] == "practice"
    assert rec1["backend"] == "simulated"  # provenance = the resolved setup's backend

    # 4. a SECOND setup makes the cycle ambiguous -> runs refuse, naming both + the fix
    reg.write_text(reg.read_text(encoding="utf-8")
                   + '\n[cd1.setup.alt]\nbackend = "simulated"\n', encoding="utf-8")
    proc = _cli(env, tmp_path, "run", "resonator_spectroscopy", "--targets", "q0")
    assert proc.returncode != 0
    assert "practice" in proc.stderr and "alt" in proc.stderr
    assert "scqo user --setup" in proc.stderr

    # 5. select one: `scqo user --setup` validates the name then WRITES the overlay
    #    named by $SCQO_USER_CONFIG. (The overlay pre-exists here; the
    #    created-when-missing form is test_user_setup_creates_missing_overlay.)
    user_toml = tmp_path / "user.toml"
    user_toml.write_text("", encoding="utf-8")
    env_user = _env(tmp_path, user_config=str(user_toml))
    proc = _cli(env_user, tmp_path, "user", "--setup", "practice")
    assert proc.returncode == 0, proc.stderr
    assert 'setup = "practice"' in user_toml.read_text(encoding="utf-8")
    proc = _cli(env_user, tmp_path, "run", "resonator_spectroscopy", "--targets", "q0")
    assert proc.returncode == 0, proc.stderr
    r2, rec2 = _run_record(proc)
    assert rec2["cooldown"] == "cd1" and rec2["setup"] == "practice"

    # 6. the show form lists the ACTIVE cycle's setups table (both names)
    show = _cli(env_user, tmp_path, "device", "cooldown")
    assert show.returncode == 0, show.stderr
    assert "setups=2" in show.stdout
    assert "setups of cd1" in show.stdout
    assert "practice" in show.stdout and "alt" in show.stdout

    # 7. both runs findable by cycle id + setup NAME
    listed = _cli(env_user, tmp_path, "find", "--cooldown", "cd1", "--setup", "practice")
    assert listed.returncode == 0, listed.stderr
    assert r1["run_id"] in listed.stdout and r2["run_id"] in listed.stdout
    assert "no runs match" in _cli(env_user, tmp_path, "find", "--setup", "alt").stdout

    # 8. end the cycle: targeted edit stays valid even with setup blocks after the
    #    cycle header (the end-surgery regression case; .bak written)…
    proc = _cli(env_user, tmp_path, "device", "cooldown", "end")
    assert proc.returncode == 0, proc.stderr
    assert reg.with_suffix(".toml.bak").is_file()
    # …and later runs REFUSE loudly instead of stamping ""
    proc = _cli(env_user, tmp_path, "run", "resonator_spectroscopy", "--targets", "q0")
    assert proc.returncode != 0
    assert "no ACTIVE cooldown cycle" in proc.stderr


def test_user_setup_creates_missing_overlay(tmp_path):
    """`scqo user --setup` must CREATE the overlay named by $SCQO_USER_CONFIG.

    A missing env-named overlay stays fatal for every OTHER command (a typo must
    not silently drop the overlay; labconfig raises FileNotFoundError) — `scqo
    user` alone tolerates it on its WRITE path (user._cfg_for_write loads the
    config with the overlay disabled for that one call, since the command is
    about to create the file) and prints "note: creating <target>".
    """
    user_toml = tmp_path / "user.toml"  # does NOT exist yet — the command creates it
    env = _env(tmp_path, user_config=str(user_toml))
    device_dir = tmp_path / "data" / "simdev"
    device_dir.mkdir(parents=True)
    (device_dir / "cooldowns.toml").write_text(
        '[cd1]\nstart = 2026-07-01\n\n[cd1.setup.practice]\nbackend = "simulated"\n',
        encoding="utf-8",
    )
    proc = _cli(env, tmp_path, "user", "--setup", "practice")
    assert proc.returncode == 0, proc.stderr
    assert user_toml.is_file()
    assert 'setup = "practice"' in user_toml.read_text(encoding="utf-8")


def test_start_escapes_metadata_and_validates_cycle_id(tmp_path):
    """Quotes/backslashes in --fridge/--packaging/--note must never corrupt the
    shared registry (values are TOML-escaped and the write rolls back on a bad
    re-parse), and a cycle id that cannot be a TOML header / CLI argument is
    refused before anything is written."""
    env = _env(tmp_path)
    registry = tmp_path / "data" / "simdev" / "cooldowns.toml"

    proc = _cli(env, tmp_path, "device", "cooldown", "start", "cd 1", "--fridge", "F")
    assert proc.returncode != 0
    assert "letters/digits" in proc.stderr
    assert not registry.exists()  # refused before any write

    proc = _cli(env, tmp_path, "device", "cooldown", "start", "cd1",
                "--packaging", 'PCB "rev3"', "--note", "D:\qpu\chipA path")
    assert proc.returncode == 0, proc.stderr
    show = _cli(env, tmp_path, "device", "cooldown")
    assert show.returncode == 0, show.stderr  # the registry re-parses cleanly
    assert 'PCB "rev3"' in show.stdout  # value survived, unmangled
