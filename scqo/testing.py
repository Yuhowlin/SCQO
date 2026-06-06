"""In-memory device + simulated backend.

These let the whole abstraction run end-to-end with no instrument and no vendor
library installed — for unit tests, demos, and AI dry-runs. A driver's real backend
(e.g. ``QbloxBackend``) is a drop-in replacement for :class:`SimulatedBackend`.
"""

from __future__ import annotations

import xarray as xr

from .backend import Backend
from .device import DeviceModel, QubitView
from .protocol import Protocol


class _InMemoryQubit(QubitView):
    """A QubitView backed by a plain dict."""

    def __init__(self, name: str, state: dict) -> None:
        self.name = name
        self._state = state

    @property
    def readout_freq(self) -> float:
        return self._state["readout_freq"]

    @readout_freq.setter
    def readout_freq(self, value: float) -> None:
        self._state["readout_freq"] = float(value)

    @property
    def drive_freq(self) -> float:
        return self._state["drive_freq"]

    @drive_freq.setter
    def drive_freq(self, value: float) -> None:
        self._state["drive_freq"] = float(value)

    @property
    def pi_amp(self) -> float:
        return self._state["pi_amp"]

    @pi_amp.setter
    def pi_amp(self, value: float) -> None:
        self._state["pi_amp"] = float(value)


class InMemoryDevice(DeviceModel):
    """A DeviceModel held entirely in memory (no JSON files)."""

    def __init__(self, qubits: dict[str, dict]) -> None:
        self._qubits = {name: dict(state) for name, state in qubits.items()}

    def qubit(self, name: str) -> _InMemoryQubit:
        return _InMemoryQubit(name, self._qubits[name])

    def save(self) -> None:  # nothing to persist
        pass

    def snapshot(self) -> dict:
        return {name: dict(state) for name, state in self._qubits.items()}


class SimulatedBackend(Backend):
    """Backend that fabricates data from ``protocol.simulate`` (never calls ``build``)."""

    def __init__(self, device: DeviceModel) -> None:
        self._device = device

    @property
    def device(self) -> DeviceModel:
        return self._device

    def acquire(self, protocol: Protocol) -> xr.Dataset:
        sweep = protocol.sweep_axes
        raw = protocol.simulate(sweep)  # {var: ndarray of shape (n_qubits, *sweep)}
        qubits = protocol.params.qubits  # type: ignore[attr-defined]
        dims = ["qubit", *sweep.keys()]
        coords = {"qubit": list(qubits), **sweep}
        data_vars = {var: (dims, array) for var, array in raw.items()}
        return xr.Dataset(data_vars, coords=coords)
