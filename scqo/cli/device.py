"""Device administration (manager) — samples, cooldown cycles, setups.

    scqo device                            # = list: every known device + its setups
    scqo device list
    scqo device add chipC --description "3-qubit test chip"
    scqo device cooldown                   # validate + list cycles + the ACTIVE cycle's setups
    scqo device cooldown start cd9 --fridge BlueforsA --packaging "PCB v3"
    scqo device cooldown end               # close the open cycle (today's date)

What is manual vs automatic when a sample arrives:

  MANUAL (manager):
    1. `scqo device add <name>` — creates the data folder, prints every next step.
    2. `scqo device cooldown start cd1 ...` — records the insertion (an EMPTY cycle).
    3. Hand-add one `[cd1.setup.<name>]` block per measurement setup to the device's
       cooldowns.toml (backend + optional note — no path keys; for real backends
       create the DERIVED folder <device>/cd1/<name>/backend_config/ and copy the
       vendor config files in under canonical names — qblox: dut_config.json +
       hw_config.json; qm: state.json + wiring.json).
    4. (optional) devices.toml: the sample's facts (description, design values).

  AUTOMATIC (no action needed):
    - <data_root>/<name>/ folders, run folders, each context's
      <cooldown>/<setup>/scqo/ folder (scqo_state.json + physical.json), the index
      row, viewer pages — all created on first use.
    - NO shared-config edit: users select the sample and setup with
      `scqo user --device <name> [--setup <name>]`.

Touches NO instrument: everything here only reads/edits the registries, so it is
safe from any account at any time. Manager-run by convention.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import date
from pathlib import Path

from scqo import DataStore, load_lab_config
from scqo.datastore import (
    COOLDOWNS_FILE,
    _SETUP_NAME_RE,
    active_cooldown,
    load_cooldowns,
    load_device_registry,
)

from ._backends import SERVED_BY

_VERBS = ("list", "add", "cooldown")

_START_TEMPLATE = """
[{cid}]
start = {today}
fridge = {fridge}
packaging = {packaging}
note = {note}

