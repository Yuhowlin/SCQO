"""Show the device's current calibration state — and who changed what, when.

State is per SETUP (v0.9.0): the first output line names the device, the resolved
setup and its state file, so two users of one sample know whose numbers they see.

    scqo state                        # calibration table per qubit (YOUR setup)
    scqo state --history              # last 20 changes (old -> new + cause + operator)
    scqo state --history 100 --qubit q0
    scqo state --physical             # the sample ledger: one row per qubit/field/SETUP
    scqo state --physical --history   # ... and its change history (rows carry setup=)
    scqo state --sources              # which run set each CURRENT value (both stores)
"""

from __future__ import annotations

import argparse

from ..config import FIELDS
from ..physical import PHYSICAL_FIELDS
from ._backends import build_session


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--history", nargs="?", const=20, type=int, metavar="N",
                        help="show the last N recorded changes instead (default N=20)")
    parser.add_argument("--qubit", help="restrict output to one qubit")
    parser.add_argument("--physical", action="store_true",
                        help="the sample's measured physical parameters instead of the instrument config")
    parser.add_argument("--sources", action="store_true",
                        help="where each current value came from: source run / (manual) / (externally changed)")
    parser.add_argument("--config", help="lab config path (default: $SCQO_CONFIG or ~/.scqo/config.toml)")
    args = parser.parse_args(argv)
    if args.sources and (args.history is not None or args.physical):
        parser.error("--sources always covers both stores; do not combine it with --history/--physical")

    sess, cfg = build_session(args.config)
    _print_context(sess, cfg)

    if args.sources:
        return _print_sources(sess, args.qubit)

    if args.history is None:
        if args.physical:
            return _print_physical(sess, args.qubit)
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

    records = sess.history(store="physical" if args.physical else "instrument")
    if args.qubit:
        records = [r for r in records if r["qubit"] == args.qubit]
    for r in records[-args.history:]:
        old = f"{r['old']:.6g}" if isinstance(r["old"], float) else r["old"]
        new = f"{r['new']:.6g}" if isinstance(r["new"], float) else r["new"]
        setup = f"  setup={r['setup']}" if r.get("setup") else ""
        print(f"{r['timestamp'][:19]}  {r['qubit']:4s} {r['field']:14s} {old} -> {new}"
              f"  ({r.get('experiment') or '?'}  run={r.get('run_id') or '-'}"
              f"  by={r.get('operator') or '-'}{setup})")
    if not records:
        print("no recorded changes yet")
    return 0


def _print_context(sess, cfg) -> None:
    """One `#` line saying WHOSE state/history follows — state is per SETUP
    (v0.9.0), so two users of one sample see different tables."""
    if not cfg.device:
        print("# built-in demo device (nothing saved)")
        return
    print(f"# device: {cfg.device}   setup: {sess.setup_name or '-'}   "
          f"cooldown: {sess.cooldown_id or '-'}   state: {sess.state_path or '-'}")


def _print_physical(sess, qubit_filter: str | None) -> int:
    """This context's measured physics (one (cooldown, setup) file). Each value is a
    measurement THROUGH this setup; compare across setups/cooldowns via the run index
    or the viewer trends page, not here."""
    values = sess.physical_state()  # flat {qubit: {field: value}} for this context
    order = {f: i for i, f in enumerate(PHYSICAL_FIELDS)}
    rows = sorted(
        ((q, f, v)
         for q, fields in values.items()
         for f, v in fields.items()
         if not qubit_filter or q == qubit_filter),
        key=lambda r: (r[0], order.get(r[1], 99)),
    )
    if not rows:
        if qubit_filter and values:  # ledger has data, just none for this qubit
            print(f"no physical parameters recorded for qubit {qubit_filter!r}")
        else:
            print("no physical parameters recorded yet (accept a run that proposes them)")
        return 0
    print(f"{'qubit':8s}{'field':20s}{'value':>14s}")
    for q, f, v in rows:
        value = f"{v:>14.6g}" if isinstance(v, float) else f"{str(v):>14s}"
        print(f"{q:8s}{f:20s}{value}")
    return 0


def _print_sources(sess, qubit_filter: str | None) -> int:
    """One provenance table over BOTH stores: which run set each current value."""
    sources = sess.live_sources()
    field_order = {f: i for i, f in enumerate([*FIELDS, *PHYSICAL_FIELDS])}
    rows = [
        info
        for store in ("instrument", "physical")
        for qubit, fields in sorted(sources[store].items())
        for info in (dict(fields[f], store=store) for f in sorted(fields, key=lambda f: field_order.get(f, 99)))
        if not qubit_filter or info["qubit"] == qubit_filter
    ]
    if not rows:
        print("no values yet")
        return 0
    print(f"{'qubit':6s} {'field':18s} {'store':10s} {'current':>14s}  {'source':46s} {'when':19s} {'by'}")
    externals = False
    for info in rows:
        value = f"{info['value']:.6g}" if isinstance(info["value"], float) else str(info["value"])
        source = {
            "run": info["run_id"],
            "manual": "(manual)",
            "external": "(externally changed)",
            "unrecorded": "(no record)",
        }[info["status"]]
        externals = externals or info["status"] == "external"
        when = (info["timestamp"] or "")[:19] or "-"
        print(f"{info['qubit']:6s} {info['field']:18s} {info['store']:10s} {value:>14s}  "
              f"{source:46s} {when:19s} {info['operator'] or '-'}")
    if externals:  # ASCII only: reaches consoles in whatever codepage the lab runs
        print("# (externally changed) = the current value matches no SCQO record - "
              "reseeded by the vendor or written by another tool")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
