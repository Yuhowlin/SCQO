"""Report data behind ``scqo state`` / ``scqo device`` — renderer-free.

Every function returns plain JSON-able rows so the CLI is a table printer,
the viewer reads the same shapes, and an AI loop can consume them directly.
Nothing here formats, sorts for looks, or truncates.
"""

from __future__ import annotations

from typing import Any

from .catalog import CHANNELS, COMPOSITES, MODES
from .design import Design, seed_anchor
from .entities import Channel, Composite, Line, Mode
from .roster import Roster


def catalog_fields(*roles: str) -> list[str]:
    """Catalog field names of the given roles, in declaration order (deduped
    across kinds)."""
    out: list[str] = []
    for kinds in (MODES, COMPOSITES, CHANNELS):
        for spec in kinds.values():
            for f, fs in spec.fields.items():
                if fs.role in roles and f not in out:
                    out.append(f)
    return out


PHYSICAL_FIELD_ORDER = catalog_fields("fact")
INSTRUMENT_FIELD_ORDER = catalog_fields("knob", "monitor")

#: Quantities never tracked as device state (instrument-dependent; a recorded
#: decision), but still worth reporting from a run's fit.
FIT_ONLY_QUANTITIES = ("p_e_given_g",)

#: Everything worth OFFERING as a trend: measured physics, then monitors, then
#: calibration knobs. Derived from the kind catalogs by field ROLE, never a
#: hand-kept list — an experiment writing catalogued fields is covered for free
#: and a quantity cannot rot out of the set. Consumer: the viewer's /trends menu,
#: where trending a knob over time is a legitimate question.
REPORTABLE_QUANTITIES = (
    *PHYSICAL_FIELD_ORDER,
    *catalog_fields("monitor"),
    *catalog_fields("knob"),
    *FIT_ONLY_QUANTITIES,
)

#: The subset that can actually MOVE on its own: facts and monitors the fit
#: measured, never knobs. A knob appearing in a fit dict is the standing value the
#: run was configured with, so in an update="none" campaign it is constant by
#: construction — printing it on a live progress line spends width on a number
#: that cannot drift. Consumer: the campaign progress line.
MEASURED_QUANTITIES = (
    *PHYSICAL_FIELD_ORDER,
    *catalog_fields("monitor"),
    *FIT_ONLY_QUANTITIES,
)


def _origin(entity) -> str:
    return str(entity.derived) if entity.derived is not None else "declared"


def expansion_rows(roster: Roster) -> list[dict[str, Any]]:
    """The EXPANDED roster — what ``scqo device`` prints so a reader sees the
    minted names (and where each came from) that the file does not list."""
    rows = []
    for name, e in sorted(roster.entities.items()):
        row: dict[str, Any] = {
            "entity": name,
            "section": type(e).__name__.lower() + "s",
            "kind": e.kind,
            "origin": _origin(e),
            "retired": e.retired,
        }
        if isinstance(e, Mode):
            row["refs"] = dict(e.refs)
            row["operations"] = list(roster.operations(name))
        elif isinstance(e, Composite):
            row["roles"] = {r: list(v) for r, v in e.roles.items()}
            row["operations"] = list(e.operations)
        elif isinstance(e, Channel):
            row["target"] = list(e.target)
            row["line"] = e.line
            row["via"] = e.via
        elif isinstance(e, Line):
            row["carries"] = sorted(c.name for c in roster.channels().values()
                                    if c.line == name)
        rows.append(row)
    return rows


def field_rows(roster: Roster) -> list[dict[str, Any]]:
    """The catalog behind ``scqo state --fields``: every legal (entity,
    field) with its routing, unit, and design/seed story."""
    rows = []
    for name in sorted(roster.entities):
        e = roster.entities[name]
        legal = roster._legal[name]  # compiled set incl. why-legal provenance
        for field, lf in sorted(legal.items()):
            spec = lf.spec
            seed = None
            if isinstance(e, Channel):
                resolved = seed_anchor(roster, name, field)
                if resolved is not None:
                    anchor, facts = resolved
                    seed = " | ".join(f"{anchor}.{f}" for f in facts)
            rows.append({
                "entity": name,
                "kind": e.kind,
                "field": field,
                "role": spec.role,
                "store": ("physical.json" if spec.role == "fact"
                          else "scqo_state.json"),
                "pushed": spec.role == "knob",
                "unit": spec.unit,
                "shape": spec.shape,
                "portable": spec.portable,
                "design_ok": spec.design_ok,
                "design_only": spec.design_only,
                "seed": seed,
                "why": lf.why,
                "doc": spec.doc,
            })
    return rows


