"""Run any cataloged experiment; every run is saved + searchable.

    scqo run                                        # no arguments = show the menu
    scqo run resonator_spectroscopy --targets q1 --tag mytest --note "first try"
    scqo run qubit_ramsey --set num_points=201
    scqo run resonator_spectroscopy --no-update     # analyze only, no writeback

Works from any directory in the right venv (the old ``python scripts\\run_experiment
.py`` form still works inside a driver repo). Parameters: code defaults <
~/.scqo/parameters.toml < --params/--set; see every knob with ``--help`` after the
experiment name.
"""

from __future__ import annotations

from ._engine import run_experiment_cli


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    return run_experiment_cli(None, doc=__doc__, argv=argv, prog=prog)


if __name__ == "__main__":
    raise SystemExit(main())
