"""The components.toml loader — topology in, validated entity graph out.

The device model of docs/greenfield-schema.md (sections 3-5, 7) with the 4.0.0
store-by-line addressing (docs/store-by-line-plan.md). The file declares four
sections — ``[modes]``, ``[composites]``, ``[lines]``, ``[channels]`` — and this
loader:

1. parses them into typed entities (scalar-or-list normalized to tuples;
   the internal model contains no unions);
2. EXPANDS rider lists into DESIGNED channels named ``<line>.<target>``: a
   rider entry declares that the line carries that function to that target, and
   readout riders also mint the target's ``<t>_res`` resonator mode (ref
   ``qubit=<t>``), stamped with (line, rider, index) provenance. An explicit
   ``[channels.<label>]`` joins the same channel ``<line>.<target>``; a pump or a
   multi-target channel (a joint readout) is ``<line>.<label>``, and a
   multi-target FLUX channel (a broadcast coil) is one channel per target -
   what a flux channel owns is the per-target transfer function;
3. mints one OPERATION entity ``<composite>.<op>`` per declared operation;
4. enforces the NAMESPACE collision rule on declared and minted names alike;
5. resolves and validates every reference: mode refs, composite roles
   (arity + kind typing + no entity in two roles + required roles + DAG over
   composite refs), channel targets against the (channel kind x target kind)
   DERIVATION table — identical for riders and explicit channels — THEN the
   readout ``via`` default (the unique resonator whose qubit == target; zero
   or several candidates = via required, candidates named);
6. adds the BORROWED channels: every mode a drive line can reach but does not
   carry by design, ``<line>.<mode>`` - a coupler driven through a neighbour's
   line, a qubit through a foreign one. They need no declaration; which line
   drives the mode is part of their name;
7. COMPILES per-entity legal-field sets: the kind catalog of a mode or a
   composite, OPERATION_FIELDS on an operation, the line-level fields of the
   kinds a line carries (a flux line's idle_flux / flux_delay_s / distortion
   taps), and the channel fields of every kind on a channel (a borrowed channel
   without the target-owned ``designed_only`` ones). All later validation
   (stores, design.toml) is set membership. The lock (``Roster.signatures``)
   freezes the SIGNATURES of the declared and designed entities only.

Design values do NOT live here: a ``[design]`` table (or an in-table
``design`` key) is refused with a pointer to design.toml. ``retired = true``
is legal in every section (post-cut decommissioning keeps names resolving).
"""

from __future__ import annotations

try:  # the repo-wide pattern: stdlib tomllib on 3.11+, tomli backport below
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - py<3.11 envs
    import tomli as tomllib  # type: ignore[no-redef]

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from .catalog import (
    ALL_FIELD_NAMES,
    BORROWABLE,
    CHANNELS,
    COMPOSITES,
    DERIVATION,
    MODES,
    OPERATION_FIELDS,
    QUBIT_LIKE,
    FieldSpec,
    derived_op,
)
from .entities import (
    Channel,
    Composite,
    Entity,
    Line,
    Mode,
    Operation,
    Provenance,
)

COMPONENTS_FILE = "components.toml"
SCHEMA = 3

#: Printed by doctor and by the missing-roster error — the smallest valid file.
TEMPLATE = """\
schema = 3
[modes.q1]
kind = "transmon"
[lines.fl1]
readout = ["q1"]
[lines.xy1]
drive = ["q1"]
"""


class RosterError(ValueError):
    """A components.toml that cannot be loaded correctly must fail loudly."""


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise RosterError(msg)


def _where(e: Entity) -> str:
    """Error location: the syntax the user actually wrote - a declared entity's
    section header, a rider's provenance, a channel's [channels.<label>]."""
    if isinstance(e, Channel):
        if e.borrowed:
            return f"borrowed channel {e.name!r}"
        labels = [o for o in e.origins.values() if o != "rider"]
        if labels:
            return f"[channels.{labels[0]}] (channel {e.name!r})"
        if e.derived is not None:
            return f"{e.derived} (channel {e.name!r})"
        return f"channel {e.name!r}"
    if isinstance(e, Operation):
        return f"[composites.{e.composite}] operation {e.op!r}"
    section = type(e).__name__.lower() + "s"
    if e.derived is not None:
        return f"{e.derived} (minted {e.name!r})"
    return f"[{section}.{e.name}]"


def _names(value, *, where: str, list_ok: bool = True) -> tuple[str, ...]:
    """Scalar-or-list normalization: the TOML accepts both, the model holds
    only tuples of names."""
    if isinstance(value, str):
        value = [value]
    _require(isinstance(value, list) and value
             and all(isinstance(v, str) and v for v in value),
             f"{where}: expected a name or a non-empty list of names")
    _require(list_ok or len(value) == 1,
             f"{where}: exactly one name allowed here, got {len(value)}")
    _require(len(set(value)) == len(value), f"{where}: duplicate names")
    return tuple(value)


