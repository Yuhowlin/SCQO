"""Find saved measurement runs — the "where is my data" command.

    scqo find                                    # latest runs
    scqo find --experiment qubit_ramsey --target q1
    scqo find --cooldown cd8 --setup qblox_main --since 2026-07-01
    scqo find --show 20260704-153012-qubit_ramsey-01   # full record

Queries the SQLite index under the lab's data_root (from ``~/.scqo/config.toml``).
No instrument is touched — this reads only the datastore, so it runs anywhere the
data drive is mounted. The index is disposable: rebuild it any time with
``python -m scqo <data_root>``.
"""

from __future__ import annotations

import argparse
import json

from scqo import DataStore, load_lab_config


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--experiment", help="filter by experiment name")
    parser.add_argument("--target", help="filter by measured component, e.g. q0")
    parser.add_argument("--tag", help="filter by tag, e.g. cooldown7")
    parser.add_argument("--since", help="ISO date/time lower bound, e.g. 2026-07-01")
    parser.add_argument("--until", help="ISO date/time upper bound")
    parser.add_argument("--outcome", choices=["successful", "partial", "failed", "no_data"])
    parser.add_argument("--device", help="filter by device (sample) name")
    parser.add_argument("--operator", help="filter by who ran it (OS login name)")
    parser.add_argument("--cooldown", help="filter by cooldown-cycle id, e.g. cd8")
    parser.add_argument("--setup", help="filter by setup name (unique per cycle only — "
                                        "combine with --cooldown)")
    parser.add_argument("--pending", action="store_true",
                        help="only runs with undecided suggested updates (decide: scqo accept)")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--show", metavar="RUN_ID", help="print one run in full (record, params, figures)")
    parser.add_argument("--config", help="lab config path (default: $SCQO_CONFIG or ~/.scqo/config.toml)")
    args = parser.parse_args(argv)

    cfg = load_lab_config(args.config)
    if cfg.data_root is None:
        raise SystemExit(f"no data_root configured in {cfg.source or 'the lab config'} — nothing is being saved")
    store = DataStore(cfg.data_root, device_name=cfg.device or "device")

    if args.show:
        print(json.dumps(store.load_run(args.show), indent=2))
        return 0

    runs = store.find_runs(
        experiment=args.experiment, target=args.target, tag=args.tag, since=args.since,
        until=args.until, outcome=args.outcome, device=args.device,
        operator=args.operator, cooldown=args.cooldown, setup=args.setup,
        pending=True if args.pending else None, limit=args.limit,
    )
    if not runs:
        print("no runs match")
        return 0
    for r in runs:
        tags = ",".join(r["tags"]) if r["tags"] else "-"
        targets = ",".join(r.get("targets") or r.get("qubits") or [])
        operator = r.get("operator") or "-"
        pend = f"pend:{r['suggestions_pending']}" if r.get("suggestions_pending") else ""
        print(f"{r['run_id']:44s} {r['outcome']:10s} {targets:12s} {operator:10s} {tags:20s} {pend:8s} {r['path']}")
    print(f"\n({len(runs)} run(s); data_root = {store.data_root})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
