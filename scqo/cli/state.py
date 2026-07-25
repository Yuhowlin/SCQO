"""Show the device's current calibration state — and who changed what, when.

State is per SETUP: the first output line names the device, the resolved setup
and its state file, so two users of one sample know whose numbers they see.
Components are grouped by CATEGORY (the roster decides what each name is).

    scqo state                        # calibration tables per category (YOUR setup)
    scqo state --history              # last 20 changes (old -> new + cause + operator)
    scqo state --history 100 --entity q0
    scqo state --physical             # the sample ledger: one row per component/field
    scqo state --physical --history   # ... and its change history (rows carry setup=)
    scqo state --sources              # which run set each CURRENT value (both stores)
    scqo state --fields               # the field catalog per category + THIS backend's
                                      #   vendor bindings + its vendor-only inventory
                                      #   (--json for machines)
    scqo state --rule                 # the placement rule: which store owns which
                                      #   kind of value (no config or driver needed)
"""

from __future__ import annotations

import argparse

from ..report import design_rows, field_rows, state_rows
from ._backends import build_session

#: The placement rule, bench form (full version: TUTORIAL.md "Where does a value
#: live?"). ASCII only: reaches consoles in whatever codepage the lab runs.
_RULE = """\
Where does a value live? (the placement rule - full version: TUTORIAL.md)

A field lives on the COMPONENT whose category declares it (components.toml is
the roster; q1 = transmon, q1_res = its resonator, q1_ro/q1_xy/q1_z = the
interaction terms). Classify each USE of a quantity; ask in order, first match wins:
 1. Gone when the run ends? (sweep windows, shot counts, analysis assumptions,
    Optional-None overrides)          -> per-run experiment Parameters
 2. True of the chip in the dark - no instrument SETTING realizes it?
    (T1, f_r, EJ; setup-plane coordinates OK when declared, e.g. v_per_phi0_v)
                                      -> the PHYSICAL category -> physical.json
                                         write: suggest -> accept, or scqo set
 3. Measured, but a vendor knob realizes the result? (time of flight)
                                      -> write the vendor knob itself: offline,
                                         in the catalog row's unit (--fields)
 4. A knob the calibration loop reads/writes vendor-neutrally - the same
    signal on every backend?          -> the INSTRUMENT category -> scqo_state.json
      absolute at a declared plane      -> portable=True  (Hz, dBm at port, s)
      fraction of an untracked chain    -> portable=False (twin or catalogued
                                           scale); write: suggest / scqo set
 5. Measured, no knob:
      consulted as standing state before the next step?
                                      -> instrument push=False (readout_fidelity)
      only compared across runs?      -> run record only (p_e_given_g)
 6. Everything else is the instrument's -> vendor config; catalogued when:
      [realizer]  realizes a neutral field - change THAT field via scqo set
      [candidate] shared concept awaiting promotion (the visible backlog)
      [vendor]    permanently vendor-owned (reason stated in the entry)
      [unique]    THIS backend only - experiments touching it run ONLY here
DESIGN values (declared chip targets) live in components.toml, device-level.

The unit you type is ALWAYS the catalog row's unit, never assumed (ns vs s!).
Chain solves are deterministic: the coarse knob is quantized, the amplitude
stays <= 0.5 full scale and absorbs the exact residual - same target, same
split, recorded in power_context every run."""


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--history", nargs="?", const=20, type=int, metavar="N",
                        help="show the last N recorded changes instead (default N=20)")
    parser.add_argument("--entity", help="restrict output to one component")
    parser.add_argument("--physical", action="store_true",
                        help="the sample's measured physical parameters instead of the instrument config")
    parser.add_argument("--sources", action="store_true",
                        help="where each current value came from: source run / (manual) / (externally changed)")
    parser.add_argument("--fields", action="store_true",
                        help="the field catalog per category + this backend's "
                             "vendor bindings and vendor-only parameters")
    parser.add_argument("--rule", action="store_true",
                        help="print the placement rule (which store owns which kind of value)")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="with --fields: machine-readable JSON instead of the table")
    parser.add_argument("--config", help="lab config path (default: $SCQO_CONFIG or ~/.scqo/config.toml)")
    args = parser.parse_args(argv)
    if args.rule and (args.history is not None or args.physical or args.sources
                      or args.entity or args.fields or args.as_json):
        parser.error("--rule prints the placement rule and combines with nothing")
    if args.sources and (args.history is not None or args.physical):
        parser.error("--sources always covers both stores; do not combine it with --history/--physical")
    if args.fields and (args.history is not None or args.physical or args.sources or args.entity):
        parser.error("--fields is a schema view (no per-component values); do not combine "
                     "it with --history/--physical/--sources/--entity")
    if args.as_json and not args.fields:
        parser.error("--json applies to --fields only")

    if args.rule:  # static text: no config, no driver, no session — works anywhere
        print(_RULE)
        return 0

    sess, cfg = build_session(args.config)
    if args.fields:  # before the context header: --json stdout must stay pure JSON
        return _print_fields(sess, cfg, as_json=args.as_json)
    _print_context(sess, cfg)

    if args.sources:
        return _print_sources(sess, args.entity)

    if args.history is None:
        if args.physical:
            return _print_physical(sess, args.entity)
        return _print_state(sess, args.entity)

    records = sess.history(store="physical" if args.physical else "state")
    if args.entity:
        records = [r for r in records if r['entity'] == args.entity]
    for r in records[-args.history:]:
        old = f"{r['old']:.6g}" if isinstance(r["old"], float) else r["old"]
        new = f"{r['new']:.6g}" if isinstance(r["new"], float) else r["new"]
        setup = f"  setup={r['setup']}" if r.get("setup") else ""
        print(f"{r['timestamp'][:19]}  {r['entity']:8s} {r['field']:16s} {old} -> {new}"
              f"  ({r.get('experiment') or '?'}  run={r.get('run_id') or '-'}"
              f"  by={r.get('operator') or '-'}{setup})")
    if not records:
        print("no recorded changes yet")
    return 0


