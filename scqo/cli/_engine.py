"""Shared CLI engine behind ``scqo run`` and the driver repos' launcher stubs.

``run_experiment_cli(None)`` = generic form (experiment name as positional argument);
``run_experiment_cli("qubit_ramsey")`` = fixed-experiment form used by the generated
launcher stubs, whose --help shows that experiment's own parameter schema.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from ._backends import build_session, default_targets, ensure_demo_experiments
from ._review import format_table, review_interactively


def _parse_value(text: str):
    """Parse a --set value: JSON if it looks like it, bare string otherwise."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


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
    lines = [entry["description"], "", "parameters (set with --set KEY=VALUE):"]
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
    parser.add_argument("--params", help="parameters as a JSON file path or an inline JSON string")
    parser.add_argument("--targets", nargs="+",
                        help="components to measure (default: every ReadableTransmon in the roster)")
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
    parser.add_argument("--config", help="lab config path (default: $SCQO_CONFIG or ~/.scqo/config.toml)")
    args = parser.parse_args(argv)
    name = experiment or args.experiment

    sess, cfg = build_session(args.config)

    if not name:
        print(f"# lab config: {cfg.source or 'built-in defaults (simulated, nothing saved)'}")
        print(f"# parameter defaults: {cfg.parameters_source or 'none (code defaults)'}")
        print(f"# user overlay: {cfg.user_source or 'none'}")
        for entry in sess.catalog():
            tag = " [contrib]" if entry.get("maturity") == "contrib" else ""
            print(f"{entry['name'] + tag:32s} {entry['description']}")
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

    applied = sorted(k for k in file_defaults if k not in params)
    if applied:  # stderr: stdout stays parseable JSON (| jq etc.)
        print(f"# parameter defaults from {cfg.parameters_source} [{name}]: {', '.join(applied)}", file=sys.stderr)
    mode = "apply" if args.accept else ("none" if args.no_update else "suggest")
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
