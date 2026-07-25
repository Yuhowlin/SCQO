"""Device layer — vendor views per channel kind + the recording wrapper.

Greenfield replacement for :mod:`scqo.device` + the RecordingDevice half of
:mod:`scqo.config`. The instrument surface follows the model: KNOBS live on
channels and composites, so a driver implements

* one view class per CHANNEL KIND it realizes (:func:`make_view_base` —
  abstract read/write properties for the kind's knobs, and only knobs:
  monitors and facts have no vendor realization), served by entity name
  (``q1_xy`` -> QUAM ``q1.xy``, ``q1_ro`` -> ``q1.resonator``, ...);
* one :class:`CompositeView` per composite it realizes — per-operation knob
  names are entity-dependent (``iswap_coupler_flux``), so the surface is a
  generic ``read_knob``/``write_knob`` pair instead of properties.

:class:`RecordingDevice` wraps a backend's :class:`DeviceModel` with the
schema-3 state store (:mod:`scqo.model.stores`):

* WRITES push to the vendor FIRST (an instrument rejection must leave no
  false history), then record into the store, then reconcile the entity's
  other knobs against the vendor (one vendor knob can move several neutral
  fields — the ``coupled_to`` echo rows). Each channel entity carries at
  most ONE chain anchor now (its ``*_power_dbm``), so the old two-chain
  anchor dance is structural history.
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
from .entities import Channel, Composite
from .roster import Roster
from .stores import Store

__all__ = [
    "ComponentInfo", "CompositeView", "DeviceModel", "EntityView",
    "RecordingDevice", "make_view_base",
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

    One abstract read/write property per KNOB of the kind — and only knobs:
    monitors (fidelity, blob positions) and facts have no vendor knob and
    must not burden driver classes. A backend that cannot realize a knob
    declares it Unrealized in its fieldmap instead of implementing it.
    """
    spec = CHANNELS[kind]
    ns: dict = {"kind": kind,
                "__doc__": f"Driver view base for {kind} channels."}
    for f, fs in spec.fields.items():
        if fs.role == "knob":
            ns[f] = _abstract_pair(
                f, f"{fs.doc} [{fs.unit}]" if fs.unit else fs.doc)
    return type(f"{kind.capitalize()}ChannelViewBase", (EntityView,), ns)


class CompositeView(EntityView):
    """Driver surface for one composite's per-operation knobs.

    Knob names are entity-dependent (``<op>_<suffix>`` for each declared
    operation), so the contract is generic by full field name — the driver
    maps names onto its gate macros/pulses.
    """

    @abstractmethod
    def read_knob(self, field: str) -> float | list[float] | None:
        """Current vendor value of one per-operation knob (None = not set)."""

    @abstractmethod
    def write_knob(self, field: str, value) -> None:
        """Realize one per-operation knob on the vendor gate implementation."""


class DeviceModel(ABC):
    """Container of entity views plus persistence — a backend implements it
    over its native tree; entity names are ROSTER names (channel names for
    knob surfaces: ``q1_xy``, ``q1_ro``, ``q1_z``; composite names for
    gate surfaces)."""

    @abstractmethod
    def component(self, name: str) -> EntityView:
        """The view for one vendor-realized entity. KeyError for names the
        vendor does not realize (modes, lines, unwired channels)."""

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


@lru_cache(maxsize=None)
def _recording_channel_view(kind: str) -> type:
    """Recording view for one channel kind: knob properties route through
    the RecordingDevice (store + vendor), monitor properties are
    record-only."""
    ns: dict = {"kind": kind, "__init__": _view_init}
    fields: list[str] = []
    for f, fs in CHANNELS[kind].fields.items():
        doc = f"{fs.doc} [{fs.unit}]" if fs.unit else fs.doc
        if fs.role == "knob":
            ns[f] = _knob_property(f, doc)
            fields.append(f)
        elif fs.role == "monitor":
            ns[f] = _monitor_property(f, doc)
            fields.append(f)
    ns["__setattr__"] = _loud_setattr(tuple(sorted(fields)))
    return type(f"Recording{kind.capitalize()}View", (EntityView,), ns)


class _RecordingCompositeView(EntityView):
    """Recording surface for one composite: generic by full knob name (the
    legal set is the entity's compiled fields). Same read/write symmetry as
    the channel views: knobs read strict, facts are refused with the
    physical-store pointer in BOTH directions."""

    def __init__(self, parent: "RecordingDevice", name: str) -> None:
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "kind",
                           parent.roster.entities[name].kind)
        object.__setattr__(self, "_parent", parent)

    def __setattr__(self, attr: str, value) -> None:
        # Attribute writes must not vanish into the instance dict — the
        # composite surface is write_knob, by full field name.
        raise AttributeError(
            f"{self.kind} entity {self.name!r} takes no attribute writes — "
            f"use write_knob({attr!r}, ...)")

    def read_knob(self, field: str) -> float | list[float] | None:
        spec = self._parent.roster.spec(self.name, field)  # exact-cause
        if spec.role == "knob":
            return self._parent._get_knob(self.name, field)
        if spec.role == "monitor":
            return self._parent._store.get(self.name, field)
        raise KeyError(
            f"{self.name}.{field} is a fact — it lives in physical.json; "
            f"read it through the physical store, not the device")

    def write_knob(self, field: str, value) -> None:
        spec = self._parent.roster.spec(self.name, field)
        if spec.role == "knob":
            self._parent._set_knob(self.name, field, value)
        else:  # monitor records; a fact write gets the store's pointer error
            self._parent._record_only(self.name, field, value)


