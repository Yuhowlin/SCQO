"""`scqo device` — the admin group (v0.7.0): list menu, add-a-sample scaffold.

Merged from the retired `scqo devices` (menu) and `scqo sample` (scaffold) tests:
`device list` renders one row per NAMED setup of each device's ACTIVE cycle, and
`device add` creates the data folder + prints every paste-ready manual step.
Cooldown start/end lives in tests/test_cli_cooldown.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _scqo(tmp_path: Path, config: Path, *args: str, user: str | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "SCQO_CONFIG": str(config), "SCQO_USER_CONFIG": "none"}
    if user is not None:
        user_file = config.parent / "user.toml"
        user_file.write_text(user, encoding="utf-8")
        env["SCQO_USER_CONFIG"] = str(user_file)
    return subprocess.run(
        [sys.executable, "-m", "scqo.cli", "device", *args],
        capture_output=True, text=True, env=env, cwd=tmp_path,
    )


def _config(tmp_path: Path) -> Path:
    (tmp_path / "data").mkdir(exist_ok=True)
    config = tmp_path / "config.toml"
    config.write_text(f"[lab]\ndata_root = '{(tmp_path / 'data').as_posix()}'\n", encoding="utf-8")
    return config


# ----------------------------------------------------------------------- list
def _registries(tmp_path: Path) -> Path:
    """chipA: ACTIVE cycle with TWO named setups; simdev: single simulated setup."""
    data_root = tmp_path / "data"
    (data_root / "chipA").mkdir(parents=True)
    (data_root / "chipA" / "cooldowns.toml").write_text(
        '[cd3]\nstart = 2026-07-01\npackaging = "PCB v3"\n\n'
        '[cd3.setup.qblox_main]\nbackend = "qblox"\n\n'
        '[cd3.setup.qm_alt]\nbackend = "qm"\n',
        encoding="utf-8",
    )
    (data_root / "simdev").mkdir()
    (data_root / "simdev" / "cooldowns.toml").write_text(
        '[cd1]\nstart = 2026-07-01\n\n[cd1.setup.sim_only]\nbackend = "simulated"\n',
        encoding="utf-8",
    )
    return _config(tmp_path)


def test_menu_one_row_per_setup_and_marker_follows_user_selection(tmp_path):
    config = _registries(tmp_path)

    # user.toml picks device + setup -> the marker lands on exactly that pair
    out = _scqo(tmp_path, config, "list",
                user='device = "chipA"\nsetup = "qm_alt"\n').stdout
    assert "cd3 [PCB v3]" in out  # cycle id + packaging on the device row
    qblox_row = next(line for line in out.splitlines() if "qblox_main" in line)
    qm_row = next(line for line in out.splitlines() if "qm_alt" in line)
    assert qblox_row.startswith("chipA")  # device name on the FIRST setup row only
    assert qm_row[:12].strip() == ""  # continuation row: device + cycle columns blank
    # backend + the DERIVED config folder (<cid>/<setup>/backend_config)
    assert "qblox" in qblox_row and "backend_config" in qblox_row
    assert "LCHQBDriver" in qblox_row and ".venv-qblox" in qblox_row  # where it runs
    assert "LCHQMDriver" in qm_row and "backend_config" in qm_row
    assert "simdev" in out and "simulated" in out
    assert "scqo user --device" in out  # the how-to-select footer
    assert out.count("<- selected") == 1
    assert "qm_alt" in next(line for line in out.splitlines() if "<- selected" in line)


def test_menu_single_setup_auto_marks_without_setup_selection(tmp_path):
    config = _registries(tmp_path)
    # bare `scqo device` = list; only the DEVICE is selected, its lone setup auto-marks
    out = _scqo(tmp_path, config, user='device = "simdev"\n').stdout
    assert out.count("<- selected") == 1
    selected_line = next(line for line in out.splitlines() if "<- selected" in line)
    assert selected_line.startswith("simdev") and "sim_only" in selected_line
    # chipA has TWO setups and no selection -> no row of its own gets the marker
    assert "qblox_main" in out and "qm_alt" in out
    assert "scqo user --device" in out


def test_menu_shows_registry_errors_and_degrades(tmp_path):
    data_root = tmp_path / "data"
    (data_root / "broken").mkdir(parents=True)
    (data_root / "broken" / "cooldowns.toml").write_text("not [valid toml", encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text(f"[lab]\ndata_root = '{data_root.as_posix()}'\n", encoding="utf-8")
    proc = _scqo(tmp_path, config, "list")
    assert proc.returncode == 0, proc.stderr
    assert "REGISTRY ERROR" in proc.stdout  # broken device shown, menu still renders

    config2 = tmp_path / "config2.toml"
    config2.write_text("[lab]\n", encoding="utf-8")
    proc = _scqo(tmp_path, config2, "list")
    assert proc.returncode == 0
    assert "no data_root" in proc.stdout  # no registries at all — still explains itself


# ------------------------------------------------------------------------ add
def test_add_prints_steps_and_creates_folder(tmp_path):
    config = _config(tmp_path)
    before = config.read_text(encoding="utf-8")
    proc = _scqo(tmp_path, config, "add", "chipC", "--description", "3-qubit test chip")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "scqo device cooldown start cd1" in out  # step 1: the EMPTY cycle
    assert "[cd1.setup." in out  # step 2: the hand-pasted named-setup block
    assert "devices.toml" in out and "[chipC]" in out and "3-qubit test chip" in out
    assert "scqo user --device chipC" in out  # how users select the new sample
    assert (tmp_path / "data" / "chipC").is_dir()  # the command's ONLY write
    # shared config untouched (governance: add never edits config files)
    assert config.read_text(encoding="utf-8") == before
    assert "chipC" not in before


def test_add_existing_name_warns(tmp_path):
    config = _config(tmp_path)
    assert _scqo(tmp_path, config, "add", "chipC").returncode == 0
    proc = _scqo(tmp_path, config, "add", "chipC")  # folder now exists from the first call
    assert proc.returncode == 0
    assert "already known" in proc.stderr


def test_add_requires_data_root(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text("[lab]\n", encoding="utf-8")
    proc = _scqo(tmp_path, config, "add", "chipC")
    assert proc.returncode != 0
    assert "data_root" in proc.stderr
