"""SCQO-native device config + change history — the loop's durable memory.

SCQO owns a **neutral calibration config** (a peer to the vendor configs — QM
``state.json``/``wiring.json``, Qblox ``HardwareAgent`` — not derived from them) and
**records every change** to it. :class:`RecordingDevice` wraps a backend's
:class:`~scqo.device.DeviceModel`:

* reads are served from the SCQO config (authoritative);
* writes append a :class:`ChangeRecord`, update the SCQO config, then **push** the value
  to the vendor device so the instrument runs calibrated.

On construction it **seeds** the config from the vendor (pull). If a saved SCQO state
exists, it instead **loads that and pushes it to the vendor** — SCQO wins for the tracked
calibration fields ("SCQO config is authoritative"). Wiring/hardware stays vendor-owned
and is never modelled here.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from .device import DeviceModel, QubitView

#: The neutral calibration fields SCQO owns (and a QubitView exposes).
TRACKED_FIELDS = ("readout_freq", "drive_freq", "pi_amp")


def _now() -> str:
    """ISO-8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ChangeRecord:
    """One recorded change to the SCQO config (provenance for the AI loop's memory)."""

    timestamp: str
    qubit: str
    field: str
    old: float | None
    new: float
    experiment: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class _RecordingQubitView(QubitView):
    """QubitView that reads/writes the parent :class:`RecordingDevice`'s SCQO config."""

    def __init__(self, parent: "RecordingDevice", name: str) -> None:
        self.name = name
        self._parent = parent

    @property
    def readout_freq(self) -> float:
        return self._parent._get(self.name, "readout_freq")

    @readout_freq.setter
    def readout_freq(self, value: float) -> None:
        self._parent._set(self.name, "readout_freq", value)

    @property
    def drive_freq(self) -> float:
        return self._parent._get(self.name, "drive_freq")

    @drive_freq.setter
    def drive_freq(self, value: float) -> None:
        self._parent._set(self.name, "drive_freq", value)

    @property
    def pi_amp(self) -> float:
        return self._parent._get(self.name, "pi_amp")

    @pi_amp.setter
    def pi_amp(self, value: float) -> None:
        self._parent._set(self.name, "pi_amp", value)


class RecordingDevice(DeviceModel):
    """Authoritative SCQO config + change history over a backend's vendor device.

    Reads serve the SCQO config; writes record + update the config + push to the vendor.
    """

    def __init__(self, inner: DeviceModel, *, state_path: str | None = None) -> None:
        self._inner = inner
        self._state_path = state_path
        self._config: dict[str, dict[str, float | None]] = {}
        self._history: list[ChangeRecord] = []
        self._experiment: str | None = None  # set by the Session around a run

        if state_path is not None and os.path.exists(state_path):
            self._load_and_push(state_path)  # SCQO authoritative: load, then push to vendor
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

    def set_experiment(self, name: str | None) -> None:
        """Tag subsequent changes with the experiment that caused them."""
        self._experiment = name

    def _get(self, qubit: str, field: str) -> float:
        return self._config[qubit][field]  # type: ignore[return-value]

    def _set(self, qubit: str, field: str, value: float) -> None:
        value = float(value)
        old = self._config.get(qubit, {}).get(field)
        self._history.append(
            ChangeRecord(
                timestamp=_now(), qubit=qubit, field=field, old=old, new=value,
                experiment=self._experiment,
            )
        )
        self._config.setdefault(qubit, {})[field] = value          # SCQO config (authoritative)
        setattr(self._inner.qubit(qubit), field, value)            # push to the vendor device

    # ---------------------------------------------------------------- persistence
    def _persist(self, path: str) -> None:
        data = {"config": self._config, "history": [r.as_dict() for r in self._history]}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

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
