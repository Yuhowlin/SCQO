"""Run the standard single-qubit calibration sequence — the daily workflow.

    scqo calibrate                          # all qubits, full sequence
    scqo calibrate --qubits q0 q1 --tag cooldown7
    scqo calibrate --skip resonator_spectroscopy

Sequence: resonator_spectroscopy -> qubit_spectroscopy -> qubit_power_rabi, each with its
effective defaults — the code defaults overlaid by your ``~/.scqo/parameters.toml``
tables (need one-off custom parameters? run that step alone via ``scqo run``).
The qubit list is chosen once for the whole sequence (``--qubits`` or all device
qubits) and deliberately overrides any per-experiment ``qubits`` in the parameters
file. Every step is saved to the datastore and tagged; fitted
values are written back to the device state as each step succeeds. Exits non-zero
if any step had no successful qubit.
"""

from __future__ import annotations

import argparse

from ._backends import build_session, default_qubits

# Bring-up order: readout -> coarse f01 (two-tone) -> pi amplitude. Ramsey (fine
# frequency + T2*) needs a calibrated pi pulse first: run it explicitly via
# `scqo run qubit_ramsey` once this sequence succeeds.
SEQUENCE = ["resonator_spectroscopy", "qubit_spectroscopy", "qubit_power_rabi"]


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--qubits", nargs="+", help="qubits to calibrate (default: all in the device)")
    parser.add_argument("--skip", action="append", default=[], choices=SEQUENCE,
                        help="skip one step (repeatable)")
    parser.add_argument("--tag", action="append", default=[], dest="tags",
                        help="extra searchable tag for every run in this sequence (repeatable)")
    parser.add_argument("--note", default="", help="note stored with every run in this sequence")
    parser.add_argument("--config", help="lab config path (default: $SCQO_CONFIG or ~/.scqo/config.toml)")
    args = parser.parse_args(argv)

    sess, _ = build_session(args.config)
    qubits = args.qubits or default_qubits(sess)
    steps = [s for s in SEQUENCE if s not in args.skip]

    print(f"calibrating {', '.join(qubits)}: {' -> '.join(steps)}\n")
    failures: list[str] = []
    for step in steps:
        result = sess.run(step, {"qubits": qubits}, tags=[*args.tags, "calibrate"], note=args.note)
        outcomes = " ".join(f"{q}:{o}" for q, o in result["outcomes"].items())
        run_id = result.get("run_id", "-")
        print(f"{step:28s} {outcomes:40s} {run_id}")
        if result.get("error"):
            print(f"{'':28s} error: {result['error']}")
        if not any(o == "successful" for o in result["outcomes"].values()):
            failures.append(step)

    print("\nfinal device state:")
    for q, fields in sess.device_state().items():
        pretty = "  ".join(f"{k}={v:.6g}" if isinstance(v, float) else f"{k}={v}" for k, v in fields.items())
        print(f"  {q}: {pretty}")

    if failures:
        print(f"\nFAILED steps (no qubit succeeded): {', '.join(failures)}")
        return 1
    print("\nall steps completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
