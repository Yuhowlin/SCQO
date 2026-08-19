"""Shared CLI engine behind ``scqo run`` and the driver repos' launcher stubs.

``run_experiment_cli(None)`` = generic form (experiment name as positional argument);
``run_experiment_cli("qubit_ramsey")`` = fixed-experiment form used by the generated
launcher stubs, whose --help shows that experiment's own parameter schema.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

from ._backends import build_session, default_targets, ensure_demo_experiments
from ._review import format_table, review_interactively


def _parse_value(text: str):
    """Parse a --set value: JSON if it looks like it, bare string otherwise."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _check_preview_flags(args, name: str | None) -> None:
    """Refuse contradictory flag mixes BEFORE any session is built.

    --preview never runs, saves or updates, so every flag about running,
    saving or updating is a contradiction to name, not to ignore."""
    if not args.preview:
        raise SystemExit(
            "--out/--no-open/--simulate-ns/--no-simulate only apply with "
            "--preview")
    if args.simulate_ns is not None and args.no_simulate:
        raise SystemExit("--simulate-ns and --no-simulate contradict each "
                         "other; pick one")
    if not name:
        raise SystemExit(
            "--preview needs an experiment name: scqo run <name> --preview")
    conflicts = [flag for flag, on in [
        ("--repeat", args.repeat is not None),
        ("--period", args.period is not None),
        ("--max-duration", args.max_duration is not None),
        ("--skip-artifacts", args.skip_artifacts),
        ("--label", bool(args.label)),
        ("--accept", args.accept),
        ("--no-update", args.no_update),
        ("--tag", bool(args.tags)),
        ("--note", bool(args.note)),
    ] if on]
    if conflicts:
        raise SystemExit(
            f"--preview builds and renders only — it never runs, saves or "
            f"updates, so {', '.join(conflicts)} cannot apply here; drop "
            f"{'it' if len(conflicts) == 1 else 'them'} or drop --preview")


def _run_preview(sess, cfg, name, params, args, file_defaults) -> int:
    """The --preview branch: render to files, print JSON, auto-open."""
    from datetime import datetime
    from pathlib import Path

    applied = sorted(k for k in file_defaults if k not in params)
    if applied:  # stderr: stdout stays parseable JSON (| jq etc.)
        print(f"# parameter defaults from {cfg.parameters_source} [{name}]: "
              f"{', '.join(applied)}", file=sys.stderr)
    if args.out:
        out_dir = Path(args.out).expanduser().resolve()
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = Path.cwd() / "scqo_preview" / f"{name}_{stamp}"
        n = 2
        while out_dir.exists():  # same-second rerun
            out_dir = out_dir.with_name(f"{name}_{stamp}-{n}")
            n += 1
    print(f"# preview: building {name} — no hardware, nothing saved",
          file=sys.stderr)
    options: dict = {}
    if args.simulate_ns is not None:
        options["simulate_ns"] = args.simulate_ns
    if args.no_simulate:
        options["no_simulate"] = True
    result = sess.preview(name, params, out_dir=out_dir, options=options)
    print(json.dumps(result, indent=2))
    for warning in result.get("warnings", []):
        print(f"# preview warning: {warning}", file=sys.stderr)
    files = result.get("files", [])
    for f in files:
        print(f"# file: {f}", file=sys.stderr)
    if files and not args.no_open:
        if hasattr(os, "startfile"):  # Windows: default app per file type
            for f in files:
                try:
                    os.startfile(f)
                except OSError as err:
                    print(f"# could not open {f}: {err}", file=sys.stderr)
        else:
            print("# open the files above manually (auto-open is "
                  "Windows-only)", file=sys.stderr)
    return 1 if result.get("error") else 0


def _check_capability_flags(caps: list[str], name: str | None) -> None:
    """Refuse contradictory --capability mixes BEFORE any session is built.

    --capability filters the no-name catalog listing, so an experiment name is
    a contradiction to name, not to ignore; and 'none' means the empty set, so
    AND-ing it with a real capability can never match anything."""
    from scqo.experiments._capabilities import CAPABILITY_SUMMARIES

    if name:
        raise SystemExit(
            "--capability filters the catalog listing; drop the experiment "
            "name (run takes exactly one experiment) or drop --capability")
    valid = [*CAPABILITY_SUMMARIES, "none"]
    unknown = sorted(set(caps) - set(valid))
    if unknown:
        raise SystemExit(
            f"unknown capability: {', '.join(unknown)}; "
            f"pick from: {', '.join(valid)}")
    if "none" in caps and len(set(caps)) > 1:
        raise SystemExit(
            "--capability none means 'no capabilities at all'; it cannot "
            "combine with other capabilities")


