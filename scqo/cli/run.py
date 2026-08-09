"""Run any cataloged experiment; every run is saved + searchable.

    scqo run                                        # no arguments = show the menu
    scqo run resonator_spectroscopy --targets q1 --tag mytest --note "first try"
    scqo run qubit_ramsey --set num_points=201
    scqo run resonator_spectroscopy --no-update     # analyze only, no writeback
    scqo run qubit_ramsey --targets q1 --preview    # render the sequence to files;
                                                    #   nothing runs, nothing saved

Works from any directory in the right venv. Parameters: code defaults <
~/.scqo/parameters.toml < --params/--set; see every knob with ``--help`` after the
experiment name. ``--preview`` writes the vendor's own view of the sequence
(Qblox: interactive pulse diagram + timing table; QM: the generated QUA script
plus, when the gateway answers, simulated output waveforms) to
./scqo_preview/<experiment>_<timestamp>/ and auto-opens it — redirect with
``--out DIR``, suppress opening with ``--no-open``. On QM the simulator is
tried automatically and skipped with a warning when the gateway is down;
``--simulate-ns N`` widens the simulated window, ``--no-simulate`` guarantees
a fully offline preview.
"""

from __future__ import annotations

from ._engine import run_experiment_cli


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    return run_experiment_cli(None, doc=__doc__, argv=argv, prog=prog)


if __name__ == "__main__":
    raise SystemExit(main())
