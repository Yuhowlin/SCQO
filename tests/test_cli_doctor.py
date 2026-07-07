"""`scqo doctor` — the health check that should be everyone's first debugging move."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


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


def test_healthy_simulated_setup_passes(tmp_path):
    data_root = tmp_path / "data"
    (data_root / "simdev").mkdir(parents=True)
    (data_root / "simdev" / "cooldowns.toml").write_text(
        '[cd1]\nstart = 2026-07-01\n\n[[cd1.mapping]]\nsince = 2026-07-01\n"q0.drive" = "c0.m2.o0"\n',
        encoding="utf-8",
    )
    proc = _doctor(
        tmp_path,
        f"[lab]\nbackend = \"simulated\"\ndevice_name = \"simdev\"\ndata_root = '{data_root.as_posix()}'\n",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "all checks passed" in proc.stdout
    assert "cd1 ACTIVE" in proc.stdout
    assert "12 experiment(s)" in proc.stdout  # simulated fills the catalog driver-less


def test_broken_cooldowns_registry_fails(tmp_path):
    data_root = tmp_path / "data"
    (data_root / "simdev").mkdir(parents=True)
    (data_root / "simdev" / "cooldowns.toml").write_text("not [valid toml", encoding="utf-8")
    proc = _doctor(
        tmp_path,
        f"[lab]\nbackend = \"simulated\"\ndevice_name = \"simdev\"\ndata_root = '{data_root.as_posix()}'\n",
    )
    assert proc.returncode == 1
    assert "[FAIL] cooldowns" in proc.stdout


def test_missing_driver_fails_with_venv_hint(tmp_path):
    from importlib.metadata import entry_points

    if any(ep.name == "qblox" for ep in entry_points(group="scqo.backends")):
        import pytest

        pytest.skip("qblox driver installed in this env")
    proc = _doctor(tmp_path, '[lab]\nbackend = "qblox"\n')
    assert proc.returncode == 1
    assert "[FAIL] backend" in proc.stdout
    assert ".venv-qblox" in proc.stdout


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
    config.write_text('[lab]\nbackend = "simulated"\n', encoding="utf-8")
    env["SCQO_CONFIG"] = str(config)
    proc = subprocess.run(
        [sys.executable, "-m", "scqo.cli", "doctor"],
        capture_output=True, text=True, env=env, cwd=tmp_path,
    )
    assert proc.returncode == 1
    assert "[FAIL] config" in proc.stdout
    assert "user.toml" in proc.stdout  # the message names the broken file
