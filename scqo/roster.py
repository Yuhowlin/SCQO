"""The component roster — WHICH components this device has (components.toml).

One hand-edited file per device, sibling of ``cooldowns.toml``::

    <data_root>/<device>/components.toml

    schema = 1
    [components.q1]
    physical   = "FixedTransmon"
    instrument = "ReadableTransmon"
    operations = ["rx", "readout"]
    [components.q1.design]
    f_01_hz = 4.7e9                 # DESIGN values: declared (not measured) sample
                                    # facts; keys must be fields of the PHYSICAL
                                    # category; device-level = one copy above all
                                    # setups/cooldowns
    [components.q1_res]
    physical = "Resonator"
    [components.q1_res.design]
    f_r_hz = 5.9e9
    [components.q1_ro]
    physical = "ReadoutLine"
    members  = { transmon = "q1", resonator = "q1_res" }
    [components.q1_xy]
    physical = "XYControl"
    members  = { transmon = "q1" }

The roster is the AUTHORITY on which names exist and what they are; the driver's
vendor-derived inventory is a witness (`scqo doctor` cross-checks). TRIAL-PHASE
rule: the file is freely editable and doctor only WARNS about drift (orphaned
names in state/history, category disagreement with the vendor); the append-only
hardening comes at the production cut.
"""

from __future__ import annotations

import math

try:  # the repo-wide pattern: stdlib tomllib on 3.11+, tomli backport below
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - py<3.11 envs
    import tomli as tomllib  # type: ignore[no-redef]
from dataclasses import dataclass, field as _field
from pathlib import Path

from .categories import CATEGORIES, FieldSpec, pushed_fields

COMPONENTS_FILE = "components.toml"

#: Printed by doctor and by the missing-roster error — the smallest valid file.
TEMPLATE = """\
schema = 1
[components.q1]
physical   = "FixedTransmon"
instrument = "ReadableTransmon"
operations = ["rx", "readout"]
[components.q1_res]
physical = "Resonator"
[components.q1_ro]
physical = "ReadoutLine"
members  = { transmon = "q1", resonator = "q1_res" }
[components.q1_xy]
physical = "XYControl"
members  = { transmon = "q1" }
# Two-qubit pair (QCQ): members are the HIGH/LOW idle-frequency qubits; the
# optional coupler member is a physical-only satellite tracked like a resonator.
# [components.q1_q2]
# physical   = "Coupling"
# instrument = "TransmonPair"
# members    = { high = "q1", low = "q2" }   # + coupler = "q1_q2_c" (optional)
# operations = ["coupler_bias", "iswap"]
# [components.q1_q2_c]
# physical = "FluxTunableTransmon"
"""


class RosterError(ValueError):
    """A components.toml that cannot be loaded correctly must fail loudly."""


@dataclass(frozen=True)
class Component:
    """One declared component: name + its two category slots + topology + design."""

    name: str
    physical: str | None = None
    instrument: str | None = None
    members: dict[str, str] = _field(default_factory=dict)
    operations: tuple[str, ...] = ()
    #: declared design targets, keyed by PHYSICAL-category field names.
    design: dict[str, float] = _field(default_factory=dict)