def _columnize(cells: list[str], width: int) -> list[str]:
    """Column-major layout (like ls): the alphabetical catalog reads DOWN each
    column, so experiment families stay vertically adjacent."""
    if not cells:
        return []
    col_w = max(len(c) for c in cells) + 2
    ncols = max(1, width // col_w)
    nrows = -(-len(cells) // ncols)
    return ["".join(f"{c:<{col_w}}" for c in cells[row::nrows]).rstrip()
            for row in range(nrows)]


def _catalog_listing_lines(entries: list[dict], capabilities: list[str] | None = None,
                           width: int | None = None) -> list[str]:
    """The no-name `scqo run` listing: compact name columns + capability footer,
    or the --capability-filtered subset. Names only by design — descriptions
    live in `scqo run <name> --help` (per-name capability brackets overflowed
    every column; the footer, the filter and the --help epilog carry them now).
    Pure and renderer-free so tests read lines, not stdout."""
    from scqo.experiments._capabilities import CAPABILITY_SUMMARIES

    def cell(entry: dict) -> str:
        contrib = " [contrib]" if entry.get("maturity") == "contrib" else ""
        return entry["name"] + contrib

    if capabilities:
        wanted = list(dict.fromkeys(capabilities))
        # ASCII only, like every CLI meta line: Windows consoles mangle wider glyphs
        if wanted == ["none"]:
            matched = [e for e in entries if not e.get("capabilities")]
            what = "none - no capability mixin yet (legitimate for new experiments)"
        else:
            matched = [e for e in entries
                       if all(w in e.get("capabilities", []) for w in wanted)]
            what = " AND ".join(f"{w} - {CAPABILITY_SUMMARIES[w]}" for w in wanted)
        n = len(matched)
        header = f"# capability: {what}  [{n} experiment{'' if n == 1 else 's'}]"
        return [header, *(cell(e) for e in matched)]

    if width is None:
        width = shutil.get_terminal_size().columns
    lines = _columnize([cell(e) for e in entries], width)
    counts = dict.fromkeys(CAPABILITY_SUMMARIES, 0)
    none_count = 0
    for entry in entries:
        caps = entry.get("capabilities") or []
        none_count += not caps
        for cap in caps:
            counts[cap] = counts.get(cap, 0) + 1
    lines.append("# capabilities: "
                 + " ".join(f"{cap}({n})" for cap, n in counts.items())
                 + f" none({none_count})")
    lines.append("# filter: scqo run --capability <name>    "
                 "detail: scqo run <name> --help")
    return lines


def _schema_epilog(experiment: str, config_path: str | None) -> str:
    """Human-readable parameter list from the experiment's pydantic schema, with the
    lab's standing defaults (~/.scqo/parameters.toml) shown as the effective values."""
    from scqo import catalog, load_lab_config

    cfg = load_lab_config(config_path)
    ensure_demo_experiments()  # register-if-absent: driver-less --help still shows schemas
    entry = next((e for e in catalog() if e["name"] == experiment), None)
    if entry is None:
        return ""
    file_defaults = cfg.parameter_defaults.get(experiment, {})
    file_label = cfg.parameters_source.name if cfg.parameters_source else "parameters file"
    schema = entry["parameters_schema"]
    required = set(schema.get("required", []))
    lines = [entry["description"]]
    if entry.get("capabilities"):
        lines.append("capabilities: " + ", ".join(entry["capabilities"]))
    lines += ["", "parameters (set with --set KEY=VALUE):"]
    for key, spec in schema.get("properties", {}).items():
        if key in file_defaults:
            default = f"{file_defaults[key]!r} [{file_label}]"
        elif key in required:
            default = "(required)"
        else:
            default = repr(spec.get("default", ""))
        lines.append(f"  {key:26s} {spec.get('type', ''):8s} default={default:16s} {spec.get('description', '')}")
    return "\n".join(lines)


def run_experiment_cli(
    experiment: str | None = None,
    doc: str | None = None,
    argv: list[str] | None = None,
    prog: str | None = None,
) -> int:
    # --help prints during parsing, so the parameter epilog must be decided BEFORE the
    # real parse. In the generic form, peek at the command line for the experiment name
    # so `scqo run qubit_power_rabi --help` shows that experiment's parameters
    # — and peek --config too, so the epilog marks the standing defaults of the right lab.
    peek = argparse.ArgumentParser(add_help=False)
    peek.add_argument("--config")
    if experiment is None:
        peek.add_argument("experiment", nargs="?")
    peeked = peek.parse_known_args(argv)[0]
    help_target = experiment or getattr(peeked, "experiment", None)

    parser = argparse.ArgumentParser(
        prog=prog,
        description=doc or __doc__,
        epilog=_schema_epilog(help_target, peeked.config) if help_target else None,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    if experiment is None:
        parser.add_argument("experiment", nargs="?", help="experiment name; omit to list the catalog")
    parser.add_argument("--capability", action="append", default=[], metavar="NAME",
                        dest="capabilities",
                        help="filter the no-name catalog listing to experiments "
                             "carrying this capability (repeatable = AND; "
                             "'none' = experiments with no capabilities)")
    parser.add_argument("--params", help="parameters as a JSON file path or an inline JSON string")
    parser.add_argument("--targets", nargs="+",
                        help="components to measure (default: every qubit-like mode in the roster)")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="override one parameter (repeatable), e.g. --set num_points=201")
    parser.add_argument("--tag", action="append", default=[], dest="tags",
                        help="searchable tag for this run (repeatable)")
    parser.add_argument("--note", default="", help="free-text note stored with the run")
    update_group = parser.add_mutually_exclusive_group()
    update_group.add_argument("--accept", action="store_true",
                              help="apply all suggested updates immediately (unattended runs / AI loop)")
    update_group.add_argument("--no-update", action="store_true",
                              help="analyze only; do not even capture suggested updates")
    preview_group = parser.add_argument_group(
        "preview", "render the built sequence to files instead of running it")
    preview_group.add_argument("--preview", action="store_true",
                               help="build + compile only: render the vendor-native sequence "
                                    "(pulse diagram / QUA script) to files — no hardware, "
                                    "nothing saved, no updates")
    preview_group.add_argument("--out", metavar="DIR",
                               help="preview output directory "
                                    "(default: ./scqo_preview/<experiment>_<timestamp>/)")
    preview_group.add_argument("--no-open", action="store_true",
                               help="do not auto-open the rendered files")
    preview_group.add_argument("--simulate-ns", type=int, metavar="NS",
                               help="QM only: simulated-waveform window in ns "
                                    "(default 20000; the gateway simulator is "
                                    "tried automatically and skipped with a "
                                    "warning when unreachable)")
    preview_group.add_argument("--no-simulate", action="store_true",
                               help="QM: skip the gateway simulator — script "
                                    "dump only, guaranteed fully offline")
    # Repeating ONE experiment is still running one experiment, so it lives here
    # rather than in a wrapper. An ordered BUNDLE of experiments is a different
    # input and lives in `scqo campaign <plan.toml>`.
    repeat_group = parser.add_argument_group(
        "repeats", "run this experiment N times and report statistics over the fits "
                   "(a 1-step campaign: every repeat is its own saved run with its own "
                   "timestamp). For a bundle of different experiments: scqo campaign")
    repeat_group.add_argument("--repeat", type=int, metavar="N",
                              help="number of times to run it")
    repeat_group.add_argument("--period", type=float, metavar="SECONDS",
                              help="minimum wall-clock seconds between repeat STARTS; "
                                   "a repeat that overruns is recorded, never padded")
    repeat_group.add_argument("--max-duration", type=float, metavar="SECONDS",
                              help="stop repeating after this much wall clock")
    repeat_group.add_argument("--on-error", choices=["continue", "stop"], default="continue",
                              help="what a failed repeat does (default: continue)")
    repeat_group.add_argument("--skip-artifacts", action="store_true",
                              help="write no analysis/ folder (no figures, no scqat "
                                   "metadata or plotdata); dataset.nc and result.json still land")
    repeat_group.add_argument("--label", help="campaign label (default: the experiment name)")
    parser.add_argument("--config", help="lab config path (default: $SCQO_CONFIG or ~/.scqo/config.toml)")
    args = parser.parse_args(argv)
    name = experiment or args.experiment

    if (args.preview or args.out or args.no_open
            or args.simulate_ns is not None or args.no_simulate):
        _check_preview_flags(args, name)  # fail fast: no session needed
    if args.capabilities:
        _check_capability_flags(args.capabilities, name)  # fail fast too

    sess, cfg = build_session(args.config)

    if not name:
        print(f"# lab config: {cfg.source or 'built-in defaults (simulated, nothing saved)'}")
        print(f"# parameter defaults: {cfg.parameters_source or 'none (code defaults)'}")
        print(f"# user overlay: {cfg.user_source or 'none'}")
        for line in _catalog_listing_lines(sess.catalog(), args.capabilities):
            print(line)
        return 0

    params: dict = {}
    if args.params:
        try:
            if os.path.isfile(args.params):
                with open(args.params, encoding="utf-8") as f:
                    loaded = json.load(f)
            else:
                loaded = json.loads(args.params)
            if not isinstance(loaded, dict):
                raise SystemExit(f"--params must be a JSON object {{...}}, got {type(loaded).__name__}")
            params.update(loaded)
        except json.JSONDecodeError as err:
            msg = f"--params expects a JSON file path or inline JSON, got: {args.params!r} ({err})"
            if "=" in args.params and not args.params.lstrip().startswith("{"):
                msg += f"\nDid you mean:  --set {args.params}"
            else:
                msg += '\nExamples:  --params my_params.json   or   --params "{""num_points"": 201}"'
            raise SystemExit(msg)
    if args.targets:
        params["targets"] = args.targets
    for item in args.set:
        key, _, value = item.partition("=")
        params[key] = _parse_value(value)
    file_defaults = cfg.parameter_defaults.get(name, {})
    # All-device fallback only when NEITHER the command line nor the parameters file
    # names targets — a file-supplied target list must survive to the Session merge.
    if "targets" not in params and "targets" not in file_defaults:
        params["targets"] = default_targets(sess, name)

    # The preview branch before the campaign branch: both return early, and
    # --preview with any repeat flag was already refused above.
    if args.preview:
        return _run_preview(sess, cfg, name, params, args, file_defaults)

    mode = "apply" if args.accept else ("none" if args.no_update else "suggest")

    # The campaign branch first: `execute` prints the standing-defaults note in the
    # per-step shape it shares with `scqo campaign`, so this must not print it too.
    if args.repeat is not None or args.max_duration is not None:
        from scqo import CampaignPlan

        from ._campaign import execute

        # A repeated run is a 1-step campaign, so "N times" and "a bundle N times"
        # produce the same folders, the same stamps and the same statistics.
        # update defaults to "none" here for the same reason run_campaign does:
        # N pending suggestion sets are undecidable (accepting one staleness-blocks
        # the rest), so only an explicit --accept opts into moving the device.
        plan = CampaignPlan(
            label=args.label or name,
            steps=[{"experiment": name, "params": params}],
            repeat=args.repeat,
            period_s=args.period,
            max_duration_s=args.max_duration,
            on_error=args.on_error,
            skip_artifacts=args.skip_artifacts,
        )
        return execute(sess, plan, update="apply" if args.accept else "none",
                       tags=args.tags, note=args.note, as_json=False)

    applied = sorted(k for k in file_defaults if k not in params)
    if applied:  # stderr: stdout stays parseable JSON (| jq etc.)
        print(f"# parameter defaults from {cfg.parameters_source} [{name}]: {', '.join(applied)}", file=sys.stderr)
    result = sess.run(name, params, update=mode, tags=args.tags, note=args.note)
    print(json.dumps(result, indent=2))
    if "data_path" in result:
        print(f"\nsaved: {result['data_path']}")
    if mode == "suggest" and result.get("suggestions"):
        if "run_id" in result:
            review_interactively(sess, result["run_id"], result["suggestions"])
        elif result.get("datastore_error"):  # data_root IS configured; saving failed
            print("\nsuggested updates (saving the run FAILED — NOT stored, nothing to "
                  "accept later; see datastore_error above):", file=sys.stderr)
            print(format_table(result["suggestions"]), file=sys.stderr)
        else:  # no data_root: nothing stored, so there is no later — apply now or lose it
            print("\nsuggested updates (no data_root configured — NOT stored; "
                  "rerun with --accept to apply at run time):", file=sys.stderr)
            print(format_table(result["suggestions"]), file=sys.stderr)
    return 1 if result.get("error") else 0
