"""What can I measure here? — the device/instrument menu for Tier-1 users.

    scqo devices                      # every configured backend + how to select it

For each backend the shared lab config wires, shows: the SAMPLE mounted on that
instrument, the instrument's connection facts (instruments.toml), the active cooldown
cycle + packaging, the current wiring size — and the exact ``~/.scqo/user.toml`` line
that selects it. Touches NO instrument: this only reads config + registries, so it is
safe from any account at any time. Pick your backend once in your own user.toml; the
sample follows the instrument automatically.
"""

from __future__ import annotations

import argparse
import sys

from scqo import load_lab_config
from scqo.datastore import (
    active_cooldown,
    current_mapping,
    load_cooldowns,
    load_instrument_registry,
)

from ._backends import SERVED_BY

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - py3.10 fallback
    import tomli as tomllib


def _cycle_summary(data_root, device: str) -> tuple[str, dict]:
    """(display string, current wiring ports) for the device's active cycle."""
    if data_root is None:
        return "-", {}
    try:
        cycles = load_cooldowns(data_root, device)
    except ValueError as err:
        return f"REGISTRY ERROR: {err}", {}
    active = active_cooldown(cycles)
    if active is None:
        return "-", {}
    cid, cycle = active
    text = cid + (f" [{cycle['packaging']}]" if cycle.get("packaging") else "")
    mapping = current_mapping(cycle) or {}
    ports = {k: v for k, v in mapping.items() if k not in ("since", "note")}
    return text, ports


def _instrument_summary(ports: dict, instruments: dict) -> str:
    """'cluster0 (192.168.0.2)' for every instrument the current wiring references."""
    names = sorted({str(v).split(".")[0] for v in ports.values()})
    parts = []
    for name in names:
        address = (instruments.get(name) or {}).get("address")
        parts.append(f"{name} ({address})" if address else name)
    return ", ".join(parts) or "-"


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="lab config path (default: $SCQO_CONFIG or ~/.scqo/config.toml)")
    args = parser.parse_args(argv)

    cfg = load_lab_config(args.config)
    print(f"# lab config: {cfg.source or 'built-in defaults (simulated, nothing saved)'}")
    print(f"# user overlay: {cfg.user_source or 'none'}")

    # Re-read the shared config: the menu needs [lab]'s own fallbacks for EVERY
    # backend, not just the resolution of the currently active one.
    raw = {}
    if cfg.source is not None:
        with open(cfg.source, "rb") as f:
            raw = tomllib.load(f)
    lab = raw.get("lab", {})
    instruments = load_instrument_registry(cfg.data_root) if cfg.data_root else {}

    candidates = []
    for family in ("qblox", "qm"):
        if family in raw or str(lab.get("backend", "")).startswith(family):
            vendor = raw.get(family, {})
            candidates.append((family, vendor.get("device_name", lab.get("device_name", "device"))))
    candidates.append(("simulated", lab.get("device_name", "device")))

    print(f"\n{'backend':11s} {'device':10s} {'instrument(s)':28s} {'cooldown':18s} run from")
    for backend, device in candidates:
        cycle_text, ports = _cycle_summary(cfg.data_root, device)
        provider, venv = SERVED_BY[backend]
        marker = "  <- selected" if backend == cfg.backend else ""
        print(f"{backend:11s} {device:10s} {_instrument_summary(ports, instruments):28s} "
              f"{cycle_text:18s} {provider} ({venv}){marker}")

    print('\nselect an instrument once per project:  backend = "<name>"  in ~/.scqo/user.toml')
    print("(the sample follows the instrument; your tags/parameters_file can live there too)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
