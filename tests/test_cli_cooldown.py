"""Cooldown-cycle lifecycle through `scqo cooldown` / `scqo run` / `scqo find`.

device -> cycle -> wiring era -> runs: start a cycle, measure (stamped), hand-add a
mapping snapshot (era moves), end the cycle (runs stamp "" again). Absorbs
LCHQBDriver/tests/test_cooldown_lifecycle.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path


def _env(tmp_path: Path) -> dict:
    config = tmp_path / "config.toml"
    config.write_text(
        "\n".join(
            [
                "[lab]",
                'backend = "simulated"',
                'device_name = "simdev"',
                f"data_root = '{(tmp_path / 'data').as_posix()}'",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return {**os.environ, "SCQO_CONFIG": str(config), "SCQO_USER_CONFIG": "none"}


def _cli(env: dict, tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "scqo.cli", *args],
        capture_output=True, text=True, env=env, cwd=tmp_path,
    )


def test_cooldown_lifecycle(tmp_path):
    env = _env(tmp_path)
    today = date.today().isoformat()

    # start a cycle; a second start must refuse while one is open
    proc = _cli(env, tmp_path, "cooldown", "start", "cd1", "--fridge", "TestFridge", "--packaging", "PCB v1")
    assert proc.returncode == 0, proc.stderr
    assert _cli(env, tmp_path, "cooldown", "start", "cd2").returncode != 0

    # a run is stamped with the cycle (no mapping yet -> empty wiring era)
    proc = _cli(env, tmp_path, "run", "resonator_spectroscopy", "--qubits", "q0")
    assert proc.returncode == 0, proc.stderr
    r1 = json.loads(proc.stdout.split("\nsaved:")[0])
    rec1 = json.loads((Path(r1["data_path"]) / "record.json").read_text(encoding="utf-8"))
    assert rec1["cooldown"] == "cd1" and rec1["wiring_since"] == ""

    # hand-add a wiring snapshot (the documented workflow) -> next run carries the era
    reg = tmp_path / "data" / "simdev" / "cooldowns.toml"
    reg.write_text(
        reg.read_text(encoding="utf-8")
        + f'\n[[cd1.mapping]]\nsince = {today}\n"q0.drive" = "cluster0.module2.out0"\n',
        encoding="utf-8",
    )
    show = _cli(env, tmp_path, "cooldown")
    assert show.returncode == 0, show.stderr
    assert "cluster0.module2.out0" in show.stdout  # validator shows the current wiring
    proc = _cli(env, tmp_path, "run", "resonator_spectroscopy", "--qubits", "q0")
    r2 = json.loads(proc.stdout.split("\nsaved:")[0])
    rec2 = json.loads((Path(r2["data_path"]) / "record.json").read_text(encoding="utf-8"))
    assert rec2["cooldown"] == "cd1" and rec2["wiring_since"] == today

    # find by cycle through the query command
    listed = _cli(env, tmp_path, "find", "--cooldown", "cd1")
    assert r1["run_id"] in listed.stdout and r2["run_id"] in listed.stdout

    # end the cycle: file stays parseable (validated, .bak written), later runs stamp ""
    proc = _cli(env, tmp_path, "cooldown", "end")
    assert proc.returncode == 0, proc.stderr
    assert reg.with_suffix(".toml.bak").is_file()
    proc = _cli(env, tmp_path, "run", "resonator_spectroscopy", "--qubits", "q0")
    r3 = json.loads(proc.stdout.split("\nsaved:")[0])
    rec3 = json.loads((Path(r3["data_path"]) / "record.json").read_text(encoding="utf-8"))
    assert rec3["cooldown"] == ""
