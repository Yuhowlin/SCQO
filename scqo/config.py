"""SCQO-native device config + change history — the loop's durable memory.

SCQO owns a **neutral calibration config** (a peer to the vendor configs — QM
``state.json``/``wiring.json``, Qblox ``HardwareAgent`` — not derived from them) and
**records every change** to it. :class:`RecordingDevice` wraps a backend's
:class:`~scqo.device.DeviceModel`:

* reads are served from the SCQO config (authoritative);
* writes append a :class:`ChangeRecord`, update the SCQO config, then **push** the value
  to the vendor device so the instrument runs calibrated.

Since the component cutover the schema is per-COMPONENT: which names exist and
which fields each carries comes from the device ROSTER (:mod:`scqo.roster`,
``components.toml``) and the category catalog (:mod:`scqo.categories`) — this
store holds the INSTRUMENT side (the fields of each component's instrument
category); the physical side lives in :mod:`scqo.physical`. State files carry
``"schema": 2``; pre-cutover files are ARCHIVED aside (``*.v1.bak``, values and
history sidecar both) on first contact — the fresh-start policy, with the old
bytes preserved but never read.

On construction it **seeds** the config from the vendor (pull). If a saved SCQO state
exists, what happens depends on ``on_load``:

* ``"pull"`` (default, safe while another tool may also write the vendor config — e.g.
  unmigrated qualibrate nodes on QM): the vendor wins at startup; only the saved
  **history** is loaded so provenance stays continuous.
* ``"push"`` (for devices SCQO fully owns, e.g. simulated): the saved SCQO config is
  loaded **and pushed to the vendor** — SCQO wins for the tracked calibration fields.

Either way, a *write* during a run always records + pushes: a fresh fit result is
always legitimate. Wiring/hardware stays vendor-owned and is never modelled here.

The state is **per (cooldown, setup)** (:func:`scqo.datastore.setup_scqo_dir`:
``<device>/<cooldown>/<setup>/scqo/scqo_state.json`` holding the current values,
with the change history in the append-only sidecar ``scqo_state.history.jsonl`` —
see :mod:`scqo._state_io`). Saves merge history under a lock file, so two
same-setup sessions cannot erase each other's rows. Every change record is
stamped with the **operator** (OS login), the **setup**, and the field-owning
**category** — attribution works for manual notebook writes too.
"""

from __future__ import annotations

import getpass
import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from ._state_io import _file_lock, history_path, read_history, write_history
from .categories import FieldSpec  # noqa: F401  (re-export: FieldSpec's home moved)
from .device import ComponentView, DeviceModel


def _current_operator() -> str:
    """OS login name of whoever is running this process ("" if undeterminable)."""
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - no login name in exotic environments
        return ""


#: State-file schema stamp; files without it are pre-cutover and archived aside.
STATE_SCHEMA = 2


def _now() -> str:
    """ISO-8601 timestamp in local time WITH the UTC offset (e.g. ``...+08:00``).

    Local (not UTC) so that the date prefix matches the datastore's run folders and
    what a human types into ``find_runs(since=..., until=...)``; the explicit offset
    keeps it machine-unambiguous. Lexicographic order == chronological order as long
    as the lab's UTC offset is fixed (no DST in the lab's timezone).
    """
    return datetime.now(timezone.utc).astimezone().isoformat()


@dataclass(frozen=True)
class ChangeRecord:
    """One recorded change to the SCQO config (provenance for the AI loop's memory)."""

    timestamp: str
    component: str
    field: str
    old: float | None
    new: float
    #: the field-owning category, stamped at write time so rows stay
    #: self-describing even if the roster later changes.
    category: str | None = None
    experiment: str | None = None
    #: run_id of the datastore run that caused this change (links history <-> run folder).
    run_id: str | None = None
    #: OS login of whoever made the change (None only when undeterminable).
    operator: str | None = None
    #: Set when this change is the vendor-side ECHO of writing another field (one
    #: vendor knob feeds several neutral fields — e.g. setting readout_power_dbm
    #: re-solves the chain and moves readout_amp): names the field whose write
    #: caused it. None for direct writes.
    coupled_to: str | None = None
    #: NAMED setup the writing session was bound to — attributes manual writes too.
    setup: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _archive_v1(path: Path) -> None:
    """Move a pre-cutover state file AND its history sidecar aside (``*.v1.bak``).

    The fresh-start policy: old bytes are preserved on disk but never read again
    — no filtered readers, no mixed-format sidecars. Idempotent; called the
    first time v2 code touches a context whose values file lacks the schema
    stamp (or whose sidecar exists without any values file)."""
    for p in (path, history_path(path)):
        if p.is_file():
            bak = p.with_name(p.name + ".v1.bak")
            if bak.exists():  # a previous archive: keep the oldest, drop the newer
                p.unlink()
            else:
                p.rename(bak)


