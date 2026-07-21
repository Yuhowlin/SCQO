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
    """Run `scqo <args>` against a temp lab: device simdev on a simulated setup."""
    data_root = tmp_path / "data"
    (data_root / "simdev").mkdir(parents=True, exist_ok=True)
    reg = data_root / "simdev" / "cooldowns.toml"
    if not reg.is_file():
        reg.write_text('[cd1]\nstart = 2026-07-01\n[cd1.setup.main]\n'
                       'backend = "simulated"\n', encoding="utf-8")
    roster = data_root / "simdev" / "components.toml"
    if not roster.is_file():
        blocks = ["schema = 1"]
        for q in ("q0", "q1"):
            blocks.append(
                f'[components.{q}]\nphysical   = "FixedTransmon"\n'
                f'instrument = "ReadableTransmon"\noperations = ["rx", "readout"]\n'
                f'[components.{q}_res]\nphysical = "Resonator"\n'
                f'[components.{q}_ro]\nphysical = "ReadoutLine"\n'
                f'members  = {{ transmon = "{q}", resonator = "{q}_res" }}\n'
                f'[components.{q}_xy]\nphysical = "XYControl"\n'
                f'members  = {{ transmon = "{q}" }}'
            )
        roster.write_text("\n".join(blocks) + "\n", encoding="utf-8")
    lines = ["[lab]", 'device = "simdev"', f"data_root = '{data_root.as_posix()}'"]
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
        parameters_toml='[resonator_spectroscopy]\nnum_points = 51\ntargets = ["q0"]\n',
    )
    assert proc.returncode == 0, proc.stderr
    result = _result(proc)
    # file-supplied targets applied — NOT masked by the all-device fallback (q0 AND q1)
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


# ------------------------------------------------------- suggest / review / accept


def test_default_run_suggests_then_accept_by_run_id(tmp_path):
    """The full deferred flow, non-TTY (a subprocess IS the script case): the run
    leaves suggestions pending with a decide-later hint on stderr (stdout stays
    parseable JSON), and `scqo accept <run_id>` applies them later."""
    proc = _run_cli(tmp_path, "run", "resonator_spectroscopy", "--targets", "q0")
    assert proc.returncode == 0, proc.stderr
    result = _result(proc)  # stdout parses despite the extra stderr output
    assert [s["field"] for s in result["suggestions"]] == ["readout_freq", "f_r_hz", "kappa_tot_hz"]
    assert {s["status"] for s in result["suggestions"]} == {"pending"}
    assert "suggested updates" in proc.stderr
    assert f"scqo accept {result['run_id']}" in proc.stderr

    # the pending run is findable three ways (all datastore-only)
    listing = _run_cli(tmp_path, "accept")
    assert result["run_id"] in listing.stdout and "pending:3" in listing.stdout
    table = _run_cli(tmp_path, "accept", result["run_id"], "--list")
    assert table.returncode == 0 and "readout_freq" in table.stdout
    found = _run_cli(tmp_path, "find", "--pending")
    assert result["run_id"] in found.stdout and "pend:3" in found.stdout

    # non-TTY accept with no selectors applies ALL pending
    accept = _run_cli(tmp_path, "accept", result["run_id"], "--comment", "looks right")
    assert accept.returncode == 0, accept.stderr
    summary = json.loads(accept.stdout)
    assert [a["field"] for a in summary["applied"]] == ["readout_freq", "f_r_hz", "kappa_tot_hz"]
    assert summary["pending_left"] == 0

    # the change history carries the ORIGINATING run id
    history = _run_cli(tmp_path, "state", "--history")
    assert result["run_id"] in history.stdout and "readout_freq" in history.stdout

    # nothing left to decide
    assert "no runs with pending suggestions" in _run_cli(tmp_path, "accept").stdout
    assert "no runs match" in _run_cli(tmp_path, "find", "--pending").stdout