def _check_name(name: str, where: str) -> None:
    """Entity names feed the ``name.field`` addressing grammar and the store
    keys: identifiers only (so never a dot - a dot joins a line to its target
    and a composite to its operation), no ``__``, no leading underscore, not
    ``schema`` (design.toml's flat layout puts entity tables beside its schema
    stamp), and never a field name (a nested store key under a line or a
    composite is either a field or a sub-entity)."""
    _require(isinstance(name, str) and name.isidentifier()
             and "__" not in name and not name.startswith("_")
             and name != "schema",
             f"{where}: invalid entity name {name!r} (identifier, no '__', "
             f"no leading '_', not the reserved word 'schema')")
    _require(name not in ALL_FIELD_NAMES,
             f"{where}: {name!r} is a field name - an entity may not share a "
             f"name with a field (the store nests entities beside fields)")


def _table(section: dict, name: str, header: str) -> dict:
    table = section[name]
    _require(isinstance(table, dict), f"[{header}.{name}]: expected a table")
    _check_name(name, f"[{header}.{name}]")
    return table


def _retired(table: dict, where: str) -> bool:
    value = table.get("retired", False)
    _require(isinstance(value, bool), f"{where}: retired must be a boolean")
    return value


def _only_keys(table: dict, allowed: set[str], where: str) -> None:
    _require("design" not in table,
             f"{where}: design values live in design.toml, not the roster")
    _require("derived" not in table,
             f"{where}: 'derived' is stamped by the loader, never hand-written")
    unknown = set(table) - allowed - {"retired"}
    _require(not unknown,
             f"{where}: unknown key(s) {sorted(unknown)} (allowed: "
             f"{sorted(allowed) + ['retired']})")


# ------------------------------------------------------------------ parsing

def _parse_modes(section: dict) -> dict[str, Mode]:
    modes: dict[str, Mode] = {}
    for name in section:
        where = f"[modes.{name}]"
        table = _table(section, name, "modes")
        kind = table.get("kind")
        _require(kind in MODES,
                 f"{where}: kind must be one of {sorted(MODES)}, got {kind!r}")
        roles = MODES[kind].refs
        _only_keys(table, {"kind", *roles}, where)
        refs: dict[str, str] = {}
        for role in roles:
            _require(role in table,
                     f"{where}: kind {kind!r} requires the {role!r} ref")
            (refs[role],) = _names(table[role], where=f"{where}.{role}",
                                   list_ok=False)
        modes[name] = Mode(name=name, kind=kind, refs=refs,
                           retired=_retired(table, where))
    return modes


def _parse_composites(section: dict) -> dict[str, Composite]:
    composites: dict[str, Composite] = {}
    for name in section:
        where = f"[composites.{name}]"
        table = _table(section, name, "composites")
        kind = table.get("kind")
        _require(kind in COMPOSITES,
                 f"{where}: kind must be one of {sorted(COMPOSITES)}, "
                 f"got {kind!r}")
        spec = COMPOSITES[kind]
        _only_keys(table, {"kind", "operations", *spec.roles}, where)
        roles: dict[str, tuple[str, ...]] = {}
        for role, rs in spec.roles.items():
            if role not in table:
                _require(rs.optional, f"{where}: kind {kind!r} requires the "
                                      f"{role!r} role")
                continue
            roles[role] = _names(table[role], where=f"{where}.{role}",
                                 list_ok=rs.list_ok)
        ops = table.get("operations", [])
        if isinstance(ops, str):  # scalar-or-list, like everywhere else
            ops = [ops]
        _require(isinstance(ops, list)
                 and all(isinstance(o, str) and o.isidentifier()
                         and "__" not in o for o in ops),
                 f"{where}: operations must be identifiers (no '__')")
        _require(len(set(ops)) == len(ops), f"{where}: duplicate operations")
        for op in ops:
            _require(op not in ALL_FIELD_NAMES,
                     f"{where}: operation {op!r} is a field name - in the "
                     f"store an operation is a key beside the composite's "
                     f"fields, so the two may not share a name")
        composites[name] = Composite(name=name, kind=kind, roles=roles,
                                     operations=tuple(ops),
                                     retired=_retired(table, where))
    return composites


#: rider key -> channel kind (the kinds a [lines.*] rider list may declare).
_RIDERS = {k: s for k, s in CHANNELS.items() if s.rider}


def _parse_lines(section: dict) -> tuple[dict[str, Line],
                                         dict[str, dict[str, tuple[str, ...]]]]:
    lines: dict[str, Line] = {}
    riders: dict[str, dict[str, tuple[str, ...]]] = {}
    for name in section:
        where = f"[lines.{name}]"
        table = _table(section, name, "lines")
        _require("pump" not in table,
                 f"{where}: pump channels are explicit-only "
                 f"([channels.*]), never rider-derived")
        _only_keys(table, set(_RIDERS), where)
        lines[name] = Line(name=name, retired=_retired(table, where))
        riders[name] = {kind: _names(targets, where=f"{where}.{kind}")
                        for kind, targets in table.items()
                        if kind in _RIDERS}
    return lines, riders


@dataclass(frozen=True)
class _Declared:
    """One explicit [channels.<label>] table, before it joins its channel."""

    label: str
    kind: str
    target: tuple[str, ...]
    line: str
    via: str | None
    retired: bool


