"""Backend resolution + demo-experiment registration + the dispatcher (in-process)."""

from __future__ import annotations

import pytest

from scqo import labconfig, registry
from scqo.cli import _backends
from scqo.cli.__main__ import _COMMANDS, main as cli_main


def _cfg(tmp_path, backend: str) -> labconfig.LabConfig:
    path = tmp_path / "config.toml"
    path.write_text(f'[lab]\nbackend = "{backend}"\n', encoding="utf-8")
    return path


def _driver_installed(family: str) -> bool:
    from importlib.metadata import entry_points

    return any(ep.name == family for ep in entry_points(group="scqo.backends"))


@pytest.mark.skipif(_driver_installed("qblox"), reason="qblox driver installed in this env")
def test_missing_driver_names_repo_and_venv(tmp_path, monkeypatch):
    """A wrong-venv attempt fails loudly and says exactly what to activate."""
    monkeypatch.delenv(labconfig.USER_ENV_VAR, raising=False)
    with pytest.raises(SystemExit) as err:
        _backends.build_session(str(_cfg(tmp_path, "qblox")))
    # (no driver installed in the test env — the message must name the fix)
    assert "LCHQBDriver" in str(err.value)
    assert ".venv-qblox" in str(err.value)


def test_unknown_backend_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv(labconfig.USER_ENV_VAR, raising=False)
    with pytest.raises(SystemExit) as err:
        _backends.build_session(str(_cfg(tmp_path, "opx-nine-thousand")))
    assert "opx-nine-thousand" in str(err.value)


def test_ensure_demo_experiments_is_idempotent_and_never_shadows():
    _backends.ensure_demo_experiments()
    first = {e["name"]: e for e in registry.catalog()}
    assert "resonator_spectroscopy" in first

    # a pre-registered class (a "driver" registration) must survive a second ensure
    sentinel = registry.get("resonator_spectroscopy")
    _backends.ensure_demo_experiments()
    assert registry.get("resonator_spectroscopy") is sentinel
    assert {e["name"] for e in registry.catalog()} == set(first)


def test_dispatcher_usage_lists_every_subcommand(capsys):
    assert cli_main(["--help"]) == 0
    out = capsys.readouterr().out
    for name in _COMMANDS:
        assert name in out


def test_dispatcher_rejects_unknown_command(capsys):
    assert cli_main(["frobnicate"]) == 2
    assert "unknown command" in capsys.readouterr().err
