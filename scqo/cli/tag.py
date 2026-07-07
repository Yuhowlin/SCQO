"""Tag or annotate an already-saved run (e.g. a week later, when it turns out to matter).

    scqo tag 20260704-153041-qubit_ramsey-01 --add thesis-fig3
    scqo tag 20260704-153041-qubit_ramsey-01 --remove mytest --note "best T2* so far"

Backend-free (touches only the datastore, like ``scqo find``). Tags live in the run's
record.json — the truth — so they survive any index rebuild.
"""

from __future__ import annotations

import argparse

from scqo import DataStore, load_lab_config


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_id", help="the run to tag (find it with `scqo find`)")
    parser.add_argument("--add", action="append", default=[], help="tag to add (repeatable)")
    parser.add_argument("--remove", action="append", default=[], help="tag to remove (repeatable)")
    parser.add_argument("--note", help="replace the run's note")
    parser.add_argument("--config", help="lab config path (default: $SCQO_CONFIG or ~/.scqo/config.toml)")
    args = parser.parse_args(argv)

    cfg = load_lab_config(args.config)
    if cfg.data_root is None:
        raise SystemExit(f"no data_root configured in {cfg.source or 'the lab config'} — nothing is saved")
    store = DataStore(cfg.data_root, device_name=cfg.device_name)

    record = store.tag_run(args.run_id, add=args.add, remove=args.remove, note=args.note)
    print(f"{args.run_id}")
    print(f"  tags: {', '.join(record['tags']) or '(none)'}")
    if record.get("note"):
        print(f"  note: {record['note']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
