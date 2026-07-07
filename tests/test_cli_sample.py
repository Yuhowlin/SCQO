"""`scqo sample` — the add-a-sample scaffold (prints snippets, never edits shared files).

Absorbs LCHQBDriver/tests/test_sample_scaffold.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _run(tmp_path: Path, config: Path | None, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "SCQO_USER_CONFIG": "none"}
    if config is not None:
        env["SCQO_CONFIG"] = str(config)
    return subprocess.run(
        [sys.executable, "-m", "scqo.cli", "sample", *args],
        capture_output=True, text=True, env=env, cwd=tmp_path,
    )


def _config(tmp_path: Path) -> Path:
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "instruments.toml").write_text(
        '[cluster0]\nkind = "qblox_cluster"\naddress = "192.168.0.2"\n', encoding="utf-8"
    )
    config = tmp_path / "config.toml"
    config.write_text(
        f"[lab]\nbackend = \"simulated\"\ndevice_name = \"demo\"\ndata_root = '{data_root.as_posix()}'\n",
        encoding="utf-8",
    )
    return config


def test_scaffold_prints_snippets_and_creates_folder(tmp_path):
    config = _config(tmp_path)
    proc = _run(tmp_path, config, "new", "chipC", "--backend", "qblox", "--instrument", "cluster0",
                "--description", "3-qubit test chip")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "[qblox]" in out and '"chipC"' in out  # paste-ready vendor table
    assert "chipC/scqo_state.json" in out
    assert "[chipC]" in out and "3-qubit test chip" in out  # devices.toml block
    assert 'mounted_on = "cluster0"' in out
    # known instrument -> NO instruments.toml scaffold section
    assert "is not\n   registered yet" not in out
    assert "scqo cooldown start" in out  # next steps use the console command
    assert (tmp_path / "data" / "chipC").is_dir()  # the one write

    # the shared files were NOT touched (governance)
    assert "chipC" not in config.read_text(encoding="utf-8")
    assert "chipC" not in (tmp_path / "data" / "instruments.toml").read_text(encoding="utf-8")


def test_unknown_instrument_gets_registry_snippet(tmp_path):
    proc = _run(tmp_path, _config(tmp_path), "new", "chipD", "--backend", "qm", "--instrument", "fridgeX")
    assert proc.returncode == 0, proc.stderr
    assert "[qm]" in proc.stdout and "state_dir" in proc.stdout
    assert "[fridgeX]" in proc.stdout  # unknown instrument -> instruments.toml scaffold


def test_existing_name_warns(tmp_path):
    config = _config(tmp_path)
    assert _run(tmp_path, config, "new", "chipC").returncode == 0
    proc = _run(tmp_path, config, "new", "chipC")  # folder now exists from the first call
    assert proc.returncode == 0
    assert "already known" in proc.stderr


def test_requires_data_root_and_checklist_works(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[lab]\nbackend = "simulated"\n', encoding="utf-8")
    proc = _run(tmp_path, config, "new", "chipC")
    assert proc.returncode != 0
    assert "data_root" in proc.stderr

    checklist = _run(tmp_path, config)  # no command -> the self-documenting checklist
    assert checklist.returncode == 0
    assert "MANUAL" in checklist.stdout and "AUTOMATIC" in checklist.stdout
