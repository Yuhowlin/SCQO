"""Session.preview + the --preview CLI flag guards — offline.

The render path is exercised with fake hardware-style backends (the simulated
backend refuses preview by design); the real vendor renderers are covered in
each driver repo's own test_preview.py.
"""

import warnings as warnings_mod

import pytest

from scqo import Session
from scqo.backend import PreviewWarning
from scqo.cli._engine import run_experiment_cli
from scqo.testing import (
    InMemoryDevice,
    SimulatedBackend,
    demo_components,
    demo_design,
    demo_vendor_state,
)


class RenderingBackend(SimulatedBackend):
    """Hardware-style preview: writes one file, records what the hook saw."""

    def __init__(self, device) -> None:
        super().__init__(device)
        self.seen_sweep_axes = None

    def preview(self, experiment, out_dir):
        self.seen_sweep_axes = dict(experiment.sweep_axes)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "sequence.txt"
        path.write_text("native vendor sequence stand-in", encoding="utf-8")
        return [path]


class WarningBackend(SimulatedBackend):
    def preview(self, experiment, out_dir):
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "main.txt"
        path.write_text("x", encoding="utf-8")
        warnings_mod.warn(PreviewWarning("circuit diagram skipped: TestReason"))
        return [path]


class BoomBackend(SimulatedBackend):
    def preview(self, experiment, out_dir):
        raise RuntimeError("kaput")


class RefusingBackend(SimulatedBackend):
    def preview(self, experiment, out_dir):
        raise ValueError("custom named refusal")


class OptionsBackend(SimulatedBackend):
    """Records the backend-specific options the Session forwarded."""

    def __init__(self, device) -> None:
        super().__init__(device)
        self.seen_options = None

    def preview(self, experiment, out_dir, **options):
        self.seen_options = options
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "sequence.txt"
        path.write_text("x", encoding="utf-8")
        return [path]


class NoHookBackend(SimulatedBackend):
    preview = None  # getattr(..., "preview", None) -> the missing-hook branch


@pytest.fixture()
def make_session(tmp_path):
    def _make(backend_cls):
        roster = demo_components()
        design = demo_design(roster)
        vendor = InMemoryDevice(roster, demo_vendor_state(roster, design))
        return Session(
            backend_cls(vendor), roster, design=design,
            scqo_dir=tmp_path / "scqo", data_root=tmp_path / "data",
            device_name="chipT", backend_label="fakehw",
            setup_name="sim", cooldown_id="cd1")
    return _make


def test_preview_renders_via_backend_hook(make_session, tmp_path):
    sess = make_session(RenderingBackend)
    out_dir = tmp_path / "prev"
    result = sess.preview("resonator_spectroscopy", {"targets": ["q0"]},
                          out_dir=out_dir)
    assert "error" not in result
    assert result["experiment"] == "resonator_spectroscopy"
    assert result["backend"] == "fakehw"
    assert result["preview_dir"] == str(out_dir)
    assert result["files"] == [str(out_dir / "sequence.txt")]
    assert result["warnings"] == []
    assert (out_dir / "sequence.txt").exists()
    # the Session set sweep_axes BEFORE the hook ran (probe() reads it)
    assert sess.backend.seen_sweep_axes  # non-empty dict
    # nothing persisted: preview never touches the datastore
    assert sess.find_runs() == []


def test_preview_missing_hook_refuses_by_name(make_session, tmp_path):
    sess = make_session(NoHookBackend)
    result = sess.preview("resonator_spectroscopy", {"targets": ["q0"]},
                          out_dir=tmp_path / "prev")
    assert "does not implement preview()" in result["error"]
    assert "fakehw" in result["error"]
    assert not (tmp_path / "prev").exists()


def test_preview_simulated_backend_refuses(make_session, tmp_path):
    sess = make_session(SimulatedBackend)
    result = sess.preview("resonator_spectroscopy", {"targets": ["q0"]},
                          out_dir=tmp_path / "prev")
    assert "nothing to render" in result["error"]
    assert "simulated backend cannot preview" in result["error"]
    assert not (tmp_path / "prev").exists()


