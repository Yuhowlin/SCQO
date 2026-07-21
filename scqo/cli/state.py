"""Show the device's current calibration state — and who changed what, when.

State is per SETUP: the first output line names the device, the resolved setup
and its state file, so two users of one sample know whose numbers they see.
Components are grouped by CATEGORY (the roster decides what each name is).

    scqo state                        # calibration tables per category (YOUR setup)
    scqo state --history              # last 20 changes (old -> new + cause + operator)
    scqo state --history 100 --component q0
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

from ..categories import CATEGORIES
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
    parser.add_argument("--component", help="restrict output to one component")
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
                      or args.component or args.fields or args.as_json):
        parser.error("--rule prints the placement rule and combines with nothing")
    if args.sources and (args.history is not None or args.physical):
        parser.error("--sources always covers both stores; do not combine it with --history/--physical")
    if args.fields and (args.history is not None or args.physical or args.sources or args.component):
        parser.error("--fields is a schema view (no per-component values); do not combine "
                     "it with --history/--physical/--sources/--component")
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
        return _print_sources(sess, args.component)

    if args.history is None:
        if args.physical:
            return _print_physical(sess, args.component)
        return _print_state(sess, args.component)

    records = sess.history(store="physical" if args.physical else "instrument")
    if args.component:
        records = [r for r in records if r["component"] == args.component]
    for r in records[-args.history:]:
        old = f"{r['old']:.6g}" if isinstance(r["old"], float) else r["old"]
        new = f"{r['new']:.6g}" if isinstance(r["new"], float) else r["new"]
        setup = f"  setup={r['setup']}" if r.get("setup") else ""
        print(f"{r['timestamp'][:19]}  {r['component']:8s} {r['field']:16s} {old} -> {new}"
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
          f"cooldown: {sess.cooldown_id or '-'}   state: {sess.state_path or '-'}")


def _print_state(sess, component_filter: str | None) -> int:
    """The calibration tables, ONE PER INSTRUMENT CATEGORY present — columns in
    the category's declaration order (no sparse union across categories)."""
    state = sess.device_state()
    by_cat: dict[str, list[str]] = {}
    for name in state:
        _phys, instr = (sess.roster.category(name) if name in sess.roster
                        else (None, None))
        by_cat.setdefault(instr or "?", []).append(name)
    printed = False
    for cat in sorted(by_cat):
        names = [n for n in by_cat[cat]
                 if not component_filter or n == component_filter]
        if not names:
            continue
        fields = (list(CATEGORIES[cat].fields) if cat in CATEGORIES
                  else sorted({f for n in names for f in state[n]}))
        fields = [f for f in fields
                  if any(f in state[n] for n in names)]  # requires_physical pruning
        print(f"# {cat}")
        print(f"{'component':10s}" + "".join(f"{f:>18s}" for f in fields))
        for name in names:
            values = state[name]
            row = "".join(
                f"{values.get(f):>18.6g}" if isinstance(values.get(f), float)
                else f"{str(values.get(f)):>18s}"
                for f in fields
            )
            print(f"{name:10s}{row}")
        printed = True
    if not printed:
        print("no instrument state (component filter matched nothing?)")
    return 0


