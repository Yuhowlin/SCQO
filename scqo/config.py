"""SCQO-native device config + change history — the loop's durable memory.

SCQO owns a **neutral calibration config** (a peer to the vendor configs — QM
``state.json``/``wiring.json``, Qblox ``HardwareAgent`` — not derived from them) and
**records every change** to it. :class:`RecordingDevice` wraps a backend's
:class:`~scqo.device.DeviceModel`:

* reads are served from the SCQO config (authoritative);
* writes append a :class:`ChangeRecord`, update the SCQO config, then **push** the value
  to the vendor device so the instrument runs calibrated.

On construction it **seeds** the config from the vendor (pull). If a saved SCQO state
exists, what happens depends on ``on_load``:

* ``"pull"`` (default, safe while another tool may also write the vendor config — e.g.
  unmigrated qualibrate nodes on QM): the vendor wins at startup; only the saved
  **history** is loaded so provenance stays continuous.
* ``"push"`` (for devices SCQO fully owns, e.g. simulated): the saved SCQO config is
  loaded **and pushed to the vendor** — SCQO wins for the tracked calibration fields.

Either way, a *write* during a run always records + pushes: a fresh fit result is
always legitimate. Wiring/hardware stays vendor-owned and is never modelled here.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from .device import DeviceModel, QubitView

#: The neutral calibration fields SCQO owns (and a QubitView exposes).
TRACKED_FIELDS = ("readout_freq", "drive_freq", "pi_amp", "readout_amp")


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
    qubit: str
    field: str
    old: float | None
    new: float
    experiment: str | None = None
    #: run_id of the datastore run that caused this change (links history <-> run folder).
    run_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _tracked_property(field: str) -> property:
    """A read/write property routing one tracked field through the RecordingDevice."""

    def getter(self: "_RecordingQubitView") -> float:
        return self._parent._get(self.name, field)

    def setter(self: "_RecordingQubitView", value: float) -> None:
        self._parent._set(self.name, field, value)

    return property(getter, setter)


class _RecordingQubitView(QubitView):
    """QubitView that reads/writes the parent :class:`RecordingDevice`'s SCQO config.

    One read/write property per :data:`TRACKED_FIELDS` is generated in the class body, so
    adding a tracked field needs no edit here — only the field list plus each backend's
    own :class:`~scqo.device.QubitView` and the ABC.
    """

    def __init__(self, parent: "RecordingDevice", name: str) -> None:
        self.name = name
        self._parent = parent

    # Generate readout_freq / drive_freq / pi_amp / ... from TRACKED_FIELDS. Assigning
    # into the class namespace (vars()) makes them real properties that satisfy the
    # QubitView ABC's abstract members.
    for _field in TRACKED_FIELDS:
        vars()[_field] = _tracked_property(_field)
    del _field


class RecordingDevice(DeviceModel):
    """Authoritative SCQO config + change history over a backend's vendor device.

    Reads serve the SCQO config; writes record + update the config + push to the vendor.
    """

    def __init__(
        self,
        inner: DeviceModel,
        *,
        state_path: str | None = None,
        on_load: Literal["push", "pull"] = "pull",
    ) -> None:
        self._inner = inner
        self._state_path = state_path
        self._config: dict[str, dict[str, float | None]] = {}
        self._history: list[ChangeRecord] = []
        self._experiment: str | None = None  # set by the Session around a run
        self._run_id: str | None = None

        if state_path is not None and os.path.exists(state_path):
            if on_load == "push":
                # SCQO fully owns this device: load the saved config and push it to the
                # vendor so the instrument matches SCQO for the tracked fields.
                self._load_and_push(state_path)
            else:
                # "pull" (safe default while another tool may also write the vendor
                # config, e.g. unmigrated qualibrate nodes on QM): the vendor wins at
                # startup, but the saved history is kept so provenance is continuous.
                self._load_history(state_path)
                self._config = inner.snapshot()
        else:
            self._config = inner.snapshot()  # first time: seed (pull) from the vendor

    # ---------------------------------------------------------------- DeviceModel
    def qubit(self, name: str) -> _RecordingQubitView:
        return _RecordingQubitView(self, name)

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

    def _get(self, qubit: str, field: str) -> float:
        return self._config[qubit][field]  # type: ignore[return-value]

    def _set(self, qubit: str, field: str, value: float) -> None:
        value = float(value)
        old = self._config.get(qubit, {}).get(field)
        # Push to the vendor FIRST: if the instrument rejects the value (e.g. an
        # out-of-range amplitude), neither the SCQO config nor the history may claim
        # a change that never reached the hardware.
        setattr(self._inner.qubit(qubit), field, value)
        self._history.append(
            ChangeRecord(
                timestamp=_now(), qubit=qubit, field=field, old=old, new=value,
                experiment=self._experiment, run_id=self._run_id,
            )
        )
        self._config.setdefault(qubit, {})[field] = value          # SCQO config (authoritative)

    # ---------------------------------------------------------------- persistence
    def _persist(self, path: str) -> None:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)  # a config'd path must not fail on first save
        data = {"config": self._config, "history": [r.as_dict() for r in self._history]}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _load_history(self, path: str) -> None:
        """Load only the change history from a saved state (pull mode keeps provenance)."""
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self._history = [ChangeRecord(**r) for r in data.get("history", [])]

    def _load_and_push(self, path: str) -> None:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self._config = data.get("config", {})
        self._history = [ChangeRecord(**r) for r in data.get("history", [])]
        # SCQO is authoritative: push the loaded values into the vendor device so it
        # matches SCQO for the tracked fields (wiring/hardware is left untouched).
        for qubit, fields in self._config.items():
            for field, value in fields.items():
                if value is not None:
                    setattr(self._inner.qubit(qubit), field, value)
