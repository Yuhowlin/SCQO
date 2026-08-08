"""`scqo campaign` and `scqo run --repeat` end-to-end over the built-in simulated backend.

Subprocess-based (python -m scqo.cli), cwd = an arbitrary tmp dir — the same temp-lab
shape as tests/test_cli_run.py. Covers the rule-preserving guard too: `scqo campaign`
must REFUSE a bare experiment name, so "there is exactly one way to run one experiment"
is a checked property rather than a convention.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_F01 = (3.8e9, 3.95e9)
_FR = (5.95e9, 6.05e9)

PLAN = """\
label = "t1_stability"
repeat = 2
skip_artifacts = true
tags = ["stability"]

[defaults]
targets = ["q0"]

[[steps]]
experiment = "qubit_relaxation"

[[steps]]
experiment = "qubit_echo"
"""


#: Standing lab defaults the plan deliberately does NOT restate — the whole point
#: of the note under test is that a reader cannot see these in the plan file.
PARAMETERS_TOML = """\
[qubit_relaxation]
num_points = 41
num_averages = 30

[qubit_echo]
num_averages = 30
"""


def _lab(tmp_path: Path) -> Path:
    """A temp lab: device simdev on a simulated setup (see tests/test_cli_run.py)."""
    data_root = tmp_path / "data"
    (data_root / "simdev").mkdir(parents=True, exist_ok=True)
    (data_root / "simdev" / "cooldowns.toml").write_text(
        '[cd1]\nstart = 2026-07-01\n[cd1.setup.main]\nbackend = "simulated"\n',
        encoding="utf-8")
    blocks = ["schema = 3"]
    for q in ("q0", "q1"):
        blocks.append(f'[modes.{q}]\nkind = "transmon"')
    blocks.append('[lines.fl]\nreadout = ["q0", "q1"]')
    blocks.extend(f'[lines.{q}_xyl]\ndrive = ["{q}"]' for q in ("q0", "q1"))
    (data_root / "simdev" / "components.toml").write_text(
        "\n".join(blocks) + "\n", encoding="utf-8")
    design = ["schema = 1"]
    for i, q in enumerate(("q0", "q1")):
        design.append(f"[{q}]\nf_01_hz = {_F01[i]:.6g}")
        design.append(f"[{q}_res]\nf_r_hz = {_FR[i]:.6g}")
    (data_root / "simdev" / "design.toml").write_text(
        "\n".join(design) + "\n", encoding="utf-8")
    params = tmp_path / "parameters.toml"
    params.write_text(PARAMETERS_TOML, encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text(
        f"[lab]\ndevice = \"simdev\"\ndata_root = '{data_root.as_posix()}'\n"
        f"parameters_file = '{params.as_posix()}'\n",
        encoding="utf-8")
    return config


def _cli(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    config = tmp_path / "config.toml"
    if not config.is_file():
        config = _lab(tmp_path)
    return subprocess.run(
        [sys.executable, "-m", "scqo.cli", *args],
        capture_output=True, text=True,
        # SCQO_USER_CONFIG=none and an empty parameters file keep the runner's real
        # ~/.scqo out of the test (a lab default could make a step fail here).
        env={**os.environ, "SCQO_CONFIG": str(config), "SCQO_USER_CONFIG": "none",
             "MPLBACKEND": "Agg"},
        cwd=tmp_path,
    )


def _plan(tmp_path: Path, text: str = PLAN) -> str:
    path = tmp_path / "plan.toml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_campaign_refuses_a_bare_experiment_name(tmp_path):
    """The one-way-to-run-one-experiment rule, enforced in code."""
    proc = _cli(tmp_path, "campaign", "qubit_relaxation")
    assert proc.returncode != 0
    assert "not an experiment name" in proc.stderr
    assert "scqo run qubit_relaxation --repeat N" in proc.stderr


def test_campaign_reports_a_missing_plan_file(tmp_path):
    proc = _cli(tmp_path, "campaign", "no_such_plan.toml")
    assert proc.returncode != 0
    assert "campaign plan not found" in proc.stderr


def test_dry_run_validates_and_creates_nothing(tmp_path):
    proc = _cli(tmp_path, "campaign", _plan(tmp_path), "--dry-run")
    assert proc.returncode == 0, proc.stderr
    assert "would create 4 runs (2 repeats x 2 steps)" in proc.stdout
    assert "plan is valid" in proc.stdout
    assert "step 0  qubit_relaxation" in proc.stdout
    # nothing ran: no day folder, no campaigns folder
    assert not (tmp_path / "data" / "simdev" / "campaigns").exists()
    assert not list((tmp_path / "data" / "simdev").glob("2*"))


def test_dry_run_refuses_a_bad_target_before_any_hardware(tmp_path):
    bad = PLAN.replace('targets = ["q0"]', 'targets = ["ghost"]')
    proc = _cli(tmp_path, "campaign", _plan(tmp_path, bad), "--dry-run")
    assert proc.returncode == 2
    assert "REFUSED" in proc.stderr and "not in this device's roster" in proc.stderr


def test_campaign_runs_and_prints_statistics(tmp_path):
    proc = _cli(tmp_path, "campaign", _plan(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert "status: complete" in proc.stdout
    assert "repeats   2/2" in proc.stdout
    for quantity in ("t1_s", "t2_echo_s"):
        assert quantity in proc.stdout
    assert "std/err" in proc.stdout

    listed = _cli(tmp_path, "campaign", "--list")
    assert listed.returncode == 0, listed.stderr
    assert "t1_stability" in listed.stdout and "complete" in listed.stdout

    campaign_id = listed.stdout.split()[0]
    shown = _cli(tmp_path, "campaign", "--show", campaign_id)
    assert shown.returncode == 0, shown.stderr
    assert "children (4, execution order):" in shown.stdout
    # interleaved: relaxation, echo, relaxation, echo — not grouped by experiment
    order = [line.split()[2] for line in shown.stdout.splitlines()
             if line.startswith("  r")]
    assert order == ["qubit_relaxation", "qubit_echo"] * 2

    found = _cli(tmp_path, "find", "--campaign", campaign_id)
    assert found.returncode == 0, found.stderr
    assert "(4 run(s)" in found.stdout


def test_campaign_json_output_is_the_manifest(tmp_path):
    proc = _cli(tmp_path, "campaign", _plan(tmp_path), "--json", "--repeat", "1")
    assert proc.returncode == 0, proc.stderr
    manifest = json.loads(proc.stdout)
    assert manifest["repeat_done"] == 1 and manifest["status"] == "complete"
    assert manifest["statistics"]["qubit_relaxation"]["q0"]["t1_s"]["n"] == 1


def test_list_says_so_when_there_are_no_campaigns(tmp_path):
    proc = _cli(tmp_path, "campaign", "--list")
    assert proc.returncode == 0, proc.stderr
    assert "no campaigns yet" in proc.stdout


def test_progress_goes_to_stderr_and_never_pollutes_stdout(tmp_path):
    """The whole reason progress is on stderr: stdout stays machine-readable.

    On QM the vendor already rewrites a \\r bar on stdout; ours must not join it."""
    proc = _cli(tmp_path, "campaign", _plan(tmp_path), "--json")
    assert proc.returncode == 0, proc.stderr

    assert "# repeat    1/2" in proc.stderr
    assert "# repeat    2/2" in proc.stderr
    assert "qubit_relaxation" in proc.stderr and "t1_s=" in proc.stderr
    assert "\r" not in proc.stderr

    assert "# repeat" not in proc.stdout
    manifest = json.loads(proc.stdout)  # still exactly the manifest, nothing else
    assert manifest["repeat_done"] == 2


def test_progress_shows_each_repeat_of_a_bare_repeat_run(tmp_path):
    proc = _cli(tmp_path, "run", "qubit_relaxation", "--targets", "q0",
                "--repeat", "3", "--skip-artifacts")
    assert proc.returncode == 0, proc.stderr
    starts = [ln for ln in proc.stderr.splitlines() if ln.startswith("# repeat ")]
    assert len(starts) == 3
    assert "eta " in starts[1]  # no history for repeat 0, then extrapolated
    assert all(line.isascii() for line in proc.stderr.splitlines())


def test_standing_defaults_are_announced_per_step(tmp_path):
    """A plan restates no physics, so without this note the operator cannot see
    what a step will actually run. One row per step, on stderr like `scqo run`."""
    proc = _cli(tmp_path, "campaign", _plan(tmp_path), "--json")
    assert proc.returncode == 0, proc.stderr

    assert "# parameter defaults from" in proc.stderr
    row = next(ln for ln in proc.stderr.splitlines() if "qubit_relaxation" in ln
               and ln.startswith("#   "))
    assert "num_points" in row and "num_averages" in row
    assert "# parameter defaults" not in proc.stdout
    json.loads(proc.stdout)  # still exactly the manifest


def test_a_key_the_plan_supplies_is_not_listed_as_a_default(tmp_path):
    """Once the plan pins a key the lab file no longer governs it, so claiming it
    came from the file would be a lie."""
    pinned = PLAN.replace('experiment = "qubit_echo"',
                          'experiment = "qubit_echo"\nparams = { num_averages = 55 }')
    proc = _cli(tmp_path, "campaign", _plan(tmp_path, pinned), "--dry-run")
    assert proc.returncode == 0, proc.stderr
    # qubit_echo's only file default is now plan-supplied, so it drops off the note
    note = [ln for ln in proc.stdout.splitlines() if ln.startswith("#   ")]
    assert any("qubit_relaxation" in ln for ln in note)
    assert not any("qubit_echo" in ln for ln in note)
    assert '"num_averages": 55' in proc.stdout  # ...and the resolved dict shows the pin


def test_repeat_run_announces_defaults_once_not_twice(tmp_path):
    """`scqo run --repeat` goes through the campaign path, so only ONE note fires."""
    proc = _cli(tmp_path, "run", "qubit_relaxation", "--targets", "q0",
                "--repeat", "2", "--skip-artifacts")
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr.count("# parameter defaults from") == 1


def test_a_finished_campaign_writes_its_figure(tmp_path):
    """An overnight run ends at 4 a.m.; the figure must be waiting, not depend on
    someone remembering to ask for it."""
    proc = _cli(tmp_path, "campaign", _plan(tmp_path))
    assert proc.returncode == 0, proc.stderr

    png = next((tmp_path / "data" / "simdev" / "campaigns").glob("*/statistics.png"))
    assert png.stat().st_size > 1000
    # the path is ADVISORY, so stderr — stdout is the manifest under --json
    assert "# figure:" in proc.stderr and "figure:" not in proc.stdout


def test_a_broken_figure_never_fails_the_campaign(monkeypatch, capsys):
    """Losing the picture must never lose the measurement.

    In-process rather than a subprocess: the point is the guard inside
    plot_statistics, and `render_statistics` is imported at CALL time so patching
    the source module lands."""
    import scqo.cli._campaign_plot as plot_module
    from scqo.cli._campaign import plot_statistics

    def boom(*args, **kwargs):
        raise RuntimeError("no display, no disk, no luck")

    monkeypatch.setattr(plot_module, "render_statistics", boom)
    manifest = {"campaign_id": "cid", "statistics": {"x": {"q0": {}}}, "path": "p"}
    plot_statistics(object(), manifest)  # must not raise

    err = capsys.readouterr().err
    assert "statistics figure skipped" in err
    assert "campaign itself is unaffected" in err


def test_no_figure_is_attempted_when_there_is_nothing_to_draw(capsys):
    """No data_root (no campaign_id) or no successful fit -> silence, not a warning."""
    from scqo.cli._campaign import plot_statistics

    plot_statistics(None, {"campaign_id": "cid", "statistics": {"x": {}}})
    plot_statistics(object(), {"campaign_id": None, "statistics": {"x": {}}})
    plot_statistics(object(), {"campaign_id": "cid", "statistics": {}})
    assert capsys.readouterr().err == ""


def test_show_plot_re_renders_without_re_running(tmp_path):
    run = _cli(tmp_path, "campaign", _plan(tmp_path))
    assert run.returncode == 0, run.stderr
    campaign_id = _cli(tmp_path, "campaign", "--list").stdout.split()[0]
    png = next((tmp_path / "data" / "simdev" / "campaigns").glob("*/statistics.png"))
    before = png.stat().st_mtime_ns
    day_folders = sorted((tmp_path / "data" / "simdev").glob("2*"))

    proc = _cli(tmp_path, "campaign", "--show", campaign_id, "--plot")
    assert proc.returncode == 0, proc.stderr
    assert "# figure:" in proc.stderr
    assert png.stat().st_mtime_ns != before                      # re-rendered
    assert sorted((tmp_path / "data" / "simdev").glob("2*")) == day_folders  # nothing re-ran


def test_campaign_appears_in_the_command_list(tmp_path):
    proc = _cli(tmp_path, "--help")
    assert proc.returncode == 0
    assert "campaign" in proc.stdout


def test_dry_run_refuses_a_shadowing_label(tmp_path):
    """A label equal to a registered experiment name would mint campaign ids
    that read as that experiment's run ids — refused at preflight, exit 2."""
    bad = PLAN.replace('label = "t1_stability"', 'label = "qubit_relaxation"')
    proc = _cli(tmp_path, "campaign", _plan(tmp_path, bad), "--dry-run")
    assert proc.returncode == 2
    assert "REFUSED" in proc.stderr
    assert "shadows a registered experiment name" in proc.stderr