def _fields_payload(sess, cfg) -> dict:
    """The field catalog per CATEGORY + the session backend's declared vendor
    bindings (category-keyed since the component cutover) + its vendor-only
    inventory. ``missing_bindings`` lists (category, field) pairs the backend
    neither binds nor declares Unrealized — PER CATEGORY, and only for
    categories the backend declares at all: a wholly absent category (e.g.
    TransmonPair on a backend with no pair support) is capability, not drift —
    its experiments are roster-refused pre-probe, never silently unbound."""
    from dataclasses import asdict

    bindings = sess.backend.field_bindings()
    unrealized = sess.backend.unrealized()
    categories = []
    missing: list[str] = []
    for cat, spec in CATEGORIES.items():
        fields = []
        for fname, fs in spec.fields.items():
            kind = ("physical" if spec.side == "physical"
                    else "pushed" if fs.push else "record-only")
            b = bindings.get(cat, {}).get(fname)
            u = unrealized.get(cat, {}).get(fname)
            fields.append({
                "name": fname, "unit": fs.unit, "kind": kind, "portable": fs.portable,
                "binding": asdict(b) if b is not None else None,
                "unrealized": asdict(u) if u is not None else None,
            })
            declared = cat in bindings or cat in unrealized
            if (declared and spec.side == "instrument" and fs.push
                    and b is None and u is None):
                missing.append(f"{cat}.{fname}")
        categories.append({
            "category": cat, "side": spec.side, "kind": spec.kind,
            "doc": spec.doc, "operations": list(spec.operations),
            "fields": fields,
        })
    return {
        "device": cfg.device or None,
        "setup": sess.setup_name or None,
        "cooldown": sess.cooldown_id or None,
        "backend": sess.backend_label,
        "categories": categories,
        "vendor_only": [
            {"name": name, **asdict(v)} for name, v in sess.backend.vendor_only().items()
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
            print(f"{f['name']:20s}{f['unit'] or '-':6s}{f['kind']:13s}"
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
    elif not any(f["binding"] for c in payload["categories"] for f in c["fields"]):
        print("\n# this backend declares no field bindings (simulated, or a pre-catalog driver)")
    print("\n# placement rule: scqo state --rule   (full text: TUTORIAL.md "
          "'Where does a value live?')")
    return 0


def _catalog_field_order() -> dict[str, int]:
    order: dict[str, int] = {}
    for spec in CATEGORIES.values():
        for f in spec.fields:
            order.setdefault(f, len(order))
    return order


def _print_physical(sess, component_filter: str | None) -> int:
    """This context's measured physics (one (cooldown, setup) file), with each
    component's category shown. Compare across setups/cooldowns via the run
    index or the viewer trends page, not here."""
    values = sess.physical_state()  # flat {component: {field: value}}
    order = _catalog_field_order()
    rows = sorted(
        ((n, f, v)
         for n, fields in values.items()
         for f, v in fields.items()
         if not component_filter or n == component_filter),
        key=lambda r: (r[0], order.get(r[1], 999)),
    )
    if not rows:
        if component_filter and values:
            print(f"no physical parameters recorded for component {component_filter!r}")
        else:
            print("no physical parameters recorded yet (accept a run that proposes them)")
        return 0
    print(f"{'component':10s}{'category':22s}{'field':18s}{'value':>14s}")
    for n, f, v in rows:
        phys = sess.roster.category(n)[0] if n in sess.roster else "?"
        value = f"{v:>14.6g}" if isinstance(v, float) else f"{str(v):>14s}"
        print(f"{n:10s}{phys or '-':22s}{f:18s}{value}")
    return 0


def _print_sources(sess, component_filter: str | None) -> int:
    """One provenance table over BOTH stores: which run set each current value."""
    sources = sess.live_sources()
    order = _catalog_field_order()
    rows = [
        info
        for store in ("instrument", "physical")
        for name, fields in sorted(sources[store].items())
        for info in (dict(fields[f], store=store)
                     for f in sorted(fields, key=lambda f: order.get(f, 999)))
        if not component_filter or info["component"] == component_filter
    ]
    if not rows:
        print("no values yet")
        return 0
    print(f"{'component':10s} {'field':18s} {'store':10s} {'current':>14s}  "
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
        print(f"{info['component']:10s} {info['field']:18s} {info['store']:10s} {value:>14s}  "
              f"{source:46s} {when:19s} {info['operator'] or '-'}")
    if externals:  # ASCII only: reaches consoles in whatever codepage the lab runs
        print("# (externally changed) = the current value matches no SCQO record - "
              "reseeded by the vendor or written by another tool")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
