"""Health check: venv, drivers, config chain, registries — and what to do about it.

    scqo doctor                 # the first command to run when anything misbehaves

Read-only: touches no instrument, writes nothing. Checks the whole resolution chain
a run would use — python/scqo install, backend driver entry points, shared config +
user overlay + parameters file (loud parse errors are caught and shown), data_root,
registries, the cooldown registry, and the experiment catalog. Exit 0 = healthy
(warnings allowed), 1 = at least one failure.
"""

from __future__ import annotations

import argparse
import os
import sys
from importlib.metadata import entry_points, version
from pathlib import Path

OK, WARN, FAIL = "OK", "WARN", "FAIL"


def _backend_check(cfg, backends: dict) -> tuple[str, str]:
    from scqo.labconfig import _backend_family

    from ._backends import SERVED_BY

    if cfg.backend == "simulated":
        return OK, "'simulated' — built into scqo (demo qubits, synthetic data)"
    family = _backend_family(cfg.backend)
    if family is None:
        return FAIL, f"unsupported backend {cfg.backend!r} (known: simulated, qblox/qblox_sim, qm/qm_sim)"
    if family in backends:
        return OK, f"{cfg.backend!r} -> {backends[family]} (entry point {family!r})"
    provider, venv = SERVED_BY[family]
    return FAIL, (f"{cfg.backend!r} needs the {family!r} driver — wrong venv (activate "
                  f"D:\\github\\{venv}) or {provider} was never (re)installed here: entry points "
                  f"register at INSTALL time (`uv pip install -e`, INSTALL §1/§5)")


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="lab config path (default: $SCQO_CONFIG or ~/.scqo/config.toml)")
    args = parser.parse_args(argv)

    checks: list[tuple[str, str, str]] = []  # (status, topic, message)

    checks.append((OK, "python", sys.executable))
    checks.append((OK, "scqo", version("scqo")))

    backends = {ep.name: ep.value for ep in entry_points(group="scqo.backends")}
    checks.append((OK, "drivers", f"backends registered: {sorted(backends) or 'none (simulated only)'}"))

    from scqo import load_lab_config

    cfg = None
    try:
        cfg = load_lab_config(args.config)
    except Exception as err:  # malformed user.toml/parameters.toml, missing named files...
        checks.append((FAIL, "config", f"{type(err).__name__}: {err}"))

    if cfg is not None:
        if cfg.source is None:
            checks.append((WARN, "lab config", "none found — built-in defaults (simulated, NOTHING SAVED); see INSTALL §2"))
        else:
            checks.append((OK, "lab config", str(cfg.source)))
        checks.append((OK, "user overlay", str(cfg.user_source) if cfg.user_source else "none"))
        checks.append((OK, "parameters",
                       f"{cfg.parameters_source} ({len(cfg.parameter_defaults)} experiment table(s))"
                       if cfg.parameters_source else "none (code defaults)"))

        status, message = _backend_check(cfg, backends)
        checks.append((status, "backend", message))

        if cfg.data_root is None:
            checks.append((WARN, "data_root", "not configured — runs are NOT saved"))
        elif not Path(cfg.data_root).is_dir():
            checks.append((WARN, "data_root", f"{cfg.data_root} does not exist yet (created on first run)"))
        elif not os.access(cfg.data_root, os.W_OK):
            checks.append((FAIL, "data_root", f"{cfg.data_root} is not writable by this account"))
        else:
            index = Path(cfg.data_root) / "index.sqlite"
            checks.append((OK, "data_root", f"{cfg.data_root} ({'index present' if index.is_file() else 'no index yet'})"))

        if cfg.data_root is not None and Path(cfg.data_root).is_dir():
            from scqo.datastore import (
                active_cooldown,
                current_mapping,
                load_cooldowns,
                load_device_registry,
                load_instrument_registry,
            )

            instruments = load_instrument_registry(cfg.data_root)
            devices = load_device_registry(cfg.data_root)
            checks.append((OK, "registries", f"instruments: {len(instruments)}, devices: {len(devices)}"))

            try:
                cycles = load_cooldowns(cfg.data_root, cfg.device_name)
            except ValueError as err:
                checks.append((FAIL, "cooldowns", str(err)))
            else:
                active = active_cooldown(cycles)
                if not cycles:
                    checks.append((WARN, "cooldowns", f"no cycle registry for {cfg.device_name} — runs stamp cooldown=''"))
                elif active is None:
                    checks.append((WARN, "cooldowns", f"{len(cycles)} cycle(s), none ACTIVE — runs stamp cooldown=''"))
                else:
                    mapping = current_mapping(active[1])
                    wiring = f"wiring since {mapping['since']}" if mapping else "NO wiring mapping yet"
                    checks.append((OK, "cooldowns", f"{cfg.device_name}: {active[0]} ACTIVE ({wiring})"))

        try:
            if cfg.backend == "simulated":
                from ._backends import ensure_demo_experiments

                ensure_demo_experiments()
            from scqo import catalog

            n = len(catalog())
            checks.append((OK if n else FAIL, "catalog",
                           f"{n} experiment(s)" if n else "EMPTY — no driver entry points and not simulated"))
        except Exception as err:
            checks.append((FAIL, "catalog", f"{type(err).__name__}: {err}"))

    failures = 0
    for status, topic, message in checks:
        if status == FAIL:
            failures += 1
        print(f"[{status:4s}] {topic:12s} {message}")
    print(f"\n{failures} problem(s) found" if failures else "\nall checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
