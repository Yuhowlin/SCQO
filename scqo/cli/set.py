"""Write a value directly — the runless recorded manual write for operator-known values.

    scqo set q1.readout_freq=5.912e9
    scqo set q1.pi_amp=0.2 q1.readout_power_dbm=-30 --yes

For values that come from EXPERIENCE, not from a measurement: there is no run to
credit, so nothing to suggest against (`scqo suggest` needs a run_id and stays
for figure-read values). `set` shows current -> new (with units) and asks once;
nothing here is pending — you are the reviewer. The write then goes through the
normal recorded path immediately: calibration knobs are pushed to the instrument
FIRST, every change lands in the history stamped with your OS login (shown as
`(manual)` by scqo state --sources), physical fields go to the sample ledger.

Values are plain numbers (5.912e9, 2.5e-05) in the field's OWN unit — the
confirmation table names it (`scqo state --fields` for the full catalog). Fields
may be calibration knobs (q1.readout_freq, ...) or physical parameters
(q1.t1_s, q1_res.f_r_hz, ...) — the owning store is routed by category. In scripts (no
terminal) the prompt cannot be asked: pass --yes to apply.
"""

from __future__ import annotations

import argparse
import json
import sys

from ._engine import _parse_value
from ._review import _confirm, _fmt_value


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("assignments", nargs="+", metavar="COMPONENT.FIELD=VALUE",
                        help="one or more values to write, e.g. q1.readout_freq=5.912e9")
    parser.add_argument("--yes", action="store_true",
                        help="write without the confirmation prompt (the script form)")
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

    from ._backends import build_session  # imports drivers: only real use pays

    sess, cfg = build_session(args.config)
    # Context header on stderr: say WHOSE device is about to change (stdout stays
    # parseable JSON). ASCII only: reaches consoles in whatever codepage the lab runs.
    if cfg.device:
        print(f"# device: {cfg.device}   setup: {sess.setup_name or '-'}   "
              f"cooldown: {sess.cooldown_id or '-'}", file=sys.stderr)
    else:
        print("# built-in demo device (nothing saved)", file=sys.stderr)

    try:
        plan = sess.set_values(assignments, dry_run=True)
    except ValueError as err:  # bad field/component/value — the message says the fix
        raise SystemExit(err.args[0] if err.args else str(err))

    print("will write:", file=sys.stderr)
    for item in plan["items"]:
        unit = f" {item['unit']}" if item["unit"] else ""
        print(f"  {item['component']:10} {item['field']:18} {item['store']:10} "
              f"{_fmt_value(item['current']):>14} -> {_fmt_value(item['after']):>14}{unit}",
              file=sys.stderr)

    if not args.yes:
        if not (sys.stdin.isatty() and sys.stderr.isatty()):
            print("not a terminal - nothing written; re-run with --yes to apply",
                  file=sys.stderr)
            return 1
        if not _confirm("write these values? [y/N]: "):
            print("nothing written - the device is unchanged", file=sys.stderr)
            return 0

    summary = sess.set_values(assignments)
    print(json.dumps(summary, indent=2))  # stdout stays parseable (| jq safe)
    for item in summary["applied"]:
        print(f"  applied  {item['component']}.{item['field']} "
              f"{_fmt_value(item['before'])} -> {_fmt_value(item['after'])}  [{item['store']}]",
              file=sys.stderr)
    for err in summary["errors"]:
        print(f"  ERROR    {err}", file=sys.stderr)
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
