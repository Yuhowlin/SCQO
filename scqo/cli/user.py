"""Show or set YOUR selection: which device (sample) and which of its setups.

    scqo user                          # where do MY runs land? (selection + resolution)
    scqo user --device chipA           # work on chipA (validated, written to user.toml)
    scqo user --setup qblox_main       # measure with this setup of the ACTIVE cycle
    scqo user --device chipA --setup qblox_main
    scqo user --clear-setup            # back to auto (single-setup cycles need no selection)
    scqo user --clear-device

The selection is PERSONAL: it lives in your per-user overlay (``~/.scqo/user.toml``,
or wherever ``$SCQO_USER_CONFIG`` points) — never in the shared lab config. Setups
are validated against the device's ACTIVE cooldown cycle: cycles are started/ended by
the manager (``scqo device cooldown``); you only pick WHICH declared setup you
measure with. The no-argument form is a pure diagnosis view (always exits 0): it
shows your selection, where it came from, and exactly what a run would resolve to —
or the precise refusal a run would print.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from scqo import DataStore, load_lab_config
from scqo.datastore import (
    active_cooldown,
    known_devices,
    load_cooldowns,
)
from scqo.labconfig import USER_DEFAULT_PATH, USER_ENV_VAR, _load_user_overlay

from ._backends import resolve_device_setup


def _overlay_target() -> Path | None:
    """The file a selection would be written to (None = overlay disabled via env)."""
    env = os.environ.get(USER_ENV_VAR)
    if env is not None:
        if env.strip().lower() in ("", "none"):
            return None
        return Path(env).expanduser()
    return USER_DEFAULT_PATH


# ---------------------------------------------------------------------------- show
def _show(config_path: str | None) -> int:
    target = _overlay_target()
    if target is None:
        print(f"user overlay: disabled (${USER_ENV_VAR}=none) - selections cannot be saved "
              "in this shell")
    else:
        exists = "" if target.is_file() else "  (not created yet)"
        print(f"user overlay: {target}{exists}")

    try:
        cfg = load_lab_config(config_path)
    except (ValueError, FileNotFoundError) as err:
        print(f"\nconfig error (fix it by hand first): {err}")
        return 0

    try:
        overlay, _ = _load_user_overlay()
    except (ValueError, FileNotFoundError):
        overlay = {}
    if cfg.device is None:
        print("device: (none - runs use the built-in simulated demo, nothing saved)")
    else:
        source = "user.toml" if overlay.get("device") else f"[lab] default ({cfg.source})"
        print(f"device: {cfg.device}   (from {source})")
    print(f"setup:  {cfg.setup or '(none - auto: a single-setup cycle selects itself)'}")

    try:
        resolved = resolve_device_setup(cfg)
    except SystemExit as err:
        print(f"\nruns would refuse:\n{err}")
        return 0
    if resolved is None:
        return 0
    cid, name, setup = resolved
    how = "selected in user.toml" if cfg.setup else f"auto - the only setup of {cid}"
    folder = setup.get("instrument_config", "(built-in)")
    print(f"\nresolves to: cycle {cid}, setup {name!r} ({how}), "
          f"backend {setup['backend']}, config {folder}")
    return 0


# ----------------------------------------------------------------------- validate
def _known_devices(cfg) -> set[str]:
    """Every device name this lab knows (registry + cooldown folders + index)."""
    names: set[str] = set()
    if cfg.device:
        names.add(cfg.device)
    if cfg.data_root is not None:
        root = Path(cfg.data_root)
        names |= known_devices(root)
        names |= set(DataStore(root, device_name=cfg.device or "device").distinct_devices())
    return names


def _validate_setup(cfg, device: str, setup: str) -> None:
    """The setup must exist in the ACTIVE cycle of ``device`` (else SystemExit)."""
    try:
        cycles = load_cooldowns(cfg.data_root, device)
    except ValueError as err:  # broken registry: refuse cleanly, not with a traceback
        raise SystemExit(str(err)) from None
    active = active_cooldown(cycles)
    if active is None:
        raise SystemExit(
            f"device {device!r} has no ACTIVE cooldown cycle - a setup selection needs one "
            "(manager: scqo device cooldown start ...)"
        )
    cid, cycle = active
    setups = cycle.get("setup", {})
    if setup not in setups:
        raise SystemExit(
            f"setup {setup!r} does not exist in the ACTIVE cycle {cid!r} of {device!r} - "
            f"available: {', '.join(setups) or 'none (hand-add a [' + cid + '.setup.<name>] block first)'}"
        )


# -------------------------------------------------------------------------- write
def _edit_overlay(target: Path, sets: dict[str, str], clears: list[str]) -> None:
    """Targeted line edit of the user overlay: replace/append ``key = "value"`` lines,
    delete cleared keys. Preserves every other line (comments, unrelated keys); a
    same-line comment on an edited key's line is replaced with it. Guarded by .bak +
    re-parse — the file is never left broken."""
    existed = target.is_file()
    if existed:
        try:
            _load_user_overlay()  # pre-validate: never line-edit an already-broken file
        except (ValueError, FileNotFoundError) as err:
            raise SystemExit(f"cannot edit {target}: {err} - fix it by hand first")
        lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = []

    for key, value in sets.items():
        replacement = f'{key} = "{value}"\n'
        for i, line in enumerate(lines):
            if line.split("=")[0].strip() == key and "=" in line:
                lines[i] = replacement
                break
        else:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(replacement)
    for key in clears:
        lines = [line for line in lines
                 if not ("=" in line and line.split("=")[0].strip() == key)]

    backup = None
    if existed:
        backup = target.with_suffix(".toml.bak")
        shutil.copy2(target, backup)
    target.write_text("".join(lines), encoding="utf-8")
    try:
        _load_user_overlay()  # re-parse: never leave a broken overlay behind
    except (ValueError, FileNotFoundError) as err:
        if backup is not None:
            shutil.copy2(backup, target)
            raise SystemExit(f"edit produced an invalid file - restored from {backup}: {err}")
        target.unlink(missing_ok=True)
        raise SystemExit(f"edit produced an invalid file - removed {target}: {err}")


def _load_without_overlay(config_path: str | None):
    """The lab config as it resolves WITHOUT the per-user overlay ([lab] facts only)."""
    prev = os.environ.get(USER_ENV_VAR)
    os.environ[USER_ENV_VAR] = "none"
    try:
        return load_lab_config(config_path)
    finally:
        if prev is None:
            del os.environ[USER_ENV_VAR]
        else:
            os.environ[USER_ENV_VAR] = prev


def _cfg_for_write(config_path: str | None, target: Path):
    """The lab config used to VALIDATE a selection before writing it.

    A missing overlay file is fatal everywhere else when ``$SCQO_USER_CONFIG``
    names it explicitly (a typo must not silently drop the overlay) — but here we
    are about to CREATE that very file, so an absent target simply means "no
    overlay yet": load the config with the overlay disabled for this one call.
    A malformed EXISTING overlay stays loud (never line-edit a broken file).
    """
    if target.is_file():
        return load_lab_config(config_path)
    return _load_without_overlay(config_path)


def _set(args) -> int:
    target = _overlay_target()
    if target is None:
        raise SystemExit(
            f"the per-user overlay is disabled in this shell (${USER_ENV_VAR}=none) - unset "
            "it (or point it at a file) before selecting a device/setup"
        )
    cfg = _cfg_for_write(args.config, target)  # loud on a malformed overlay/base config

    sets: dict[str, str] = {}
    clears: list[str] = []
    if args.clear_device:
        clears.append("device")
    if args.clear_setup:
        clears.append("setup")

    if args.device:
        if cfg.data_root is None:
            raise SystemExit(
                f"no data_root configured in {cfg.source or 'the lab config'} - device "
                "registries live under it; selecting a device needs one"
            )
        known = _known_devices(cfg)
        if args.device not in known:
            raise SystemExit(
                f"unknown device {args.device!r} - known: {', '.join(sorted(known)) or 'none'} "
                "(managers create one with: scqo device add <name>)"
            )
        sets["device"] = args.device

    # The device the overlay resolves to AFTER this edit — a setup must be validated
    # against THAT device, not the one being replaced or cleared.
    if args.device:
        effective_device = args.device
    elif args.clear_device:
        effective_device = _load_without_overlay(args.config).device  # [lab] default or None
    else:
        effective_device = cfg.device

    if args.setup:
        if effective_device is None:
            hint = ("clearing your device leaves no device to validate against - the lab "
                    "config has no default" if args.clear_device else
                    "select one first (or in the same command)")
            raise SystemExit(
                f"a setup belongs to a device - {hint}:\n"
                "  scqo user --device <name> --setup <name>"
            )
        if cfg.data_root is None:
            raise SystemExit(
                f"no data_root configured in {cfg.source or 'the lab config'} - device "
                "registries live under it; selecting a setup needs one"
            )
        _validate_setup(cfg, effective_device, args.setup)
        sets["setup"] = args.setup
    elif (args.device or args.clear_device) and not args.clear_setup:
        # The device changes and a standing setup selection may survive the edit:
        # keep it only if it exists in the EFFECTIVE device's active cycle — a stale
        # name would refuse every next run. A registry too broken to check keeps the
        # selection (runs refuse loudly on the registry itself, the honest reason).
        try:
            overlay, _ = _load_user_overlay()
        except (ValueError, FileNotFoundError):
            overlay = {}
        standing = overlay.get("setup")
        if standing:
            if effective_device is None or cfg.data_root is None:
                clears.append("setup")
                print(f"cleared setup {standing!r} (no device selected anymore)",
                      file=sys.stderr)
            else:
                try:
                    cycles = load_cooldowns(cfg.data_root, effective_device)
                    active = active_cooldown(cycles)
                    ok = active is not None and standing in active[1].get("setup", {})
                except ValueError as err:
                    print(f"warning: could not validate standing setup {standing!r} against "
                          f"{effective_device!r} (broken registry: {err}) - keeping it",
                          file=sys.stderr)
                    ok = True
                if not ok:
                    clears.append("setup")
                    print(f"cleared setup {standing!r} (not in {effective_device!r}'s active "
                          "cycle) - pick one:  scqo user --setup <name>", file=sys.stderr)

    if not target.is_file() and os.environ.get(USER_ENV_VAR):
        # An explicit env target is explicit intent — create it, but say so.
        print(f"note: creating {target} (named by ${USER_ENV_VAR})", file=sys.stderr)
    _edit_overlay(target, sets, clears)
    print(f"updated {target}\n")
    return _show(args.config)


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", metavar="NAME",
                        help="select the sample you work on (validated against the lab's devices)")
    parser.add_argument("--setup", metavar="NAME",
                        help="select a setup of the device's ACTIVE cooldown cycle")
    parser.add_argument("--clear-device", action="store_true",
                        help="remove the device selection (back to the [lab] default, if any)")
    parser.add_argument("--clear-setup", action="store_true",
                        help="remove the setup selection (single-setup cycles auto-select)")
    parser.add_argument("--config", help="lab config path (default: $SCQO_CONFIG or ~/.scqo/config.toml)")
    args = parser.parse_args(argv)
    if args.device and args.clear_device:
        parser.error("--device and --clear-device are mutually exclusive")
    if args.setup and args.clear_setup:
        parser.error("--setup and --clear-setup are mutually exclusive")

    if not (args.device or args.setup or args.clear_device or args.clear_setup):
        return _show(args.config)
    return _set(args)


if __name__ == "__main__":
    sys.exit(main())
