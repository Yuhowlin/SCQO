"""The design.toml loader — the chip datasheet, validated against the roster.

docs/greenfield-schema.md section 7 ("design.toml"): entity-named tables (the
``design.`` prefix dissolved into the filename), loaded and validated AFTER
roster expansion — an unknown entity is a load error, a field that is not
design-legal for the entity's kind is a load error naming the legal set.
Context-free vocabulary only, enforced per field by ``FieldSpec.design_ok``
(f_01_hz design-legal solely on fixed transmons; flux-tunables use
f_q_max_hz). Shape mirrors physical.json key-for-key, which makes the
doctor's design-vs-measured column a trivial join (:meth:`Design.compare`).

Design values are DECLARATIONS, not measurements: hand-edited TOML, no
provenance flow, editable after the production cut. A missing file is an
empty datasheet, not an error — design is optional per entity and per field.

The bring-up seed lives here too: :func:`seed_value` resolves a channel
knob's ``design_source`` hop (``drive_freq_hz <- target.f_01_hz``,
``readout_freq_hz <- via.f_r_hz``). The anchor order stays standing state,
else design (this module), else code default — callers try in that order.
"""

from __future__ import annotations

import math

try:  # the repo-wide pattern: stdlib tomllib on 3.11+, tomli backport below
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - py<3.11 envs
    import tomli as tomllib  # type: ignore[no-redef]

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .entities import Channel
from .roster import Roster, RosterError

DESIGN_FILE = "design.toml"
SCHEMA = 1


class DesignError(ValueError):
    """A design.toml that cannot be loaded correctly must fail loudly."""


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise DesignError(msg)


@dataclass(frozen=True)
class Design:
    """One device's validated datasheet: {entity: {field: value}} — read-only
    after construction (same immutability pattern as the roster entities)."""

    values: Mapping[str, Mapping[str, float]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(
            {e: MappingProxyType(dict(f)) for e, f in self.values.items()}))

    def get(self, entity: str, field: str) -> float | None:
        """The declared target (None when not declared — design is sparse)."""
        return self.values.get(entity, {}).get(field)

    def compare(self, measured: dict[str, dict[str, float]]
                ) -> list[tuple[str, str, float, float | None]]:
        """The design-vs-measured join, key-for-key against a physical.json
        ``values`` block: (entity, field, designed, measured-or-None) rows,
        in the datasheet's declaration order."""
        return [(entity, field, designed,
                 measured.get(entity, {}).get(field))
                for entity, fields in self.values.items()
                for field, designed in fields.items()]


def parse_design(text: str, roster: Roster, *,
                 source: str = DESIGN_FILE) -> Design:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as err:
        raise DesignError(f"{source}: {err}") from None
    stamp = data.get("schema")
    _require(isinstance(stamp, int) and not isinstance(stamp, bool)
             and stamp == SCHEMA,
             f"{source}: schema = {SCHEMA} required (the integer; found "
             f"{stamp!r})")
    values: dict[str, dict[str, float]] = {}
    for entity, table in data.items():
        if entity == "schema":
            continue
        where = f"{source} [{entity}]"
        _require(isinstance(table, dict), f"{where}: expected a table")
        _require(entity in roster,
                 f"{where}: unknown entity — design keys must name entities "
                 f"of the EXPANDED roster (derived names like q1_res are "
                 f"fine)")
        legal = roster.fields_of(entity, design=True)
        fields: dict[str, float] = {}
        for field, value in table.items():
            if field not in legal:
                raise DesignError(_design_illegal(roster, entity, field,
                                                  legal, where))
            _require(isinstance(value, (int, float))
                     and not isinstance(value, bool),
                     f"{where}.{field}: expected a number, got {value!r}")
            try:
                value = float(value)
            except OverflowError:  # arbitrary-precision TOML int beyond float
                raise DesignError(
                    f"{where}.{field}: refusing non-finite {value!r}"
                    ) from None
            _require(math.isfinite(value),
                     f"{where}.{field}: refusing non-finite {value!r}")
            fields[field] = value
        values[entity] = fields
    return Design(values)