def _print_context(sess, cfg) -> None:
    """One `#` line saying WHOSE state/history follows — state is per SETUP,
    so two users of one sample see different tables."""
    if not cfg.device:
        print("# built-in demo device (nothing saved)")
        return
    print(f"# device: {cfg.device}   setup: {sess.setup_name or '-'}   "
          f"cooldown: {sess.cooldown_id or '-'}   scqo dir: {sess.scqo_dir or '-'}")


def _print_state(sess, entity_filter: str | None) -> int:
    """The operating tables, ONE PER ENTITY KIND present — columns in the
    kind catalog's declaration order."""
    rows = [r for r in state_rows(sess.roster, sess.device_state(),
                                  sess.physical_state())
            if r["store"] == "scqo_state.json"
            and (not entity_filter or r["entity"] == entity_filter)]
    by_kind: dict[str, list[dict]] = {}
    for r in rows:
        by_kind.setdefault(r["kind"], []).append(r)
    for kind in sorted(by_kind):
        group = by_kind[kind]
        names = sorted({r["entity"] for r in group})
        fields: list[str] = []
        for r in group:  # catalog order: state_rows preserves it per entity
            if r["field"] not in fields:
                fields.append(r["field"])
        values = {(r["entity"], r["field"]): r["value"] for r in group}
        print(f"# {kind}")
        print(f"{'entity':12s}" + "".join(f"{f:>20s}" for f in fields))
        for name in names:
            row = "".join(
                f"{values.get((name, f)):>20.6g}"
                if isinstance(values.get((name, f)), float)
                else f"{str(values.get((name, f))):>20s}"
                for f in fields)
            print(f"{name:12s}{row}")
    if not by_kind:
        print("no operating state (entity filter matched nothing?)")
    return 0


