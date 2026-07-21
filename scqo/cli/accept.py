"""Review / apply / reject a run's suggested updates — by run id, any time later.

    scqo accept                                  # list runs with pending suggestions
    scqo accept RUN_ID --list                    # show the suggestion table, decide nothing
    scqo accept RUN_ID                           # terminal: pick interactively; script: apply ALL pending
    scqo accept RUN_ID --component q0 --field readout_freq --comment "looks right"
    scqo accept RUN_ID --reject --comment "fit chased a noise spike"
    scqo accept RUN_ID --force                   # bypass the cooldown-era + staleness guards
    scqo accept RUN_ID --reapply --field readout_freq --comment "rolling back to this run"
                                                 # re-decide ALREADY accepted/rejected items:
                                                 # deliberately overwrites the newer value

Applying builds the device's backend session (calibration knobs must reach the live
instrument config, vendor-push-first, ChangeRecords stamped with the ORIGINATING
run id). The pending list, ``--list`` and ``--reject`` touch only the datastore —
they work anywhere the data drive is mounted, no instrument needed. Decisions live
in the run's ``record.json`` (the truth), so they survive any index rebuild.

Guards on apply: the run's cooldown/setup era must match the device's current one,
and each value's ``before`` must still equal the store's current value (someone
else may have calibrated in between). **At a terminal you never need flags** — a
guard trip becomes a warning + [y/N] question (Enter = No): era mismatch asks
once, an already-decided row asks "re-apply (rollback)?", a stale row shows the
before/current diff. In scripts (non-TTY) nobody can answer, so the flags decide:
``--force`` = yes to era+stale, ``--reapply`` = yes to re-deciding decided items
(staleness is then irrelevant — overwriting the newer value is the point). All
(re-)applications land in the change history like any other apply, linked to THIS
run.

Nothing to decide? If the estimator failed but the run's figure shows the value,
``scqo suggest RUN_ID q0.readout_freq=5.912e9`` attaches YOUR value to the run as
a pending suggestion — decided right here, with the same guards.
"""

from __future__ import annotations

import argparse
import json
import sys

from scqo import DataStore, load_lab_config
from scqo.suggestions import pending_count, reject_suggestions

from ._review import format_summary, format_table, review_interactively


def _load_record(store: DataStore, run_id: str) -> dict:
    """load_run with the KeyError turned into a clean exit (typos are the common case)."""
    try:
        return store.load_run(run_id)["record"]
    except KeyError as err:
        raise SystemExit(err.args[0] if err.args else str(err))


def _no_suggestions_hint(run_id: str, targets: list[str]) -> str:
    """The estimator proposed nothing (it failed, or --no-update): name the escape
    hatch. Without this the fit-failed run is a dead end — the very moment someone
    needs to learn `scqo suggest` exists. ASCII only: this text reaches consoles in
    whatever codepage the lab runs."""
    name = targets[0] if targets else "q0"  # the run's OWN first target: paste-ready
    return (
        f"{run_id}: no suggested updates recorded - the estimator proposed none.\n"
        f"If the run's figure shows the value, attach it yourself:\n"
        f"  scqo suggest {run_id} {name}.FIELD=VALUE   "
        f"(e.g. {name}.readout_freq=5.912e9)"
    )


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_id", nargs="?",
                        help="the run whose suggestions to decide (omit to list pending runs)")
    parser.add_argument("--list", action="store_true", dest="show",
                        help="show the run's suggestion table and exit (decides nothing)")
    parser.add_argument("--reject", action="store_true",
                        help="reject instead of apply (datastore-only, no instrument)")
    parser.add_argument("--component", action="append", default=None,
                        help="restrict to one component (repeatable)")
    parser.add_argument("--field", action="append", default=None,
                        help="restrict to one field (repeatable), e.g. readout_freq")
    parser.add_argument("--comment", default="", help="decision comment stored with each item")
    parser.add_argument("--force", action="store_true",
                        help="apply despite a cooldown-era mismatch or stale before-values "
                             "(scripts; at a terminal you are warned and asked instead)")
    parser.add_argument("--reapply", action="store_true",
                        help="re-decide items that were already accepted/rejected, e.g. roll "
                             "back to this run's value (scripts; a terminal asks per row)")
    parser.add_argument("--limit", type=int, default=20, help="max rows for the pending list")
    parser.add_argument("--config", help="lab config path (default: $SCQO_CONFIG or ~/.scqo/config.toml)")
    args = parser.parse_args(argv)

    cfg = load_lab_config(args.config)
    if cfg.data_root is None:
        raise SystemExit(f"no data_root configured in {cfg.source or 'the lab config'} — nothing is saved")
    store = DataStore(cfg.data_root, device_name=cfg.device or "device")

    if args.run_id is None:  # ---------------------------------- pending-run list
        runs = store.find_runs(pending=True, limit=args.limit)
        if not runs:
            print("no runs with pending suggestions")
            return 0
        for r in runs:
            n = r.get("suggestions_pending", 0)
            print(f"{r['run_id']:44s} {r['outcome']:10s} pending:{n:<3d} {r['path']}")
        print(f"\n({len(runs)} run(s); decide with: scqo accept <run_id>)")
        return 0

    if args.show:  # -------------------------------------------- table only
        record = _load_record(store, args.run_id)
        suggestions = record.get("suggestions", [])
        if not suggestions:
            print(_no_suggestions_hint(args.run_id,
                                       record.get("targets") or record.get("qubits") or []))
            return 0
        print(f"{args.run_id} ({pending_count(suggestions)} pending):")
        print(format_table(suggestions))
        return 0

    if args.reject:  # ------------------------------------------ datastore-only
        _load_record(store, args.run_id)  # loud unknown-run_id check first
        summary = reject_suggestions(store, args.run_id, components=args.component,
                                     fields=args.field, comment=args.comment)
        print(json.dumps(summary, indent=2))
        return 0

    # ------------------------------------------------------------ apply
    # The record first (datastore-only): a run the estimator left without suggestions
    # has nothing to decide, and saying so must not cost a driver import. The device
    # term keeps every message truthful — a foreign run_id falls through to the
    # session path, whose wrong-device error names the sample to select.
    record = _load_record(store, args.run_id)
    if not record.get("suggestions") and record.get("device") == store.device_name:
        print(_no_suggestions_hint(args.run_id,
                                   record.get("targets") or record.get("qubits") or []),
              file=sys.stderr)
        # stdout stays parseable: the same empty summary Session.accept would return
        print(json.dumps({"run_id": args.run_id, "applied": [], "stale": [],
                          "errors": [], "pending_left": 0}, indent=2))
        return 0

    from ._backends import build_session  # imports drivers: only the apply path pays

    sess, _ = build_session(args.config)
    interactive = (args.component is None and args.field is None
                   and sys.stdin.isatty() and sys.stderr.isatty())
    try:
        if interactive:
            summary = review_interactively(sess, args.run_id, record.get("suggestions", []),
                                           force=args.force, comment=args.comment,
                                           reapply=args.reapply)
            return 0 if summary is None else (1 if summary["errors"] or summary["stale"] else 0)
        summary = sess.accept(args.run_id, components=args.component, fields=args.field,
                              comment=args.comment, force=args.force, reapply=args.reapply)
    # device mismatch / era guard / unknown run_id — the message says the fix; a
    # 44-character run id makes typos the COMMON case, so never show a traceback.
    except (RuntimeError, KeyError) as err:
        raise SystemExit(err.args[0] if err.args else str(err))
    print(format_summary(summary), file=sys.stderr)
    print(json.dumps(summary, indent=2))
    return 1 if summary["errors"] or summary["stale"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
