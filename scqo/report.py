"""Report data behind ``scqo state`` / ``scqo device`` — renderer-free.

Every function returns plain JSON-able rows so the CLI is a table printer,
the viewer reads the same shapes, and an AI loop can consume them directly.
Nothing here formats, sorts for looks, or truncates.
"""

from __future__ import annotations

from typing import Any

from .catalog import CHANNELS, COMPOSITES, MODES, OPERATION_FIELDS
from .design import Design, seed_anchor
from .entities import Channel, Composite, Line, Mode, Operation
from .roster import Roster


def _catalog_field_specs():
    """Every catalog (name, FieldSpec), in declaration order: modes,
    composites, channel kinds (their channel fields, then their line fields),
    operations."""
    for kinds in (MODES, COMPOSITES):
        for spec in kinds.values():
            yield from spec.fields.items()
    for spec in CHANNELS.values():
        yield from spec.fields.items()
        yield from spec.line_fields.items()
    yield from OPERATION_FIELDS.items()


def catalog_fields(*roles: str) -> list[str]:
    """Catalog field names of the given roles, in declaration order (deduped
    across kinds)."""
    out: list[str] = []
    for f, fs in _catalog_field_specs():
        if fs.role in roles and f not in out:
            out.append(f)
    return out


PHYSICAL_FIELD_ORDER = catalog_fields("fact")
INSTRUMENT_FIELD_ORDER = catalog_fields("knob", "monitor")

#: Quantities never tracked as device state (instrument-dependent; a recorded
#: decision), but still worth reporting from a run's fit.
#: `p_*_given_*` are COUNTED (nearest-center assignment: population + overlap
#: error); `pop_*_prep_*` are the FITTED blob weights (population alone).
FIT_ONLY_QUANTITIES = ("p_e_given_g", "p_g_given_e", "pop_e_prep_g", "pop_g_prep_e")

#: Catalog unit per field name (declaration order, first kind wins — a field
#: name never carries two different units across kinds). Derived, never
#: hand-kept. Consumer: the viewer's per-parameter unit column.
FIELD_UNITS: dict[str, str] = {}
for _f, _fs in _catalog_field_specs():
    FIELD_UNITS.setdefault(_f, _fs.unit)
del _f, _fs

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
    if isinstance(entity, Channel) and entity.borrowed:
        return "borrowed"
    if isinstance(entity, Operation):
        return f"[composites.{entity.composite}] operations"
    return str(entity.derived) if entity.derived is not None else "declared"


def expansion_rows(roster: Roster, *, borrowed: bool = False
                   ) -> list[dict[str, Any]]:
    """The EXPANDED roster — what ``scqo device`` prints so a reader sees the
    derived names (and where each came from) that the file does not list.
    Borrowed channels are summarized on their line (``lends``) unless asked
    for one by one."""
    rows = []
    for name, e in sorted(roster.entities.items()):
        if isinstance(e, Channel) and e.borrowed and not borrowed:
            continue
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
        elif isinstance(e, Operation):
            row["composite"] = e.composite
        elif isinstance(e, Channel):
            row["kinds"] = list(e.kinds)
            row["target"] = list(e.target)
            row["line"] = e.line
            row["via"] = e.via
            row["borrowed"] = e.borrowed
        elif isinstance(e, Line):
            row["carries"] = sorted(c.name for c in roster.channels_on(name))
            row["lends"] = sorted(
                c.target[0] for c in roster.borrowed_channels().values()
                if c.line == name)
        rows.append(row)
    return rows


def field_rows(roster: Roster, *, borrowed: bool = False
               ) -> list[dict[str, Any]]:
    """The catalog behind ``scqo state --fields``: every legal (entity,
    field) with its routing, unit, and design/seed story. BORROWED channels
    (every drive line x every drivable mode) are left out unless asked for:
    they share one field set, and listing them per entity drowns the rest."""
    rows = []
    for name in sorted(roster.entities):
        e = roster.entities[name]
        if isinstance(e, Channel) and e.borrowed and not borrowed:
            continue
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
    """The per-qubit ASSEMBLED view: the mode plus its closure
    (:meth:`Roster.closure` - default channels, the flux line they ride, the
    attached resonator), each row tagged with the closure ROLE it plays."""
    rows = []
    for name, role in roster.closure(qubit):
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
                "line_fields": sorted(getattr(spec, "line_fields", {})),
                "roles": sorted(getattr(spec, "roles", {}) or
                                getattr(spec, "refs", {}) or {}),
            })
    rows.append({"family": "operation", "kind": "operation",
                 "doc": "A declared composite operation, <composite>.<op>.",
                 "fields": sorted(OPERATION_FIELDS), "line_fields": [],
                 "roles": []})
    return rows


def campaign_statistics_rows(statistics: dict) -> list[dict[str, Any]]:
    """A campaign's per-(experiment, target, quantity) statistics as flat rows.

    Renderer-free twin of the CLI table
    (``scqo.cli._campaign.format_statistics``), encoding the same two rules:
    (a) a quantity that is only some other quantity's stderr twin gets no row
    of its own — it is that row's ``mean_stderr``/``scatter_ratio``; (b) an
    all-array quantity (the record-only diagnostics) becomes a ``nonscalar``
    row rather than a row of dashes that would read as a failed measurement.
    """
    from .campaign import stderr_twin

    rows: list[dict[str, Any]] = []
    for experiment in sorted(statistics):
        for target in sorted(statistics[experiment]):
            quantities = statistics[experiment][target]
            twins = {stderr_twin(q) for q in quantities}
            for quantity in sorted(quantities):
                if quantity in twins:
                    continue
                stat = quantities[quantity]
                if not stat["n"] and stat.get("n_nonscalar"):
                    rows.append({"experiment": experiment, "target": target,
                                 "quantity": quantity, "nonscalar": True})
                    continue
                rows.append({
                    "experiment": experiment, "target": target,
                    "quantity": quantity, "nonscalar": False,
                    "n": stat["n"], "n_missing": stat["n_missing"],
                    "mean": stat.get("mean"), "std": stat.get("std"),
                    "sem": stat.get("sem"), "min": stat.get("min"),
                    "max": stat.get("max"),
                    "scatter_ratio": stat.get("scatter_ratio"),
                    "stderr_key": stat.get("stderr_key"),
                })
    return rows


def live_sources(values: dict, history: list[dict]) -> dict:
    """:func:`scqo.provenance.live_sources` over model history rows.

    Re-exported here so the report surface is one import for callers; the
    utility speaks the model's ``entity`` vocabulary directly.
    """
    from .provenance import live_sources as _live

    return _live(values, history)
