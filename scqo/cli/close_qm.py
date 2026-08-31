"""Halt running jobs and close open Quantum Machines on the QM OPX cluster.

    scqo close-qm               # halt all running jobs and close all open QMs on active setup
    scqo close-qm --qm-id QM-1  # close a specific QM instance
    scqo close_qm               # alias for close-qm

Connects to the QM cluster of the currently selected setup, halts/cancels any
active jobs to prevent blocking hardware resources, and closes open Quantum Machine sessions.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from ._backends import build_session


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--qm-id",
        metavar="ID",
        help="close a specific QM ID instead of all open Quantum Machines",
    )
    parser.add_argument(
        "--config",
        help="lab config path (default: $SCQO_CONFIG or ~/.scqo/config.toml)",
    )
    args = parser.parse_args(argv)

    sess, cfg = build_session(args.config)
    options: dict[str, Any] = {}
    if args.qm_id:
        options["qm_id"] = args.qm_id

    report = sess.close_qm(**options)
    success = report.get("success", True)
    open_qms = report.get("open_qms", [])
    halted_jobs = report.get("halted_jobs", [])
    closed_qms = report.get("closed_qms", [])
    errors = report.get("errors", [])
    msg = report.get("message")

    if msg:
        print(msg)
    elif not open_qms and not closed_qms and not halted_jobs:
        print("No open Quantum Machines found on the cluster. All sessions are closed.")
    else:
        print(f"QM status: {len(closed_qms)} Quantum Machine(s) closed, {len(halted_jobs)} active job(s) halted.")
        if open_qms:
            print(f"  open QMs detected: {', '.join(open_qms)}")
        if halted_jobs:
            print(f"  halted job(s):     {', '.join(halted_jobs)}")
        if closed_qms:
            print(f"  closed QM(s):      {', '.join(closed_qms)}")

    if errors:
        for err in errors:
            print(f"error: {err}", file=sys.stderr)
        return 1

    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())

