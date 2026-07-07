"""Manage the device's cooldown-cycle registry — cycles + wiring-mapping eras.

    scqo cooldown                                  # validate + list cycles, show current wiring
    scqo cooldown start cd9 --fridge BlueforsA --packaging "PCB v3"
    scqo cooldown end                              # close the open cycle (today's date)

Every run is auto-stamped with the ACTIVE cycle id and the wiring era in effect
(query with ``scqo find --cooldown``). Wiring-mapping snapshots are HAND-edited in
the file: add a ``[[<id>.mapping]]`` block with a ``since`` date whenever any port
changes — a broken channel moving on the same instrument counts, and so does swapping
the whole instrument. The no-args form is the validator. Manager-run by convention.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import date
from pathlib import Path

from scqo import load_lab_config
from scqo.datastore import COOLDOWNS_FILE, active_cooldown, current_mapping, load_cooldowns

_START_TEMPLATE = """
[{cid}]
start = {today}
fridge = "{fridge}"
packaging = "{packaging}"
note = "{note}"

# Wiring: add a FULL snapshot whenever any port changes (a same-instrument channel
# swap counts). Reserved keys: since (required), note. Every other key is a device
# port -> "instrument.port" string (instrument keys come from instruments.toml).
# [[{cid}.mapping]]
# since = {today}
# "q1.drive"   = "cluster0.module2.out0"
# "q1.readout" = "cluster0.module6.in0"
"""


def _registry_path(cfg) -> Path:
    if cfg.data_root is None:
        raise SystemExit("no data_root configured — cooldown cycles live under <data_root>/<device>/")
    return Path(cfg.data_root) / cfg.device_name / COOLDOWNS_FILE


def _show(cfg) -> int:
    cycles = load_cooldowns(cfg.data_root, cfg.device_name)  # raises loudly on a broken file
    if not cycles:
        print(f"no cooldown cycles declared for {cfg.device_name} ({_registry_path(cfg)})")
        return 0
    active = active_cooldown(cycles)
    for cid, cycle in cycles.items():
        marker = "ACTIVE" if active and cid == active[0] else f"ended {cycle.get('end', '?')}"
        extras = "  ".join(f"{k}={cycle[k]}" for k in ("fridge", "packaging") if cycle.get(k))
        print(f"{cid:12s} start={cycle.get('start', '?')}  {marker:18s} "
              f"mappings={len(cycle.get('mapping', []))}  {extras}")
    if active:
        mapping = current_mapping(active[1])
        if mapping:
            print(f"\ncurrent wiring of {active[0]} (since {mapping['since']}):")
            for port, target in mapping.items():
                if port not in ("since", "note"):
                    print(f"  {port:16s} -> {target}")
        else:
            print(f"\n{active[0]} has no wiring mapping yet — hand-add a [[{active[0]}.mapping]] block")
    return 0


def _start(cfg, cid: str, fridge: str, packaging: str, note: str) -> int:
    path = _registry_path(cfg)
    cycles = load_cooldowns(cfg.data_root, cfg.device_name)
    active = active_cooldown(cycles)
    if active:
        raise SystemExit(f"cycle {active[0]!r} is still open — run `scqo cooldown end` first")
    if cid in cycles:
        raise SystemExit(f"cycle {cid!r} already exists in {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Append-only: hand-written content and comments above stay untouched.
    with open(path, "a", encoding="utf-8") as f:
        f.write(_START_TEMPLATE.format(cid=cid, today=date.today().isoformat(),
                                       fridge=fridge, packaging=packaging, note=note))
    load_cooldowns(cfg.data_root, cfg.device_name)  # re-parse: the write must be valid
    print(f"started {cid} in {path} — hand-add its [[{cid}.mapping]] wiring snapshot")
    return 0


def _end(cfg) -> int:
    path = _registry_path(cfg)
    cycles = load_cooldowns(cfg.data_root, cfg.device_name)
    active = active_cooldown(cycles)
    if active is None:
        raise SystemExit(f"no open cycle in {path}")
    cid = active[0]
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    # Targeted line edit (comments preserved): insert `end = <today>` right after the
    # open cycle's `start = ...` line inside its [cid] block.
    in_block, inserted = False, False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == f"[{cid}]":
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
        load_cooldowns(cfg.data_root, cfg.device_name)  # re-parse: never leave a broken registry
    except ValueError as err:
        shutil.copy2(backup, path)
        raise SystemExit(f"edit produced an invalid file — restored from {backup}: {err}")
    print(f"ended {cid} ({date.today().isoformat()}) in {path}")
    return 0


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", choices=["start", "end"],
                        help="omit to validate + list cycles and the current wiring")
    parser.add_argument("cycle", nargs="?", help="cycle id for `start`, e.g. cd9")
    parser.add_argument("--fridge", default="", help="which fridge this insertion is in")
    parser.add_argument("--packaging", default="", help="packaging description (fixed for the cycle)")
    parser.add_argument("--note", default="", help="free-text note stored with the cycle")
    parser.add_argument("--config", help="lab config path (default: $SCQO_CONFIG or ~/.scqo/config.toml)")
    args = parser.parse_args(argv)

    cfg = load_lab_config(args.config)
    if args.command == "start":
        if not args.cycle:
            raise SystemExit("start needs a cycle id, e.g.: scqo cooldown start cd9")
        return _start(cfg, args.cycle, args.fridge, args.packaging, args.note)
    if args.command == "end":
        return _end(cfg)
    return _show(cfg)


if __name__ == "__main__":
    sys.exit(main())
