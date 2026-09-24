"""Frozen estimate inputs — the acquisition-time snapshot embedded in
``dataset.nc``, and the read-only surface ``estimate()`` runs against.

The mirror of :class:`scqo.suggestions.SuggestionCapture`. That one wraps
``update()``: reads pass through to the live device, writes become proposals.
This one wraps ``estimate()``: reads come from the snapshot the dataset
carries, writes are refused by name.

Why it exists: ``estimate()`` reads device values through FOUR paths
(``anchor()``, ``fact()``/``fact_sourced()``, ``self.device.channel(...)``
directly, ``self.physical``/``self.design`` directly) at ~60 call sites. Those
reads used to hit the LIVE device, so re-fitting a saved dataset later saw
today's values, not the ones the data was taken with —
``resonator_spectroscopy`` turns a detuning into an absolute frequency through
``anchor(q, "readout_freq_hz")``, so once the run's own suggestion is accepted
a re-fit adds the correction twice. Intercepting per experiment is not
possible; the interception point has to be the three SURFACES.

The live run goes through the frozen surface too (:meth:`Experiment.
run_estimate`). What is stored and what is analysed therefore cannot disagree
— that is structural, not a test — and any divergence between this surface and
:class:`~scqo.device.RecordingDevice` breaks the offline suite of every
experiment immediately. To keep that true, :class:`FrozenDevice` does NOT
reimplement the entity views: it implements the parent protocol the recording
views already call (``_get_knob`` / ``_store.get`` / ``_set_knob`` /
``_record_only`` / ``roster``) and hands out the SAME generated view classes.

Embedded ``dataset.nc`` global attrs (values are JSON strings, except the
integer schema):

======================  =================================================
``scqo_schema``         embedding format version (see :data:`SCHEMA`)
``scqo_experiment``     registered experiment name
``scqo_parameters``     the run's Parameters
``scqo_device``         ``RecordingDevice.snapshot()``: knobs + monitors
``scqo_physical``       ``physical.values()`` — facts (``null`` standalone)
``scqo_design``         ``design.values`` — the datasheet
``scqo_acquisition``    per-experiment values ``run()`` computed and
                        ``estimate()`` needs (kept out of instance state)
``scqo_run``            run_id / era / versions, stamped by the Session
======================  =================================================

Immutable run data is the one exemption from the no-backward-compatibility
rule, so readers check :data:`SCHEMA` and refuse an unknown one by name.
"""

from __future__ import annotations

import json
import math
import sys
from typing import Any, Mapping

from .design import Design
from .device import entity_view, resonator_of

#: Embedding format version. Bump ONLY for a change that makes an older
#: dataset unreadable by :func:`load_frozen` (e.g. splitting the device
#: snapshot into per-entity attrs to stay under the HDF5 attribute limit).
SCHEMA = 1

PREFIX = "scqo_"
ATTR_SCHEMA = "scqo_schema"
ATTR_EXPERIMENT = "scqo_experiment"
ATTR_PARAMETERS = "scqo_parameters"
ATTR_DEVICE = "scqo_device"
ATTR_PHYSICAL = "scqo_physical"
ATTR_DESIGN = "scqo_design"
ATTR_ACQUISITION = "scqo_acquisition"
ATTR_RUN = "scqo_run"

#: HDF5 stores an attribute compactly only up to 64 KB; past that netCDF
#: needs a dense attribute and some readers choke. A 5-qubit device embeds
#: ~10 KB, so this is a warning line, not a limit we expect to meet.
ATTR_SIZE_WARN = 48 * 1024


class FrozenWriteError(RuntimeError):
    """A write reached the frozen estimate surface. ``estimate()`` reads;
    ``update()`` writes — and it runs against the live device, later."""


class MissingEmbeddedInputs(RuntimeError):
    """The dataset carries no acquisition-time snapshot (or one this version
    cannot read), so ``estimate()`` has no inputs to run against."""


# --------------------------------------------------------------- encoding