def _parse_channels(section: dict) -> list[_Declared]:
    declared: list[_Declared] = []
    for label in section:
        where = f"[channels.{label}]"
        table = _table(section, label, "channels")
        kind = table.get("kind")
        _require(kind in CHANNELS,
                 f"{where}: kind must be one of {sorted(CHANNELS)}, "
                 f"got {kind!r}")
        allowed = {"kind", "target", "line"} | ({"via"} if CHANNELS[kind].via_ok
                                                else set())
        _only_keys(table, allowed, where)
        _require("target" in table, f"{where}: target is required")
        _require("line" in table, f"{where}: line is required")
        via = table.get("via")
        if via is not None:
            (via,) = _names(via, where=f"{where}.via", list_ok=False)
        (line,) = _names(table["line"], where=f"{where}.line", list_ok=False)
        declared.append(_Declared(
            label=label, kind=kind, via=via, line=line,
            target=_names(table["target"], where=f"{where}.target"),
            retired=_retired(table, where)))
    return declared


# ---------------------------------------------------------------- expansion

def _channels_carry_knobs(kind: str) -> bool:
    return any(fs.role == "knob" for fs in CHANNELS[kind].fields.values())


def _expand(modes: dict[str, Mode],
            riders: dict[str, dict[str, tuple[str, ...]]],
            declared: list[_Declared],
            ) -> tuple[dict[str, Mode], dict[str, Channel]]:
    """Designed channels from rider lists and explicit tables, keyed by their
    address, plus the readout riders' minted resonators. One (line, target)
    is one channel whatever functions it carries (a combined drive+flux wire);
    the same function twice on it is a load error naming both origins."""
    minted_modes: dict[str, Mode] = {}
    slots: dict[str, dict] = {}

    def add(address: str, kind: str, target: tuple[str, ...], line: str, *,
            origin: str, where: str, via: str | None = None,
            derived: Provenance | None = None, retired: bool = False,
            broadcast: bool = False) -> None:
        slot = slots.setdefault(address, {
            "kinds": [], "target": target, "line": line, "via": None,
            "derived": derived, "origins": {}, "retired": retired,
            "where": where, "broadcast": broadcast})
        _require(kind not in slot["kinds"],
                 f"{where}: a second {kind} channel {address!r} - "
                 f"{slot['where']} already declares it")
        slot["kinds"].append(kind)
        slot["origins"][kind] = origin
        if via is not None:
            slot["via"] = via
        knobbed = [k for k in slot["kinds"] if _channels_carry_knobs(k)]
        _require(len(knobbed) <= 1,
                 f"{where}: channel {address!r} would carry both {knobbed} - "
                 f"one (line, target) carries at most one function with "
                 f"knobs (with {slot['where']})")

    for line, by_kind in riders.items():
        for kind, targets in by_kind.items():
            spec = _RIDERS[kind]
            for index, target in enumerate(targets):
                prov = Provenance(line=line, rider=kind, index=index)
                via = None
                if spec.mints_resonator:
                    known = modes.get(target)
                    # A rider cannot name a via mediator, and the minted
                    # resonator's qubit ref is QUBIT_LIKE-typed — so a
                    # non-qubit readout (cavity emission collection) must use
                    # the explicit hatch. Say so here, against the syntax the
                    # user wrote, instead of failing later on a minted mode.
                    if known is not None and known.kind not in QUBIT_LIKE:
                        raise RosterError(
                            f"{prov}: readout rider on {target!r} (kind "
                            f"{known.kind!r}) — a non-qubit readout needs "
                            f"an explicit [channels.*] entry with via (a "
                            f"rider cannot name the mediator)")
                    res = target + "_res"
                    if res in minted_modes:
                        raise RosterError(
                            f"{prov}: a second readout rider for {target!r} "
                            f"(the first is {minted_modes[res].derived}) - a "
                            f"second readout of one qubit needs an explicit "
                            f"[channels.*] entry with via")
                    minted_modes[res] = Mode(
                        name=res, kind="resonator",
                        refs={"qubit": target}, derived=prov)
                    via = res
                add(f"{line}.{target}", kind, (target,), line,
                    origin="rider", where=str(prov), via=via, derived=prov)

    for d in declared:
        where = f"[channels.{d.label}]"
        if d.kind == "pump" or (len(d.target) > 1 and d.kind != "flux"):
            # A pump addresses a PROCESS, a joint readout one measurement of
            # several modes: neither is any single target's channel.
            add(f"{d.line}.{d.label}", d.kind, d.target, d.line,
                origin=d.label, where=where, via=d.via, retired=d.retired)
        else:
            # One target, or a broadcast flux coil: what a flux channel owns
            # is the per-target transfer function, so each target is a channel.
            for t in d.target:
                add(f"{d.line}.{t}", d.kind, (t,), d.line, origin=d.label,
                    where=where, via=d.via, retired=d.retired,
                    broadcast=len(d.target) > 1)

    channels: dict[str, Channel] = {}
    for address, slot in slots.items():
        kinds = tuple(slot["kinds"])
        knobbed = [k for k in kinds if _channels_carry_knobs(k)]
        channels[address] = Channel(
            name=address, kind=knobbed[0] if knobbed else kinds[0],
            kinds=kinds, target=slot["target"], line=slot["line"],
            via=slot["via"], derived=slot["derived"],
            origins=slot["origins"], retired=slot["retired"],
            broadcast=slot["broadcast"])
    return minted_modes, channels


