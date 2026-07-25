"""The components.toml loader — topology in, validated entity graph out.

Greenfield replacement for the old :mod:`scqo.roster`
(docs/greenfield-schema.md sections 3-5, 7). The file declares four sections —
``[modes]``, ``[composites]``, ``[lines]``, ``[channels]`` — and this loader:

1. parses them into typed entities (scalar-or-list normalized to tuples;
   the internal model contains no unions);
2. EXPANDS rider lists: each rider entry mints one channel via the frozen
   suffix map, and readout riders also mint the target's ``<t>_res``
   resonator mode (ref ``qubit=<t>``) — every minted entity stamped with
   (line, rider, index) provenance, which every later error about it cites;
3. enforces the NAMESPACE collision rule: any derived name colliding with any
   declared name, in any section, is a load error naming both origins;
4. resolves and validates every reference: mode refs, composite roles
   (arity + kind typing + no entity in two roles + required roles + DAG over
   composite refs), channel targets against the (channel kind x target kind)
   DERIVATION table — identical for riders and explicit channels — THEN the
   readout ``via`` default (the unique resonator whose qubit == target; zero
   or several candidates = via required, candidates named). Graph validation
   runs first so a bad target is reported as a bad target, never as a
   missing via;
5. COMPILES per-entity legal-field sets: kind catalog fields, plus full-name
   per-operation knobs for each declared composite operation, plus
   ``__<target>``-parameterized fact instances on multi-target channels
   (their ``paired_with`` partners re-pointed per target). All later
   validation (stores, design.toml) is set membership — no prefix parsing
   anywhere. The lock (``Roster.signatures``) freezes the expanded entity
   SIGNATURES — (name, kind, target(s)) — not the field sets.

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

from .catalog import (
    ALL_STATIC_FIELDS,
    CHANNELS,
    COMPOSITES,
    MODES,
    OP_KNOBS,
    QUBIT_LIKE,
    FieldSpec,
    derived_op,
    op_knob_fields,
)
from .entities import Channel, Composite, Entity, Line, Mode, Provenance

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
    """Error location: minted entities cite their provenance (the syntax the
    user actually wrote), declared ones their section header."""
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
    """Entity names feed the ``name.field`` addressing grammar and store
    keys: identifiers only, no ``__`` (reserved for the field parameter
    grammar), no leading underscore."""
    _require(isinstance(name, str) and name.isidentifier()
             and "__" not in name and not name.startswith("_"),
             f"{where}: invalid entity name {name!r} (identifier, no '__', "
             f"no leading '_')")


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
            clash = sorted(set(op_knob_fields(op)) & ALL_STATIC_FIELDS)
            _require(not clash,
                     f"{where}: operation {op!r} would mint field(s) {clash} "
                     f"that already exist in the static catalogs — pick "
                     f"another operation name (field-name uniqueness is the "
                     f"invariant behind default addressing)")
        composites[name] = Composite(name=name, kind=kind, roles=roles,
                                     operations=tuple(ops),
                                     retired=_retired(table, where))
    return composites


#: rider key -> channel kind (exactly the kinds with a rider suffix).
_RIDERS = {k: s for k, s in CHANNELS.items() if s.rider_suffix is not None}


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


def _parse_channels(section: dict) -> dict[str, Channel]:
    channels: dict[str, Channel] = {}
    for name in section:
        where = f"[channels.{name}]"
        table = _table(section, name, "channels")
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
        channels[name] = Channel(
            name=name, kind=kind, via=via, line=line,
            target=_names(table["target"], where=f"{where}.target"),
            retired=_retired(table, where))
    return channels


# ---------------------------------------------------------------- expansion

def _expand(modes: dict[str, Mode],
            riders: dict[str, dict[str, tuple[str, ...]]],
            ) -> tuple[dict[str, Mode], dict[str, Channel]]:
    """Mint channels (and readout resonators) from rider lists, with
    provenance. Derived-derived collisions (one target in two same-kind
    riders) are caught here with both origins named."""
    minted_modes: dict[str, Mode] = {}
    minted_channels: dict[str, Channel] = {}
    origin: dict[str, Provenance] = {}
    for line, by_kind in riders.items():
        for kind, targets in by_kind.items():
            spec = _RIDERS[kind]
            for index, target in enumerate(targets):
                prov = Provenance(line=line, rider=kind, index=index)
                ch_name = target + spec.rider_suffix
                if ch_name in minted_channels:
                    raise RosterError(
                        f"{prov}: derived channel {ch_name!r} already minted "
                        f"by {origin[ch_name]} — a second {kind} channel for "
                        f"{target!r} needs an explicit [channels.*] name")
                via = None
                if spec.mints_resonator:
                    declared = modes.get(target)
                    # A rider cannot name a via mediator, and the minted
                    # resonator's qubit ref is QUBIT_LIKE-typed — so a
                    # non-qubit readout (cavity emission collection) must use
                    # the explicit hatch. Say so here, against the syntax the
                    # user wrote, instead of failing later on a minted mode.
                    if declared is not None and declared.kind not in QUBIT_LIKE:
                        raise RosterError(
                            f"{prov}: readout rider on {target!r} (kind "
                            f"{declared.kind!r}) — a non-qubit readout needs "
                            f"an explicit [channels.*] entry with via (a "
                            f"rider cannot name the mediator)")
                    res = target + "_res"
                    minted_modes[res] = Mode(
                        name=res, kind="resonator",
                        refs={"qubit": target}, derived=prov)
                    via = res
                minted_channels[ch_name] = Channel(
                    name=ch_name, kind=kind, target=(target,), line=line,
                    via=via, derived=prov)
                origin[ch_name] = prov
    return minted_modes, minted_channels


# --------------------------------------------------------------- validation

def _origin(e: Entity) -> str:
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
                    _require(member is not None and not isinstance(
                        member, (Line, Channel)),
                        f"{where}.{role}: {n!r} is not a mode or composite")
                    _require(member.kind in allows,
                             f"{where}.{role}: {n!r} is kind "
                             f"{member.kind!r}, allowed: {allows}")
        elif isinstance(e, Channel):
            spec = CHANNELS[e.kind]
            _require(isinstance(entities.get(e.line), Line),
                     f"{where}: line {e.line!r} is not a declared [lines.*] "
                     f"entry")
            for t in e.target:
                target = entities.get(t)
                _require(target is not None and not isinstance(
                    target, (Line, Channel)),
                    f"{where}: target {t!r} is not a mode or composite")
                if spec.any_target:
                    # A LIST target is a multi-mode physical path; a composite
                    # is spelled as the single scalar target (doc section 5).
                    _require(len(e.target) == 1 or isinstance(target, Mode),
                             f"{where}: {t!r} is a composite inside a target "
                             f"list — a composite target is spelled alone")
                    continue
                _require(isinstance(target, Mode),
                         f"{where}: a {e.kind} channel targets modes, "
                         f"{t!r} is a composite")
                try:
                    derived_op(e.kind, target.kind)
                except KeyError:
                    raise RosterError(
                        f"{where}: no ({e.kind} x {target.kind}) row in the "
                        f"legality table — a {target.kind} cannot carry a "
                        f"{e.kind} channel (capability by construction)"
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
    """Fill the via default on explicit readout channels (the unique
    resonator whose qubit ref == the single target), then validate every via
    ref. Runs AFTER graph validation: targets are known-good here."""
    out = dict(entities)
    for name, e in entities.items():
        if not (isinstance(e, Channel) and e.kind == "readout"):
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
            for op in e.operations:
                for f, spec in op_knob_fields(op).items():
                    fields[f] = LegalField(spec, f"operation {op!r}")
        elif isinstance(e, Channel):
            multi = len(e.target) > 1
            for f, spec in CHANNELS[e.kind].fields.items():
                if multi and spec.role == "fact":
                    # Per-target facts on a broadcast/joint channel: only the
                    # __<target> instances are legal (a bare per-target fact
                    # would be ambiguous); knobs and monitors stay singular
                    # (one knob, one joint monitor — that is the point of a
                    # multi-target channel). Paired-array partners re-point to
                    # the same target's instance so the equal-length store
                    # check never dangles.
                    for t in e.target:
                        inst = spec if spec.paired_with is None else replace(
                            spec, paired_with=f"{spec.paired_with}__{t}")
                        fields[f"{f}__{t}"] = LegalField(
                            inst, f"channel kind {e.kind!r}, target {t!r}")
                else:
                    fields[f] = LegalField(spec, f"channel kind {e.kind!r}")
        else:  # Line: zero neutral fields in v1
            pass
        legal[name] = fields
    return legal


# ------------------------------------------------------------------- roster

class Roster:
    """The validated, expanded entity graph of one device."""

    def __init__(self, entities: dict[str, Entity],
                 legal: dict[str, dict[str, LegalField]]) -> None:
        self.entities = entities
        self._legal = legal
        #: (target name, channel kind) -> the ONE channel default addressing
        #: resolves to; absent when zero or several candidates exist
        #: (multi-target channels never consume the slot).
        self.defaults: dict[tuple[str, str], str] = {}
        seen: dict[tuple[str, str], int] = {}
        for e in entities.values():
            if isinstance(e, Channel) and len(e.target) == 1:
                key = (e.target[0], e.kind)
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

    def lines(self) -> dict[str, Line]:
        return {n: e for n, e in self.entities.items() if isinstance(e, Line)}

    def channels(self) -> dict[str, Channel]:
        return {n: e for n, e in self.entities.items()
                if isinstance(e, Channel)}

    def channels_of(self, target: str) -> tuple[Channel, ...]:
        return tuple(e for e in self.entities.values()
                     if isinstance(e, Channel) and target in e.target)

    def operations(self, name: str) -> tuple[str, ...]:
        """Derived single-mode operations (from wiring, keyed on kind) for a
        mode; declared operations for a composite."""
        e = self.entities.get(name)
        _require(e is not None, f"unknown entity {name!r}")
        if isinstance(e, Composite):
            return e.operations
        _require(isinstance(e, Mode), f"{name!r} carries no operations")
        ops: list[str] = []
        for ch in self.channels_of(name):
            op = derived_op(ch.kind, e.kind)
            if op is not None and op not in ops:
                ops.append(op)
        return tuple(ops)

    def fields_of(self, name: str, *, design: bool = False
                  ) -> dict[str, FieldSpec]:
        """The compiled legal field set of one entity — store-legal by
        default; ``design=True`` gives the design.toml vocabulary instead."""
        _require(name in self._legal, f"unknown entity {name!r}")
        if design:
            return {f: lf.spec for f, lf in self._legal[name].items()
                    if lf.spec.design_ok}
        return {f: lf.spec for f, lf in self._legal[name].items()
                if not lf.spec.design_only}

    def spec(self, name: str, field: str) -> FieldSpec:
        """FieldSpec for a store write, with exact-cause errors."""
        _require(name in self._legal, f"unknown entity {name!r}")
        lf = self._legal[name].get(field)
        if lf is not None and lf.spec.design_only:
            raise RosterError(
                f"{name}.{field}: design.toml-only vocabulary (a declared "
                f"fabrication constant, never measured into a store)")
        if lf is not None:
            return lf.spec
        e = self.entities[name]
        if isinstance(e, Composite):
            for suffix in OP_KNOBS:
                if field.endswith("_" + suffix):
                    op = field[: -len(suffix) - 1]
                    if op and op not in e.operations:
                        raise RosterError(
                            f"{name}.{field}: operation {op!r} is not "
                            f"declared on this composite "
                            f"(declared: {sorted(e.operations)})")
        raise RosterError(
            f"{name}.{field}: unknown field for this entity "
            f"(kind {e.kind!r}; legal: {sorted(self.fields_of(name))})")

    def default_channel(self, target: str, kind: str) -> str:
        """Default addressing: the ONE channel of this kind on this target."""
        name = self.defaults.get((target, kind))
        _require(name is not None,
                 f"no unique {kind} channel for {target!r} — extras need "
                 f"explicit channel addressing")
        return name

    def signatures(self) -> dict[str, tuple]:
        """name -> lock signature of the EXPANDED roster: (entity class,
        name, kind, target(s) for channels) — exactly the doc section-7
        identity. Provenance, line, via, roles, and operations are NOT here,
        so doc-legal post-cut appends never change a frozen signature."""
        return {n: e.signature() for n, e in self.entities.items()}


# ------------------------------------------------------------------ loading

_SECTIONS = ("modes", "composites", "lines", "channels")


def parse_components(text: str, *, source: str = COMPONENTS_FILE) -> Roster:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        raise RosterError(f"{source}: {err}") from None
    _require(data.get("schema") == SCHEMA,
             f"{source}: schema = {SCHEMA} required (found "
             f"{data.get('schema')!r}); pre-greenfield rosters are not read")
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
    channels = _parse_channels(data.get("channels", {}))
    minted_modes, minted_channels = _expand(modes, riders)
    entities = _collide(modes, minted_modes, composites, lines, channels,
                        minted_channels)
    _validate_graph(entities)
    entities = _resolve_via(entities)
    return Roster(entities, _compile(entities))


def load_components(path: str | Path) -> Roster:
    path = Path(path)
    if not path.is_file():
        raise RosterError(
            f"no {path} — the roster is required. Smallest valid file:\n"
            + TEMPLATE)
    # utf-8-sig: PowerShell 5.1's `-Encoding utf8` writes a BOM into this
    # hand-edited file; tolerate it (the old loader's deliberate choice).
    return parse_components(path.read_text(encoding="utf-8-sig"),
                            source=str(path))
