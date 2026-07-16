"""`scqo user` — show or set YOUR selection (device + setup), written to the user overlay.

The no-argument form is a pure diagnosis view (ALWAYS exit 0, even when a run would
refuse); --device/--setup validate against the registries and then line-edit the
per-user overlay file ($SCQO_USER_CONFIG, else ~/.scqo/user.toml), preserving
comments and unrelated keys, with .toml.bak + re-parse guarding every edit.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

#: a healthy single-setup cycle (auto-selects; simulated needs no vendor folder)
SINGLE_SETUP = (
    "[cd1]\nstart = 2026-07-01\nfridge = \"BF1\"\n\n"
    '[cd1.setup.sim_main]\nbackend = "simulated"\nnote = "demo"\n'
)
#: two setups in one cycle: a selection is mandatory
TWO_SETUPS = (
    "[cd1]\nstart = 2026-07-02\n\n"
    '[cd1.setup.alpha]\nbackend = "simulated"\n\n'
    '[cd1.setup.beta]\nbackend = "qblox"\n'
)


def _lab(tmp_path: Path, registries: dict[str, str]) -> Path:
    """A data_root with one cooldown registry per device + a lab config naming it."""
    data_root = tmp_path / "data"
    for device, body in registries.items():
        (data_root / device).mkdir(parents=True)
        (data_root / device / "cooldowns.toml").write_text(body, encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text(f"[lab]\ndata_root = '{data_root.as_posix()}'\n", encoding="utf-8")
    return config


def _overlay(tmp_path: Path, text: str = "") -> Path:
    user = tmp_path / "user.toml"
    user.write_text(text, encoding="utf-8")
    return user


def _scqo_user(tmp_path: Path, config: Path, *args: str, user_env: str | None,
               extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "SCQO_CONFIG": str(config)}
    env.pop("SCQO_USER_CONFIG", None)
    if user_env is not None:
        env["SCQO_USER_CONFIG"] = user_env
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "scqo.cli", "user", *args],
        capture_output=True, text=True, env=env, cwd=tmp_path,
    )


# ------------------------------------------------------------------ no-arg diagnosis
def test_show_healthy_single_setup_resolves_auto(tmp_path):
    config = _lab(tmp_path, {"chipA": SINGLE_SETUP})
    user = _overlay(tmp_path, 'device = "chipA"\n')
    proc = _scqo_user(tmp_path, config, user_env=str(user))
    assert proc.returncode == 0, proc.stderr
    assert "user overlay:" in proc.stdout
    assert 'device: chipA' in proc.stdout
    assert "resolves to:" in proc.stdout
    assert "'sim_main'" in proc.stdout  # the setup NAME
    assert "auto" in proc.stdout and "the only setup of cd1" in proc.stdout
    assert "backend simulated" in proc.stdout
    assert "runs would refuse:" not in proc.stdout


def test_show_setup_without_device_refuses_but_exits_zero(tmp_path):
    config = _lab(tmp_path, {"chipA": SINGLE_SETUP})
    user = _overlay(tmp_path, 'setup = "sim_main"\n')  # setup selected, no device anywhere
    proc = _scqo_user(tmp_path, config, user_env=str(user))
    assert proc.returncode == 0, proc.stderr  # diagnosis view NEVER fails
    assert "runs would refuse:" in proc.stdout
    assert "Select the device first" in proc.stdout


def test_show_zero_setups_refuses_with_skeleton(tmp_path):
    config = _lab(tmp_path, {"chipA": "[cd1]\nstart = 2026-07-01\n"})  # empty cycle: legal
    user = _overlay(tmp_path, 'device = "chipA"\n')
    proc = _scqo_user(tmp_path, config, user_env=str(user))
    assert proc.returncode == 0, proc.stderr
    assert "runs would refuse:" in proc.stdout
    assert "has no setups yet" in proc.stdout
    assert "[cd1.setup.<name>]" in proc.stdout  # paste-ready hand-edit skeleton


def test_show_ambiguous_refuses_naming_the_choices(tmp_path):
    config = _lab(tmp_path, {"chipB": TWO_SETUPS})
    user = _overlay(tmp_path, 'device = "chipB"\n')
    proc = _scqo_user(tmp_path, config, user_env=str(user))
    assert proc.returncode == 0, proc.stderr
    assert "runs would refuse:" in proc.stdout
    assert "setups and none is selected" in proc.stdout
    assert "alpha" in proc.stdout and "beta" in proc.stdout
    assert "scqo user --setup" in proc.stdout  # the exact fix command


def test_show_unknown_selected_setup_refuses(tmp_path):
    config = _lab(tmp_path, {"chipA": SINGLE_SETUP})
    user = _overlay(tmp_path, 'device = "chipA"\nsetup = "ghost"\n')
    proc = _scqo_user(tmp_path, config, user_env=str(user))
    assert proc.returncode == 0, proc.stderr
    assert "runs would refuse:" in proc.stdout
    assert "does not exist in the ACTIVE cycle" in proc.stdout
    assert "sim_main" in proc.stdout  # the available names
    assert "--clear-setup" in proc.stdout


# ------------------------------------------------------------------------- --device
def test_set_unknown_device_refuses_listing_known(tmp_path):
    config = _lab(tmp_path, {"chipA": SINGLE_SETUP})
    user = _overlay(tmp_path)
    proc = _scqo_user(tmp_path, config, "--device", "nosuch", user_env=str(user))
    assert proc.returncode != 0
    assert "unknown device" in proc.stderr and "'nosuch'" in proc.stderr
    assert "chipA" in proc.stderr  # the known devices are listed
    assert "scqo device add" in proc.stderr
    assert user.read_text(encoding="utf-8") == ""  # nothing written


def test_set_valid_device_writes_overlay(tmp_path):
    config = _lab(tmp_path, {"chipA": SINGLE_SETUP})
    user = _overlay(tmp_path)
    proc = _scqo_user(tmp_path, config, "--device", "chipA", user_env=str(user))
    assert proc.returncode == 0, proc.stderr
    assert "updated" in proc.stdout
    assert 'device = "chipA"' in user.read_text(encoding="utf-8")
    assert "resolves to:" in proc.stdout  # the diagnosis view follows the write


# -------------------------------------------------------------------------- --setup
def test_set_unknown_setup_refuses_listing_available(tmp_path):
    config = _lab(tmp_path, {"chipA": SINGLE_SETUP})
    user = _overlay(tmp_path, 'device = "chipA"\n')
    proc = _scqo_user(tmp_path, config, "--setup", "ghost", user_env=str(user))
    assert proc.returncode != 0
    assert "does not exist in the ACTIVE cycle" in proc.stderr
    assert "available: sim_main" in proc.stderr
    assert "setup =" not in user.read_text(encoding="utf-8")  # nothing written


def test_set_valid_setup_writes_overlay(tmp_path):
    config = _lab(tmp_path, {"chipB": TWO_SETUPS})
    user = _overlay(tmp_path, 'device = "chipB"\n')
    proc = _scqo_user(tmp_path, config, "--setup", "beta", user_env=str(user))
    assert proc.returncode == 0, proc.stderr
    content = user.read_text(encoding="utf-8")
    assert 'setup = "beta"' in content and 'device = "chipB"' in content
    assert "selected in user.toml" in proc.stdout  # resolves via the selection now


def test_combined_device_and_setup_validates_against_that_device(tmp_path):
    registries = {
        "chipA": SINGLE_SETUP,
        "chipB": '[cd1]\nstart = 2026-07-02\n\n'
                 '[cd1.setup.qm_top]\nbackend = "qm"\n',
    }
    config = _lab(tmp_path, registries)

    # Y is validated against X's active cycle, not the currently selected device's.
    user = _overlay(tmp_path, 'device = "chipA"\n')
    proc = _scqo_user(tmp_path, config, "--device", "chipB", "--setup", "qm_top",
                      user_env=str(user))
    assert proc.returncode == 0, proc.stderr
    content = user.read_text(encoding="utf-8")
    assert 'device = "chipB"' in content and 'setup = "qm_top"' in content

    # a setup of ANOTHER device's cycle is refused, and the file stays untouched
    user2 = _overlay(tmp_path, 'device = "chipA"\n')
    proc = _scqo_user(tmp_path, config, "--device", "chipB", "--setup", "sim_main",
                      user_env=str(user2))
    assert proc.returncode != 0
    assert "does not exist in the ACTIVE cycle" in proc.stderr
    assert "'chipB'" in proc.stderr
    assert user2.read_text(encoding="utf-8") == 'device = "chipA"\n'


# ------------------------------------------------------------- stale-setup auto-clear
def test_switching_device_auto_clears_stale_setup(tmp_path):
    registries = {
        "chipA": '[cd1]\nstart = 2026-07-01\n\n[cd1.setup.old_rig]\nbackend = "simulated"\n',
        "chipB": '[cd1]\nstart = 2026-07-02\n\n[cd1.setup.new_rig]\nbackend = "simulated"\n',
    }
    config = _lab(tmp_path, registries)
    user = _overlay(tmp_path, 'device = "chipA"\nsetup = "old_rig"\n')
    proc = _scqo_user(tmp_path, config, "--device", "chipB", user_env=str(user))
    assert proc.returncode == 0, proc.stderr
    assert "cleared setup" in proc.stderr and "'old_rig'" in proc.stderr  # stderr note
    content = user.read_text(encoding="utf-8")
    assert 'device = "chipB"' in content
    assert "setup =" not in content  # the stale line is gone
    assert "'new_rig'" in proc.stdout  # chipB's single setup auto-resolves again


# ------------------------------------------------------- overlay file lifecycle / env
def test_user_config_none_disables_setting(tmp_path):
    config = _lab(tmp_path, {"chipA": SINGLE_SETUP})
    proc = _scqo_user(tmp_path, config, "--device", "chipA", user_env="none")
    assert proc.returncode != 0
    assert "disabled" in proc.stderr

    # the diagnosis view still works (and says so) with the overlay disabled
    proc = _scqo_user(tmp_path, config, user_env="none")
    assert proc.returncode == 0, proc.stderr
    assert "disabled" in proc.stdout


def test_missing_default_overlay_is_created(tmp_path):
    config = _lab(tmp_path, {"chipA": SINGLE_SETUP})
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    proc = _scqo_user(tmp_path, config, "--device", "chipA", user_env=None,
                      extra_env={"USERPROFILE": str(fake_home), "HOME": str(fake_home)})
    assert proc.returncode == 0, proc.stderr
    created = fake_home / ".scqo" / "user.toml"
    assert created.is_file()
    assert 'device = "chipA"' in created.read_text(encoding="utf-8")
    assert not created.with_suffix(".toml.bak").exists()  # no backup for a fresh file


def test_missing_env_named_overlay_is_created(tmp_path):
    """$SCQO_USER_CONFIG names a not-yet-existing file: --device must CREATE it.

    A missing env-named overlay stays fatal for every OTHER command (a typo must
    not silently drop the overlay) — `scqo user` alone tolerates it on its WRITE
    path (user._cfg_for_write loads the config with the overlay disabled for that
    one call) and prints 'note: creating <target> (named by $SCQO_USER_CONFIG)'.
    """
    config = _lab(tmp_path, {"chipA": SINGLE_SETUP})
    target = tmp_path / "fresh-user.toml"
    proc = _scqo_user(tmp_path, config, "--device", "chipA", user_env=str(target))
    assert proc.returncode == 0, proc.stderr
    assert target.is_file()
    assert 'device = "chipA"' in target.read_text(encoding="utf-8")
    assert "creating" in proc.stderr  # the explicit-intent note


# --------------------------------------------------------------- edit-in-place rules
def test_edit_preserves_comments_and_unrelated_keys_with_backup(tmp_path):
    config = _lab(tmp_path, {"chipA": SINGLE_SETUP})
    original = ('# projA selections (keep me)\n'
                'default_tags = ["projX"]\n'
                'device = "chipA"\n')
    user = _overlay(tmp_path, original)
    proc = _scqo_user(tmp_path, config, "--setup", "sim_main", user_env=str(user))
    assert proc.returncode == 0, proc.stderr
    content = user.read_text(encoding="utf-8")
    assert "# projA selections (keep me)" in content  # comment survives
    assert 'default_tags = ["projX"]' in content  # unrelated key survives
    assert 'device = "chipA"' in content
    assert 'setup = "sim_main"' in content  # the new line appended
    backup = user.with_suffix(".toml.bak")
    assert backup.is_file()  # pre-existing file -> backup
    assert backup.read_text(encoding="utf-8") == original


def test_clear_setup_removes_only_that_line(tmp_path):
    config = _lab(tmp_path, {"chipA": SINGLE_SETUP})
    user = _overlay(tmp_path, '# note\ndevice = "chipA"\nsetup = "sim_main"\n')
    proc = _scqo_user(tmp_path, config, "--clear-setup", user_env=str(user))
    assert proc.returncode == 0, proc.stderr
    content = user.read_text(encoding="utf-8")
    assert "setup =" not in content
    assert 'device = "chipA"' in content and "# note" in content
    assert user.with_suffix(".toml.bak").is_file()


# ------------------------------------------------------------------- clear-device
def test_clear_device_clears_dangling_setup(tmp_path):
    """--clear-device with no [lab] default leaves NO device — a standing setup
    selection would refuse every run, so it is cleared in the same edit."""
    config = _lab(tmp_path, {"chipA": SINGLE_SETUP})
    user = _overlay(tmp_path, 'device = "chipA"\nsetup = "sim_main"\n')
    proc = _scqo_user(tmp_path, config, "--clear-device", user_env=str(user))
    assert proc.returncode == 0, proc.stderr
    text = user.read_text(encoding="utf-8")
    assert "device" not in text and "setup" not in text
    assert "cleared setup" in proc.stderr


def test_clear_device_validates_standing_setup_against_lab_default(tmp_path):
    """--clear-device falls back to the [lab] default device: a standing setup
    survives only if THAT device's ACTIVE cycle declares it, and --setup in the
    same command validates against the default device too (never the one being
    cleared)."""
    data_root = tmp_path / "data"
    for device, body in {"chipA": TWO_SETUPS, "chipB": SINGLE_SETUP}.items():
        (data_root / device).mkdir(parents=True)
        (data_root / device / "cooldowns.toml").write_text(body, encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text(
        f"[lab]\ndata_root = '{data_root.as_posix()}'\ndevice = \"chipB\"\n",
        encoding="utf-8",
    )

    # standing setup 'alpha' (a chipA setup) does not exist on chipB -> cleared
    user = _overlay(tmp_path, 'device = "chipA"\nsetup = "alpha"\n')
    proc = _scqo_user(tmp_path, config, "--clear-device", user_env=str(user))
    assert proc.returncode == 0, proc.stderr
    assert 'setup = "alpha"' not in user.read_text(encoding="utf-8")
    assert "cleared setup 'alpha'" in proc.stderr

    # --setup X in the same command: validated against chipB, not the cleared chipA
    user = _overlay(tmp_path, 'device = "chipA"\n')
    proc = _scqo_user(tmp_path, config, "--clear-device", "--setup", "alpha",
                      user_env=str(user))
    assert proc.returncode != 0
    assert "chipB" in proc.stderr  # refused against the DEFAULT device
    proc = _scqo_user(tmp_path, config, "--clear-device", "--setup", "sim_main",
                      user_env=str(user))
    assert proc.returncode == 0, proc.stderr
    text = user.read_text(encoding="utf-8")
    assert "device" not in text and 'setup = "sim_main"' in text
