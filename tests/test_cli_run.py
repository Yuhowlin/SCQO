"""`scqo run` end-to-end over the built-in simulated backend (no driver installed).

Subprocess-based (python -m scqo.cli), cwd = an arbitrary tmp dir — the commands must
work from ANY directory. Absorbs the parameter-cascade coverage that previously lived
in LCHQBDriver/tests/test_cli_parameters.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _run_cli(tmp_path: Path, *args: str, parameters_toml: str | None = None) -> subprocess.CompletedProcess:
    """Run `scqo <args>` against a temp lab config (simulated, persisted)."""
    lines = ["[lab]", 'backend = "simulated"', f"data_root = '{(tmp_path / 'data').as_posix()}'"]
    if parameters_toml is not None:
        params = tmp_path / "parameters.toml"
        params.write_text(parameters_toml, encoding="utf-8")
        lines.append(f"parameters_file = '{params.as_posix()}'")
    config = tmp_path / "config.toml"
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "scqo.cli", *args],
        capture_output=True,
        text=True,
        env={**os.environ, "SCQO_CONFIG": str(config), "SCQO_USER_CONFIG": "none"},
        cwd=tmp_path,  # an arbitrary directory — NOT a repo
    )


def _result(proc: subprocess.CompletedProcess) -> dict:
    return json.loads(proc.stdout.split("\nsaved:")[0])


def test_catalog_lists_without_any_driver(tmp_path):
    """The empty-catalog trap: scqo core registers nothing, so the simulated backend
    must self-register demo experiments (ensure_demo_experiments)."""
    proc = _run_cli(tmp_path, "run")
    assert proc.returncode == 0, proc.stderr
    for name in ("resonator_spectroscopy", "qubit_ramsey", "single_shot_readout"):
        assert name in proc.stdout
    assert "# user overlay: none" in proc.stdout


def test_run_help_shows_schema_epilog(tmp_path):
    proc = _run_cli(tmp_path, "run", "resonator_spectroscopy", "--help")
    assert proc.returncode == 0, proc.stderr
    assert "frequency_span_hz" in proc.stdout  # pydantic schema rendered in --help


def test_file_defaults_reach_the_saved_run(tmp_path):
    proc = _run_cli(
        tmp_path, "run", "resonator_spectroscopy",
        parameters_toml='[resonator_spectroscopy]\nnum_points = 51\nqubits = ["q0"]\n',
    )
    assert proc.returncode == 0, proc.stderr
    result = _result(proc)
    # file-supplied qubits applied — NOT masked by the all-device fallback (q0 AND q1)
    assert result["outcomes"] == {"q0": "successful"}
    saved = json.loads((Path(result["data_path"]) / "parameters.json").read_text(encoding="utf-8"))
    assert saved["num_points"] == 51
    # provenance goes to stderr so stdout stays parseable JSON
    assert "# parameter defaults from" in proc.stderr


def test_cli_set_beats_file_defaults(tmp_path):
    proc = _run_cli(
        tmp_path, "run", "resonator_spectroscopy", "--set", "num_points=99",
        parameters_toml="[resonator_spectroscopy]\nnum_points = 51\n",
    )
    assert proc.returncode == 0, proc.stderr
    saved = json.loads((Path(_result(proc)["data_path"]) / "parameters.json").read_text(encoding="utf-8"))
    assert saved["num_points"] == 99
