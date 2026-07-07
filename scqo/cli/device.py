"""Show the device's current calibration state — and who changed what, when.

    scqo device                       # calibration table per qubit
    scqo device --history             # last 20 changes (old -> new + cause + operator)
    scqo device --history 100 --qubit q0
"""

from __future__ import annotations

import argparse

from ._backends import build_session


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--history", nargs="?", const=20, type=int, metavar="N",
                        help="show the last N recorded changes instead (default N=20)")
    parser.add_argument("--qubit", help="restrict output to one qubit")
    parser.add_argument("--config", help="lab config path (default: $SCQO_CONFIG or ~/.scqo/config.toml)")
    args = parser.parse_args(argv)

    sess, cfg = build_session(args.config)

    if args.history is None:
        state = sess.device_state()
        fields = sorted({f for q in state.values() for f in q})
        print(f"{'qubit':8s}" + "".join(f"{f:>16s}" for f in fields))
        for qubit, values in state.items():
            if args.qubit and qubit != args.qubit:
                continue
            row = "".join(
                f"{values.get(f):>16.6g}" if isinstance(values.get(f), float) else f"{str(values.get(f)):>16s}"
                for f in fields
            )
            print(f"{qubit:8s}{row}")
        return 0

    records = sess.history()
    if args.qubit:
        records = [r for r in records if r["qubit"] == args.qubit]
    for r in records[-args.history:]:
        old = f"{r['old']:.6g}" if isinstance(r["old"], float) else r["old"]
        new = f"{r['new']:.6g}" if isinstance(r["new"], float) else r["new"]
        print(f"{r['timestamp'][:19]}  {r['qubit']:4s} {r['field']:14s} {old} -> {new}"
              f"  ({r.get('experiment') or '?'}  run={r.get('run_id') or '-'}"
              f"  by={r.get('operator') or '-'})")
    if not records:
        print("no recorded changes yet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
