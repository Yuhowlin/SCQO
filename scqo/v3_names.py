"""The pre-4.0.0 store names, mapped onto the 4.0.0 entities.

Before 4.0.0 a rider minted a channel NAME from its target and a frozen suffix
(``q1_xy``, ``q1_ro``, ``q1_z``), an explicit ``[channels.<label>]`` was named by
its label, a composite's per-operation knob was flattened to ``<op>_<suffix>``
(``iswap_coupler_flux``), and a per-target fact on a multi-target channel to
``<field>__<target>``. 4.0.0 addresses the same values by owner
(docs/store-by-line-plan.md section 5.4):

    q1_xy.pi_amp                  -> xy1.q1.pi_amp
    q1_z.idle_flux                -> z1.idle_flux           (the LINE's)
    q1_z.flux_per_phi0            -> z1.q1.flux_per_phi0    (the channel's)
    q1_z.distortion_amp           -> z1.distortion_amp      (the LINE's)
    q1_q2.iswap_coupler_flux      -> q1_q2.iswap.coupler_flux
    coil.flux_per_phi0__q1        -> bias.q1.flux_per_phi0

The map is computed from the CURRENT roster: components.toml's syntax did not
change, so it still says which rider or label produced every old name.

This module is PERMANENT, unlike the one-time store conversion that uses it:
immutable run data keeps its old names forever - a pre-4.0.0 dataset.nc's
frozen estimate inputs, a pre-4.0.0 setup snapshot that ``scqo restore``
rebuilds, a pre-4.0.0 campaign's statistics - and reading them is the one
place old names are still understood.
"""

from __future__ import annotations

from typing import Any, Mapping

from .catalog import CHANNELS, OPERATION_FIELDS
from .entities import Channel, Composite, Mode
from .roster import Roster

#: The frozen suffix map riders used to mint channel names before 4.0.0.
V3_RIDER_SUFFIXES: dict[str, str] = {"drive": "_xy", "readout": "_ro",
                                     "flux": "_z"}


def v3_channel_names(channel: Channel) -> dict[str, str]:
    """kind -> the pre-4.0.0 name of that kind's channel on ``channel``."""
    out: dict[str, str] = {}
    for kind in channel.kinds:
        origin = channel.origins.get(kind)
        if origin is None:  # borrowed: never existed before 4.0.0
            continue
        out[kind] = (channel.target[0] + V3_RIDER_SUFFIXES[kind]
                     if origin == "rider" else origin)
    return out


def v3_address_map(roster: Roster) -> dict[tuple[str, str], tuple[str, str]]:
    """``(old entity, old field) -> (entity, field)`` for every value a
    pre-4.0.0 store could hold under this roster. Mode and composite FACTS keep
    their names and are included as identities, so a caller needs one lookup."""
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for name, e in roster.entities.items():
        if isinstance(e, (Mode, Composite)):
            for field in roster.fields_of(name):
                out[(name, field)] = (name, field)
        if isinstance(e, Composite):
            for op in e.operations:
                for suffix in OPERATION_FIELDS:
                    out[(name, f"{op}_{suffix}")] = (f"{name}.{op}", suffix)
    for ch in roster.channels().values():
        for kind, old in v3_channel_names(ch).items():
            spec = CHANNELS[kind]
            multi = ch.broadcast or len(ch.target) > 1
            target = ch.target[0]
            for field, fs in spec.fields.items():
                # Before 4.0.0 a FACT on a multi-target channel was per target
                # (<field>__<target>); knobs and monitors stayed singular.
                old_field = (f"{field}__{target}"
                             if multi and fs.role == "fact" else field)
                out[(old, old_field)] = (ch.name, field)
            for field, fs in spec.line_fields.items():
                old_field = (f"{field}__{target}"
                             if multi and fs.role == "fact" else field)
                out.setdefault((old, old_field), (ch.line, field))
    return out


def translate_values(values: Mapping[str, Mapping[str, Any]], roster: Roster
                     ) -> tuple[dict[str, dict[str, Any]],
                                dict[str, dict[str, Any]]]:
    """A pre-4.0.0 ``{entity: {field: value}}`` in 4.0.0 names, plus what did
    not map (an entity or field this roster no longer has). Two old values
    landing on one new key (several flux channels on one line each carrying
    the line's delay) keep the FIRST; the rest are reported as unmapped."""
    amap = v3_address_map(roster)
    out: dict[str, dict[str, Any]] = {}
    unmapped: dict[str, dict[str, Any]] = {}
    for old_entity, fields in values.items():
        if not isinstance(fields, Mapping):
            unmapped.setdefault(old_entity, {})["<value>"] = fields
            continue
        for old_field, value in fields.items():
            new = amap.get((old_entity, old_field))
            if new is None:
                unmapped.setdefault(old_entity, {})[old_field] = value
                continue
            entity, field = new
            slot = out.setdefault(entity, {})
            if field in slot:
                unmapped.setdefault(old_entity, {})[old_field] = value
                continue
            slot[field] = value
    return out, unmapped


def convert_store_payload(data: Mapping[str, Any], roster: Roster
                          ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """A 3.x store FILE (``{"schema": 3, "values": {entity: {field: v}}}``) as
    its 4.0.0 file (schema 4, nested by owner), plus what did not map. The
    values are carried unchanged - only their addresses move."""
    from .stores import STATE_SCHEMA, nest_values

    values = data.get("values") if isinstance(data, Mapping) else None
    flat, unmapped = translate_values(values or {}, roster)
    return {"schema": STATE_SCHEMA, "values": nest_values(flat)}, unmapped