def _operations(composites: dict[str, Composite]) -> dict[str, Operation]:
    """One entity per declared operation: ``<composite>.<op>`` owns that gate's
    knobs (OPERATION_FIELDS)."""
    return {f"{c.name}.{op}": Operation(name=f"{c.name}.{op}",
                                        composite=c.name, op=op,
                                        retired=c.retired)
            for c in composites.values() for op in c.operations}


# --------------------------------------------------------------- validation

def _origin(e: Entity) -> str:
    if isinstance(e, Channel) or isinstance(e, Operation):
        return _where(e)
    if e.derived is not None:
        return f"minted by {e.derived}"
    return f"declared as {_where(e)}"


def _collide(*groups: dict[str, Entity]) -> dict[str, Entity]:
    """The namespace rule: ONE flat namespace across all sections, declared
    and derived alike; any collision is a load error naming both origins."""
    entities: dict[str, Entity] = {}
    for group in groups:
        for name, entity in group.items():
            prior = entities.get(name)
            if prior is not None:
                raise RosterError(
                    f"name {name!r} is both {_origin(prior)} and "
                    f"{_origin(entity)} — one name, one entity")
            entities[name] = entity
    return entities


def _check_labels(declared: list[_Declared],
                  entities: dict[str, Entity]) -> None:
    """A [channels.<label>] name is not an entity any more, but it still may
    not reuse a declared entity's name - it would read as that entity."""
    for d in declared:
        prior = entities.get(d.label)
        _require(prior is None,
                 f"[channels.{d.label}]: the label {d.label!r} is also "
                 f"{_origin(prior) if prior is not None else ''} — one name, "
                 f"one meaning")


def _validate_graph(entities: dict[str, Entity]) -> None:
    """Reference, typing, and table validation — runs BEFORE via defaulting
    so a bad target is reported as a bad target."""
    for e in entities.values():
        where = _where(e)
        if isinstance(e, Mode):
            for role, allows in MODES[e.kind].refs.items():
                m = entities.get(e.refs[role])
                _require(isinstance(m, Mode),
                         f"{where}.{role}: {e.refs[role]!r} is not a declared "
                         f"mode")
                _require(m.kind in allows,
                         f"{where}.{role}: {m.name!r} is kind {m.kind!r}, "
                         f"allowed: {allows}")
        elif isinstance(e, Composite):
            filled: dict[str, str] = {}
            for role, members in e.roles.items():
                allows = COMPOSITES[e.kind].roles[role].allows
                for n in members:
                    _require(n not in filled or filled[n] == role,
                             f"{where}: {n!r} fills both {filled.get(n)!r} "
                             f"and {role!r} — one entity, one role")
                    filled[n] = role
                    member = entities.get(n)
                    _require(isinstance(member, (Mode, Composite)),
                             f"{where}.{role}: {n!r} is not a mode or "
                             f"composite")
                    _require(member.kind in allows,
                             f"{where}.{role}: {n!r} is kind "
                             f"{member.kind!r}, allowed: {allows}")
        elif isinstance(e, Channel):
            _require(isinstance(entities.get(e.line), Line),
                     f"{where}: line {e.line!r} is not a declared [lines.*] "
                     f"entry")
            for kind in e.kinds:
                spec = CHANNELS[kind]
                for t in e.target:
                    target = entities.get(t)
                    _require(isinstance(target, (Mode, Composite)),
                             f"{where}: target {t!r} is not a mode or "
                             f"composite")
                    if spec.any_target:
                        # A LIST target is a multi-mode physical path; a
                        # composite is spelled as the single scalar target.
                        _require(len(e.target) == 1 or isinstance(target, Mode),
                                 f"{where}: {t!r} is a composite inside a "
                                 f"target list — a composite target is "
                                 f"spelled alone")
                        continue
                    _require(isinstance(target, Mode),
                             f"{where}: a {kind} channel targets modes, "
                             f"{t!r} is a composite")
                    try:
                        derived_op(kind, target.kind)
                    except KeyError:
                        raise RosterError(
                            f"{where}: no ({kind} x {target.kind}) row in the "
                            f"legality table — a {target.kind} cannot carry "
                            f"a {kind} channel (capability by construction)"
                            ) from None
    _check_dag(entities)


def _check_dag(entities: dict[str, Entity]) -> None:
    """Composite -> composite references must form a DAG (cycle = load
    error). Unreachable with today's kinds (no role allows a composite yet);
    pinned separately so hierarchy kinds arrive onto a tested rule."""
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(name: str, path: tuple[str, ...]) -> None:
        if name in done:
            return
        _require(name not in visiting,
                 f"composite reference cycle: {' -> '.join(path + (name,))}")
        visiting.add(name)
        e = entities[name]
        if isinstance(e, Composite):
            for members in e.roles.values():
                for n in members:
                    if isinstance(entities.get(n), Composite):
                        visit(n, path + (name,))
        visiting.discard(name)
        done.add(name)

    for name, e in entities.items():
        if isinstance(e, Composite):
            visit(name, ())