# ------------------------------------------------------ accept --campaign

PLAN3 = PLAN.replace("repeat = 2", "repeat = 3")  # min_n default 3: proposals appear


def _finished_campaign(tmp_path: Path) -> str:
    """Run the 3-repeat plan and return its campaign_id (scraped from --list)."""
    proc = _cli(tmp_path, "campaign", _plan(tmp_path, PLAN3))
    assert proc.returncode == 0, proc.stderr
    listed = _cli(tmp_path, "campaign", "--list")
    return listed.stdout.split()[0]


def test_accept_campaign_end_to_end(tmp_path):
    cid = _finished_campaign(tmp_path)

    listed = _cli(tmp_path, "accept", "--campaign", cid, "--list")
    assert listed.returncode == 0
    assert "pending" in listed.stdout and "experiment" in listed.stdout
    assert "qubit_relaxation" in listed.stdout and "t1_s" in listed.stdout

    # non-TTY apply-all, exactly like the run path
    applied = _cli(tmp_path, "accept", "--campaign", cid)
    assert applied.returncode == 0, applied.stderr
    summary = json.loads(applied.stdout)
    assert summary["campaign_id"] == cid and summary["pending_left"] == 0
    assert {a["field"] for a in summary["applied"]} >= {"t1_s", "t2_echo_s"}
    assert all(a["experiment"] for a in summary["applied"])
    assert "applied" in applied.stderr

    # a second apply finds nothing pending and stays exit 0
    again = _cli(tmp_path, "accept", "--campaign", cid)
    assert again.returncode == 0
    assert json.loads(again.stdout)["applied"] == []

    # --show renders the decided table; provenance names the campaign
    shown = _cli(tmp_path, "campaign", "--show", cid)
    assert "suggested updates (0 pending" in shown.stdout
    assert "accepted" in shown.stdout
    sources = _cli(tmp_path, "state", "--sources")  # covers both stores
    assert sources.returncode == 0, sources.stderr
    assert f"campaign {cid}" in sources.stdout