def _load_v2(path: Path) -> dict | None:
    """The values file's parsed JSON iff it carries the v2 stamp, else None
    (after archiving any pre-cutover file + sidecar aside)."""
    if not path.is_file():
        # A sidecar without a values file: v2 sidecars are legal there (the
        # documented values-only reset) — but only if they parse as v2 rows.
        hp = history_path(path)
        if hp.is_file():
            rows = read_history(path)
            if rows and any("component" not in r for r in rows):
                _archive_v1(path)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = None
    if not isinstance(data, dict) or data.get("schema") != STATE_SCHEMA:
        _archive_v1(path)
        return None
    return data


def _tracked_property(field: str, spec: FieldSpec) -> property:
    """A read/write property routing one field through the RecordingDevice.

    Pushed fields keep strict reads and vendor-coupled writes; record-only fields
    read as None until first measured and never touch the vendor.
    """
    if spec.push:

        def getter(self: "ComponentView") -> float:
            return self._parent._get(self.name, field)

        def setter(self: "ComponentView", value: float) -> None:
            self._parent._set(self.name, field, value)

    else:

        def getter(self: "ComponentView") -> float | None:  # type: ignore[misc]
            return self._parent._get_recorded(self.name, field)

        def setter(self: "ComponentView", value: float) -> None:
            self._parent._record(self.name, field, value)

    return property(getter, setter, doc=f"{spec.doc} [{spec.unit}]" if spec.unit else spec.doc)


_recording_view_classes: dict[tuple[str | None, str | None], type] = {}


def _recording_view_class(instrument: str | None, physical: str | None) -> type:
    """The generated recording-view class for one (instrument, physical) pair.

    Properties = the instrument category's fields, pruned by requires_physical.
    Cached per pair (all same-shaped names share one class)."""
    key = (instrument, physical)
    cls = _recording_view_classes.get(key)
    if cls is None:
        from .categories import CATEGORIES  # late: config defines FieldSpec first

        ns: dict[str, Any] = {}
        if instrument is not None:
            for f, fs in CATEGORIES[instrument].fields.items():
                if fs.requires_physical and physical not in fs.requires_physical:
                    continue
                ns[f] = _tracked_property(f, fs)
        fields = sorted(n for n in ns)

        def __init__(self, parent: "RecordingDevice", name: str) -> None:
            object.__setattr__(self, "name", name)
            object.__setattr__(self, "_parent", parent)

        def __setattr__(self, attr: str, value) -> None:
            # A write to an untracked name must fail LOUDLY (it used to vanish
            # into the instance dict).
            if isinstance(getattr(type(self), attr, None), property):
                object.__setattr__(self, attr, value)
                return
            raise AttributeError(
                f"{instrument or 'component'} {self.name!r} has no instrument "
                f"field {attr!r} — its fields: {', '.join(fields) or '(none)'} "
                f"(physical fields route through scqo.physical)")

        ns["__init__"] = __init__
        ns["__setattr__"] = __setattr__
        ns["category"] = instrument
        cls = type(f"Recording{instrument or 'Bare'}View", (ComponentView,), ns)
        _recording_view_classes[key] = cls
    return cls