def _resolve_via(entities: dict[str, Entity]) -> dict[str, Entity]:
    """Fill the via default on readout channels declared without one (the
    unique resonator whose qubit ref == the single target), then validate
    every via ref. Runs AFTER graph validation: targets are known-good here."""
    out = dict(entities)
    for name, e in entities.items():
        if not (isinstance(e, Channel) and "readout" in e.kinds):
            continue
        where = _where(e)
        if e.via is None:
            _require(len(e.target) == 1,
                     f"{where}: a multi-target readout channel needs an "
                     f"explicit via (one measurement, one mediator)")
            candidates = [m.name for m in entities.values()
                          if isinstance(m, Mode) and m.kind == "resonator"
                          and m.refs.get("qubit") == e.target[0]]
            _require(len(candidates) == 1,
                     f"{where}: via is required — "
                     + (f"no resonator has qubit = {e.target[0]!r}"
                        if not candidates else
                        f"several resonators claim qubit = {e.target[0]!r}: "
                        f"{sorted(candidates)}"))
            e = replace(e, via=candidates[0])
            out[name] = e
        _require(isinstance(entities.get(e.via), Mode),
                 f"{where}.via: {e.via!r} is not a declared mode")
    return out


def _borrow(entities: dict[str, Entity]) -> dict[str, Entity]:
    """The BORROWED channels (docs/store-by-line-plan.md section 2.2): through
    every line that carries a borrowable function by design, every mode that
    function can reach and the line does not carry it to. Retired lines and
    modes lend and borrow nothing; an address a designed channel already holds
    (another function on a combined wire) is left to that channel."""
    out = dict(entities)
    for kind in BORROWABLE:
        lines = [e.line for e in entities.values()
                 if isinstance(e, Channel) and kind in e.kinds]
        for line in dict.fromkeys(lines):
            if entities[line].retired:
                continue
            for m in entities.values():
                if (not isinstance(m, Mode) or m.retired
                        or (kind, m.kind) not in DERIVATION):
                    continue
                address = f"{line}.{m.name}"
                if address in out:
                    continue
                out[address] = Channel(name=address, kind=kind, kinds=(kind,),
                                       target=(m.name,), line=line,
                                       borrowed=True)
    return out


# -------------------------------------------------------------- compilation

@dataclass(frozen=True)
class LegalField:
    """One compiled legal field of one entity, with why-legal provenance."""

    spec: FieldSpec
    why: str


def _compile(entities: dict[str, Entity]) -> dict[str, dict[str, LegalField]]:
    legal: dict[str, dict[str, LegalField]] = {}
    for name, e in entities.items():
        fields: dict[str, LegalField] = {}
        if isinstance(e, Mode):
            for f, spec in MODES[e.kind].fields.items():
                fields[f] = LegalField(spec, f"modes kind {e.kind!r}")
        elif isinstance(e, Composite):
            for f, spec in COMPOSITES[e.kind].fields.items():
                fields[f] = LegalField(spec, f"composites kind {e.kind!r}")
            # Per-leg couplings are single-coupler facts: with zero or several
            # coupler modes the referent is ambiguous, so the fields are
            # illegal (the audit's data-integrity rule for qubit_pair).
            if e.kind == "qubit_pair" and len(e.roles.get("coupler", ())) != 1:
                fields.pop("j_high_c_hz", None)
                fields.pop("j_low_c_hz", None)
        elif isinstance(e, Operation):
            for f, spec in OPERATION_FIELDS.items():
                fields[f] = LegalField(
                    spec, f"operation {e.op!r} of {e.composite!r}")
        elif isinstance(e, Line):
            for ch in entities.values():
                if (isinstance(ch, Channel) and not ch.borrowed
                        and ch.line == name):
                    for kind in ch.kinds:
                        for f, spec in CHANNELS[kind].line_fields.items():
                            fields[f] = LegalField(
                                spec, f"line carrying {kind} ({ch.name})")
        elif isinstance(e, Channel):
            for kind in e.kinds:
                kspec = CHANNELS[kind]
                for f, spec in kspec.fields.items():
                    if e.borrowed and f in kspec.designed_only:
                        continue
                    fields[f] = LegalField(
                        spec, f"{'borrowed ' if e.borrowed else ''}{kind} "
                              f"channel")
        legal[name] = fields
    return legal


# ------------------------------------------------------------------- roster

