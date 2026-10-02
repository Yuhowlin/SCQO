"""Device layer — vendor views per owner + the recording wrapper.

The instrument surface follows the model (docs/store-by-line-plan.md): KNOBS
live on CHANNELS (``xy1.q1``), on LINES (a flux line's ``z1.idle_flux``) and on
composite OPERATIONS (``q1_q2.iswap``), so a driver implements

* one view class per CHANNEL KIND it realizes (:func:`make_view_base` —
  abstract read/write properties for the kind's channel knobs, and only
  knobs: monitors and facts have no vendor realization), served by entity
  name (``xy1.q1`` -> QUAM ``q1.xy``, ``feedline.q1`` -> ``q1.resonator``);
* one view class per kind of LINE knobs it realizes
  (:func:`make_line_view_base` — a flux line's bias and delay);
* one :class:`OperationView` per declared operation it realizes, a generic
  ``read_knob``/``write_knob`` pair by plain field name (``coupler_flux``).

A BORROWED channel (a coupler driven through a neighbour's line) is realized
only once the vendor carries an element for it; until then the driver answers
KeyError for it, like any entity it does not realize.

:class:`RecordingDevice` wraps a backend's :class:`DeviceModel` with the
state store (:mod:`scqo.stores`):

* WRITES push to the vendor FIRST (an instrument rejection must leave no
  false history), then record into the store, then reconcile every knob on
  the same WIRE against the vendor — the entity's own other knobs and those
  of every owner sharing its line — because one vendor setting can move
  several neutral fields, on one channel (the power chain moving its
  amplitude) or across channels (a port's full scale shared by everything on
  it). Those are the ``coupled_to`` echo rows. Each channel entity carries at
  most ONE chain anchor (its ``*_power_dbm``).
* READS of knobs serve the runtime config; in ``pull`` mode that config is
  seeded from the vendor at construction (the vendor wins at startup — safe
  while another tool also calibrates) with NO history rows (seeding is not a
  change); in ``push`` mode the store's saved knobs are pushed into the
  vendor (SCQO wins — devices it fully owns). Monitors read from the store
  (None until first measured) and never touch the vendor.
* The store persists only what SCQO actually recorded — scqo_state.json is
  the loop's memory, not a vendor mirror; run snapshots use
  :meth:`RecordingDevice.snapshot`, the merged runtime view.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field as _field
from functools import lru_cache
from typing import Literal

from .catalog import CHANNELS
from .entities import Channel, Composite, Line, Operation
from .roster import Roster
from .stores import Store

__all__ = [
    "ComponentInfo", "DeviceModel", "EntityView", "OperationView",
    "RecordingDevice", "make_line_view_base", "make_view_base",
]


class EntityView(ABC):
    """Backend-agnostic accessor for ONE entity's vendor-realized knobs."""

    name: str
    kind: str


@dataclass(frozen=True)
class ComponentInfo:
    """One entry of a driver's derived inventory (the doctor's witness) —
    the lock-signature shape: kind plus (for channels) targets."""

    kind: str
    target: tuple[str, ...] = ()
    line: str = ""
    operations: tuple[str, ...] = ()
    members: dict[str, tuple[str, ...]] = _field(default_factory=dict)


def _abstract_pair(field: str, doc: str) -> property:
    def getter(self):  # pragma: no cover - abstract surface
        raise NotImplementedError(field)

    def setter(self, value):  # pragma: no cover - abstract surface
        raise NotImplementedError(field)

    getter.__isabstractmethod__ = True  # type: ignore[attr-defined]
    setter.__isabstractmethod__ = True  # type: ignore[attr-defined]
    # property.__isabstractmethod__ is read-only, COMPUTED from the accessors
    # flagged above — do not assign it (AttributeError on CPython).
    return property(getter, setter, doc=doc)


