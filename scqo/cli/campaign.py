"""Run an ordered bundle of experiments N times and report the statistics.

    scqo campaign t1_stability.toml               # run it
    scqo campaign t1_stability.toml --dry-run     # validate + show what would run
    scqo campaign --list                          # recent campaigns
    scqo campaign --show <campaign_id>            # plan + statistics + children

A campaign is OUTER repetition: every (repeat, step) is a separate saved run with
its own timestamp, so drift is visible rather than averaged away. The outer loop is
the repeat, so all of one repeat's quantities share a drift epoch.

To repeat ONE experiment, use ``scqo run <name> --repeat N`` — that is still running
one experiment. This command exists for the ordered bundle, which needs a plan file::

    label = "t1_stability"
    repeat = 100
    period_s = 300
    skip_artifacts = true

    [defaults]
    targets = ["q1"]

    [[steps]]
    experiment = "qubit_relaxation"
    [[steps]]
    experiment = "qubit_ramsey"
    params = { frequency_detuning_hz = 2.0e5 }
    [[steps]]
    experiment = "qubit_echo"
    [[steps]]
    experiment = "single_shot_readout"
    params = { num_shots = 4000 }
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scqo import DataStore, load_campaign_plan, load_lab_config

from ._backends import build_session
from ._campaign import (
    execute,
    format_statistics,
    format_summary,
    parameter_default_lines,
    plot_statistics,
)


def _refuse_experiment_name(plan_arg: str) -> None:
    """A campaign takes a plan file. Keep the one-way-to-run-one-experiment rule.

    Enforced in code, not by convention: without this, ``scqo campaign
    qubit_relaxation`` would be a second way to run a single experiment.
    """
    if Path(plan_arg).exists():
        return
    from scqo import catalog

    try:
        known = {entry["name"] for entry in catalog()}
    except Exception:  # a driver failed to import: fall back to the path check alone
        known = set()
    if plan_arg in known:
        raise SystemExit(
            f"a campaign takes a plan file, not an experiment name.\n"
            f"       to repeat one experiment:  scqo run {plan_arg} --repeat N")
    raise SystemExit(f"campaign plan not found: {plan_arg}")


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("plan", nargs="?", help="path to a campaign plan (TOML)")
    parser.add_argument("--dry-run", action="store_true",
                        help="validate the plan and print what would run; touch nothing")
    parser.add_argument("--list", action="store_true", dest="list_campaigns",
                        help="list recent campaigns instead of running one")
    parser.add_argument("--show", metavar="CAMPAIGN_ID",
                        help="print one campaign's plan, statistics and children")
    parser.add_argument("--plot", action="store_true",
                        help="with --show: re-render statistics.png (a finished "
                             "campaign already wrote one)")
    parser.add_argument("--recompute-readout", action="store_true", dest="refit",
                        help="with --show --plot: re-derive the single_shot Gaussian "
                             "populations from the saved dataset.nc, for campaigns run "
                             "before pop_e_prep_g was recorded; no instrument is touched")
    parser.add_argument("--repeat", type=int, metavar="N", help="override the plan's repeat count")
    parser.add_argument("--period", type=float, metavar="SECONDS",
                        help="override the plan's minimum seconds between repeat starts")
    parser.add_argument("--max-duration", type=float, metavar="SECONDS",
                        help="override the plan's wall-clock budget")
    parser.add_argument("--on-error", choices=["continue", "stop"],
                        help="override what a failed repeat does")
    parser.add_argument("--skip-artifacts", action="store_true",
                        help="write no analysis/ folder for the children")
    parser.add_argument("--tag", action="append", default=[], dest="tags",
                        help="searchable tag for every child run (repeatable)")
    parser.add_argument("--note", default="", help="free-text note stored with the campaign")
    parser.add_argument("--accept", action="store_true",
                        help="apply each repeat's updates (a re-tuning campaign: the device "
                             "MOVES between repeats, so the spread describes the loop)")
    parser.add_argument("--device", help="filter --list by device (sample) name")
    parser.add_argument("--limit", type=int, default=20, help="rows for --list")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="print the raw manifest instead of the table")
    parser.add_argument("--config", help="lab config path (default: $SCQO_CONFIG or ~/.scqo/config.toml)")
    args = parser.parse_args(argv)

    if args.list_campaigns or args.show:
        return _read_only(args)

    if not args.plan:
        parser.print_help()
        return 2
    _refuse_experiment_name(args.plan)
    plan = load_campaign_plan(args.plan)
    # Command-line overrides go back through the model, so the plan's validators
    # (a slug label, an open-ended campaign needing max_duration_s) still run.
    overrides = {"repeat": args.repeat, "period_s": args.period,
                 "max_duration_s": args.max_duration, "on_error": args.on_error}
    overrides = {k: v for k, v in overrides.items() if v is not None}
    if args.skip_artifacts:
        overrides["skip_artifacts"] = True
    if overrides:
        plan = type(plan)(**{**plan.model_dump(), **overrides})

    sess, cfg = build_session(args.config)
    if args.dry_run:
        return _dry_run(sess, plan, cfg)
    return execute(sess, plan, update="apply" if args.accept else "none",
                   tags=args.tags, note=args.note, as_json=args.as_json)


def _dry_run(sess, plan, cfg) -> int:
    """Resolve and validate the plan without creating anything.

    The whole point of a pre-flight: a typo found now instead of at 3 a.m. on
    repeat 100. Uses the same gate ``run_campaign`` runs before repeat 0.
    """
    print(f"# lab config: {cfg.source or 'built-in defaults (simulated, nothing saved)'}")
    print(f"label     {plan.label}")
    print(f"repeats   {plan.repeat if plan.repeat is not None else 'open-ended'}"
          f"   period_s: {plan.period_s}   max_duration_s: {plan.max_duration_s}")
    print(f"policy    on_error={plan.on_error}   "
          f"max_consecutive_failures={plan.max_consecutive_failures}   "
          f"skip_artifacts={plan.skip_artifacts}")
    for step_idx, step in enumerate(plan.steps):
        merged = {**sess.parameter_defaults.get(step.experiment, {}), **plan.step_params(step)}
        print(f"  step {step_idx}  {step.experiment:28s} {json.dumps(merged, sort_keys=True)}")
    # ...and say which of those the plan did NOT supply, so the resolved dict above
    # can be read as "plan" vs "lab file" rather than one undifferentiated blob.
    for line in parameter_default_lines(sess, plan):
        print(line)
    if plan.repeat is not None:
        total = plan.repeat * len(plan.steps)
        print(f"\nwould create {total} runs ({plan.repeat} repeats x {len(plan.steps)} steps)"
              + (f", about {plan.repeat * plan.period_s / 3600:.1f} h at the requested cadence"
                 if plan.period_s else ""))
    problems = sess.check_campaign(plan)
    if problems:
        print("\nREFUSED — fix these before running:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 2
    print("\nplan is valid (parameters build, every target is on the roster).")
    return 0


def _read_only(args) -> int:
    """``--list`` / ``--show``: query the index, touch no instrument."""
    cfg = load_lab_config(args.config)
    if cfg.data_root is None:
        raise SystemExit(
            f"no data_root configured in {cfg.source or 'the lab config'} — nothing is being saved")
    store = DataStore(cfg.data_root, device_name=cfg.device or "device")

    if args.show:
        loaded = store.load_campaign(args.show)
        manifest = loaded["manifest"]
        if args.as_json:
            print(json.dumps(loaded, indent=2))
            return 0
        print(format_summary({**manifest, "data_path": loaded["path"]}))
        print()
        print(format_statistics(manifest.get("statistics") or {}))
        if args.plot:
            plot_statistics(store, manifest, refit=args.refit)
        children = store.campaign_runs(args.show)
        print(f"\nchildren ({len(children)}, execution order):")
        for child in children:
            print(f"  r{child['repeat_idx']:<4d} s{child['step_idx']:<3d} "
                  f"{child['experiment']:26s} {child['outcome']:10s} {child['run_id']}")
        return 0

    campaigns = store.find_campaigns(device=args.device, limit=args.limit)
    if not campaigns:
        print("no campaigns yet")
        return 0
    for c in campaigns:
        planned = c["repeat_planned"]
        done = f"{c['repeat_done']}/{planned if planned is not None else '-'}"
        print(f"{c['campaign_id']:52s} {c['status']:9s} {done:>9s} "
              f"failed:{c['repeats_failed']:<4d} {','.join(c['experiments'])}")
    print(f"\n({len(campaigns)} campaign(s); data_root = {store.data_root})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