class Roster:
    """The validated, expanded entity graph of one device."""

    def __init__(self, entities: dict[str, Entity],
                 legal: dict[str, dict[str, LegalField]]) -> None:
        self.entities = entities
        self._legal = legal
        #: (target name, channel kind) -> the ONE designed channel default
        #: addressing resolves to; absent when zero or several candidates
        #: exist. Multi-target, broadcast and borrowed channels never consume it.
        self.defaults: dict[tuple[str, str], str] = {}
        seen: dict[tuple[str, str], int] = {}
        for e in entities.values():
            if (isinstance(e, Channel) and not e.borrowed and not e.broadcast
                    and len(e.target) == 1):
                for kind in e.kinds:
                    key = (e.target[0], kind)
                    seen[key] = seen.get(key, 0) + 1
                    self.defaults[key] = e.name
        for key, count in seen.items():
            if count > 1:
                del self.defaults[key]

    def __contains__(self, name: str) -> bool:
        return name in self.entities

    def modes(self) -> dict[str, Mode]:
        return {n: e for n, e in self.entities.items() if isinstance(e, Mode)}

    def composites(self) -> dict[str, Composite]:
        return {n: e for n, e in self.entities.items()
                if isinstance(e, Composite)}

    def operation_entities(self) -> dict[str, Operation]:
        """Every declared operation, ``<composite>.<op>``."""
        return {n: e for n, e in self.entities.items()
                if isinstance(e, Operation)}

    def lines(self) -> dict[str, Line]:
        return {n: e for n, e in self.entities.items() if isinstance(e, Line)}

    def channels(self) -> dict[str, Channel]:
        """The DESIGNED channels - what the roster declares."""
        return {n: e for n, e in self.entities.items()
                if isinstance(e, Channel) and not e.borrowed}

    def borrowed_channels(self) -> dict[str, Channel]:
        """The channels no roster entry declares (section 2.2 of the plan)."""
        return {n: e for n, e in self.entities.items()
                if isinstance(e, Channel) and e.borrowed}

    def channels_of(self, target: str) -> tuple[Channel, ...]:
        """The designed channels aimed at ``target``."""
        return tuple(e for e in self.channels().values() if target in e.target)

    def channels_on(self, line: str) -> tuple[Channel, ...]:
        """The designed channels riding ``line``."""
        return tuple(e for e in self.channels().values() if e.line == line)

    def operations(self, name: str) -> tuple[str, ...]:
        """Derived single-mode operations (from designed wiring, keyed on
        kind) for a mode; declared operations for a composite."""
        e = self.entities.get(name)
        _require(e is not None, f"unknown entity {name!r}")
        if isinstance(e, Composite):
            return e.operations
        _require(isinstance(e, Mode), f"{name!r} carries no operations")
        ops: list[str] = []
        for ch in self.channels_of(name):
            for kind in ch.kinds:
                op = derived_op(kind, e.kind)
                if op is not None and op not in ops:
                    ops.append(op)
        return tuple(ops)

    def _unknown(self, name: str) -> RosterError:
        """An exact-cause error for a name that is no entity."""
        v3 = self._v3_hint(name)
        if v3:
            return RosterError(f"unknown entity {name!r}: {v3}")
        head, dot, tail = name.partition(".")
        e = self.entities.get(head) if dot else None
        if isinstance(e, Composite):
            return RosterError(
                f"unknown entity {name!r}: operation {tail!r} is not declared "
                f"on {head!r} (declared: {sorted(e.operations)})")
        if isinstance(e, Line):
            here = sorted(n.partition(".")[2] for n, c in self.entities.items()
                          if isinstance(c, Channel) and c.line == head)
            return RosterError(
                f"unknown entity {name!r}: line {head!r} has no channel to "
                f"{tail!r} (it reaches: {here or '(nothing)'})")
        return RosterError(f"unknown entity {name!r}")

    def _v3_hint(self, name: str) -> str:
        """Where a 3.x channel NAME went (``q1_z`` -> the line ``z1`` and its
        channel ``z1.q1``) - an error hint only: 4.0.0 never accepts the old
        name, it just says what to write instead."""
        from .v3_names import v3_channel_names  # lazy: v3_names imports roster

        for ch in self.channels().values():
            for kind, old in v3_channel_names(ch).items():
                if old != name:
                    continue
                line_fields = sorted(CHANNELS[kind].line_fields)
                where = (f"{ch.name}, with {', '.join(line_fields)} on its "
                         f"line {ch.line}" if line_fields else ch.name)
                return (f"that is the 3.x name of {where} - since 4.0.0 a "
                        f"value is addressed by line and channel "
                        f"(docs/store-by-line-plan.md)")
        return ""

    def fields_of(self, name: str, *, design: bool = False
                  ) -> dict[str, FieldSpec]:
        """The compiled legal field set of one entity — store-legal by
        default; ``design=True`` gives the design.toml vocabulary instead."""
        if name not in self._legal:
            raise self._unknown(name)
        if design:
            return {f: lf.spec for f, lf in self._legal[name].items()
                    if lf.spec.design_ok}
        return {f: lf.spec for f, lf in self._legal[name].items()
                if not lf.spec.design_only}

    def spec(self, name: str, field: str) -> FieldSpec:
        """FieldSpec for a store write, with exact-cause errors."""
        if name not in self._legal:
            raise self._unknown(name)
        lf = self._legal[name].get(field)
        if lf is not None and lf.spec.design_only:
            raise RosterError(
                f"{name}.{field}: design.toml-only vocabulary (a declared "
                f"fabrication constant, never measured into a store)")
        if lf is not None:
            return lf.spec
        e = self.entities[name]
        if isinstance(e, Composite) and field in OPERATION_FIELDS:
            raise RosterError(
                f"{name}.{field}: a gate knob lives on an operation - "
                f"{', '.join(f'{name}.{op}.{field}' for op in e.operations) or 'no operation is declared on ' + repr(name)}")
        if isinstance(e, Channel) and e.borrowed:
            kspec = CHANNELS[e.kind]
            if field in kspec.designed_only:
                raise RosterError(
                    f"{name}.{field}: {name!r} is a BORROWED channel and "
                    f"{field} belongs to {e.target[0]!r} itself - it lives on "
                    f"its designed {e.kind} channel only")
        raise RosterError(
            f"{name}.{field}: unknown field for this entity "
            f"(kind {e.kind!r}; legal: {sorted(self.fields_of(name))})")

    def resolve_field(self, name: str, field: str, *,
                      valued: Callable[[str, str], bool] | None = None
                      ) -> tuple[str, FieldSpec]:
        """Qubit-closure addressing: route ``q1.pi_amp`` to the entity that
        owns the field. Search order — SELF first, then the target's DESIGNED
        default channels, then the LINE such a channel rides (only when the
        target is that line's sole designed target of the kind: ``q1.idle_flux``
        -> ``z1.idle_flux``), then the attached resonator; first hit wins.
        A borrowed channel is never reached this way - it must be named with
        its line (``xy2.q1_q2_c.pi_amp``). Explicit entity names hit on self and
        pass straight through.

        ``valued(entity, field)``, when the caller has the stores, orders the
        routes that refusal lists: the ones already holding a value first."""
        if name not in self._legal:
            raise self._unknown(name)
        fields = self.fields_of(name)
        if field in fields:
            return name, fields[field]
        e = self.entities[name]
        if isinstance(e, Mode):
            for kind, chspec in CHANNELS.items():
                ch = self.defaults.get((name, kind))
                if ch is not None:
                    chf = self.fields_of(ch)
                    if field in chf:
                        return ch, chf[field]
                    line = self.entities[ch].line
                    lf = self.fields_of(line)
                    if field in chspec.line_fields and field in lf:
                        sharing = sorted(
                            c.name for c in self.channels_on(line)
                            if kind in c.kinds and c.name != ch)
                        if sharing:
                            raise RosterError(
                                f"{name}.{field}: line {line!r} is shared with "
                                f"{sharing} - the value belongs to the line; "
                                f"address it as {line}.{field}")
                        return line, lf[field]
                elif field in chspec.fields or field in chspec.line_fields:
                    # The field belongs to this kind but the target has no
                    # DEFAULT channel of it — several declared, or none.
                    extras = sorted(c.name for c in self.channels_of(name)
                                    if kind in c.kinds)
                    if len(extras) > 1:
                        raise RosterError(
                            f"{name}.{field}: ambiguous — several {kind} "
                            f"channels on {name!r}: {extras}; address one "
                            f"explicitly")
                    if extras:
                        # one channel of the kind, and not a default: it is
                        # shared (a broadcast coil, a joint readout)
                        only = self.entities[extras[0]]
                        why = ("a broadcast channel" if only.broadcast else
                               f"a multi-target channel {list(only.target)}")
                        where = (f"{only.line}.{field}"
                                 if field in chspec.line_fields
                                 else f"{only.name}.{field}")
                        raise RosterError(
                            f"{name}.{field}: {name!r}'s only {kind} channel "
                            f"{only.name!r} is {why}, which never answers the "
                            f"shorthand - address {where}")
                    borrowed = sorted(
                        c.name for c in self.borrowed_channels().values()
                        if c.target == (name,) and kind in c.kinds)
                    if valued is not None:  # stable: valued routes first
                        borrowed.sort(key=lambda b: not valued(b, field))
                    if borrowed and field in chspec.fields:
                        raise RosterError(
                            f"{name}.{field}: {name!r} has no designed {kind} "
                            f"line - name the line it is driven through: "
                            f"{', '.join(f'{b}.{field}' for b in borrowed)}")
            hits = []
            for res in self.entities.values():
                if (isinstance(res, Mode) and res.kind == "resonator"
                        and res.refs.get("qubit") == name
                        and field in self.fields_of(res.name)):
                    hits.append(res.name)
            if len(hits) == 1:
                return hits[0], self.fields_of(hits[0])[field]
            if len(hits) > 1:
                raise RosterError(
                    f"{name}.{field}: ambiguous — several resonators claim "
                    f"qubit {name!r}: {sorted(hits)}; address one explicitly")
        if isinstance(e, Composite) and field in OPERATION_FIELDS:
            raise RosterError(
                f"{name}.{field}: a gate knob lives on an operation - "
                f"{', '.join(f'{name}.{op}.{field}' for op in e.operations) or 'no operation is declared on ' + repr(name)}")
        if isinstance(e, Composite):
            for op in e.operations:  # the 3.x flattening <op>_<suffix>
                suffix = field[len(op) + 1:]
                if field.startswith(op + "_") and suffix in OPERATION_FIELDS:
                    raise RosterError(
                        f"{name}.{field}: that is the 3.x spelling of "
                        f"{name}.{op}.{suffix} - since 4.0.0 a gate knob "
                        f"lives on the operation")
        if isinstance(e, Channel) and e.borrowed:
            self.spec(name, field)  # the exact cause (a target-owned field)
        owners = sorted(f"{n}.{field}" for n in self.entities
                        if not (isinstance(self.entities[n], Channel)
                                and self.entities[n].borrowed)
                        and field in self.fields_of(n))
        hint = (f" — did you mean {' or '.join(owners[:3])}?"
                if owners else "")
        raise RosterError(
            f"{name}.{field}: no entity in {name!r}'s closure carries this "
            f"field{hint}")

    def closure(self, mode: str) -> tuple[tuple[str, str], ...]:
        """The qubit-closure members of one mode, each with the ROLE it plays:
        the mode itself, its DESIGNED default channels, the line such a
        channel rides when the kind puts fields on its wire (the flux bias),
        and the attached resonator(s) - the owners :meth:`resolve_field`
        searches. Borrowed channels are never members; grouping derives from
        the wiring and refs, never from a declaration."""
        members: list[tuple[str, str]] = [(mode, "mode")]
        seen = {mode}
        for kind, spec in CHANNELS.items():
            ch = self.defaults.get((mode, kind))
            if ch is None:
                continue
            if ch not in seen:
                members.append((ch, f"{kind} channel"))
                seen.add(ch)
            line = self.entities[ch].line
            if spec.line_fields and line not in seen:
                members.append((line, f"{kind} line"))
                seen.add(line)
        members += [(m.name, "resonator") for m in self.modes().values()
                    if m.refs.get("qubit") == mode]
        return tuple(members)

    def pairs_of_coupler(self, mode: str) -> tuple[str, ...]:
        """The composites whose ``coupler`` role holds ``mode`` - the pairs a
        coupler can be read through (a mapped readout reads it via a member)."""
        return tuple(name for name, c in self.composites().items()
                     if mode in (c.roles.get("coupler") or ()))

    def default_channel(self, target: str, kind: str) -> str:
        """Default addressing: the ONE designed channel of this kind on this
        target."""
        name = self.defaults.get((target, kind))
        _require(name is not None,
                 f"no unique {kind} channel for {target!r} — extras need "
                 f"explicit channel addressing")
        return name

    def signatures(self) -> dict[str, tuple]:
        """name -> lock signature of the declared and DESIGNED entities:
        (entity class, name, kind, target(s) for channels) — exactly the doc
        section-7 identity. Operations (declared on their composite) and
        borrowed channels (declared by nobody) are never locked; provenance,
        via, roles, and operations are not in any signature, so doc-legal
        post-cut appends never change a frozen one."""
        return {n: e.signature() for n, e in self.entities.items()
                if not isinstance(e, Operation)
                and not (isinstance(e, Channel) and e.borrowed)}