def _fields_payload(sess, cfg) -> dict:
    """The field catalog per entity KIND + the session backend's declared
    vendor bindings + its vendor-only inventory. ``missing_bindings`` lists
    ``kind.field`` pairs the backend neither binds nor declares Unrealized —
    PER KIND, and only for kinds the backend declares at all: a wholly
    absent kind (pump channels on a backend with no parametric support) is
    capability, not drift; its experiments are roster-refused pre-probe."""
    from dataclasses import asdict

    from ..catalog import CHANNELS, COMPOSITES, MODES

    bindings = sess.backend.field_bindings()
    unrealized = sess.backend.unrealized()
    kinds = []
    missing: list[str] = []
    for family, catalog in (("mode", MODES), ("composite", COMPOSITES),
                            ("channel", CHANNELS)):
        for kind, spec in catalog.items():
            fields = []
            declared = kind in bindings or kind in unrealized
            for fname, fs in spec.fields.items():
                b = bindings.get(kind, {}).get(fname)
                u = unrealized.get(kind, {}).get(fname)
                fields.append({
                    "name": fname, "unit": fs.unit, "role": fs.role,
                    "portable": fs.portable, "shape": fs.shape,
                    "design_ok": fs.design_ok,
                    "binding": asdict(b) if b is not None else None,
                    "unrealized": asdict(u) if u is not None else None,
                })
                if declared and fs.role == "knob" and b is None and u is None:
                    missing.append(f"{kind}.{fname}")
            kinds.append({
                "kind": kind, "family": family, "doc": spec.doc,
                "roles": sorted(getattr(spec, "roles", None)
                                or getattr(spec, "refs", None) or {}),
                "fields": fields,
            })
    return {
        "device": cfg.device or None,
        "setup": sess.setup_name or None,
        "cooldown": sess.cooldown_id or None,
        "backend": sess.backend_label,
        "kinds": kinds,
        "vendor_only": [
            {"name": name, **asdict(v)}
            for name, v in sess.backend.vendor_only().items()
        ],
        "missing_bindings": missing,
    }


def _print_fields(sess, cfg, *, as_json: bool) -> int:
    """The field catalog view, one section per category. Values are elsewhere
    (`scqo state`); this is schema + where the SELECTED backend realizes each
    instrument field + the backend-unique untracked inventory."""
    import json

    payload = _fields_payload(sess, cfg)
    if as_json:
        print(json.dumps(payload, indent=2))  # pure JSON: stdout stays | jq safe
        return 0
    _print_context(sess, cfg)
    print(f"# backend: {payload['backend']}   (bindings are declared metadata; the "
          f"executable conversion lives in the driver's views)")
    indent = 20 + 6 + 13 + 10
    for cat in payload["categories"]:
        ops = f"   operations: {', '.join(cat['operations'])}" if cat["operations"] else ""
        print(f"\n# {cat['category']} ({cat['side']}/{cat['kind']}){ops}")
        print(f"{'field':20s}{'unit':6s}{'kind':13s}{'portable':10s}"
              f"vendor binding ({payload['backend']})")
        for f in cat["fields"]:
            b = f["binding"]
            if b:
                bound = f"{b['path']} [{b['unit']}]" if b["unit"] else b["path"]
            elif f["unrealized"]:
                bound = f"(unrealized here: {f['unrealized']['reason']})"
            else:
                bound = "-"
            # portable "NO" is upper-case on purpose: it is the value you must
            # NOT copy to another backend's config.
            print(f"{f['name']:20s}{f['unit'] or '-':6s}{f['role']:13s}"
                  f"{'yes' if f['portable'] else 'NO':10s}{bound}")
            if b:
                for label, text in (("convert", b["convert"]),
                                    ("coupled", ", ".join(b["coupled"])),
                                    ("note", b["note"])):
                    if text:
                        print(f"{'':{indent}s}  {label}: {text}")
    shared = [v for v in payload["vendor_only"] if v["kind"] != "unique"]
    unique = [v for v in payload["vendor_only"] if v["kind"] == "unique"]
    if shared:
        print(f"\n# {payload['backend']}-only parameters (vendor config, untracked by SCQO):")
        for v in shared:
            print(f"{v['name']:28s} {v['unit'] or '-':8s}[{v['kind']}] {v['path']}")
            print(f"{'':38s}{v['doc']}")
    if unique:
        # the lock-in corollary: an experiment touching one of these cannot run
        # on the other backend (the concept does not exist there)
        print(f"\n# instrument-UNIQUE parameters - experiments touching these run "
              f"ONLY on {payload['backend']}:")
        for v in unique:
            print(f"{v['name']:28s} {v['unit'] or '-':8s}{v['path']}")
            print(f"{'':38s}{v['doc']}")
    if payload["missing_bindings"]:
        print("\n# WARN: pushed field(s) neither bound nor declared unrealized here: "
              + ", ".join(payload["missing_bindings"]))
    elif not any(f["binding"] for c in payload["kinds"] for f in c["fields"]):
        print("\n# this backend declares no field bindings (simulated, or a pre-catalog driver)")
    print("\n# placement rule: scqo state --rule   (full text: TUTORIAL.md "
          "'Where does a value live?')")
    return 0


