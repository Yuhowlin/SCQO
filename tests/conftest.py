"""Suite-wide test setup."""

import os

import pytest

# Headless, deterministic figure generation. Without this, matplotlib may pick the
# interactive TkAgg backend on Windows, and Tk initialization intermittently fails
# mid-suite (TclError: "Can't find a usable tk.tcl") — the artifact fallback in
# scqo/_scqat.py then drops the figure PNGs and layout tests flake.
os.environ.setdefault("MPLBACKEND", "Agg")


@pytest.fixture(autouse=True)
def _isolate_personal_scqo_files(monkeypatch, tmp_path):
    """No test may read the runner's real ~/.scqo files (config/parameters/user.toml).

    Found on the lab server: any account with a personal user.toml turned
    test_cli_backends red — every IN-PROCESS labconfig.load() is affected, not just
    test_labconfig (which additionally re-points these paths per test). Subprocess
    tests are already hermetic via SCQO_CONFIG / SCQO_USER_CONFIG in their env dicts.
    """
    from scqo import labconfig

    monkeypatch.setattr(labconfig, "DEFAULT_PATH", tmp_path / "no-config.toml")
    monkeypatch.setattr(labconfig, "PARAMS_DEFAULT_PATH", tmp_path / "no-parameters.toml")
    monkeypatch.setattr(labconfig, "USER_DEFAULT_PATH", tmp_path / "no-user.toml")
    monkeypatch.delenv(labconfig.ENV_VAR, raising=False)
    monkeypatch.delenv(labconfig.USER_ENV_VAR, raising=False)