@lru_cache(maxsize=None)
def make_view_base(kind: str) -> type[EntityView]:
    """The driver-facing abstract base for one CHANNEL KIND.

    One abstract read/write property per KNOB of the kind's channel fields —
    and only knobs: monitors (fidelity, blob positions) and facts have no
    vendor knob and must not burden driver classes. A backend that cannot
    realize a knob declares it Unrealized in its fieldmap instead of
    implementing it.
    """
    spec = CHANNELS[kind]
    ns: dict = {"kind": kind,
                "__doc__": f"Driver view base for {kind} channels."}
    for f, fs in spec.fields.items():
        if fs.role == "knob":
            ns[f] = _abstract_pair(
                f, f"{fs.doc} [{fs.unit}]" if fs.unit else fs.doc)
    return type(f"{kind.capitalize()}ChannelViewBase", (EntityView,), ns)


@lru_cache(maxsize=None)
def make_line_view_base(kind: str) -> type[EntityView]:
    """The driver-facing abstract base for the LINE knobs one channel kind
    puts on its wire (a flux line's ``idle_flux`` and ``flux_delay_s``)."""
    spec = CHANNELS[kind]
    ns: dict = {"kind": "line",
                "__doc__": f"Driver view base for a line carrying {kind}."}
    for f, fs in spec.line_fields.items():
        if fs.role == "knob":
            ns[f] = _abstract_pair(
                f, f"{fs.doc} [{fs.unit}]" if fs.unit else fs.doc)
    return type(f"{kind.capitalize()}LineViewBase", (EntityView,), ns)


class OperationView(EntityView):
    """Driver surface for one declared composite operation (``q1_q2.iswap``).

    The fields are OPERATION_FIELDS by plain name (``coupler_flux``); the
    contract is a generic pair so a driver maps each onto its gate macro and
    refuses by name what that gate cannot realize.
    """

    @abstractmethod
    def read_knob(self, field: str) -> float | list[float] | None:
        """Current vendor value of one operation knob (None = not set)."""

    @abstractmethod
    def write_knob(self, field: str, value) -> None:
        """Realize one operation knob on the vendor gate implementation."""


class DeviceModel(ABC):
    """Container of entity views plus persistence — a backend implements it
    over its native tree; entity names are ROSTER names: a channel
    (``xy1.q1``, ``feedline.q1``, a borrowed ``xy2.q1_q2_c``), a line
    (``z1``), an operation (``q1_q2.iswap``)."""

    @abstractmethod
    def component(self, name: str) -> EntityView:
        """The view for one vendor-realized entity. KeyError for names the
        vendor does not realize (modes, composites, a line without knobs, an
        unwired or not-yet-adopted borrowed channel)."""

    @abstractmethod
    def save(self) -> None:
        """Persist current vendor state (e.g. to the vendor's own JSON)."""

    @abstractmethod
    def snapshot(self) -> dict:
        """JSON-serialisable ``{entity: {knob: value}}`` of vendor-realized
        knob state."""

    def components(self) -> dict[str, ComponentInfo]:
        """The driver's DERIVED inventory read from the vendor tree — a
        WITNESS the doctor cross-checks against the authoritative roster,
        never the source of truth."""
        return {}


# -------------------------------------------------------- recording views

def _knob_property(field: str, doc: str) -> property:
    def getter(self):
        return self._parent._get_knob(self.name, field)

    def setter(self, value):
        self._parent._set_knob(self.name, field, value)

    return property(getter, setter, doc=doc)


def _monitor_property(field: str, doc: str) -> property:
    def getter(self):
        return self._parent._store.get(self.name, field)

    def setter(self, value):
        self._parent._record_only(self.name, field, value)

    return property(getter, setter, doc=doc)


def _loud_setattr(fields: tuple[str, ...]):
    def __setattr__(self, attr: str, value) -> None:
        # A write to an untracked name must fail LOUDLY (it used to vanish
        # into the instance dict).
        if isinstance(getattr(type(self), attr, None), property):
            object.__setattr__(self, attr, value)
            return
        raise AttributeError(
            f"{self.kind} entity {self.name!r} has no field {attr!r} — its "
            f"fields: {', '.join(fields) or '(none)'} (facts route through "
            f"the physical store)")
    return __setattr__


def _view_init(self, parent: "RecordingDevice", name: str) -> None:
    object.__setattr__(self, "name", name)
    object.__setattr__(self, "_parent", parent)