# Setups are hand-added, one NAMED sub-table per measurement setup of this cycle:
#   [{cid}.setup.<name>]      # the name IS the setup's identity
#   backend = "qblox"         # qblox | qm | simulated
#   note = "..."              # optional
# No path keys: the vendor-config folder is DERIVED from the keys —
#   <device>/{cid}/<name>/backend_config/  (create it and copy the vendor files in
#   under canonical names — qblox: dut_config.json + hw_config.json; qm: state.json +
#   wiring.json; simulated needs no folder). SCQO writes its own state + physics to
#   the sibling <device>/{cid}/<name>/scqo/ (auto-created).
# Users pick one with: scqo user --setup <name>
"""


def _device(cfg) -> str:
    if cfg.device is None:
        raise SystemExit('no device selected — set yours first:  scqo user --device <name>   '
                         "(`scqo device list` shows the menu)")
    if cfg.data_root is None:
        raise SystemExit("no data_root configured — cooldown registries live under <data_root>/<device>/")
    return cfg.device


def _registry_path(cfg) -> Path:
    return Path(cfg.data_root) / _device(cfg) / COOLDOWNS_FILE


# --------------------------------------------------------------------------- list
def _device_rows(data_root, device: str) -> list[tuple[str, str, str, str]]:
    """(cycle text, setup name, backend, config folder) — one row per setup of the
    ACTIVE cycle; a single degraded row when there is no cycle/setup/registry."""
    try:
        cycles = load_cooldowns(data_root, device)
    except ValueError as err:
        return [(f"REGISTRY ERROR: {err}", "-", "-", "-")]
    active = active_cooldown(cycles)
    if active is None:
        ended = len(cycles)
        return [((f"none ACTIVE ({ended} past)" if ended else "no cycles yet"), "-", "-", "-")]
    cid, cycle = active
    cycle_text = cid + (f" [{cycle['packaging']}]" if cycle.get("packaging") else "")
    setups = cycle.get("setup", {})
    if not setups:
        return [(cycle_text, "(no setups yet)", "-", "-")]
    return [(cycle_text, name, setup["backend"], setup.get("instrument_config", "built-in"))
            for name, setup in setups.items()]


def _list(cfg) -> int:
    print(f"# lab config: {cfg.source or 'built-in defaults (simulated, nothing saved)'}")
    print(f"# user overlay: {cfg.user_source or 'none'}")

    if cfg.data_root is None:
        print("\nno data_root configured — no device registries to list; runs use the "
              "built-in simulated demo and are NOT saved (see INSTALL §2)")
        return 0

    root = Path(cfg.data_root)
    known: set[str] = set(load_device_registry(root))
    if root.is_dir():
        known |= {p.parent.name for p in root.glob(f"*/{COOLDOWNS_FILE}")}
        known |= {p.name for p in root.iterdir() if p.is_dir()}
    if cfg.device:
        known.add(cfg.device)

    if not known:
        print("\nno devices known yet — create one:  scqo device add <name>")
        return 0

    print(f"\n{'device':12s} {'cooldown':22s} {'setup':16s} {'backend':11s} "
          f"{'instrument config':36s} run from")
    for device in sorted(known):
        rows = _device_rows(root, device)
        # Which row is "mine"? cfg.setup when set; else the row a run would auto-select
        # (the only setup). A device with no setup rows gets the marker on its one row.
        auto = rows[0][1] if len(rows) == 1 else None
        for i, (cycle_text, name, backend, folder) in enumerate(rows):
            provider, venv = SERVED_BY.get(backend, ("-", "-"))
            served = f"{provider} ({venv})" if provider != "-" else "-"
            selected = device == cfg.device and (cfg.setup == name if cfg.setup else name == auto)
            marker = "  <- selected" if selected else ""
            head = device if i == 0 else ""
            cyc = cycle_text if i == 0 else ""
            print(f"{head:12s} {cyc:22s} {name:16s} {backend:11s} {str(folder):36s} {served}{marker}")

    print("\nselect yours:  scqo user --device <name> [--setup <name>]   "
          "(written to ~/.scqo/user.toml)")
    print("(a single-setup cycle auto-selects; tags/parameters_file can live in user.toml too)")
    return 0


# ---------------------------------------------------------------------------- add
def _existing_names(cfg) -> set[str]:
    """Every device name this lab already knows: registry + index (+ the default)."""
    names: set[str] = set()
    if cfg.device:
        names.add(cfg.device)
    if cfg.data_root is not None:
        names |= set(load_device_registry(cfg.data_root))
        names |= set(DataStore(cfg.data_root, device_name=cfg.device or "device").distinct_devices())
    return names


def _add(cfg, name: str, description: str) -> int:
    if cfg.data_root is None:
        raise SystemExit(
            f"no data_root configured in {cfg.source or 'the lab config'} — "
            "a sample needs somewhere for its data to land"
        )
    device_dir = Path(cfg.data_root) / name
    if device_dir.exists() or name in _existing_names(cfg):
        print(f"note: {name!r} is already known to this lab (folder/registry/index) — "
              "steps below anyway, skip what exists\n", file=sys.stderr)
    device_dir.mkdir(parents=True, exist_ok=True)  # the command's ONLY write

    print(f"created {device_dir}\n")
    print("=" * 72)
    print("1. Record the first cooldown cycle (manager) — an EMPTY cycle, no setups yet:\n")
    print("   scqo device cooldown start cd1 --fridge <name> --packaging <text>")
    print("\n" + "=" * 72)
    print(f"2. PASTE one block per measurement setup into "
          f"{device_dir / COOLDOWNS_FILE}:\n")
    print("[cd1.setup.qblox_main]")
    print('backend = "qblox"                # qblox | qm | simulated')
    print("\n   (no path keys — the vendor folder is DERIVED from the keys: for real")
    print("    backends create <device>/cd1/qblox_main/backend_config/ and copy the vendor")
    print("    config files in under canonical names: qblox dut_config.json +")
    print("    hw_config.json; qm state.json + wiring.json. SCQO keeps its own state +")
    print("    physics in the sibling cd1/qblox_main/scqo/ — auto-created.)")
    print("\n" + "=" * 72)
    print(f"3. PASTE into {Path(cfg.data_root) / 'devices.toml'} (optional sample facts):\n")
    print(f"[{name}]")
    print(f'description = "{description or "..."}"')
    print("\n" + "=" * 72)
    print("then:")
    print(f"  users select it with:  scqo user --device {name}")
    print("  scqo device list                    # verify the new menu row")
    print("  scqo run resonator_spectroscopy     # first stamped run")
    return 0


# ----------------------------------------------------------------------- cooldown
def _show(cfg) -> int:
    device = _device(cfg)
    cycles = load_cooldowns(cfg.data_root, device)  # raises loudly on a broken file
    if not cycles:
        print(f"no cooldown cycles declared for {device} ({_registry_path(cfg)})")
        return 0
    active = active_cooldown(cycles)
    for cid, cycle in cycles.items():
        marker = "ACTIVE" if active and cid == active[0] else f"ended {cycle.get('end', '?')}"
        extras = "  ".join(f"{k}={cycle[k]}" for k in ("fridge", "packaging") if cycle.get(k))
        print(f"{cid:12s} start={cycle.get('start', '?')}  {marker:18s} "
              f"setups={len(cycle.get('setup', {}))}  {extras}")
    if active:
        cid, cycle = active
        setups = cycle.get("setup", {})
        if not setups:
            print(f"\n{cid} has no setups yet — hand-add [{cid}.setup.<name>] blocks "
                  "(backend [+ note]) to the registry; runs refuse until one exists")
            return 0
        print(f"\nsetups of {cid} (users pick one with `scqo user --setup <name>`):")
        for name, setup in setups.items():
            folder = setup.get("instrument_config", "(built-in)")
            note = f"  note={setup['note']}" if setup.get("note") else ""
            print(f"  {name:16s} backend={setup['backend']:11s} config={folder}{note}")
    return 0


def _start(cfg, cid: str, fridge: str, packaging: str, note: str) -> int:
    device = _device(cfg)
    path = _registry_path(cfg)
    if not _SETUP_NAME_RE.match(cid):
        raise SystemExit(f"cycle id {cid!r} must be letters/digits/_/- only "
                         "(it becomes a TOML table header, a run stamp and a CLI argument)")
    cycles = load_cooldowns(cfg.data_root, device)
    active = active_cooldown(cycles)
    if active:
        raise SystemExit(f"cycle {active[0]!r} is still open — run `scqo device cooldown end` first")
    twin = next((c for c in cycles if c.casefold() == cid.casefold()), None)
    if twin is not None:  # casefold: [cd2] and [CD2] would alias one folder tree on Windows
        raise SystemExit(f"cycle {twin!r} already exists in {path}"
                         + ("" if twin == cid else f" (differs from {cid!r} only by letter case)"))
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_text(encoding="utf-8") if path.is_file() else None
    # Append-only: hand-written content and comments above stay untouched.
    # json.dumps = a valid TOML basic string (quotes/backslashes/newlines escaped) —
    # a Windows path or a quote in --note must never corrupt the shared registry.
    with open(path, "a", encoding="utf-8") as f:
        f.write(_START_TEMPLATE.format(cid=cid, today=date.today().isoformat(),
                                       fridge=json.dumps(fridge),
                                       packaging=json.dumps(packaging),
                                       note=json.dumps(note)))
    try:
        load_cooldowns(cfg.data_root, device)  # re-parse: never leave a broken registry
    except ValueError as err:
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(original, encoding="utf-8")
        raise SystemExit(f"start produced an invalid registry — {path} restored: {err}")
    print(f"started {cid} in {path} — hand-add its setups as [{cid}.setup.<name>] blocks; "
          "runs refuse until one exists")
    return 0


def _end(cfg) -> int:
    device = _device(cfg)
    path = _registry_path(cfg)
    cycles = load_cooldowns(cfg.data_root, device)
    active = active_cooldown(cycles)
    if active is None:
        raise SystemExit(f"no open cycle in {path}")
    cid = active[0]
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    # Targeted line edit (comments preserved): insert `end = <today>` right after the
    # open cycle's `start = ...` line inside its [cid] block (which always precedes
    # its [cid.setup.<name>] blocks).
    header = re.compile(rf"^\[\s*{re.escape(cid)}\s*\]\s*(#.*)?$")
    in_block, inserted = False, False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if header.match(stripped):
            in_block = True
        elif in_block and stripped.startswith("start"):
            lines.insert(i + 1, f"end = {date.today().isoformat()}\n")
            inserted = True
            break
    if not inserted:
        raise SystemExit(f"could not locate the start line of [{cid}] in {path} — add `end = ...` by hand")
    backup = path.with_suffix(".toml.bak")
    shutil.copy2(path, backup)
    path.write_text("".join(lines), encoding="utf-8")
    try:
        load_cooldowns(cfg.data_root, device)  # re-parse: never leave a broken registry
    except ValueError as err:
        shutil.copy2(backup, path)
        raise SystemExit(f"edit produced an invalid file — restored from {backup}: {err}")
    print(f"ended {cid} ({date.today().isoformat()}) in {path} — runs refuse until the next "
          f"`scqo device cooldown start`")
    return 0


def _cooldown(cfg, args) -> int:
    if args.command == "start":
        if not args.cycle:
            raise SystemExit("start needs a cycle id, e.g.: scqo device cooldown start cd9 "
                             "--fridge BlueforsA")
        return _start(cfg, args.cycle, args.fridge, args.packaging, args.note)
    if args.command == "end":
        return _end(cfg)
    return _show(cfg)


# ------------------------------------------------------------------------- verbs
def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    argv = list(argv or [])
    prog = prog or "scqo device"
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv and not argv[0].startswith("-"):
        verb, rest = argv[0], argv[1:]
        if verb not in _VERBS:
            print(f"unknown verb {verb!r} — one of: {', '.join(_VERBS)} "
                  f"(see `{prog} --help`)", file=sys.stderr)
            return 2
    else:
        verb, rest = "list", argv  # bare `scqo device` = the read-only menu

    parser = argparse.ArgumentParser(prog=f"{prog} {verb}", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    if verb == "add":
        parser.add_argument("name", help="the new sample's unique id (device name)")
        parser.add_argument("--description", default="",
                            help="one-line sample description for devices.toml")
    elif verb == "cooldown":
        parser.add_argument("command", nargs="?", choices=["start", "end"],
                            help="omit to validate + list cycles and the ACTIVE cycle's setups")
        parser.add_argument("cycle", nargs="?", help="cycle id for `start`, e.g. cd9")
        parser.add_argument("--fridge", default="", help="which fridge this insertion is in")
        parser.add_argument("--packaging", default="",
                            help="packaging description (fixed for the cycle)")
        parser.add_argument("--note", default="", help="free-text note stored with the cycle")
    parser.add_argument("--config", help="lab config path (default: $SCQO_CONFIG or ~/.scqo/config.toml)")
    args = parser.parse_args(rest)

    cfg = load_lab_config(args.config)
    if verb == "add":
        return _add(cfg, args.name, args.description)
    if verb == "cooldown":
        return _cooldown(cfg, args)
    return _list(cfg)


if __name__ == "__main__":
    sys.exit(main())
