"""Roster entities — five thin dataclasses over one shared base.

Deliberately NOT one dataclass with an untyped attrs dict (the audit's
"honest in the spec" ruling): modes carry their kind's scalar refs, composites
carry ``roles`` (role -> member names) plus a typed ``operations`` tuple, each
declared operation is an entity of its own (``<composite>.<op>``), and channels
carry ``target``/``line``/``via``. Rider lists are consumed by the expansion
pass and never retained on the line entity — channels hold the line ref, so the
reverse lookup derives.

Every entity is one OWNER of values in the stores (docs/store-by-line-plan.md):
a mode, a composite, an operation, a line, or a channel. Declared names are
identifiers; the two derived kinds of name carry one dot - ``<line>.<target>``
for a channel, ``<composite>.<op>`` for an operation - which is exactly the
nesting of the store files.

Entities are frozen and their mappings are wrapped read-only at construction:
the roster is immutable after load. ``signature()`` is exactly the
components.lock identity of docs/greenfield-schema.md section 7 —
(name, kind, target(s)) and NOTHING more, so doc-legal post-cut appends
(a new operation on a frozen composite) never change a frozen signature;
provenance, via, roles and operations are diagnostics/topology, not lock
identity. Operations and borrowed channels are never locked at all.
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
    """A named mode group with joint physics. Each of its ``operations`` is an
    entity of its own (``<composite>.<op>``); appending one post-cut is legal,
    so they are NOT part of the signature."""

    roles: Mapping[str, tuple[str, ...]] = _field(default_factory=dict)
    operations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "roles", MappingProxyType(dict(self.roles)))


@dataclass(frozen=True)
class Operation(Entity):
    """One declared operation of a composite, ``<composite>.<op>`` - the owner
    of that gate's knobs (OPERATION_FIELDS). Declared on the composite, so it
    carries no signature of its own."""

    kind: str = "operation"
    composite: str = ""
    op: str = ""


@dataclass(frozen=True)
class Line(Entity):
    """One physical control path reaching the sample. Owner of the fields that
    exist once per wire whatever rides it (a flux line's DC offset, delay and
    impulse response); the vendor wiring annotation keys on its name."""

    kind: str = "line"


@dataclass(frozen=True)
class Channel(Entity):
    """The signal(s) on ``line`` aimed at ``target``, named ``<line>.<target>``
    (a multi-target or pump channel by its declared label, ``<line>.<label>``).

    ``kinds`` are every channel kind on that (line, target) - a combined
    drive+flux wire puts two on one channel; ``kind`` is the one that carries
    knobs, else the first. ``target`` is always a tuple (multi-target simply
    means ``len > 1``). ``via`` is the readout mediator mode. ``borrowed``
    marks a channel no roster entry declares: a mode driven through a line
    that does not carry it by design (docs/store-by-line-plan.md section 2.2).
    ``broadcast`` marks one target's share of a multi-target declaration (a
    flux coil reaching several SQUIDs): it never takes the target's default
    slot, which stays with the target's own line.
    ``origins`` records, per kind, how the channel was declared - "rider" or
    the explicit [channels.<label>] - which is all the pre-4.0.0 store names
    are derived from (scqo.v3_names).
    """

    kinds: tuple[str, ...] = ()
    target: tuple[str, ...] = ()
    line: str = ""
    via: str | None = None
    borrowed: bool = False
    broadcast: bool = False
    origins: Mapping[str, str] = _field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.kinds:
            object.__setattr__(self, "kinds", (self.kind,))
        object.__setattr__(self, "origins",
                           MappingProxyType(dict(self.origins)))

    def signature(self) -> tuple:
        return (type(self).__name__, self.name, tuple(sorted(self.kinds)),
                tuple(sorted(self.target)))
