"""Attach YOUR manually-read value to a saved run — when the fit failed but the figure didn't.

    scqo suggest RUN_ID q1.readout_freq=5.912e9
    scqo suggest RUN_ID q1_res.f_r_hz=5.912e9 q1_res.kappa_tot_hz=1.1e6 --comment "read off the dip"

An estimator sometimes fails on data whose figure shows the answer plainly (a
clearly visible dip the fit chased past). Read the value off the run's saved
figure and attach it HERE — to that run — instead of writing it into the device
directly: the proposal lands on the run record as a pending suggestion
(origin: operator, stamped with your OS login) and applying goes through the
normal accept flow with its era + staleness guards, so the applied value stays
credited to the run whose data justified it. At a terminal the selection prompt
follows immediately (Enter = decide later); in scripts, decide with:
scqo accept RUN_ID.

Values are plain numbers (5.912e9, 2.5e-05). Fields may be calibration knobs
(q1.readout_freq, q1.pi_amp, ...) or physical parameters (q1.t1_s,
q1_res.f_r_hz, ...) — the owning store is routed by the component's category. Needs a persisted run: a run that failed
before it was saved has no run_id to attach to. A value with NO run behind it
(experience, not a figure) is `scqo set` instead — it writes immediately,
recorded as (manual).
"""

from __future__ import annotations

import argparse
import json
import sys

from ._engine import _parse_value


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_id", help="the run whose figure/data justifies the value")
    parser.add_argument("assignments", nargs="+", metavar="COMPONENT.FIELD=VALUE",
                        help="one or more values to propose, e.g. q1.readout_freq=5.912e9")
    parser.add_argument("--comment", default="",
                        help="why the manual value, e.g. 'estimator missed the dip'")
    parser.add_argument("--config", help="lab config path (default: $SCQO_CONFIG or ~/.scqo/config.toml)")
    args = parser.parse_args(argv)

    assignments: dict[str, object] = {}
    for token in args.assignments:
        key, sep, raw = token.partition("=")
        if not sep or not key or not raw:
            raise SystemExit(
                f"bad assignment {token!r} - expected COMPONENT.FIELD=VALUE (e.g. q1.readout_freq=5.912e9)"
            )
        assignments[key] = _parse_value(raw)

    # The live device is needed (the current value becomes each item's `before`,
    # the baseline the accept-time staleness guard compares against).
    from ._backends import build_session  # imports drivers: only real use pays

    sess, _ = build_session(args.config)
    try:
        summary = sess.suggest(args.run_id, assignments, comment=args.comment)
        record = sess.load_run(args.run_id)["record"]
    # bad field/qubit/value, another device's run, unknown run_id — the message
    # says the fix; never show a traceback for these.
    except (RuntimeError, KeyError, ValueError) as err:
        raise SystemExit(err.args[0] if err.args else str(err))
    print(json.dumps(summary, indent=2))  # stdout stays parseable (| jq safe)

    # Same tail as `scqo run`: table + selection prompt at a terminal (Enter =
    # decide later), table + decide-later hint when scripted.
    from ._review import review_interactively

    applied = review_interactively(sess, args.run_id, record.get("suggestions", []))
    if applied is None:
        return 0
    return 1 if applied["errors"] or applied["stale"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
