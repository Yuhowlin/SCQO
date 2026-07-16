"""Health check: venv, drivers, config chain, registries — and what to do about it.

    scqo doctor                 # the first command to run when anything misbehaves

Read-only: touches no instrument, writes nothing. Checks the whole resolution chain
a run would use — python/scqo install, the device selection (user overlay / [lab]),
the device's cooldown registry (active cycle, current setup, instrument-config folder
and its expected vendor files), backend driver entry points, data_root, registries,
and the experiment catalog. Exit 0 = healthy (warnings allowed), 1 = failures.
"""

from __future__ import annotations

import argparse
import os
import sys
from importlib.metadata import entry_points, version
from pathlib import Path

OK, WARN, FAIL = "OK", "WARN", "FAIL"

_EXPECTED_FILES = {"qblox": ("dut_config.json", "hw_config.json"),
                   "qm": ("state.json", "wiring.json")}


def _setup_checks(cfg, backends: dict) -> list[tuple[str, str, str]]:
    """Device -> cycle -> selected setup -> backend/folder checks (the run-time chain)."""
    from scqo.datastore import (
        SetupResolutionError,
        active_cooldown,
        load_cooldowns,
        resolve_setup,
    )

    from ._backends import SERVED_BY

    fix = ("scqo device cooldown start cd1 [--fridge <name>] — then hand-add "
           "[cd1.setup.<name>] blocks (backend [+ note])")
    try:
        cycles = load_cooldowns(cfg.data_root, cfg.device)
    except ValueError as err:
        return [(FAIL, "cooldowns", str(err))]
    if not cycles:
        return [(FAIL, "cooldowns", f"device {cfg.device!r} has no cycle registry — runs will "
                                    f"refuse; the manager runs: {fix}")]
    active = active_cooldown(cycles)
    if active is None:
        return [(FAIL, "cooldowns", f"{len(cycles)} cycle(s), none ACTIVE — runs will refuse; "
                                    f"start the next one: {fix}")]
    cid, cycle = active
    try:
        name, setup = resolve_setup(cycle, cfg.setup or None)
    except SetupResolutionError as err:
        if err.reason == "none":
            return [(FAIL, "cooldowns", f"cycle {cid!r} ACTIVE but has NO setups — runs will "
                                        f"refuse; hand-add [{cid}.setup.<name>] blocks "
                                        "(backend [+ note]) to its cooldowns.toml")]
        if err.reason == "ambiguous":
            return [(FAIL, "cooldowns", f"cycle {cid!r} has {len(err.available)} setups and none "
                                        f"is selected — runs will refuse for this account; pick "
                                        f"one: scqo user --setup <name> "
                                        f"(available: {', '.join(err.available)})")]
        return [(FAIL, "cooldowns", f"selected setup {cfg.setup!r} is not in ACTIVE cycle "
                                    f"{cid!r} (available: {', '.join(err.available) or 'none'}) "
                                    "— scqo user --setup <name>")]
    how = "selected" if cfg.setup else "auto"
    out = [(OK, "cooldowns", f"{cfg.device}: {cid} ACTIVE — setup {name!r} ({how}), "
                             f"backend={setup['backend']}")]

    backend = setup["backend"]
    if backend == "simulated":
        out.append((OK, "backend", "'simulated' — built into scqo (demo qubits, synthetic data)"))
    else:
        folder = Path(setup["instrument_config"])
        if not folder.is_dir():
            out.append((FAIL, "instr config", f"{folder} does not exist on this machine"))
        else:
            missing = [n for n in _EXPECTED_FILES[backend] if not (folder / n).is_file()]
            if missing:
                out.append((FAIL, "instr config", f"{folder}: missing {', '.join(missing)} — copy "
                                                  "the vendor files there under canonical names"))
            else:
                out.append((OK, "instr config", str(folder)))
        if backend in backends:
            out.append((OK, "backend", f"{backend!r} -> {backends[backend]} (entry point)"))
        else:
            provider, venv = SERVED_BY[backend]
            out.append((FAIL, "backend", f"{backend!r} driver not registered here — wrong venv "
                                         f"(activate D:\\github\\{venv}) or {provider} needs "
                                         "`uv pip install -e` (entry points register at INSTALL time)"))
    # The OTHER setups of the cycle (any account may select them): folder existence only.
    for other, s in cycle.get("setup", {}).items():
        if other == name or s["backend"] == "simulated":
            continue
        f = Path(s["instrument_config"])
        if not f.is_dir():
            out.append((WARN, "instr config", f"setup {other!r}: {f} does not exist on this "
                                              "machine (fine unless someone selects it here)"))
    # Per-(cooldown, setup) state files: <device>/<cid>/<setup>/scqo/scqo_state.json.
    # Auto-created on first save (always under data_root, so the viewer always sees
    # them), so absence is informational, not a failure.
    from scqo.datastore import setup_state_path

    for sname in cycle.get("setup", {}):
        spath = setup_state_path(cfg.data_root, cfg.device, cid, sname)
        out.append((OK, "state", f"setup {sname!r}: {spath} "
                                 f"({'exists' if spath.is_file() else 'not created yet'})"))
    return out


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
        if cfg.device:
            checks.append((OK, "device", cfg.device))
        elif cfg.setup:  # a setup selection with no device refuses every run
            checks.append((FAIL, "device", f"setup {cfg.setup!r} is selected but no device is — "
                                           "runs will refuse; scqo user --device <name> "
                                           "(or scqo user --clear-setup)"))
        else:
            checks.append((WARN, "device", "none selected — built-in simulated demo, NOTHING "
                                           "SAVED; select one: scqo user --device <name>"))

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
            from scqo.datastore import load_device_registry

            checks.append((OK, "registries", f"devices.toml entries: {len(load_device_registry(cfg.data_root))}"))
            if cfg.device is not None:
                checks.extend(_setup_checks(cfg, backends))

        try:
            from ._backends import ensure_demo_experiments

            ensure_demo_experiments()
            from scqo import catalog

            n = len(catalog())
            checks.append((OK if n else FAIL, "catalog",
                           f"{n} experiment(s)" if n else "EMPTY — no driver entry points registered"))
        except Exception as err:
            checks.append((FAIL, "catalog", f"{type(err).__name__}: {err}"))

    failures = 0
    for status, topic, message in checks:
        if status == FAIL:
            failures += 1
        print(f"[{status:4s}] {topic:13s} {message}")
    print(f"\n{failures} problem(s) found" if failures else "\nall checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