def state_rows(roster: Roster, state: dict, physical: dict, *,
               sources: dict | None = None) -> list[dict[str, Any]]:
    """Current values across BOTH stores, one row per (entity, field) that
    has a value — the body of ``scqo state``. ``sources`` is a
    :func:`scqo.provenance.live_sources` result keyed by entity."""
    rows = []
    for entity in sorted(set(state) | set(physical)):
        if entity not in roster:
            # A store row whose entity left the roster (pre-freeze editing):
            # surfaced, never hidden — the doctor's lock check explains it.
            for store_name, store in (("scqo_state.json", state),
                                      ("physical.json", physical)):
                for field, value in sorted(store.get(entity, {}).items()):
                    rows.append({"entity": entity, "kind": "(orphan)",
                                 "field": field, "value": value,
                                 "role": None, "unit": "",
                                 "store": store_name, "source": None})
            continue
        specs = roster.fields_of(entity)
        merged = {**physical.get(entity, {}), **state.get(entity, {})}
        for field, value in sorted(merged.items()):
            spec = specs.get(field)
            rows.append({
                "entity": entity,
                "kind": roster.entities[entity].kind,
                "field": field,
                "value": value,
                "role": spec.role if spec else None,
                "unit": spec.unit if spec else "",
                "store": ("physical.json" if spec and spec.role == "fact"
                          else "scqo_state.json"),
                "source": (sources or {}).get(entity, {}).get(field),
            })
    return rows


def qubit_rows(roster: Roster, qubit: str, state: dict,
               physical: dict) -> list[dict[str, Any]]:
    """The per-qubit ASSEMBLED view: the mode plus its closure (default
    channels, attached resonator), each row tagged with the closure ROLE it
    plays — grouping derived from refs, never declared."""
    members: list[tuple[str, str]] = [(qubit, "mode")]
    for kind in CHANNELS:
        ch = roster.defaults.get((qubit, kind))
        if ch is not None:
            members.append((ch, f"{kind} channel"))
    members += [(m.name, "resonator") for m in roster.modes().values()
                if m.refs.get("qubit") == qubit]
    rows = []
    for name, role in members:
        for row in state_rows(roster, {name: state.get(name, {})},
                              {name: physical.get(name, {})}):
            rows.append({**row, "member": role})
    return rows


def design_rows(roster: Roster, design: Design,
                physical: dict) -> list[dict[str, Any]]:
    """The design-vs-measured column: one row per declared target, joined
    key-for-key against the measured facts."""
    rows = []
    for entity, field, designed, measured in design.compare(physical):
        spec = roster.fields_of(entity, design=True).get(field)
        delta = None if measured is None else measured - designed
        rows.append({
            "entity": entity,
            "kind": roster.entities[entity].kind,
            "field": field,
            "unit": spec.unit if spec else "",
            "designed": designed,
            "measured": measured,
            "delta": delta,
            "rel": (None if measured is None or not designed
                    else delta / designed),
        })
    return rows


def catalog_rows() -> list[dict[str, Any]]:
    """The static kind catalogs — the vocabulary reference ``scqo state
    --fields`` shows without a device."""
    rows = []
    for family, kinds in (("mode", MODES), ("composite", COMPOSITES),
                          ("channel", CHANNELS)):
        for kind, spec in kinds.items():
            rows.append({
                "family": family,
                "kind": kind,
                "doc": spec.doc,
                "fields": sorted(spec.fields),
                "roles": sorted(getattr(spec, "roles", {}) or
                                getattr(spec, "refs", {}) or {}),
            })
    return rows


def live_sources(values: dict, history: list[dict]) -> dict:
    """:func:`scqo.provenance.live_sources` over model history rows.

    Re-exported here so the report surface is one import for callers; the
    utility speaks the model's ``entity`` vocabulary directly.
    """
    from .provenance import live_sources as _live

    return _live(values, history)