def test_accept_campaign_reject_and_filters(tmp_path):
    cid = _finished_campaign(tmp_path)

    rejected = _cli(tmp_path, "accept", "--campaign", cid, "--reject",
                    "--field", "t2_echo_s", "--comment", "chip was sick")
    assert rejected.returncode == 0
    assert [r["field"] for r in json.loads(rejected.stdout)["rejected"]] == ["t2_echo_s"]

    only_t1 = _cli(tmp_path, "accept", "--campaign", cid, "--field", "t1_s")
    assert only_t1.returncode == 0, only_t1.stderr
    summary = json.loads(only_t1.stdout)
    assert [a["field"] for a in summary["applied"]] == ["t1_s"]
    assert summary["pending_left"] == 1  # the thermalization knob still waits

    knob = _cli(tmp_path, "accept", "--campaign", cid, "--entity", "q0_xy")
    assert json.loads(knob.stdout)["pending_left"] == 0


def test_accept_refuses_run_id_and_campaign_together(tmp_path):
    proc = _cli(tmp_path, "accept", "some-run-id", "--campaign", "some-cid")
    assert proc.returncode == 2
    assert "not both" in proc.stderr


def test_bare_accept_lists_pending_campaigns(tmp_path):
    cid = _finished_campaign(tmp_path)
    proc = _cli(tmp_path, "accept")
    assert proc.returncode == 0
    assert "no runs with pending suggestions" in proc.stdout  # children carry none
    assert cid in proc.stdout
    assert "scqo accept --campaign <campaign_id>" in proc.stdout


def test_campaign_suggest_regenerates_for_an_old_campaign(tmp_path):
    cid = _finished_campaign(tmp_path)
    # simulate a pre-feature campaign: strip the stored suggestions entirely
    manifest_path = (tmp_path / "data" / "simdev" / "campaigns" / cid
                     / "campaign.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["suggestions"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    proc = _cli(tmp_path, "campaign", "--suggest", cid)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["suggestions"] > 0

    refused = _cli(tmp_path, "campaign", "--suggest", cid)
    assert refused.returncode != 0
    assert "force" in refused.stderr

    forced = _cli(tmp_path, "campaign", "--suggest", cid, "--force")
    assert forced.returncode == 0, forced.stderr
    shown = _cli(tmp_path, "campaign", "--show", cid)
    assert "suggested updates" in shown.stdout
    assert "scqo accept --campaign" in shown.stdout
