"""`scqo devices` — the Tier-1 discovery menu (config + registries only, no instrument).

Absorbs LCHQBDriver/tests/test_devices_menu.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _run(tmp_path: Path, config: Path, user: str | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "SCQO_CONFIG": str(config), "SCQO_USER_CONFIG": "none"}
    if user is not None:
        user_file = config.parent / "user.toml"
        user_file.write_text(user, encoding="utf-8")
        env["SCQO_USER_CONFIG"] = str(user_file)
    return subprocess.run(
        [sys.executable, "-m", "scqo.cli", "devices"],
        capture_output=True, text=True, env=env, cwd=tmp_path,
    )


def test_menu_lists_backends_and_selection_hint(tmp_path):
    data_root = tmp_path / "data"
    (data_root / "chipA").mkdir(parents=True)
    (data_root / "chipA" / "cooldowns.toml").write_text(
        '[cd3]\nstart = 2026-07-01\npackaging = "PCB v3"\n\n'
        '[[cd3.mapping]]\nsince = 2026-07-01\n"q1.drive" = "cluster0.module2.out0"\n',
        encoding="utf-8",
    )
    (data_root / "instruments.toml").write_text(
        '[cluster0]\nkind = "qblox_cluster"\naddress = "192.168.0.2"\n', encoding="utf-8"
    )
    config = tmp_path / "config.toml"
    config.write_text(
        "[lab]\n"
        'backend = "simulated"\n'
        'device_name = "demo"\n'
        f"data_root = '{data_root.as_posix()}'\n\n"
        "[qblox]\n"
        'device_name = "chipA"\n',
        encoding="utf-8",
    )

    out = _run(tmp_path, config).stdout
    assert "qblox" in out and "chipA" in out  # vendor table -> a selectable backend
    assert "cd3 [PCB v3]" in out  # active cycle + packaging
    assert "cluster0 (192.168.0.2)" in out  # wiring-referenced instrument + address
    assert "LCHQBDriver" in out and ".venv-qblox" in out  # where to run it
    assert "scqo built-in" in out  # the simulated row
    assert "user.toml" in out  # the how-to-select hint
    assert out.count("<- selected") == 1  # exactly one current backend marked

    # the overlay moves the selection marker (choose device = choose instrument)
    out = _run(tmp_path, config, user='backend = "qblox"\n').stdout
    assert "# user overlay:" in out
    selected_line = next(line for line in out.splitlines() if "<- selected" in line)
    assert selected_line.startswith("qblox")


def test_menu_degrades_without_registries(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[lab]\nbackend = "simulated"\n', encoding="utf-8")
    proc = _run(tmp_path, config)
    assert proc.returncode == 0, proc.stderr
    assert "simulated" in proc.stdout  # no data_root, no registries — still a menu
