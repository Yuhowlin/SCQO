"""Roster entities — four thin dataclasses over one shared base.

Deliberately NOT one dataclass with an untyped attrs dict (the audit's
"honest in the spec" ruling): modes carry their kind's scalar refs, composites
carry ``roles`` (role -> member names) plus a typed ``operations`` tuple,
channels carry ``target``/``line``/``via``. Rider lists are consumed by the
expansion pass and never retained on the line entity — channels hold the line
ref, so the reverse lookup derives.

Entities are frozen and their mappings are wrapped read-only at construction:
the roster is immutable after load. ``signature()`` is exactly the
components.lock identity of docs/greenfield-schema.md section 7 —
(name, kind, target(s)) and NOTHING more, so doc-legal post-cut appends
(a new operation on a frozen composite, a rider moved to a promoted line)
never change a frozen signature; provenance, line, via, roles, and operations
are diagnostics/topology, not lock identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field as _field
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class Provenance:
    """Where a minted (derived) entity came from — error attribution ONLY,
    never part of the lock signature."""

    line: str
    rider: str
    index: int

    def __str__(self) -> str:  # doctor/error attribution
        return f"[lines.{self.line}] {self.rider}[{self.index}]"


@dataclass(frozen=True)
class Entity:
    """Shared base: one name in the single flat namespace, one kind."""

    name: str
    kind: str
    #: None for declared entities; set by the expansion pass for minted ones.
    derived: Provenance | None = None
    #: Post-cut decommissioning marker (doc section 7: never delete — store
    #: keys and history keep resolving). Parsed and carried now; addressing
    #: semantics land with the freeze tooling.
    retired: bool = False

    def signature(self) -> tuple:
        """The identity the production-cut lock freezes."""
        return (type(self).__name__, self.name, self.kind)


@dataclass(frozen=True)
class Mode(Entity):
    """A quantum degree of freedom. ``refs`` carries the kind's declared
    roles — today only the resonator's ``qubit`` attachment."""

    refs: Mapping[str, str] = _field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "refs", MappingProxyType(dict(self.refs)))


@dataclass(frozen=True)
class Composite(Entity):
    """A named mode group with joint physics. ``operations`` instantiate the
    OP_KNOBS family on this entity; appending one post-cut is legal, so it is
    NOT part of the signature."""

    roles: Mapping[str, tuple[str, ...]] = _field(default_factory=dict)
    operations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "roles", MappingProxyType(dict(self.roles)))


@dataclass(frozen=True)
class Line(Entity):
    """One physical control path reaching the sample. Pure topology node —
    zero neutral fields in v1; the vendor wiring annotation keys on its name."""

    kind: str = "line"


@dataclass(frozen=True)
class Channel(Entity):
    """One signal of one kind aimed at ``target``, riding ``line``.

    ``target`` is always a tuple (scalar-or-list normalized at parse; multi-
    target simply means ``len > 1``). ``via`` is the readout mediator mode.
    ``line`` and ``via`` are wiring, not lock identity — promoting a wire or
    re-mediating a readout is a new-context fact the doctor witnesses.
    """

    target: tuple[str, ...] = ()
    line: str = ""
    via: str | None = None

    def signature(self) -> tuple:
        return (*super().signature(), tuple(sorted(self.target)))