def test_preview_invalid_params_reports_like_run(make_session, tmp_path):
    sess = make_session(RenderingBackend)
    result = sess.preview(
        "resonator_spectroscopy",
        {"targets": ["q0"], "num_points": "not_a_number"},
        out_dir=tmp_path / "prev")
    assert result.get("error")
    assert "num_points" in result["error"]
    assert not (tmp_path / "prev").exists()


def test_preview_target_gate_refuses(make_session, tmp_path):
    sess = make_session(RenderingBackend)
    result = sess.preview("resonator_spectroscopy", {"targets": ["q0_q1"]},
                          out_dir=tmp_path / "prev")
    assert result.get("error")
    assert not (tmp_path / "prev").exists()


def test_preview_hook_exception_is_reported(make_session, tmp_path):
    sess = make_session(BoomBackend)
    result = sess.preview("resonator_spectroscopy", {"targets": ["q0"]},
                          out_dir=tmp_path / "prev")
    assert result["error"] == "preview failed: RuntimeError: kaput"


def test_preview_hook_refusal_passes_through_verbatim(make_session, tmp_path):
    sess = make_session(RefusingBackend)
    result = sess.preview("resonator_spectroscopy", {"targets": ["q0"]},
                          out_dir=tmp_path / "prev")
    assert result["error"] == "custom named refusal"


def test_preview_forwards_backend_options(make_session, tmp_path):
    sess = make_session(OptionsBackend)
    result = sess.preview("resonator_spectroscopy", {"targets": ["q0"]},
                          out_dir=tmp_path / "prev",
                          options={"simulate_ns": 5000, "no_simulate": False})
    assert "error" not in result
    assert sess.backend.seen_options == {"simulate_ns": 5000,
                                         "no_simulate": False}
    # and the simulated backend's refusal survives options (no TypeError)
    sim = make_session(SimulatedBackend)
    refused = sim.preview("resonator_spectroscopy", {"targets": ["q0"]},
                          out_dir=tmp_path / "prev2",
                          options={"no_simulate": True})
    assert "nothing to render" in refused["error"]


def test_preview_collects_preview_warnings(make_session, tmp_path):
    sess = make_session(WarningBackend)
    result = sess.preview("resonator_spectroscopy", {"targets": ["q0"]},
                          out_dir=tmp_path / "prev")
    assert "error" not in result
    assert result["warnings"] == ["circuit diagram skipped: TestReason"]
    assert result["files"] == [str(tmp_path / "prev" / "main.txt")]


# --------------------------------------------------------------- CLI guards
# In-process: every one of these SystemExits fires BEFORE build_session, so no
# lab is needed — but the parser's schema epilog reads the lab config at
# construction, so pin it to a stub (the parameters.toml leak guard).

@pytest.fixture()
def stub_config(tmp_path, monkeypatch):
    params = tmp_path / "parameters.toml"
    params.write_text("", encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text(f"parameters_file = '{params.as_posix()}'\n",
                      encoding="utf-8")
    monkeypatch.setenv("SCQO_CONFIG", str(config))
    monkeypatch.setenv("SCQO_USER_CONFIG", "none")


@pytest.mark.parametrize("argv, fragment", [
    (["qubit_ramsey", "--preview", "--accept"], "--accept"),
    (["qubit_ramsey", "--preview", "--repeat", "3"], "--repeat"),
    (["qubit_ramsey", "--preview", "--tag", "x", "--note", "y"], "--tag"),
    (["qubit_ramsey", "--out", "somewhere"], "only apply with --preview"),
    (["qubit_ramsey", "--simulate-ns", "5000"], "only apply with --preview"),
    (["qubit_ramsey", "--preview", "--simulate-ns", "5000", "--no-simulate"],
     "contradict"),
    (["--preview"], "needs an experiment name"),
])
def test_cli_preview_flag_conflicts(stub_config, argv, fragment):
    with pytest.raises(SystemExit) as excinfo:
        run_experiment_cli(None, doc="", argv=argv)
    assert fragment in str(excinfo.value.code)
