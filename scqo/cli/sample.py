"""Add a new sample (device) — prints everything the manager needs, edits nothing shared.

    scqo sample                            # the checklist: what's manual vs automatic
    scqo sample new chipC --backend qblox --instrument cluster0 --description "..."

Adding a sample is ONE manual edit (the shared config's vendor table — machine wiring
is manager-owned and hand-written by design) plus optional registry entries. This
command prints ready-to-paste snippets for all of them and creates the sample's data
folder; it NEVER edits the shared config or the registries itself. Everything else
(run folders, scqo_state.json, the index, cooldowns.toml via ``scqo cooldown start``)
auto-creates on first use.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scqo import DataStore, load_lab_config
from scqo.datastore import load_device_registry, load_instrument_registry

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - py3.10 fallback
    import tomli as tomllib

CHECKLIST = """\
Adding a new sample — what is manual vs automatic:

  MANUAL (manager, hand-edited by design):
    1. The shared config.toml: a vendor table naming the sample on its instrument
       ([qblox]/[qm] device_name + state_path), or [lab] device_name for simulated.
       Twin modes additionally need a working copy of the vendor config.
    2. (optional) devices.toml: the sample's facts (description, mounted_on, design).
    3. (optional, once per INSTRUMENT) instruments.toml: kind/address/connection.

  AUTOMATIC (no action needed):
    - <data_root>/<name>/ folders, run folders, scqo_state.json, the index row,
      viewer pages — all created on first use.
    - cooldowns.toml — created by `scqo cooldown start cd1 --fridge ... --packaging ...`
      (then hand-add its [[cd1.mapping]] wiring snapshot).

`scqo sample new <name> [--backend ...] [--instrument ...]` prints the paste-ready
snippets for the manual steps and creates the data folder."""


def _existing_names(cfg) -> set[str]:
    """Every device name this lab already knows: config tables, registry, index."""
    names: set[str] = set()
    if cfg.source is not None:
        with open(cfg.source, "rb") as f:
            raw = tomllib.load(f)
        lab = raw.get("lab", {})
        if lab.get("device_name"):
            names.add(lab["device_name"])
        for family in ("qblox", "qm"):
            if raw.get(family, {}).get("device_name"):
                names.add(raw[family]["device_name"])
    if cfg.data_root is not None:
        names |= set(load_device_registry(cfg.data_root))
        names |= set(DataStore(cfg.data_root, device_name=cfg.device_name).distinct_devices())
    return names


def _new(cfg, name: str, backend: str, instrument: str | None, description: str) -> int:
    if cfg.data_root is None:
        raise SystemExit(
            f"no data_root configured in {cfg.source or 'the lab config'} — "
            "a sample needs somewhere for its data to land"
        )
    device_dir = Path(cfg.data_root) / name
    if device_dir.exists() or name in _existing_names(cfg):
        print(f"note: {name!r} is already known to this lab (folder/config/registry/index) — "
              "snippets below anyway, skip what exists\n", file=sys.stderr)
    device_dir.mkdir(parents=True, exist_ok=True)  # the command's ONLY write
    state_path = (device_dir / "scqo_state.json").as_posix()

    print(f"created {device_dir}\n")
    print("=" * 72)
    if backend in ("qblox", "qm"):
        vendor_dir = "config_dir" if backend == "qblox" else "state_dir"
        vendor_note = ("working copy of dut_config.json (+ hw_config.json for real mode)"
                       if backend == "qblox" else "working copy of state.json + wiring.json")
        print(f"1. PASTE into the shared config.toml (the [{backend}] table — the sample\n"
              f"   follows this instrument; replaces any previous sample on it):\n")
        print(f"[{backend}]")
        print(f'{vendor_dir}  = \'{(device_dir / (backend + "_state")).as_posix()}\'   # {vendor_note}')
        print(f'device_name = "{name}"')
        print(f"state_path  = '{state_path}'")
    else:
        print("1. PASTE into the shared config.toml (simulated/dev — [lab] table):\n")
        print(f'device_name = "{name}"')
        print(f"state_path  = '{state_path}'")

    print("\n" + "=" * 72)
    print(f"2. PASTE into {Path(cfg.data_root) / 'devices.toml'} (optional sample facts):\n")
    print(f"[{name}]")
    print(f'description = "{description or "..."}"')
    if instrument:
        print(f'mounted_on = "{instrument}"')

    if instrument:
        instruments = load_instrument_registry(cfg.data_root)
        if instrument not in instruments:
            print("\n" + "=" * 72)
            print(f"3. PASTE into {Path(cfg.data_root) / 'instruments.toml'} — {instrument!r} is not\n"
                  "   registered yet (once per instrument, not per sample):\n")
            print(f"[{instrument}]")
            print('kind = "..."          # e.g. qblox_cluster / qm_opx1000')
            print('address = "..."       # IP or hostname')
            print('connection = "..."')

    print("\n" + "=" * 72)
    print("then:")
    print('  scqo cooldown start cd1 --fridge ... --packaging "..."')
    print(f"  (hand-add the [[cd1.mapping]] wiring snapshot in {device_dir / 'cooldowns.toml'})")
    print("  scqo devices                        # verify the new menu row")
    print("  scqo run resonator_spectroscopy     # first stamped run")
    return 0


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", choices=["new"],
                        help="omit to print the add-a-sample checklist")
    parser.add_argument("name", nargs="?", help="the new sample's unique id (device_name)")
    parser.add_argument("--backend", default="simulated", choices=["qblox", "qm", "simulated"],
                        help="which instrument family will carry the sample (default: simulated)")
    parser.add_argument("--instrument", help="instruments.toml key it is mounted on, e.g. cluster0")
    parser.add_argument("--description", default="", help="one-line sample description for devices.toml")
    parser.add_argument("--config", help="lab config path (default: $SCQO_CONFIG or ~/.scqo/config.toml)")
    args = parser.parse_args(argv)

    if args.command != "new":
        print(CHECKLIST)
        return 0
    if not args.name:
        raise SystemExit("new needs a sample name, e.g.: scqo sample new chipC --backend qblox")
    return _new(load_lab_config(args.config), args.name, args.backend, args.instrument, args.description)


if __name__ == "__main__":
    sys.exit(main())