def _design_illegal(roster: Roster, entity: str, field: str,
                    legal: dict, where: str) -> str:
    """Exact-cause message for a non-design-legal field: redirect to the
    right home instead of printing an empty legal set."""
    store_spec = roster.fields_of(entity).get(field)
    if store_spec is not None:
        home = ("a calibration knob — it lives in scqo_state.json"
                if store_spec.role == "knob" else
                "measured — it lives in "
                + ("physical.json" if store_spec.role == "fact"
                   else "scqo_state.json (monitor)"))
        hint = (f"; design-legal here: {sorted(legal)}" if legal else "")
        return (f"{where}.{field}: {home}, never the datasheet (design "
                f"values are declared targets, not chosen or measured "
                f"ones){hint}")
    if not legal:
        return (f"{where}.{field}: no field is design-legal on this entity "
                f"kind — design targets live on modes and composites; "
                f"channel and line values belong in the stores")
    return (f"{where}.{field}: not design-legal for this entity "
            f"(context-free design vocabulary: {sorted(legal)})")


def load_design(device_dir: str | Path, roster: Roster) -> Design:
    """The device's datasheet, beside its ``components.toml`` (``device_dir``
    is the device folder; a direct path to the file itself is accepted too).
    A missing file is an empty (all-defaults) datasheet."""
    path = Path(device_dir)
    if path.is_dir() or path.suffix != ".toml":
        path = path / DESIGN_FILE
    if not path.is_file():
        return Design({})
    # utf-8-sig: PowerShell 5.1's `-Encoding utf8` writes a BOM into this
    # hand-edited file; tolerate it (same rule as the roster loader).
    return parse_design(path.read_text(encoding="utf-8-sig"), roster,
                        source=str(path))


def seed_anchor(roster: Roster, channel: str,
                field: str) -> tuple[str, tuple[str, ...]] | None:
    """STRUCTURAL half of the seed lookup: the (entity, candidate facts) a
    channel knob's ``design_source`` hop points at, independent of any
    datasheet — what the field catalog shows. None when the knob declares
    no source or the hop does not resolve."""
    try:
        spec = roster.fields_of(channel).get(field)
    except RosterError as err:  # one exception surface for the seed lookup
        raise DesignError(str(err)) from None
    _require(spec is not None,
             f"{channel}.{field}: unknown field (seed lookup)")
    if spec.design_source is None:
        return None
    hop, facts = spec.design_source
    if isinstance(facts, str):
        facts = (facts,)
    entity = roster.entities[channel]
    if not isinstance(entity, Channel):
        return None
    if hop is None:
        anchor = entity.target[0] if len(entity.target) == 1 else None
    elif hop == "via":
        anchor = entity.via
    else:  # a future ref role; silent None keeps the anchor order intact
        anchor = None
    return None if anchor is None else (anchor, facts)


def seed_source(roster: Roster, design: Design, channel: str,
                field: str) -> tuple[str, str] | None:
    """The (entity, fact) a channel knob's ``design_source`` hop resolves to
    — with candidate facts, the first one the datasheet actually declares
    (so drive_freq_hz seeds from f_01_hz on fixed transmons and f_q_max_hz
    on flux-tunables). None when the knob declares no source, the hop does
    not resolve (multi-target channel, no via), or no candidate is
    declared."""
    resolved = seed_anchor(roster, channel, field)
    if resolved is None:
        return None
    anchor, facts = resolved
    for fact in facts:
        if design.get(anchor, fact) is not None:
            return anchor, fact
    return None


def seed_value(roster: Roster, design: Design, channel: str,
               field: str) -> float | None:
    """The design fallback for one channel knob — the datasheet value at its
    :func:`seed_source`, or None (callers fall through to code defaults)."""
    source = seed_source(roster, design, channel, field)
    if source is None:
        return None
    return design.get(*source)