def test_run_accept_flag_applies_immediately(tmp_path):
    proc = _run_cli(tmp_path, "run", "resonator_spectroscopy", "--targets", "q0", "--accept")
    assert proc.returncode == 0, proc.stderr
    result = _result(proc)
    assert {s["status"] for s in result["suggestions"]} == {"accepted"}
    history = _run_cli(tmp_path, "state", "--history")
    assert result["run_id"] in history.stdout
    # the context header says WHOSE state/history this is (per (cooldown, setup))
    header = history.stdout.splitlines()[0]
    assert header.startswith("# device: simdev") and "setup: main" in header
    assert "cd1" in header and "main" in header and "scqo_state.json" in header


def test_reject_needs_no_backend(tmp_path):
    proc = _run_cli(tmp_path, "run", "qubit_relaxation", "--targets", "q0")
    run_id = _result(proc)["run_id"]

    reject = _run_cli(tmp_path, "accept", run_id, "--reject", "--comment", "noisy fit")
    assert reject.returncode == 0, reject.stderr
    summary = json.loads(reject.stdout)
    assert summary["rejected"] == [{"component": "q0", "field": "t1_s"}]
    assert "no runs match" in _run_cli(tmp_path, "find", "--pending").stdout
    # the T1 was never applied: the physical table stays empty
    physical = _run_cli(tmp_path, "state", "--physical")
    assert "no physical parameters recorded yet" in physical.stdout


def test_suggest_attaches_operator_value_then_accept(tmp_path):
    """`scqo suggest` end-to-end, non-TTY: the estimator was told not to update
    (--no-update stands in for a failed fit), the operator attaches figure-read
    values, the run becomes pending, and a later accept applies them."""
    proc = _run_cli(tmp_path, "run", "resonator_spectroscopy", "--targets", "q0", "--no-update")
    run_id = _result(proc)["run_id"]
    assert "no runs match" in _run_cli(tmp_path, "find", "--pending").stdout

    suggest = _run_cli(tmp_path, "suggest", run_id,
                       "q0.readout_freq=5.912e9", "q0_res.f_r_hz=5.912e9",
                       "--comment", "read off the dip")
    assert suggest.returncode == 0, suggest.stderr
    summary = json.loads(suggest.stdout)  # stdout stays parseable JSON
    assert summary["pending_total"] == 2
    assert [a["field"] for a in summary["added"]] == ["readout_freq", "f_r_hz"]
    # non-TTY: table + operator marker + decide-later hint on stderr, nothing applied
    assert "[operator:" in suggest.stderr and "read off the dip" in suggest.stderr
    assert f"scqo accept {run_id}" in suggest.stderr

    assert run_id in _run_cli(tmp_path, "find", "--pending").stdout
    table = _run_cli(tmp_path, "accept", run_id, "--list")
    assert "[operator:" in table.stdout

    accept = _run_cli(tmp_path, "accept", run_id)
    assert accept.returncode == 0, accept.stderr
    assert [a["field"] for a in json.loads(accept.stdout)["applied"]] == ["readout_freq", "f_r_hz"]
    history = _run_cli(tmp_path, "state", "--history")
    assert run_id in history.stdout  # credited to the run whose figure justified it

    # a bad assignment fails loudly, with the fix in the message and no traceback
    bad = _run_cli(tmp_path, "suggest", run_id, "q0.t1_sec=1e-6")
    assert bad.returncode != 0
    assert "no field 't1_sec'" in bad.stderr and "Traceback" not in bad.stderr
    malformed = _run_cli(tmp_path, "suggest", run_id, "q0.readout_freq")
    assert malformed.returncode != 0 and "COMPONENT.FIELD=VALUE" in malformed.stderr


def test_accept_points_at_suggest_when_the_estimator_proposed_nothing(tmp_path):
    """The dead end: a run with no suggestions (the fit failed) used to print NOTHING
    at a terminal / a bare '0 applied' in a script, pointing nowhere. Both paths must
    name `scqo suggest` — that is the moment a user needs to learn it exists."""
    run_id = _result(_run_cli(tmp_path, "run", "resonator_spectroscopy",
                              "--targets", "q0", "--no-update"))["run_id"]

    listed = _run_cli(tmp_path, "accept", run_id, "--list")
    assert listed.returncode == 0, listed.stderr
    assert f"scqo suggest {run_id} q0." in listed.stdout  # the run's OWN qubit

    accept = _run_cli(tmp_path, "accept", run_id)
    assert accept.returncode == 0, accept.stderr
    assert f"scqo suggest {run_id} q0." in accept.stderr
    assert json.loads(accept.stdout)["pending_left"] == 0  # stdout stays parseable