# ------------------------------------------------------------------ loading

_SECTIONS = ("modes", "composites", "lines", "channels")


def parse_components(text: str, *, source: str = COMPONENTS_FILE) -> Roster:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        raise RosterError(f"{source}: {err}") from None
    stamp = data.get("schema")
    _require(isinstance(stamp, int) and not isinstance(stamp, bool)
             and stamp == SCHEMA,
             f"{source}: schema = {SCHEMA} required (the integer; found "
             f"{stamp!r}); pre-greenfield rosters are not read")
    unknown = set(data) - {"schema", *_SECTIONS}
    _require(not unknown,
             f"{source}: unknown section(s) {sorted(unknown)}"
             + (" — design values live in design.toml"
                if "design" in unknown else ""))
    for sec in _SECTIONS:
        _require(isinstance(data.get(sec, {}), dict),
                 f"{source}: [{sec}] must be a table of tables")
    modes = _parse_modes(data.get("modes", {}))
    composites = _parse_composites(data.get("composites", {}))
    lines, riders = _parse_lines(data.get("lines", {}))
    declared = _parse_channels(data.get("channels", {}))
    minted_modes, channels = _expand(modes, riders, declared)
    entities = _collide(modes, minted_modes, composites, lines, channels,
                        _operations(composites))
    _check_labels(declared, entities)
    _validate_graph(entities)
    entities = _resolve_via(entities)
    entities = _borrow(entities)
    return Roster(entities, _compile(entities))


def load_components(device_dir: str | Path) -> Roster:
    """The device's roster. ``device_dir`` is the device folder (its
    ``components.toml`` sibling of ``cooldowns.toml``); a direct path to the
    file itself is accepted too."""
    path = Path(device_dir)
    if path.is_dir() or path.suffix != ".toml":
        path = path / COMPONENTS_FILE
    if not path.is_file():
        raise RosterError(
            f"no {path} — the roster is required. Smallest valid file:\n"
            + TEMPLATE)
    # utf-8-sig: PowerShell 5.1's `-Encoding utf8` writes a BOM into this
    # hand-edited file; tolerate it (the old loader's deliberate choice).
    return parse_components(path.read_text(encoding="utf-8-sig"),
                            source=str(path))