def _recording_view_class(label: str, kind: str, fields) -> type:
    """A recording view over ``fields`` ((name, FieldSpec) pairs): knob
    properties route through the RecordingDevice (store + vendor), monitor
    properties are record-only."""
    ns: dict = {"kind": kind, "__init__": _view_init}
    names: list[str] = []
    for f, fs in fields:
        doc = f"{fs.doc} [{fs.unit}]" if fs.unit else fs.doc
        if fs.role == "knob":
            ns[f] = _knob_property(f, doc)
            names.append(f)
        elif fs.role == "monitor":
            ns[f] = _monitor_property(f, doc)
            names.append(f)
    ns["__setattr__"] = _loud_setattr(tuple(sorted(names)))
    return type(f"Recording{label}View", (EntityView,), ns)


@lru_cache(maxsize=None)
def _recording_channel_view(kind: str, kinds: tuple[str, ...],
                            borrowed: bool) -> type:
    """Recording view for one channel: the channel fields of every kind on it
    (a combined drive+flux wire carries both), a borrowed one without the
    target-owned ``designed_only`` fields."""
    fields = [(f, fs) for k in kinds for f, fs in CHANNELS[k].fields.items()
              if not (borrowed and f in CHANNELS[k].designed_only)]
    label = "".join(k.capitalize() for k in kinds) + (
        "Borrowed" if borrowed else "")
    return _recording_view_class(label, kind, fields)


@lru_cache(maxsize=None)
def _recording_line_view(kinds: tuple[str, ...]) -> type:
    """Recording view for one line: the LINE fields of the kinds it carries."""
    fields = [(f, fs) for k in kinds for f, fs in CHANNELS[k].line_fields.items()]
    label = "".join(k.capitalize() for k in kinds) + "Line"
    return _recording_view_class(label, "line", fields)


class _RecordingOperationView(OperationView):
    """Recording surface for one declared operation (``q1_q2.iswap``): generic
    by plain field name, the legal set being OPERATION_FIELDS. Subclasses the
    OperationView ABC — everything that routes on ``isinstance(view,
    OperationView)`` (session writes, capture reads) must treat it as one."""

    def __init__(self, parent: "RecordingDevice", name: str) -> None:
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "kind", "operation")
        object.__setattr__(self, "_parent", parent)

    def __setattr__(self, attr: str, value) -> None:
        # Attribute writes must not vanish into the instance dict — the
        # operation surface is write_knob, by field name.
        raise AttributeError(
            f"operation {self.name!r} takes no attribute writes — "
            f"use write_knob({attr!r}, ...)")

    def read_knob(self, field: str) -> float | list[float] | None:
        spec = self._parent.roster.spec(self.name, field)  # exact-cause
        if spec.role == "knob":
            return self._parent._get_knob(self.name, field)
        return self._parent._store.get(self.name, field)

    def write_knob(self, field: str, value) -> None:
        spec = self._parent.roster.spec(self.name, field)
        if spec.role == "knob":
            self._parent._set_knob(self.name, field, value)
        else:  # monitor records; a fact write gets the store's pointer error
            self._parent._record_only(self.name, field, value)


