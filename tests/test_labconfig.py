"""Lab config: resolution order, loud failure on a mistyped explicit config, parsing."""

from __future__ import annotations

import pytest

from scqo import labconfig


def test_defaults_when_no_config(monkeypatch, tmp_path):
    monkeypatch.delenv(labconfig.ENV_VAR, raising=False)
    monkeypatch.setattr(labconfig, "DEFAULT_PATH", tmp_path / "absent.toml")
    cfg = labconfig.load()
    assert cfg.backend == "simulated"
    assert cfg.data_root is None and cfg.state_path is None
    assert cfg.source is None  # built-in defaults, nothing loaded


def test_explicit_missing_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        labconfig.load(tmp_path / "nope.toml")


def test_env_var_missing_file_raises(monkeypatch, tmp_path):
    """A typo'd $SCQO_CONFIG must fail loudly, not silently run simulated + unsaved."""
    monkeypatch.setenv(labconfig.ENV_VAR, str(tmp_path / "gone.toml"))
    with pytest.raises(FileNotFoundError):
        labconfig.load()


def test_tilde_paths_are_expanded(tmp_path):
    """macOS/Linux configs say data_root = '~/qpu_data'; that must not create a
    literal './~' folder."""
    path = tmp_path / "config.toml"
    path.write_text(
        '[lab]\ndata_root = "~/qpu_data"\nstate_path = "~/qpu_data/scqo_state.json"\n',
        encoding="utf-8",
    )
    cfg = labconfig.load(path)
    assert "~" not in str(cfg.data_root)
    assert cfg.data_root.is_absolute()
    assert "~" not in str(cfg.state_path)


_TWO_SAMPLE_CONFIG = """
[lab]
data_root = "D:/qpu_data"
device_name = "fallback"
state_path = "D:/qpu_data/fallback/scqo_state.json"
backend = "%s"

[qblox]
config_dir = "./qblox_state"
device_name = "chipA"
state_path = "D:/qpu_data/chipA/scqo_state.json"

[qm]
device_name = "chipB"
"""


def test_backend_table_overrides_device(tmp_path):
    """Two instruments carrying two samples: the ACTIVE backend's vendor table names
    the mounted sample, so switching backend switches device (device = the sample)."""
    path = tmp_path / "config.toml"

    path.write_text(_TWO_SAMPLE_CONFIG % "qblox_sim", encoding="utf-8")
    cfg = labconfig.load(path)
    assert cfg.device_name == "chipA"  # qblox_sim reads the [qblox] table
    assert "chipA" in str(cfg.state_path)

    path.write_text(_TWO_SAMPLE_CONFIG % "qm", encoding="utf-8")
    cfg = labconfig.load(path)
    assert cfg.device_name == "chipB"
    assert "fallback" in str(cfg.state_path)  # [qm] has no state_path -> [lab] wins

    path.write_text(_TWO_SAMPLE_CONFIG % "simulated", encoding="utf-8")
    cfg = labconfig.load(path)
    assert cfg.device_name == "fallback"  # no vendor family -> [lab] values
    assert cfg.extras["qblox"]["config_dir"] == "./qblox_state"  # passthrough intact


def test_parse_full_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        """
[lab]
data_root = "D:/qpu_data"
device_name = "SQ4B_v3"
state_path = "D:/qpu_data/SQ4B_v3/scqo_state.json"
backend = "qblox"
state_sync = "push"
default_tags = ["cooldown7", "run-b"]

[qblox]
config_dir = "./qblox_state"
""",
        encoding="utf-8",
    )
    cfg = labconfig.load(path)
    assert cfg.device_name == "SQ4B_v3"
    assert cfg.backend == "qblox"
    assert cfg.state_sync == "push"
    assert cfg.default_tags == ["cooldown7", "run-b"]
    assert cfg.data_root is not None and cfg.state_path is not None
    assert cfg.extras["qblox"]["config_dir"] == "./qblox_state"
    assert cfg.source == path