def test_reapply_rolls_back_from_the_cli(tmp_path):
    """Two accepted runs; `scqo accept <first> --reapply` restores the first value."""
    proc_a = _run_cli(tmp_path, "run", "resonator_spectroscopy", "--targets", "q0", "--accept")
    run_a = _result(proc_a)["run_id"]
    value_a = _result(proc_a)["suggestions"][0]["after"]
    _run_cli(tmp_path, "run", "resonator_spectroscopy", "--targets", "q0", "--accept")

    # decided items are refused without the flag...
    plain = _run_cli(tmp_path, "accept", run_a)
    assert json.loads(plain.stdout)["applied"] == []
    # ...and restored with it
    proc = _run_cli(tmp_path, "accept", run_a, "--reapply", "--comment", "rollback")
    assert proc.returncode == 0, proc.stderr
    summary = json.loads(proc.stdout)
    assert [a["field"] for a in summary["applied"]] == ["readout_freq", "f_r_hz", "kappa_tot_hz"]
    assert summary["applied"][0]["after"] == value_a

    history = _run_cli(tmp_path, "state", "--history")
    assert history.stdout.count(run_a) == 2  # first accept + the rollback


def test_accepted_physics_shows_in_device_physical(tmp_path):
    proc = _run_cli(tmp_path, "run", "qubit_relaxation", "--targets", "q0", "--accept")
    assert proc.returncode == 0, proc.stderr
    physical = _run_cli(tmp_path, "state", "--physical")
    # this context's flat physics: one row per (qubit, field) — no setup column
    t1_row = next(line for line in physical.stdout.splitlines() if "t1_s" in line)
    assert "q0" in t1_row
    history = _run_cli(tmp_path, "state", "--physical", "--history")
    assert "t1_s" in history.stdout and _result(proc)["run_id"] in history.stdout
    assert "setup=main" in history.stdout  # rows still carry the measuring setup


def test_device_sources_traces_current_values(tmp_path):
    """`scqo state --sources`: every current value names the run that set it,
    across BOTH stores; a hand-edited state file shows as externally changed."""
    proc_a = _run_cli(tmp_path, "run", "resonator_spectroscopy", "--targets", "q0", "--accept")
    run_a = _result(proc_a)["run_id"]
    proc_t1 = _run_cli(tmp_path, "run", "qubit_relaxation", "--targets", "q0", "--accept")
    run_t1 = _result(proc_t1)["run_id"]

    src = _run_cli(tmp_path, "state", "--sources")
    assert src.returncode == 0, src.stderr
    readout_row = next(line for line in src.stdout.splitlines() if "readout_freq" in line)
    assert run_a in readout_row
    t1_row = next(line for line in src.stdout.splitlines() if "t1_s" in line)
    assert run_t1 in t1_row and "physical" in t1_row

    # after a rollback the credit follows the value back to the first run
    _run_cli(tmp_path, "run", "resonator_spectroscopy", "--targets", "q0", "--accept")
    _run_cli(tmp_path, "accept", run_a, "--reapply", "--comment", "rollback")
    src2 = _run_cli(tmp_path, "state", "--sources")
    assert run_a in next(line for line in src2.stdout.splitlines() if "readout_freq" in line)

    # strict match: a hand-edited value credits no run (per-(cooldown, setup) file)
    state_path = tmp_path / "data" / "simdev" / "cd1" / "main" / "scqo" / "scqo_state.json"
    data = json.loads(state_path.read_text(encoding="utf-8"))
    data["config"]["q0"]["readout_freq"] = 9.9e9  # another tool wrote the config
    state_path.write_text(json.dumps(data), encoding="utf-8")
    src3 = _run_cli(tmp_path, "state", "--sources")
    readout_row3 = next(line for line in src3.stdout.splitlines() if "readout_freq" in line)
    assert "(externally changed)" in readout_row3 and run_a not in readout_row3

    # --sources is one table over both stores; combining is a usage error
    assert _run_cli(tmp_path, "state", "--sources", "--physical").returncode == 2