def entity_view(parent, name: str) -> EntityView:
    """The view for one roster entity, over any parent that answers the
    recording protocol (``roster`` + ``_get_knob`` / ``_store`` / ``_set_knob``
    / ``_record_only``): the live :class:`RecordingDevice`, or the
    acquisition-time :class:`scqo.estimate_inputs.FrozenDevice`. Sharing the
    generated view classes is what makes their READ semantics identical by
    construction rather than by a second implementation that can drift."""
    roster = parent.roster
    e = roster.entities.get(name)
    if isinstance(e, Channel):
        return _recording_channel_view(e.kind, e.kinds, e.borrowed)(
            parent, name)
    if isinstance(e, Operation):
        return _RecordingOperationView(parent, name)
    if e is None:
        # KeyError, as for every entity a view cannot serve; the message names
        # the exact cause (an undeclared operation, a line not reaching a mode)
        raise KeyError(str(roster._unknown(name)))
    if isinstance(e, Line):
        kinds = tuple(dict.fromkeys(k for c in roster.channels_on(name)
                                    for k in c.kinds))
        if any(fs.role in ("knob", "monitor")
               for k in kinds for fs in CHANNELS[k].line_fields.values()):
            return _recording_line_view(kinds)(parent, name)
        riding = [c.name for c in roster.channels_on(name)]
        raise KeyError(
            f"{name!r} is a line whose functions ({', '.join(kinds) or 'none'}) "
            f"have no line-level knobs; the channels riding it: "
            f"{riding or '(none)'}")
    if isinstance(e, Composite):
        ops = sorted(f"{name}.{op}" for op in e.operations)
        raise KeyError(
            f"{name!r} is a composite — its knobs live on its operations: "
            f"{ops or '(none declared)'}")
    channels = [c.name for c in roster.channels_of(name)]
    raise KeyError(
        f"{name!r} is a {type(e).__name__.lower()} — it carries no "
        f"knobs; address its channels {channels or '(none wired)'}")


def resonator_of(roster, target: str) -> str:
    """The NAME of the target's attached resonator (unique qubit ref) — the
    topology hop update() and estimate() both take."""
    hits = [m.name for m in roster.modes().values()
            if m.kind == "resonator" and m.refs.get("qubit") == target]
    if len(hits) != 1:
        raise KeyError(
            f"{target!r} has {'no' if not hits else 'several'} attached "
            f"resonator(s){': ' + str(sorted(hits)) if hits else ''}")
    return hits[0]


# ---------------------------------------------------------------- recorder