class RecordingDevice(DeviceModel):
    """Authoritative SCQO config + change history over a backend's vendor device.

    Reads serve the SCQO config; writes record + update the config + push to the vendor.
    The ROSTER decides which names exist and which fields each carries.
    """

    def __init__(
        self,
        inner: DeviceModel,
        roster,
        *,
        state_path: str | None = None,
        on_load: Literal["push", "pull"] = "pull",
        setup: str | None = None,
    ) -> None:
        self._inner = inner
        self.roster = roster
        self._state_path = state_path
        self._setup = setup or None  # stamped on every ChangeRecord
        self._config: dict[str, dict[str, float | None]] = {}
        self._history: list[ChangeRecord] = []
        #: merge-on-save baseline: rows [:_saved] are already in the sidecar, rows
        #: [_saved:] are ours and not yet persisted.
        self._saved = 0
        self._experiment: str | None = None  # set by the Session around a run
        self._run_id: str | None = None

        saved = None
        if state_path is not None:
            saved = self._load_state(state_path)  # archives v1 files, loads v2
        if saved is not None and on_load == "push":
            # SCQO fully owns this device: load the saved config and push it to
            # the vendor. Fields added since the file was saved are backfilled
            # from the vendor snapshot — pushed reads are strict.
            self._config = saved
            vendor = inner.snapshot()
            for name in self.roster.names():
                all_fields = self._all_fields(name)
                if not all_fields:
                    continue  # physical-only components live in scqo.physical
                fields = self._config.setdefault(name, {})
                for f in all_fields:
                    fields.setdefault(f, vendor.get(name, {}).get(f))
            self._push_config()
        else:
            # "pull" (or nothing saved): the vendor wins at startup for the
            # pushed fields; saved RECORD-ONLY values are merged back in (the
            # vendor knows nothing about them).
            self._config = self._pull_seed(inner.snapshot(), saved or {})

    # ------------------------------------------------------------ field schema
    def _instrument_pair(self, name: str) -> tuple[str | None, str | None]:
        phys, instr = self.roster.category(name)
        return instr, phys

    def _all_fields(self, name: str) -> tuple[str, ...]:
        """All INSTRUMENT-side fields of one name (pushed + record-only, pruned)."""
        return tuple(f for f, (side, _s) in self.roster.fields_of(name).items()
                     if side == "instrument")

    def _pushed(self, name: str) -> tuple[str, ...]:
        return self.roster.pushed(name)

    def _pull_seed(self, vendor: dict, saved: dict) -> dict:
        """Vendor-authoritative seed over ROSTER names: pushed fields from the
        vendor snapshot; saved record-only values retained."""
        merged: dict[str, dict[str, float | None]] = {}
        for name in self.roster.names():
            fields = self._all_fields(name)
            if not fields:
                continue  # physical-only components live in scqo.physical
            pushed = set(self._pushed(name))
            vend = vendor.get(name, {})
            merged[name] = {f: vend.get(f) for f in fields if f in pushed}
            for f in fields:
                if f not in pushed and saved.get(name, {}).get(f) is not None:
                    merged[name][f] = saved[name][f]
        return merged

    # ---------------------------------------------------------------- DeviceModel
    def component(self, name: str):
        instr, phys = self._instrument_pair(name)
        return _recording_view_class(instr, phys)(self, name)

    def one(self, name: str, category: str) -> str:
        """Topology lookup (``one("q1", "Resonator") -> "q1_res"``) — identical
        surface on the live device and the SuggestionCapture, so ``update()``
        and ``estimate()`` code is oblivious to which wraps it."""
        return self.roster.one(name, category)

    def design(self, name: str, field: str) -> float | None:
        """Design-value passthrough (bring-up anchors read through the device)."""
        return self.roster.design(name, field)

    def snapshot(self) -> dict:
        """The authoritative SCQO config (deep-copied)."""
        return {q: dict(fields) for q, fields in self._config.items()}

    def save(self) -> None:
        """Persist the vendor config (push) and the SCQO config + history (own format)."""
        self._inner.save()
        if self._state_path is not None:
            self._persist(self._state_path)

    # ---------------------------------------------------------------- SCQO-native
    def history(self) -> list[ChangeRecord]:
        """The recorded change history (the loop's memory)."""
        return list(self._history)

    def set_context(self, experiment: str | None, run_id: str | None = None) -> None:
        """Tag subsequent changes with the experiment (and datastore run) causing them."""
        self._experiment = experiment
        self._run_id = run_id

    def _category_of(self, name: str, field: str) -> str | None:
        instr, phys = self._instrument_pair(name)
        return instr

    def _get(self, component: str, field: str) -> float:
        return self._config[component][field]  # type: ignore[return-value]

    def _get_recorded(self, component: str, field: str) -> float | None:
        """Record-only fields read as None until first measured (pushed stay strict)."""
        return self._config.get(component, {}).get(field)

    def _record(self, component: str, field: str, value: float, *,
                coupled_to: str | None = None) -> None:
        """Record a measured value: history + SCQO config, NO vendor push."""
        value = float(value)
        if not math.isfinite(value):  # the state JSON must stay strictly parseable
            raise ValueError(f"refusing to record non-finite {field}={value!r} for {component}")
        old = self._config.get(component, {}).get(field)
        self._history.append(
            ChangeRecord(
                timestamp=_now(), component=component, field=field, old=old, new=value,
                category=self._category_of(component, field),
                experiment=self._experiment, run_id=self._run_id,
                # Stamped here (not via set_context) so manual writes outside a run —
                # a notebook tweaking pi_amp — are attributed too.
                operator=_current_operator() or None,
                coupled_to=coupled_to,
                setup=self._setup,
            )
        )
        self._config.setdefault(component, {})[field] = value

    def _set(self, component: str, field: str, value: float) -> None:
        value = float(value)
        if not math.isfinite(value):  # never hand the instrument a NaN/Inf
            raise ValueError(f"refusing to push non-finite {field}={value!r} for {component}")
        # Push to the vendor FIRST: if the instrument rejects the value (e.g. an
        # out-of-range amplitude), neither the SCQO config nor the history may claim
        # a change that never reached the hardware.
        setattr(self._inner.component(component), field, value)
        self._record(component, field, value)
        self._sync_coupled(component, field)

    def _sync_coupled(self, component: str, changed_field: str) -> None:
        """Reconcile vendor-side write echoes so the SCQO config never desyncs.

        One vendor knob may feed several neutral fields (setting readout_power_dbm
        re-solves the output chain, which moves readout_amp). After a push of one
        field, re-read the component's OTHER pushed fields from the vendor; any
        drifted value gets its own ChangeRecord (``coupled_to`` = the written
        field) and a config update."""
        try:
            view = self._inner.component(component)
        except KeyError:
            return  # roster name the vendor does not realize: nothing to reconcile
        for other in self._pushed(component):
            if other == changed_field:
                continue
            try:
                current = getattr(view, other)
            except Exception:
                continue
            if current is None:
                continue
            current = float(current)
            if not math.isfinite(current):  # never poison the strict-JSON config
                continue
            if self._config.get(component, {}).get(other) == current:
                continue  # exact comparison — the same rule the staleness guard uses
            self._record(component, other, current, coupled_to=changed_field)

    # ---------------------------------------------------------------- persistence
    def _persist(self, path: str) -> None:
        """Two writes under the shared lock: merge-append history, rewrite values.

        The v2 gate applies HERE too (the audit's resurrection bug): a
        pre-cutover file on disk is archived aside before the merge re-reads it,
        so v1 values can never leak into a v2 file."""
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)  # a config'd path must not fail on first save
        with _file_lock(Path(path)):
            _load_v2(Path(path))  # side effect: archives any v1 file + sidecar
            file_history = [ChangeRecord(**r) for r in read_history(path)]
            ours = self._history[self._saved:]
            merged = sorted(file_history + ours, key=lambda r: r.timestamp)  # stable
            write_history(path, [r.as_dict() for r in merged])
            self._history = merged
            self._saved = len(merged)
            data = {"schema": STATE_SCHEMA, "config": self._config}
            # Unique temp + replace: a save can no longer tear the JSON mid-write.
            tmp = f"{path}.{os.getpid()}.tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp, path)
            except OSError:
                try:
                    os.unlink(tmp)  # no orphan temp; the in-memory config stays intact
                except OSError:
                    pass
                raise

    def _load_state(self, path: str) -> dict | None:
        """Load a saved v2 state file (archiving pre-cutover files aside).

        Returns the saved config filtered to roster-known (name, field) pairs,
        or None when nothing v2 is on disk. Installs the (v2) history either way.
        """
        data = _load_v2(Path(path))
        self._history = [ChangeRecord(**r) for r in read_history(path)]
        self._saved = len(self._history)
        if data is None:
            return None
        out: dict[str, dict[str, float | None]] = {}
        for name, fields in data.get("config", {}).items():
            if name not in self.roster:
                continue
            known = set(self._all_fields(name))
            if not known:
                continue  # physical-only components never belong in this store
            out[name] = {f: v for f, v in fields.items() if f in known}
        return out

    def _push_config(self) -> None:
        """Push the pushed fields of the SCQO config into the vendor device (SCQO is
        authoritative in push mode). Fields go in catalog declaration order —
        readout_power_dbm after readout_amp, so the absolute power wins and
        readout_amp becomes its chain residual."""
        for name, fields in self._config.items():
            pushed = self._pushed(name)
            for field in pushed:
                value = fields.get(field)
                if value is not None:
                    try:
                        setattr(self._inner.component(name), field, value)
                    except KeyError:
                        # A roster name the vendor does not realize (mismatch the
                        # doctor witnesses) must not brick session construction.
                        break
            if pushed:
                # Reconcile against the chain-owning field (the absolute power)
                # so coupled provenance is attributed to the write that wins.
                anchor = "readout_power_dbm" if "readout_power_dbm" in pushed else pushed[-1]
                self._sync_coupled(name, anchor)