def _print_physical(sess, entity_filter: str | None) -> int:
    """This context's measured physics (one (cooldown, setup) file), with each
    entity's KIND shown. Compare across contexts via the run index or the
    viewer trends page, not here."""
    rows = [r for r in state_rows(sess.roster, {}, sess.physical_state())
            if not entity_filter or r["entity"] == entity_filter]
    if not rows:
        if entity_filter and sess.physical_state():
            print(f"no physical parameters recorded for entity {entity_filter!r}")
        else:
            print("no physical parameters recorded yet (accept a run that "
                  "proposes them)")
        return 0
    print(f"{'entity':12s}{'kind':18s}{'field':20s}{'value':>16s}{'  unit'}")
    for r in rows:
        value = (f"{r['value']:>16.6g}" if isinstance(r["value"], float)
                 else f"{str(r['value']):>16s}")
        print(f"{r['entity']:12s}{r['kind']:18s}{r['field']:20s}{value}"
              f"  {r['unit']}")
    return 0


def _print_design(sess, entity_filter: str | None) -> int:
    """The design-vs-measured column: declared targets joined key-for-key
    against this context's measured facts."""
    rows = [r for r in design_rows(sess.roster, sess.design,
                                   sess.physical_state())
            if not entity_filter or r["entity"] == entity_filter]
    if not rows:
        print("no design targets declared (design.toml)")
        return 0
    print(f"{'entity':12s}{'field':20s}{'designed':>16s}{'measured':>16s}"
          f"{'delta':>16s}  unit")
    for r in rows:
        measured = ("-" if r["measured"] is None
                    else f"{r['measured']:.6g}")
        delta = "-" if r["delta"] is None else f"{r['delta']:+.3g}"
        print(f"{r['entity']:12s}{r['field']:20s}{r['designed']:>16.6g}"
              f"{measured:>16s}{delta:>16s}  {r['unit']}")
    return 0


def _print_sources(sess, entity_filter: str | None) -> int:
    """One provenance table over BOTH stores: which run set each current value."""
    from ..report import live_sources

    sources = {
        "state": live_sources(sess.device_state(), sess.history()),
        "physical": live_sources(sess.physical_state(),
                                 sess.history(store="physical")),
    }
    rows = [
        dict(info, role=store)
        for store in ("state", "physical")
        for name, fields in sorted(sources[store].items())
        for info in (fields[f] for f in sorted(fields))
        if not entity_filter or info["entity"] == entity_filter
    ]
    if not rows:
        print("no values yet")
        return 0
    print(f"{'entity':10s} {'field':18s} {'role':10s} {'current':>14s}  "
          f"{'source':46s} {'when':19s} {'by'}")
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
        print(f"{info['entity']:10s} {info['field']:18s} {info['role']:10s} {value:>14s}  "
              f"{source:46s} {when:19s} {info['operator'] or '-'}")
    if externals:  # ASCII only: reaches consoles in whatever codepage the lab runs
        print("# (externally changed) = the current value matches no SCQO record - "
              "reseeded by the vendor or written by another tool")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