class RecordingDevice:
    """Authoritative SCQO state + history over a backend's vendor device.

    Reads serve the runtime config (vendor-seeded in ``pull`` mode); writes
    record into the state store AND push to the vendor. The ROSTER decides
    which entities exist and which fields each carries.
    """

    def __init__(self, inner: DeviceModel, roster: Roster, store: Store, *,
                 on_load: Literal["pull", "push"] = "pull") -> None:
        self._inner = inner
        self.roster = roster
        self._store = store
        self._experiment: str | None = None
        self._run_id: str | None = None
        self._campaign_id: str | None = None
        #: runtime knob config: entity -> field -> value|None. Seeded from
        #: the vendor (pull) or the store (push). Seeding writes no rows —
        #: it is not a change — with one honest exception: push-mode chain
        #: RECONCILE echoes are genuine changes and do record (coupled_to).
        self._config: dict[str, dict[str, float | list[float] | None]] = {}
        if on_load == "push":
            self._seed_push()
        else:
            self._seed_pull()

    # ------------------------------------------------------------- seeding

    def _knob_entities(self):
        """Every owner with knobs: channels (designed and borrowed), lines,
        operations. A caller seeds a BORROWED channel only when the vendor or
        the store holds something for it - 40 unused ones must not each carry
        a row of Nones."""
        for name, e in self.roster.entities.items():
            if isinstance(e, (Channel, Line, Operation)):
                knobs = tuple(f for f, s in self.roster.fields_of(name).items()
                              if s.role == "knob")
                if knobs:
                    yield name, e, knobs

    @staticmethod
    def _copy(value):
        return list(value) if isinstance(value, list) else value

    def _seed_pull(self) -> None:
        """Vendor-authoritative seed: pushed knobs from the vendor snapshot
        (None where the vendor does not realize the entity/knob). No history
        rows — seeding is not a change."""
        vendor = self._inner.snapshot()
        for name, e, knobs in self._knob_entities():
            if isinstance(e, Channel) and e.borrowed and name not in vendor:
                continue  # not adopted on this instrument
            vend = vendor.get(name, {})
            self._config[name] = {f: self._copy(vend.get(f)) for f in knobs}

    def _seed_push(self) -> None:
        """SCQO-authoritative seed: the store's saved knobs are pushed into
        the vendor in catalog declaration order (amp before its power anchor,
        so the absolute power wins and the amp becomes the chain residual;
        dt before its waveform), then EVERY entity that pushed reconciles —
        against its power anchor when that was pushed, else against the last
        pushed field (old-recorder parity). Reconcile echoes DO record rows:
        a chain echo is a genuine change, unlike the seeding itself.
        Store-silent knobs backfill from the vendor snapshot."""
        vendor = self._inner.snapshot()
        saved = self._store.values()
        for name, e, knobs in self._knob_entities():
            if (isinstance(e, Channel) and e.borrowed and name not in vendor
                    and name not in saved):
                continue
            fields: dict = {}
            for f in knobs:
                value = saved.get(name, {}).get(f)
                fields[f] = self._copy(value if value is not None
                                       else vendor.get(name, {}).get(f))
            self._config[name] = fields
            pushed: list[str] = []
            for f in knobs:
                value = saved.get(name, {}).get(f)
                if value is None:
                    continue
                try:
                    self._vendor_write(name, f, self._copy(value))
                    pushed.append(f)
                except KeyError:
                    # A roster ENTITY the vendor does not realize (a mismatch
                    # the doctor witnesses) must not brick construction.
                    break
                except NotImplementedError:
                    # A single knob the backend declares Unrealized (its
                    # fieldmap says so, and `scqo state --fields` shows it):
                    # capability, not failure. Skip it and keep seeding the
                    # entity's other knobs.
                    continue
            if pushed:
                anchor = next((f for f in pushed
                               if f.endswith("_power_dbm")), pushed[-1])
                self._sync_coupled(name, anchor)

    # ------------------------------------------------------------- context

    def set_context(self, experiment: str | None,
                    run_id: str | None = None,
                    campaign_id: str | None = None) -> None:
        """Tag subsequent changes with the experiment/run — or, for a
        campaign-level accept, the campaign — causing them. The keyword
        defaults make the existing ``set_context(None, None)`` reset clear
        every stamp."""
        self._experiment = experiment
        self._run_id = run_id
        self._campaign_id = campaign_id

    # ------------------------------------------------------------- surface

    def component(self, name: str) -> EntityView:
        return entity_view(self, name)

    def channel(self, target: str, kind: str) -> EntityView:
        """The view of the target's DEFAULT (designed) channel of one kind —
        how update() addresses knobs without knowing wiring names
        (``device.channel(q, "readout").readout_freq_hz = ...``)."""
        return self.component(self.roster.default_channel(target, kind))

    def channel_on(self, line: str, target: str) -> EntityView:
        """The view of ``target``'s channel on ``line`` — designed or
        borrowed (``device.channel_on("xy2", "q1_q2_c").pi_amp``)."""
        return self.component(f"{line}.{target}")

    def line(self, name: str) -> EntityView:
        """The view of one line's own knobs (``device.line("z1").idle_flux``)."""
        return self.component(name)

    def flux_line(self, target: str) -> EntityView:
        """The line of ``target``'s designed flux channel - where its standing
        bias lives (``device.flux_line(q).idle_flux``)."""
        channel = self.roster.default_channel(target, "flux")
        return self.component(self.roster.entities[channel].line)

    def operation(self, composite: str, op: str) -> EntityView:
        """The view of one declared operation (``q1_q2.iswap``)."""
        return self.component(f"{composite}.{op}")

    def resonator_of(self, target: str) -> str:
        """The NAME of the target's attached resonator (unique qubit ref) —
        update() proposes resonator facts under it."""
        return resonator_of(self.roster, target)

    def snapshot(self) -> dict:
        """The merged runtime view (run before/after records): knobs from
        the runtime config PLUS the store's monitor values — the old
        snapshot carried both, and run records must keep the fidelity/blob
        context of the calibration they document."""
        out = {name: {f: self._copy(v) for f, v in fields.items()}
               for name, fields in self._config.items()}
        stored = self._store.values()
        for name, fields in stored.items():
            specs = self.roster.fields_of(name) if name in self.roster else {}
            for f, v in fields.items():
                if specs.get(f) is not None and specs[f].role == "monitor":
                    out.setdefault(name, {})[f] = self._copy(v)
        return out

    def save(self) -> None:
        """Persist the vendor config AND the SCQO store (+ history)."""
        self._inner.save()
        self._store.save()

    def history(self, **kwargs):
        return self._store.history(**kwargs)

    # ------------------------------------------------------------- plumbing

    def _vendor_write(self, entity: str, field: str, value) -> None:
        view = self._inner.component(entity)  # KeyError = not realized
        if isinstance(view, OperationView):
            view.write_knob(field, value)
        else:
            setattr(view, field, value)

    def _vendor_read(self, entity: str, field: str):
        view = self._inner.component(entity)
        if isinstance(view, OperationView):
            return view.read_knob(field)
        return getattr(view, field)

    def _get_knob(self, entity: str, field: str, *, strict: bool = True):
        value = self._config.get(entity, {}).get(field)
        if value is None and strict:
            raise KeyError(
                f"{entity}.{field} has no value yet — the vendor did not "
                f"seed it and nothing calibrated it (anchor order: standing "
                f"state, design value, code default)")
        return list(value) if isinstance(value, list) else value

    def _set_knob(self, entity: str, field: str, value) -> None:
        # Validate through the store BEFORE the vendor sees anything: a
        # store-invalid value (NaN list element, wrong shape, waveform
        # without its dt, paired-length break) must never reach the
        # instrument. Then push FIRST: if the instrument rejects the value,
        # the store and history must not claim a change that never happened.
        value = self._store.check(entity, field, value)
        self._vendor_write(entity, field, value)
        self._store.record(entity, field, value,
                           experiment=self._experiment, run_id=self._run_id,
                           campaign_id=self._campaign_id)
        self._config.setdefault(entity, {})[field] = self._store.get(
            entity, field)
        self._sync_coupled(entity, field)

    def _record_only(self, entity: str, field: str, value) -> None:
        self._store.record(entity, field, value,
                           experiment=self._experiment, run_id=self._run_id,
                           campaign_id=self._campaign_id)

    def _wire_mates(self, entity: str) -> list[str]:
        """``entity`` first, then every owner sharing its wire that the
        runtime config tracks - its line and the other channels on that line.
        One vendor setting can be port-level (an MW-FEM full scale shared by
        every channel on the port), so a write may move knobs of an entity
        that was not written (BACKLOG I28)."""
        e = self.roster.entities.get(entity)
        if isinstance(e, Channel):
            line = e.line
        elif isinstance(e, Line):
            line = entity
        else:
            return [entity]
        mates = [entity]
        for name, other in self.roster.entities.items():
            if name == entity or name not in self._config:
                continue
            if name == line or (isinstance(other, Channel)
                                and other.line == line):
                mates.append(name)
        return mates

    def _sync_coupled(self, entity: str, changed_field: str) -> None:
        """Reconcile vendor-side write echoes so the config never desyncs.

        One vendor knob may feed several neutral fields (setting
        readout_power_dbm re-solves the chain, moving readout_amp), and a
        port-level one moves the knobs of every channel on the port. After a
        push, re-read the OTHER knobs of the entity and of its wire mates from
        the vendor; any drifted value gets its own ChangeRecord
        (``coupled_to`` = the written field, as ``entity.field`` when the
        echo landed on a different entity).
        """
        for owner in self._wire_mates(entity):
            try:
                self._inner.component(owner)
            except KeyError:
                continue  # unrealized entity: nothing to reconcile
            cause = (changed_field if owner == entity
                     else f"{entity}.{changed_field}")
            knobs = (f for f, s in self.roster.fields_of(owner).items()
                     if s.role == "knob"
                     and not (owner == entity and f == changed_field))
            for other in knobs:
                try:
                    current = self._vendor_read(owner, other)
                except Exception:
                    continue
                if current is None or isinstance(current, list):
                    continue
                current = float(current)
                if not math.isfinite(current):  # never poison the store
                    continue
                if self._config.get(owner, {}).get(other) == current:
                    continue  # exact comparison — the staleness guard's rule
                self._store.record(owner, other, current,
                                   experiment=self._experiment,
                                   run_id=self._run_id,
                                   campaign_id=self._campaign_id,
                                   coupled_to=cause)
                self._config.setdefault(owner, {})[other] = current