def _plain(value: Any) -> Any:
    """JSON-able deep copy of a nested mapping/sequence (MappingProxyType,
    numpy scalars and tuples all appear in these snapshots)."""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    item = getattr(value, "item", None)  # numpy scalar
    if callable(item):
        try:
            return _plain(item())
        except Exception:  # noqa: BLE001 - fall through to the string form
            pass
    if isinstance(value, str):
        return value
    return str(value)


def _scrub(value: Any) -> Any:
    """:func:`_plain`, with every non-finite float replaced by ``None`` — the
    fallback when a value refuses to encode. Provenance degrades; the
    measurement is never lost over it."""
    if isinstance(value, Mapping):
        return {str(k): _scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return _plain(value)


def _dumps(value: Any, what: str) -> str:
    """Compact JSON, or a scrubbed version of it with a warning. NEVER
    raises: a provenance failure must not kill a measurement."""
    try:
        return json.dumps(_plain(value), allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as err:
        print(f"# scqo: could not embed {what} verbatim ({type(err).__name__}: "
              f"{err}); non-finite values dropped", file=sys.stderr)
    try:
        return json.dumps(_scrub(value), allow_nan=False, separators=(",", ":"))
    except Exception as err:  # noqa: BLE001 - last resort, still no raise
        print(f"# scqo: {what} could not be embedded at all "
              f"({type(err).__name__}: {err})", file=sys.stderr)
        return "null"


def _loads(dataset, attr: str, what: str):
    raw = dataset.attrs.get(attr)
    if raw is None:
        raise MissingEmbeddedInputs(
            f"dataset.nc carries no {attr!r} ({what}) — it was acquired "
            f"before the embedded snapshot existed, or by a run() override "
            f"that bypassed Experiment.run_estimate()")
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as err:
        raise MissingEmbeddedInputs(
            f"dataset.nc has {attr!r} but it is not readable JSON "
            f"({type(err).__name__}: {err})") from err


def embed(dataset, *, experiment: str, params: Any, device: dict,
          physical: dict | None, design: Any) -> None:
    """Write the acquisition-time snapshot onto ``dataset``'s global attrs.

    Called from :meth:`Experiment.run_estimate` at the moment the live reads
    used to happen: acquire finished, boundary writes reverted, ``estimate()``
    not yet started. ``design`` takes a :class:`~scqo.design.Design` or its
    ``values`` mapping.
    """
    dataset.attrs[ATTR_SCHEMA] = SCHEMA
    dataset.attrs[ATTR_EXPERIMENT] = str(experiment)
    dump = getattr(params, "model_dump", None)
    dataset.attrs[ATTR_PARAMETERS] = _dumps(
        dump(mode="json") if callable(dump) else params, "parameters")
    dataset.attrs[ATTR_DEVICE] = _dumps(device, "the device snapshot")
    dataset.attrs[ATTR_PHYSICAL] = _dumps(physical, "the physical facts")
    dataset.attrs[ATTR_DESIGN] = _dumps(
        getattr(design, "values", design), "the design values")
    oversized = [k for k in (ATTR_DEVICE, ATTR_PHYSICAL, ATTR_DESIGN,
                             ATTR_PARAMETERS)
                 if len(dataset.attrs[k]) > ATTR_SIZE_WARN]
    if oversized:
        print(f"# scqo: embedded {', '.join(oversized)} exceeds "
              f"{ATTR_SIZE_WARN} bytes — close to the HDF5 attribute limit",
              file=sys.stderr)


def embed_run(dataset, run: dict) -> None:
    """Stamp the run's identity (run_id, era, versions) onto the dataset —
    the Session's half of the embedding, written before persisting."""
    dataset.attrs[ATTR_RUN] = _dumps(run, "the run stamps")


def is_embedded(dataset) -> bool:
    """Has this dataset been through :meth:`Experiment.run_estimate`?"""
    return dataset is not None and ATTR_SCHEMA in getattr(dataset, "attrs", {})


def note_acquisition(dataset, key: str, value: Any) -> None:
    """Record one acquisition-time value ``estimate()`` will need.

    The home for what ``run()`` computes and ``estimate()`` consumes — a
    Bayesian prior, a punchout's top-of-sweep chain context. Kept on the
    DATASET rather than on the instance, because a re-fit builds a fresh
    instance and would otherwise find the attribute missing (loudly) or
    default (silently).
    """
    notes = {}
    raw = dataset.attrs.get(ATTR_ACQUISITION)
    if raw:
        try:
            notes = json.loads(raw)
        except (TypeError, ValueError):
            notes = {}
    notes[str(key)] = _plain(value)
    dataset.attrs[ATTR_ACQUISITION] = _dumps(notes, "acquisition notes")


def acquisition_note(dataset, key: str, default: Any = None) -> Any:
    """Read back a :func:`note_acquisition` value (``default`` when absent)."""
    raw = getattr(dataset, "attrs", {}).get(ATTR_ACQUISITION)
    if not raw:
        return default
    try:
        notes = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return notes.get(str(key), default)


def strip_attrs(dataset):
    """A view of ``dataset`` without the ``scqo_*`` attrs — what scqat sees.

    scqat reads datasets, not SCQO's embedding, and the two places that copy
    ``dataset.attrs`` into ``plotdata.nc`` would otherwise carry ~10 KB of
    JSON along with them.
    """
    extra = [k for k in getattr(dataset, "attrs", {}) if k.startswith(PREFIX)]
    if not extra:
        return dataset
    out = dataset.copy()
    for key in extra:
        out.attrs.pop(key, None)
    return out


# ------------------------------------------------------------ frozen surface

def _copy(value):
    return list(value) if isinstance(value, list) else value


class FrozenStore:
    """Read-only twin of :class:`scqo.stores.Store`'s READ surface — the one
    ``Experiment.fact_sourced`` and the flux family use."""

    def __init__(self, values: dict, *, reads: list | None = None,
                 source: str = "physical") -> None:
        self._values = {e: dict(f) for e, f in (values or {}).items()}
        self._reads = reads if reads is not None else []
        self._source = source

    def get(self, entity: str, field: str):
        self._reads.append((self._source, entity, field))
        return _copy(self._values.get(entity, {}).get(field))

    def values(self) -> dict:
        return {e: {f: _copy(v) for f, v in fields.items()}
                for e, fields in self._values.items()}


class FrozenDesign(Design):
    """:class:`~scqo.design.Design` that records which datasheet values a fit
    leaned on. Same class, so ``seed_source`` and ``compare`` are unchanged."""

    def __init__(self, values, *, reads: list | None = None) -> None:
        super().__init__(values)
        object.__setattr__(self, "_reads", reads if reads is not None else [])

    def get(self, entity: str, field: str):
        self._reads.append(("design", entity, field))
        return super().get(entity, field)


class FrozenDevice:
    """The acquisition-time device, read-only.

    Implements the parent protocol the recording entity views call, so
    :func:`scqo.device.entity_view` hands out the very same view classes the
    live device uses: knob reads are strict (``KeyError`` when unset, which is
    how ``anchor()`` falls through to the design seed), monitor reads return
    ``None`` when absent, composites route through ``read_knob``, facts are
    refused with the physical-store pointer — none of it reimplemented here.
    """

    def __init__(self, roster, snapshot: dict, *,
                 reads: list | None = None) -> None:
        self.roster = roster
        self._reads = reads if reads is not None else []
        knobs: dict[str, dict] = {}
        monitors: dict[str, dict] = {}
        for entity, fields in (snapshot or {}).items():
            specs = roster.fields_of(entity) if entity in roster else {}
            for field, value in fields.items():
                spec = specs.get(field)
                bucket = monitors if (spec is not None
                                      and spec.role == "monitor") else knobs
                bucket.setdefault(entity, {})[field] = _copy(value)
        self._knobs = knobs
        self._store = FrozenStore(monitors, reads=self._reads,
                                  source="monitor")

    # ------------------------------------------------------------- surface
    def component(self, name: str):
        return entity_view(self, name)

    def channel(self, target: str, kind: str):
        return self.component(self.roster.default_channel(target, kind))

    def resonator_of(self, target: str) -> str:
        return resonator_of(self.roster, target)

    def snapshot(self) -> dict:
        """The same merged ``{entity: {field: value}}`` shape
        :meth:`~scqo.device.RecordingDevice.snapshot` returns."""
        out = {name: {f: _copy(v) for f, v in fields.items()}
               for name, fields in self._knobs.items()}
        for name, fields in self._store.values().items():
            out.setdefault(name, {}).update(fields)
        return out

    # ------------------------------------------------------------ plumbing
    def _get_knob(self, entity: str, field: str, *, strict: bool = True):
        self._reads.append(("device", entity, field))
        value = self._knobs.get(entity, {}).get(field)
        if value is None and strict:
            raise KeyError(
                f"{entity}.{field} had no value when this data was acquired "
                f"(frozen snapshot from dataset.nc: the vendor had not "
                f"seeded it and nothing had calibrated it)")
        return _copy(value)

    def _set_knob(self, entity: str, field: str, value) -> None:
        raise FrozenWriteError(
            f"estimate() tried to write {entity}.{field} = {value!r}: the "
            f"estimate surface is the acquisition-time snapshot and is "
            f"read-only — propose the value from update() instead")

    def _record_only(self, entity: str, field: str, value) -> None:
        self._set_knob(entity, field, value)

    def save(self) -> None:
        raise FrozenWriteError(
            "estimate() tried to save the device: the estimate surface is "
            "read-only")


class FrozenInputs:
    """What :func:`load_frozen` returns: the three surfaces plus the reads
    they served (record-only provenance)."""

    def __init__(self, *, device: FrozenDevice, physical: FrozenStore | None,
                 design: FrozenDesign, experiment: str, parameters: dict,
                 reads: list) -> None:
        self.device = device
        self.physical = physical
        self.design = design
        self.experiment = experiment
        self.parameters = parameters
        self._reads = reads

    def reads(self) -> list[tuple[str, str, str]]:
        """``(source, entity, field)`` in first-read order, deduplicated."""
        return list(dict.fromkeys(self._reads))


def load_frozen(dataset, roster) -> FrozenInputs:
    """Rebuild the acquisition-time surfaces from the dataset's own attrs.

    Refuses BY NAME when the embedding is absent or of an unknown version —
    silently falling back to the live device is exactly the bug this module
    exists to prevent.
    """
    schema = dataset.attrs.get(ATTR_SCHEMA) if dataset is not None else None
    if schema is None:
        raise MissingEmbeddedInputs(
            f"dataset.nc carries no {ATTR_SCHEMA!r}: it has no "
            f"acquisition-time snapshot, so estimate() has no inputs "
            f"(a run() override that calls estimate() directly instead of "
            f"Experiment.run_estimate() produces this)")
    if int(schema) != SCHEMA:
        raise MissingEmbeddedInputs(
            f"dataset.nc was embedded with {ATTR_SCHEMA}={int(schema)}, this "
            f"scqo reads {SCHEMA} — run data is immutable, so the reader "
            f"names the mismatch rather than guessing")

    reads: list[tuple[str, str, str]] = []
    physical_values = _loads(dataset, ATTR_PHYSICAL, "physical facts")
    return FrozenInputs(
        device=FrozenDevice(roster, _loads(dataset, ATTR_DEVICE,
                                           "the device snapshot"),
                            reads=reads),
        # None, not an empty store: `physical is None` is the standalone case
        # (no facts existed), and fact_sourced() skips the measured tier for
        # it — an empty store would silently answer "nothing measured".
        physical=(None if physical_values is None
                  else FrozenStore(physical_values, reads=reads)),
        design=FrozenDesign(_loads(dataset, ATTR_DESIGN, "design values")
                            or {}, reads=reads),
        experiment=str(dataset.attrs.get(ATTR_EXPERIMENT, "")),
        parameters=_loads(dataset, ATTR_PARAMETERS, "parameters"),
        reads=reads,
    )