class Roster:
    """The validated component set of one device, with routing + topology lookup."""

    def __init__(self, components: dict[str, Component]) -> None:
        self.components = components

    def __contains__(self, name: str) -> bool:
        return name in self.components

    def __iter__(self):
        return iter(self.components)

    def category(self, name: str) -> tuple[str | None, str | None]:
        """The (physical, instrument) category pair bound to ``name``."""
        c = self.components[name]
        return c.physical, c.instrument

    def members(self, name: str) -> dict[str, str]:
        return dict(self.components[name].members)

    def operations(self, name: str) -> tuple[str, ...]:
        return self.components[name].operations

    def design(self, name: str, field: str) -> float | None:
        """The declared design value (None if not declared)."""
        return self.components[name].design.get(field)

    def fields_of(self, name: str) -> dict[str, tuple[str, FieldSpec]]:
        """Effective ``{field: (side, spec)}`` for one name — the union of its
        two category slots, with ``requires_physical`` pruning applied (a
        FixedTransmon's ReadableTransmon face has no idle_flux_v)."""
        c = self.components[name]
        out: dict[str, tuple[str, FieldSpec]] = {}
        for cat_name in (c.physical, c.instrument):
            if cat_name is None:
                continue
            spec = CATEGORIES[cat_name]
            for f, fs in spec.fields.items():
                if fs.requires_physical and c.physical not in fs.requires_physical:
                    continue
                out[f] = (spec.side, fs)
        return out

    def resolve(self, name: str, field: str) -> tuple[str, FieldSpec]:
        """THE routing function: (store side, spec) for one component's field.

        Raises :class:`KeyError` with a category-aware message naming the
        component's actual fields (callers wrap it for their own error style).
        """
        if name not in self.components:
            raise KeyError(f"unknown component {name!r} — roster has: "
                           f"{', '.join(sorted(self.components))}")
        fields = self.fields_of(name)
        if field not in fields:
            c = self.components[name]
            cats = " + ".join(x for x in (c.physical, c.instrument) if x)
            raise KeyError(
                f"{cats or 'component'} {name!r} has no field {field!r} — its "
                f"fields: {', '.join(sorted(fields)) or '(none)'}")
        return fields[field]

    def pushed(self, name: str) -> tuple[str, ...]:
        """The vendor-realized fields of one name (instrument slot, pruned),
        in declaration order — the per-name successor of PUSHED_FIELDS."""
        c = self.components[name]
        if c.instrument is None:
            return ()
        return tuple(f for f in pushed_fields(c.instrument)
                     if f in self.fields_of(name))

    def one(self, name: str, category: str) -> str:
        """The exactly-one component of ``category`` reachable from ``name``:
        directly (a term referencing it) or via one shared interaction term
        (``one("q1", "Resonator") -> "q1_res"`` through q1_ro). Raises
        :class:`RosterError` unless exactly one exists."""
        found: set[str] = set()
        for other in self.components.values():
            if name in other.members.values():
                if category in (other.physical, other.instrument):
                    found.add(other.name)          # the term itself
                for m in other.members.values():   # siblings through the term
                    if m != name and category in (
                            self.components[m].physical, self.components[m].instrument):
                        found.add(m)
        if len(found) != 1:
            raise RosterError(
                f"expected exactly one {category} related to {name!r}, found "
                f"{sorted(found) or 'none'} — declare the topology in components.toml")
        return found.pop()

    def names(self, instrument_category: str | None = None) -> list[str]:
        """All names, or those whose instrument slot matches (default targets)."""
        if instrument_category is None:
            return list(self.components)
        return [n for n, c in self.components.items()
                if c.instrument == instrument_category]