# ---------------------------------------------------------------- recorder

class RecordingDevice:
    """Authoritative SCQO state + history over a backend's vendor device.

    Reads serve the runtime config (vendor-seeded in ``pull`` mode); writes
    record into the schema-3 state store AND push to the vendor. The ROSTER
    decides which entities exist and which fields each carries.
    """

    def __init__(self, inner: DeviceModel, roster: Roster, store: Store, *,
                 on_load: Literal["pull", "push"] = "pull") -> None:
        self._inner = inner
        self.roster = roster
        self._store = store
        self._experiment: str | None = None
        self._run_id: str | None = None
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
        for name, e in self.roster.entities.items():
            if isinstance(e, (Channel, Composite)):
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
        for name, _e, knobs in self._knob_entities():
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
        for name, _e, knobs in self._knob_entities():
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
                    # A roster entity the vendor does not realize (a mismatch
                    # the doctor witnesses) must not brick construction.
                    break
            if pushed:
                anchor = next((f for f in pushed
                               if f.endswith("_power_dbm")), pushed[-1])
                self._sync_coupled(name, anchor)

    # ------------------------------------------------------------- context

    def set_context(self, experiment: str | None,
                    run_id: str | None = None) -> None:
        """Tag subsequent changes with the experiment/run causing them."""
        self._experiment = experiment
        self._run_id = run_id

    # ------------------------------------------------------------- surface

    def component(self, name: str) -> EntityView:
        e = self.roster.entities.get(name)
        if isinstance(e, Channel):
            return _recording_channel_view(e.kind)(self, name)
        if isinstance(e, Composite):
            return _RecordingCompositeView(self, name)
        if e is None:
            raise KeyError(f"unknown entity {name!r}")
        from .entities import Line
        if isinstance(e, Line):
            riding = [c.name for c in self.roster.channels().values()
                      if c.line == name]
            raise KeyError(
                f"{name!r} is a line — it carries no knobs; the channels "
                f"riding it: {riding or '(none)'}")
        channels = [c.name for c in self.roster.channels_of(name)]
        raise KeyError(
            f"{name!r} is a {type(e).__name__.lower()} — it carries no "
            f"knobs; address its channels {channels or '(none wired)'}")

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

    def history(self):
        return self._store.history()

    # ------------------------------------------------------------- plumbing

    def _vendor_write(self, entity: str, field: str, value) -> None:
        view = self._inner.component(entity)  # KeyError = not realized
        if isinstance(view, CompositeView):
            view.write_knob(field, value)
        else:
            setattr(view, field, value)

    def _vendor_read(self, entity: str, field: str):
        view = self._inner.component(entity)
        if isinstance(view, CompositeView):
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
                           experiment=self._experiment, run_id=self._run_id)
        self._config.setdefault(entity, {})[field] = self._store.get(
            entity, field)
        self._sync_coupled(entity, field)

    def _record_only(self, entity: str, field: str, value) -> None:
        self._store.record(entity, field, value,
                           experiment=self._experiment, run_id=self._run_id)

    def _sync_coupled(self, entity: str, changed_field: str) -> None:
        """Reconcile vendor-side write echoes so the config never desyncs.

        One vendor knob may feed several neutral fields (setting
        readout_power_dbm re-solves the chain, moving readout_amp). After a
        push, re-read the entity's OTHER knobs from the vendor; any drifted
        value gets its own ChangeRecord (``coupled_to`` = the written field).
        """
        try:
            self._inner.component(entity)
        except KeyError:
            return  # unrealized entity: nothing to reconcile
        knobs = (f for f, s in self.roster.fields_of(entity).items()
                 if s.role == "knob" and f != changed_field)
        for other in knobs:
            try:
                current = self._vendor_read(entity, other)
            except Exception:
                continue
            if current is None or isinstance(current, list):
                continue
            current = float(current)
            if not math.isfinite(current):  # never poison the store
                continue
            if self._config.get(entity, {}).get(other) == current:
                continue  # exact comparison — the staleness guard's rule
            self._store.record(entity, other, current,
                               experiment=self._experiment,
                               run_id=self._run_id,
                               coupled_to=changed_field)
            self._config.setdefault(entity, {})[other] = current
