"""Tests for the `scqo close-qm` / `scqo close_qm` CLI command and Session.close_qm."""

from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace
from scqo import Session
from scqo.testing import demo_components


def test_cli_help_includes_close_qm():
    proc = subprocess.run(
        [sys.executable, "-m", "scqo.cli", "--help"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "close-qm" in proc.stdout


def test_cli_close_qm_subcommand_help():
    for cmd in ("close-qm", "close_qm"):
        proc = subprocess.run(
            [sys.executable, "-m", "scqo.cli", cmd, "--help"],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0
        assert "--qm-id" in proc.stdout


def test_cli_close_qm_simulated_demo(tmp_path):
    env = {**os.environ, "SCQO_USER_CONFIG": "none"}
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[lab]\n", encoding="utf-8")
    env["SCQO_CONFIG"] = str(cfg_path)
    proc = subprocess.run(
        [sys.executable, "-m", "scqo.cli", "close-qm"],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    assert proc.returncode == 0
    assert "backend 'simulated' does not have hardware QM sessions to close" in proc.stdout


def test_session_close_qm_delegates_to_backend():
    called_with = {}

    class MockBackend:
        def __init__(self):
            self.device = SimpleNamespace(snapshot=lambda: {})

        def close_qm(self, **options):
            called_with.update(options)
            return {
                "success": True,
                "backend": "qm",
                "open_qms": ["QM-1"],
                "halted_jobs": ["job-100"],
                "closed_qms": ["QM-1"],
                "errors": [],
            }

    roster = demo_components()
    sess = Session(MockBackend(), roster, backend_label="qm")
    res = sess.close_qm(qm_id="QM-1")

    assert called_with == {"qm_id": "QM-1"}
    assert res["success"] is True
    assert res["open_qms"] == ["QM-1"]
    assert res["halted_jobs"] == ["job-100"]
    assert res["closed_qms"] == ["QM-1"]

