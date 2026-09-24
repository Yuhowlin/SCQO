"""Recreate a run's setup snapshot as a NEW named setup - re-run an old config for comparison.

    scqo restore 20260903-151504-chipA-qubit_ramsey-01 --setup replay_0903
    scqo restore <run_id> --setup <name> --force      # the run's cooldown is not the ACTIVE one

Every run stores the setup it executed against (the vendor config as the session held it
in memory, plus this context's scqo values) under <device>/setup_snapshots/<hash>/.
`restore` copies that snapshot into <device>/<active cycle>/<name>/ (backend_config/ +
scqo/) and appends a [<cycle>.setup.<name>] block to the device's cooldowns.toml naming
the run. The CURRENT setup is untouched: select the new one with `scqo user --setup <name>`
and re-run with the run's own parameters (`scqo run <experiment> --params <run
folder>/parameters.json`). A run from another cooldown is refused unless --force
(frequencies shift between cooldowns). The restored context starts with an empty change
history, so `scqo state --sources` shows its values as "(no record)". Touches NO
instrument.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from importlib.metadata import version
from pathlib import Path

from ._review import _confirm


def _version_warnings(manifest: dict) -> list[str]:
    """One line per distribution whose installed version differs from the one that
    wrote the snapshot (a QM state file names its QUAM classes by dotted path, so a
    different driver may not load it). Distributions not installed here are skipped."""
    out: list[str] = []
    for name, was in sorted((manifest.get("versions") or {}).items()):
        try:
            now = version(name)
        except Exception:  # not installed here (PackageNotFoundError) or unreadable
            continue
        if str(now) != str(was):
            out.append(f"WARNING: {name} was {was} when the snapshot was taken; "
                       f"this environment has {now}")
    return out


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_id", help="the run whose setup snapshot to restore (scqo find lists runs)")
    parser.add_argument("--setup", required=True, metavar="NAME",
                        help="name of the NEW setup to create in the device's ACTIVE cooldown cycle")
    parser.add_argument("--force", action="store_true",
                        help="restore even if the run was measured in another cooldown cycle")
    parser.add_argument("--yes", action="store_true",
                        help="restore without the confirmation prompt (the script form)")
    parser.add_argument("--config", help="lab config path (default: $SCQO_CONFIG or ~/.scqo/config.toml)")
    args = parser.parse_args(argv)

    from scqo import load_lab_config
    from scqo.datastore import (
        BACKEND_CONFIG_SUBDIR,
        COOLDOWNS_FILE,
        SCQO_SUBDIR,
        SLUG_RE,
        STATE_FILE,
        DataStore,
        active_cooldown,
        load_cooldowns,
        setup_backend_config_dir,
        setup_scqo_dir,
    )
    from scqo.stores import PHYSICAL_FILE

    try:
        cfg = load_lab_config(args.config)
    except (ValueError, FileNotFoundError) as err:  # a broken config must never traceback
        raise SystemExit(str(err)) from None
    if cfg.data_root is None or cfg.device is None:
        raise SystemExit("restore needs a selected device with a data_root "
                         "(scqo user --device <name>; data_root in the lab config)")
    device = cfg.device
    store = DataStore(cfg.data_root, device_name=device)
    try:
        loaded = store.load_run(args.run_id)
    except KeyError:
        raise SystemExit(f"unknown run_id {args.run_id!r} (scqo find lists them)") from None
    record = loaded["record"]
    if record.get("device") != device:
        raise SystemExit(f"run {args.run_id} belongs to device {record.get('device')!r} but "
                         f"your selection is {device!r} (scqo user --device ...)")
    snap = record.get("setup_snapshot") or {}
    if not snap.get("hash"):
        raise SystemExit(f"run {args.run_id} carries no setup snapshot (simulated backend, or "
                         "recorded before setup snapshots existed) - nothing to restore")
    try:
        found = store.load_setup_snapshot(device, snap["hash"])
    except KeyError as err:
        raise SystemExit(f"setup snapshot folder missing: {snap.get('path')} - not mirrored, "
                         f"or moved? ({err})") from None
    snap_dir, manifest = found["dir"], found["manifest"]

    try:
        cycles = load_cooldowns(cfg.data_root, device)
    except ValueError as err:
        raise SystemExit(str(err)) from None
    active = active_cooldown(cycles)
    if active is None:
        raise SystemExit(f"device {device!r} has no ACTIVE cooldown cycle - start one first "
                         "(scqo device cooldown start <cid> ...)")
    cid, cycle = active
    if record.get("cooldown") != cid and not args.force:
        raise SystemExit(f"run {args.run_id} was measured in cooldown {record.get('cooldown')!r}; "
                         f"the ACTIVE cycle is {cid!r} - frequencies shift between cooldowns. "
                         "Pass --force to restore it anyway")
    name = args.setup
    if not SLUG_RE.match(name):
        raise SystemExit(f"setup name {name!r} must be letters/digits/_/- only (it becomes a "
                         "TOML table header, a folder and a run stamp)")
    twin = next((s for s in cycle.get("setup", {}) if s.casefold() == name.casefold()), None)
    if twin is not None:
        raise SystemExit(f"setup {twin!r} already exists in cycle {cid!r} (names are immutable "
                         "for their cycle) - pick another name")
    target_cfg = setup_backend_config_dir(cfg.data_root, device, cid, name)
    target_scqo = setup_scqo_dir(cfg.data_root, device, cid, name)
    setup_dir = target_cfg.parent
    if setup_dir.exists():
        raise SystemExit(f"folder {setup_dir} already exists - remove it or pick another name")
    registry = Path(cfg.data_root) / device / COOLDOWNS_FILE
    backend = record.get("backend") or manifest.get("backend") or ""
    files = sorted(manifest.get("files") or {})

    # Context + plan on stderr (stdout stays parseable JSON). ASCII only.
    print(f"# device: {device}   active cycle: {cid}   source run: {args.run_id}", file=sys.stderr)
    print(f"will restore setup snapshot {snap['hash']} ({backend}) as setup {name!r}:", file=sys.stderr)
    for rel in files:
        print(f"  {rel}", file=sys.stderr)
    print(f"  -> {setup_dir}", file=sys.stderr)
    print(f"  + [{cid}.setup.{name}] appended to {registry}", file=sys.stderr)
    for line in _version_warnings(manifest):
        print(line, file=sys.stderr)
    if not args.yes:
        if not (sys.stdin.isatty() and sys.stderr.isatty()):
            print("not a terminal - nothing restored; re-run with --yes to apply", file=sys.stderr)
            return 1
        if not _confirm(f"restore this snapshot as setup {name!r}? [y/N]: "):
            print("nothing restored - the registry is unchanged", file=sys.stderr)
            return 0

    # The folders: backend_config/ verbatim; scqo/ = the two value files only (the
    # change history starts empty - the Store mints history.sqlite on first save).
    src_cfg = snap_dir / BACKEND_CONFIG_SUBDIR
    src_scqo = snap_dir / SCQO_SUBDIR
    try:
        if src_cfg.is_dir():
            shutil.copytree(src_cfg, target_cfg)
        else:
            target_cfg.mkdir(parents=True, exist_ok=True)
        target_scqo.mkdir(parents=True, exist_ok=True)
        for fname in (STATE_FILE, PHYSICAL_FILE):
            src = src_scqo / fname
            if src.is_file():
                shutil.copy2(src, target_scqo / fname)
    except OSError as err:
        shutil.rmtree(setup_dir, ignore_errors=True)
        raise SystemExit(f"copy failed - {setup_dir} removed again: {err}") from None

    # Append-only registry edit, then re-parse: never leave a broken registry (the
    # `scqo device cooldown start` discipline; json.dumps = a valid TOML basic string).
    original = registry.read_text(encoding="utf-8") if registry.is_file() else None
    note = f"restored from run {args.run_id} (setup snapshot {snap['hash']})"
    block = f"\n[{cid}.setup.{name}]\nbackend = {json.dumps(backend)}\nnote = {json.dumps(note)}\n"
    with open(registry, "a", encoding="utf-8") as f:
        f.write(block)
    try:
        load_cooldowns(cfg.data_root, device)
    except ValueError as err:
        if original is None:
            registry.unlink(missing_ok=True)
        else:
            registry.write_text(original, encoding="utf-8")
        shutil.rmtree(setup_dir, ignore_errors=True)
        raise SystemExit(f"restore produced an invalid registry - {registry} restored and "
                         f"{setup_dir} removed again: {err}") from None

    print(json.dumps({"run_id": args.run_id, "setup": name, "cooldown": cid, "backend": backend,
                      "snapshot": snap["hash"], "folder": str(setup_dir)}, indent=2))
    params = Path(loaded["path"]) / "parameters.json"
    print(f"next:\n  scqo user --setup {name}\n"
          f"  scqo run {record.get('experiment')} --params {params}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
