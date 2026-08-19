"""`scqo doctor` — the health check that should be everyone's first debugging move."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from scqo.checks import profile_residency_checks


def _doctor(tmp_path: Path, config_body: str | None) -> subprocess.CompletedProcess:
    env = {**os.environ, "SCQO_USER_CONFIG": "none"}
    if config_body is not None:
        config = tmp_path / "config.toml"
        config.write_text(config_body, encoding="utf-8")
        env["SCQO_CONFIG"] = str(config)
    else:
        # hermetic "fresh machine": no env var AND no real ~/.scqo — Path.home()
        # follows USERPROFILE on Windows, so point it at the tmp dir
        env.pop("SCQO_CONFIG", None)
        env["USERPROFILE"] = str(tmp_path)
        env["HOME"] = str(tmp_path)
    return subprocess.run(
        [sys.executable, "-m", "scqo.cli", "doctor"],
        capture_output=True, text=True, env=env, cwd=tmp_path,
    )


def _lab_body(tmp_path: Path, device: str = "simdev") -> str:
    return f"[lab]\ndevice = \"{device}\"\ndata_root = '{(tmp_path / 'data').as_posix()}'\n"


# The device's roster in the greenfield schema: one transmon mode on a
# multiplexed feedline + its own drive wire. The readout rider mints q0_ro
# (and the q0_res resonator mode); the drive rider mints q0_xy.
_COMPONENTS = """\
schema = 3
[modes.q0]
kind = "transmon"
[lines.fl]
readout = ["q0"]
[lines.q0_xyl]
drive = ["q0"]
"""


def test_healthy_simulated_setup_passes(tmp_path):
    data_root = tmp_path / "data"
    (data_root / "simdev").mkdir(parents=True)
    (data_root / "simdev" / "cooldowns.toml").write_text(
        '[cd1]\nstart = 2026-07-01\n\n[cd1.setup.sim_main]\nbackend = "simulated"\n',
        encoding="utf-8",
    )
    # required since the model cutover: the device's roster
    (data_root / "simdev" / "components.toml").write_text(_COMPONENTS, encoding="utf-8")
    proc = _doctor(tmp_path, _lab_body(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "all checks passed" in proc.stdout
    assert "cd1 ACTIVE" in proc.stdout and "backend=simulated" in proc.stdout
    assert "'sim_main' (auto)" in proc.stdout  # single-setup cycle auto-selects
    # the per-(cooldown, setup) state file: named even before its first save
    assert "sim_main" in proc.stdout and "scqo_state.json (not created yet)" in proc.stdout
    # simulated fills the catalog driver-less. A FLOOR, not an exact count: the
    # point is that doctor reports a populated catalog with no driver installed,
    # and an exact number has to be bumped by every PR that adds an experiment.
    # It cannot be derived from the in-process registry either — this doctor runs
    # in a SUBPROCESS, whose discovery legitimately differs from the test session's.
    n_reported = re.search(r"catalog\s+(\d+) experiment\(s\)", proc.stdout)
    assert n_reported, proc.stdout
    assert int(n_reported.group(1)) >= 20, proc.stdout


def test_missing_registry_or_setup_fails(tmp_path):
    (tmp_path / "data").mkdir()
    proc = _doctor(tmp_path, _lab_body(tmp_path))  # device set, no cooldowns.toml
    assert proc.returncode == 1
    assert "[FAIL] cooldowns" in proc.stdout
    assert "scqo device cooldown start" in proc.stdout  # names the fix


def test_zero_setup_cycle_fails(tmp_path):
    data_root = tmp_path / "data"
    (data_root / "simdev").mkdir(parents=True)
    # An empty cycle is LEGAL at load time (v0.7.0), but runs would refuse — doctor FAILs.
    (data_root / "simdev" / "cooldowns.toml").write_text(
        "[cd1]\nstart = 2026-07-01\n", encoding="utf-8",
    )
    proc = _doctor(tmp_path, _lab_body(tmp_path))
    assert proc.returncode == 1
    assert "[FAIL] cooldowns" in proc.stdout
    assert "has NO setups" in proc.stdout
    assert "[cd1.setup.<name>]" in proc.stdout  # names the hand-edit fix


def test_ambiguous_setup_without_selection_fails(tmp_path):
    data_root = tmp_path / "data"
    (data_root / "simdev").mkdir(parents=True)
    (data_root / "simdev" / "cooldowns.toml").write_text(
        '[cd1]\nstart = 2026-07-01\n\n'
        '[cd1.setup.sim_a]\nbackend = "simulated"\n\n'
        '[cd1.setup.sim_b]\nbackend = "simulated"\n',
        encoding="utf-8",
    )
    proc = _doctor(tmp_path, _lab_body(tmp_path))  # SCQO_USER_CONFIG=none: no selection
    assert proc.returncode == 1
    assert "[FAIL] cooldowns" in proc.stdout
    assert "scqo user --setup" in proc.stdout  # names the fix command
    assert "sim_a" in proc.stdout and "sim_b" in proc.stdout  # and the choices


def test_missing_instrument_config_files_fail(tmp_path):
    data_root = tmp_path / "data"
    # the DERIVED vendor folder exists but is EMPTY: canonical vendor files absent
    folder = data_root / "chipA" / "cd1" / "qblox_main" / "backend_config"
    folder.mkdir(parents=True)
    (data_root / "chipA" / "cooldowns.toml").write_text(
        '[cd1]\nstart = 2026-07-01\n\n[cd1.setup.qblox_main]\nbackend = "qblox"\n',
        encoding="utf-8",
    )
    proc = _doctor(tmp_path, _lab_body(tmp_path, device="chipA"))
    assert proc.returncode == 1
    assert "[FAIL] instr config" in proc.stdout
    assert "dut_config.json" in proc.stdout


def test_no_config_warns_but_passes(tmp_path):
    proc = _doctor(tmp_path, None)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[WARN] lab config" in proc.stdout
    assert "NOTHING SAVED" in proc.stdout


def test_malformed_user_overlay_is_caught_not_crashed(tmp_path):
    user = tmp_path / "user.toml"
    user.write_text("not [valid toml", encoding="utf-8")
    env = {**os.environ, "SCQO_USER_CONFIG": str(user)}
    config = tmp_path / "config.toml"
    config.write_text("[lab]\n", encoding="utf-8")
    env["SCQO_CONFIG"] = str(config)
    proc = subprocess.run(
        [sys.executable, "-m", "scqo.cli", "doctor"],
        capture_output=True, text=True, env=env, cwd=tmp_path,
    )
    assert proc.returncode == 1
    assert "[FAIL] config" in proc.stdout
    assert "user.toml" in proc.stdout  # the message names the broken file


# ---- the multi-account-server witness (profile-resident install paths) ------
#
# The real incident (BLUEFORSAS2, 2026-08-11): uv's default layout bakes the
# base interpreter inside the INSTALLING user's profile, so doctor passes for
# the installer while every other account dies before Python starts ("uv
# trampoline failed to spawn Python child process"). The check logic is
# renderer-free in scqo.checks (all paths injectable); these tests build fake
# profile trees under tmp_path — no real foreign profile is ever touched.


def _fake_home(tmp_path: Path, name: str = "me") -> Path:
    home = tmp_path / "Users" / name
    home.mkdir(parents=True, exist_ok=True)
    return home


def test_profile_resident_base_warns_even_for_the_current_account(tmp_path):
    home = _fake_home(tmp_path)
    base = home / "AppData" / "Roaming" / "uv" / "python" / "cpython-3.12-windows-x86_64-none"
    [check] = profile_residency_checks(base=base, home=home)
    assert check.status == "WARN" and check.topic == "venv base"
    assert str(base) in check.message                        # names the path
    assert "uv trampoline failed to spawn" in check.message  # names the symptom
    # points at the documented fix
    assert "INSTALL §1" in check.message and "UV_PYTHON_INSTALL_DIR" in check.message


def test_foreign_profile_base_warns_naming_the_owner(tmp_path):
    home = _fake_home(tmp_path)
    base = tmp_path / "Users" / "Qualibrator" / "AppData" / "Roaming" / "uv" / "python"
    [check] = profile_residency_checks(base=base, home=home)
    assert check.status == "WARN"
    assert "Qualibrator's profile" in check.message and "(you are me)" in check.message


def test_shared_dir_base_is_ok_and_named(tmp_path):
    home = _fake_home(tmp_path)
    base = tmp_path / "uv" / "python" / "cpython-3.12.13-windows-x86_64-none"
    [check] = profile_residency_checks(base=base, home=home)
    assert check.status == "OK" and check.topic == "venv base"
    assert str(base) in check.message


def test_own_profile_config_and_data_root_are_fine(tmp_path):
    # ~/.scqo/config.toml + a data_root in one's OWN profile = the normal
    # dev-machine layout; only the venv-base row appears, as OK.
    home = _fake_home(tmp_path)
    checks = profile_residency_checks(
        base=tmp_path / "uv" / "python",
        home=home,
        config_source=home / ".scqo" / "config.toml",
        data_root=home / "scqo_data",
    )
    assert [c.status for c in checks] == ["OK"]


def test_foreign_profile_config_and_data_root_warn(tmp_path):
    home = _fake_home(tmp_path)
    checks = profile_residency_checks(
        base=tmp_path / "uv" / "python",
        home=home,
        config_source=tmp_path / "Users" / "alice" / ".scqo" / "config.toml",
        data_root=tmp_path / "Users" / "bob" / "scqo_data",
    )
    by_topic = {c.topic: c for c in checks}
    assert by_topic["venv base"].status == "OK"
    assert by_topic["lab config"].status == "WARN"
    assert "alice's profile" in by_topic["lab config"].message
    assert by_topic["data_root"].status == "WARN"
    assert "bob's profile" in by_topic["data_root"].message


@pytest.mark.skipif(os.name != "nt",
                    reason="profile paths compare case-insensitively only on Windows")
def test_profile_match_is_case_insensitive_on_windows(tmp_path):
    home = _fake_home(tmp_path, "Me")
    base = tmp_path / "USERS" / "ME" / "uv" / "python"
    [check] = profile_residency_checks(base=base, home=home)
    assert check.status == "WARN"
    assert "this account's profile" in check.message  # own profile, despite the case


def test_home_directly_under_the_anchor_witnesses_nothing(tmp_path):
    # /root-style: the "profiles directory" would be the filesystem root and
    # every absolute path would look profile-resident — the witness stands down.
    home = Path(tmp_path.anchor) / "root"
    [check] = profile_residency_checks(base=tmp_path / "shared" / "python", home=home)
    assert check.status == "OK"


def test_default_base_resolves_via_pyvenv_cfg_home(tmp_path, monkeypatch):
    # Doctor witnesses the RUNNING venv: pyvenv.cfg `home` is the path the uv
    # trampoline re-executes, so that is the flagged path.
    home = _fake_home(tmp_path)
    baked = (tmp_path / "Users" / "Qualibrator" / "AppData" / "Roaming" / "uv"
             / "python" / "cpython-3.11.9-windows-x86_64-none")
    venv = tmp_path / "venv"
    venv.mkdir()
    (venv / "pyvenv.cfg").write_text(f"home = {baked}\nversion = 3.11.9\n", encoding="utf-8")
    monkeypatch.setattr(sys, "prefix", str(venv))
    [check] = profile_residency_checks(home=home)
    assert check.status == "WARN"
    assert "pyvenv.cfg home" in check.message and str(baked) in check.message


def test_default_base_falls_back_to_sys_base_executable(tmp_path, monkeypatch):
    home = _fake_home(tmp_path)
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "not-a-venv"))  # no pyvenv.cfg
    monkeypatch.setattr(sys, "_base_executable",
                        str(tmp_path / "Users" / "alice" / "python.exe"), raising=False)
    [check] = profile_residency_checks(home=home)
    assert check.status == "WARN" and "alice's profile" in check.message


def test_doctor_renders_the_profile_witness_rows(tmp_path):
    # End-to-end through the real CLI: faking USERPROFILE/HOME makes
    # tmp_path/Users the profiles directory, so a config + data_root planted
    # under OTHER fake accounts must warn — with no real foreign profile
    # involved. The venv-base row must render on every doctor run; its status
    # depends on where THIS interpreter lives, so only presence is asserted.
    home = tmp_path / "Users" / "me"
    home.mkdir(parents=True)
    foreign_cfg = tmp_path / "Users" / "alice" / "config.toml"
    foreign_cfg.parent.mkdir(parents=True)
    data_root = tmp_path / "Users" / "bob" / "data"
    foreign_cfg.write_text(f"[lab]\ndata_root = '{data_root.as_posix()}'\n", encoding="utf-8")
    env = {**os.environ, "SCQO_USER_CONFIG": "none", "SCQO_CONFIG": str(foreign_cfg),
           "USERPROFILE": str(home), "HOME": str(home)}
    proc = subprocess.run(
        [sys.executable, "-m", "scqo.cli", "doctor"],
        capture_output=True, text=True, env=env, cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr  # WARNs never fail doctor
    assert "venv base" in proc.stdout
    assert "[WARN] lab config" in proc.stdout and "alice's profile" in proc.stdout
    assert "[WARN] data_root" in proc.stdout and "bob's profile" in proc.stdout
    # ASCII-safe token from the INSTALL §1 pointer (the § itself can mangle when
    # subprocess stdout round-trips through the OS locale encoding on Windows)
    assert "UV_PYTHON_INSTALL_DIR" in proc.stdout