def load_components(device_dir: str | Path) -> Roster:
    """Load and validate ``<device_dir>/components.toml``.

    Raises :class:`RosterError` naming the exact fault (a wrong roster must
    never half-load), or :class:`FileNotFoundError` whose message carries the
    minimal template (the component model requires a roster per device).
    """
    path = Path(device_dir) / COMPONENTS_FILE
    if not path.is_file():
        raise FileNotFoundError(
            f"{path} not found — the component model needs a roster per device. "
            f"Minimal template:\n{TEMPLATE}")
    # utf-8-sig: tolerate a UTF-8 BOM — PowerShell 5.1's `-Encoding utf8`
    # writes one, and the roster is a hand-edited file.
    data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    raw = data.get("components", {})
    if not isinstance(raw, dict) or not raw:
        raise RosterError(f"{path}: no [components.<name>] tables")

    components: dict[str, Component] = {}
    for name, entry in raw.items():
        if "." in name:
            raise RosterError(f"{path}: component name {name!r} must be dot-free "
                              f"(the name.field grammar splits on the first dot)")
        if not isinstance(entry, dict):
            raise RosterError(f"{path}: [components.{name}] must be a table")
        phys = entry.get("physical")
        instr = entry.get("instrument")
        if phys is None and instr is None:
            raise RosterError(f"{path}: {name} declares neither physical nor "
                              f"instrument category")
        for slot, cat in (("physical", phys), ("instrument", instr)):
            if cat is None:
                continue
            if cat not in CATEGORIES:
                raise RosterError(f"{path}: {name}.{slot} = {cat!r} is not a known "
                                  f"category ({', '.join(sorted(CATEGORIES))})")
            if CATEGORIES[cat].side != slot:
                raise RosterError(f"{path}: {cat} is a {CATEGORIES[cat].side}-side "
                                  f"category and cannot fill {name}.{slot}")
        operations = tuple(entry.get("operations", ()))
        if instr is not None:
            bad_ops = set(operations) - set(CATEGORIES[instr].operations)
            if bad_ops:
                raise RosterError(f"{path}: {name} declares operations "
                                  f"{sorted(bad_ops)} outside {instr}'s vocabulary "
                                  f"{CATEGORIES[instr].operations}")
        design = entry.get("design", {})
        if not isinstance(design, dict):
            raise RosterError(f"{path}: [components.{name}.design] must be a table")
        components[name] = Component(
            name=name, physical=phys, instrument=instr,
            members=dict(entry.get("members", {})),
            operations=operations, design=dict(design),
        )

    roster = Roster(components)
    for name, c in components.items():
        # members: roles legal for the category, targets exist, SAME-SIDE slot
        # of the member matches the allowed categories (interaction terms are
        # physical-side, so they constrain the member's physical slot).
        role_spec = CATEGORIES[c.physical].member_roles if c.physical else {}
        if c.members and not role_spec:
            raise RosterError(f"{COMPONENTS_FILE}: {name} takes no members "
                              f"(kind {CATEGORIES[c.physical].kind if c.physical else '-'})")
        for role, target in c.members.items():
            if role not in role_spec:
                raise RosterError(f"{COMPONENTS_FILE}: {name} has unknown member "
                                  f"role {role!r} (allowed: {', '.join(role_spec)})")
            if target not in components:
                raise RosterError(f"{COMPONENTS_FILE}: {name}.{role} = {target!r} "
                                  f"is not a declared component")
            member_cat = components[target].physical
            if member_cat not in role_spec[role]:
                raise RosterError(
                    f"{COMPONENTS_FILE}: {name}.{role} = {target!r} must be one of "
                    f"{role_spec[role]} (its physical slot is {member_cat!r})")
        if c.physical:  # optional roles (e.g. Coupling's coupler satellite) may be omitted
            missing = (set(role_spec) - set(CATEGORIES[c.physical].optional_roles)
                       - set(c.members))
        else:
            missing = set(role_spec) - set(c.members)
        if missing:
            raise RosterError(f"{COMPONENTS_FILE}: {name} is missing member "
                              f"role(s): {', '.join(sorted(missing))}")
        # per-name field collision across the two slots
        if c.physical and c.instrument:
            clash = set(CATEGORIES[c.physical].fields) & set(CATEGORIES[c.instrument].fields)
            if clash:
                raise RosterError(f"{COMPONENTS_FILE}: {name}: field(s) "
                                  f"{sorted(clash)} declared by BOTH its categories "
                                  f"— routing would be ambiguous")
        # design keys: physical vocabulary, finite numbers
        if c.design and not c.physical:
            raise RosterError(f"{COMPONENTS_FILE}: {name} has design values but no "
                              f"physical category (design is physical-side only)")
        for f, v in c.design.items():
            if c.physical and f not in CATEGORIES[c.physical].fields:
                raise RosterError(
                    f"{COMPONENTS_FILE}: {name}.design.{f} is not a "
                    f"{c.physical} field ({', '.join(CATEGORIES[c.physical].fields)})")
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise RosterError(f"{COMPONENTS_FILE}: {name}.design.{f} = {v!r} "
                                  f"must be a finite number")
    return roster
