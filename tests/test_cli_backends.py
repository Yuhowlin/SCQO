"""The device -> cycle -> named setup -> backend resolution chain (in-process)."""

from __future__ import annotations

import pytest

from scqo import labconfig, registry
from scqo.cli import _backends
from scqo.cli.__main__ import _COMMANDS, main as cli_main


def _config(tmp_path, body: str) -> str:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return str(path)


def _lab(tmp_path, device: str | None = "chipT") -> str:
    data_root = tmp_path / "data"
    data_root.mkdir(exist_ok=True)
    device_line = f'device = "{device}"\n' if device else ""
    return _config(tmp_path, f"[lab]\n{device_line}data_root = '{data_root.as_posix()}'\n")


def _registry(tmp_path, text: str) -> None:
    ddir = tmp_path / "data" / "chipT"
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "cooldowns.toml").write_text(text, encoding="utf-8")


def _user_overlay(tmp_path, monkeypatch, body: str) -> str:
    """Point $SCQO_USER_CONFIG at a per-test overlay (device/setup selection)."""
    path = tmp_path / "user.toml"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setenv(labconfig.USER_ENV_VAR, str(path))
    return str(path)


def test_no_device_falls_back_to_demo(tmp_path):
    """No device selected anywhere = the built-in simulated demo, nothing saved."""
    sess, cfg = _backends.build_session(_lab(tmp_path, device=None))
    assert cfg.device is None
    assert sess.backend_label == "simulated"
    assert sess.setup_name == ""  # demo fallback has no setup era
    assert sess.datastore is None  # demo fallback persists nothing
    assert "q0" in sess.device_state()


def test_device_without_registry_names_the_fix(tmp_path):
    with pytest.raises(SystemExit) as err:
        _backends.build_session(_lab(tmp_path))
    assert "no cooldown registry" in str(err.value)
    assert "scqo device cooldown start" in str(err.value)
    assert "hand-adds" in str(err.value)  # setups are added by hand, not by a flag


def test_device_without_active_cycle_refuses(tmp_path):
    _registry(tmp_path, '[cd1]\nstart = 2026-01-01\nend = 2026-02-01\n'
                        '[cd1.setup.sim_bench]\nbackend = "simulated"\n')
    with pytest.raises(SystemExit) as err:
        _backends.build_session(_lab(tmp_path))
    assert "no ACTIVE cooldown cycle" in str(err.value)
    assert "scqo device cooldown start" in str(err.value)


def test_zero_setups_refuses_with_skeleton(tmp_path):
    """An ACTIVE cycle with no setups loads fine but refuses runs, printing the block."""
    _registry(tmp_path, '[cd1]\nstart = 2026-01-01\n')
    with pytest.raises(SystemExit) as err:
        _backends.build_session(_lab(tmp_path))
    assert "has no setups yet" in str(err.value)
    assert "[cd1.setup.<name>]" in str(err.value)  # paste-ready skeleton


def test_ambiguous_setups_name_the_choices(tmp_path):
    """Several setups and no selection = refuse, listing every name + the fix."""
    _registry(tmp_path, '[cd1]\nstart = 2026-01-01\n'
                        '[cd1.setup.alpha]\nbackend = "simulated"\n'
                        '[cd1.setup.beta]\nbackend = "simulated"\n')
    with pytest.raises(SystemExit) as err:
        _backends.build_session(_lab(tmp_path))
    assert "setups and none is selected" in str(err.value)
    assert "alpha" in str(err.value)
    assert "beta" in str(err.value)
    assert "scqo user --setup" in str(err.value)


def test_unknown_selected_setup_refuses(tmp_path, monkeypatch):
    """A stale/mistyped user.toml setup selection fails loudly with the fix."""
    _registry(tmp_path, '[cd1]\nstart = 2026-01-01\n'
                        '[cd1.setup.alpha]\nbackend = "simulated"\n')
    _user_overlay(tmp_path, monkeypatch, 'device = "chipT"\nsetup = "ghost"\n')
    with pytest.raises(SystemExit) as err:
        _backends.build_session(_lab(tmp_path, device=None))
    assert "'ghost'" in str(err.value)
    assert "does not exist in the ACTIVE cycle" in str(err.value)
    assert "alpha" in str(err.value)  # the available names
    assert "--clear-setup" in str(err.value)


def test_setup_selected_without_device_refuses(tmp_path, monkeypatch):
    """A setup belongs to a device's cycle — selecting one device-less is an error."""
    _user_overlay(tmp_path, monkeypatch, 'setup = "alpha"\n')
    with pytest.raises(SystemExit) as err:
        _backends.build_session(_lab(tmp_path, device=None))
    assert "Select the device first" in str(err.value)
    assert "scqo user --device" in str(err.value)


def test_simulated_setup_builds_and_persists(tmp_path):
    """A device on a simulated setup saves runs under <data_root>/<device>/."""
    _registry(tmp_path, '[cd1]\nstart = 2026-01-01\n'
                        '[cd1.setup.sim_bench]\nbackend = "simulated"\n')
    sess, cfg = _backends.build_session(_lab(tmp_path))
    assert cfg.device == "chipT"
    assert sess.backend_label == "simulated"
    assert sess.setup_name == "sim_bench"  # the resolved setup NAME is era provenance
    assert sess.datastore is not None  # device + data_root = persisted
    # per-(cooldown, setup) scqo/ folder convention
    assert sess.state_path.replace("\\", "/").endswith("chipT/cd1/sim_bench/scqo/scqo_state.json")


def _driver_installed(family: str) -> bool:
    from importlib.metadata import entry_points

    return any(ep.name == family for ep in entry_points(group="scqo.backends"))


@pytest.mark.skipif(_driver_installed("qblox"), reason="qblox driver installed in this env")
def test_missing_driver_names_repo_and_venv(tmp_path):
    """A wrong-venv attempt fails loudly and says exactly what to activate/install."""
    _registry(tmp_path, '[cd1]\nstart = 2026-01-01\n'
                        '[cd1.setup.qblox_main]\nbackend = "qblox"\n')
    with pytest.raises(SystemExit) as err:
        _backends.build_session(_lab(tmp_path))
    assert "'qblox_main'" in str(err.value)  # which setup demanded the driver
    assert "LCHQBDriver" in str(err.value)
    assert ".venv-qblox" in str(err.value)
    assert "uv pip install -e" in str(err.value)


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
    for name in ("state", "user", "device"):  # the v0.7.0 command set
        assert name in _COMMANDS
    for name in ("devices", "sample", "cooldown", "calibrate"):  # retired, no aliases
        assert name not in _COMMANDS
    assert cli_main(["--help"]) == 0
    out = capsys.readouterr().out
    for name in _COMMANDS:
        assert name in out


def test_dispatcher_rejects_unknown_command(capsys):
    assert cli_main(["frobnicate"]) == 2
    assert "unknown command" in capsys.readouterr().err
