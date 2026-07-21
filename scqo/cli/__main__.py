"""``scqo <subcommand>`` — the lab command (also ``python -m scqo.cli``).

Manual dispatch (not argparse subparsers) so each subcommand keeps its flags
byte-identical to the historical driver scripts, including ``run``'s peek-based
--help that renders the chosen experiment's parameter schema.

``python -m scqo <data_root>`` (the index rebuilder in ``scqo/__main__.py``) is a
separate, unchanged entry point.
"""

from __future__ import annotations

import sys

#: subcommand -> (module in scqo.cli, one-line help)
_COMMANDS = {
    "run": ("run", "run any cataloged experiment (no name = show the catalog)"),
    "find": ("find", "query saved runs (no instrument touched)"),
    "accept": ("accept", "review / apply / reject a run's suggested updates (by run id)"),
    "suggest": ("suggest", "attach YOUR manually-read value to a run (fit failed, figure didn't)"),
    "set": ("set", "write a value directly (recorded manual write - experience values, no run)"),
    "tag": ("tag", "retro-tag / annotate a saved run"),
    "state": ("state", "current calibration table + change history (who/what/when)"),
    "user": ("user", "show or set YOUR selection: device + setup (writes user.toml)"),
    "device": ("device", "device admin (manager): add, list, cooldown start/end"),
    "doctor": ("doctor", "health check: venv, drivers, config chain, registries (run me first)"),
}


def _usage() -> str:
    lines = ["usage: scqo <command> [options]   (scqo <command> --help for that command's flags)", ""]
    lines += [f"  {name:16s} {help_}" for name, (_, help_) in _COMMANDS.items()]
    # ASCII only: this text reaches consoles in whatever codepage the lab runs
    lines += ["", "backends: simulated is built in; qblox/qm come from the installed driver",
              "(LCHQBDriver / LCHQMDriver - activate the matching venv)."]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(_usage())
        return 0
    if argv[0] == "--version":
        from importlib.metadata import version

        print(version("scqo"))
        return 0
    command = argv[0]
    if command not in _COMMANDS:
        print(f"unknown command {command!r}\n\n{_usage()}", file=sys.stderr)
        return 2
    from importlib import import_module

    module = import_module(f"scqo.cli.{_COMMANDS[command][0]}")
    return module.main(argv[1:], prog=f"scqo {command}") or 0


if __name__ == "__main__":
    sys.exit(main())
