"""In-memory device + simulated backend + a demo roster.

These let the whole abstraction run end-to-end with no instrument and no vendor
library installed — for unit tests, demos, and AI dry-runs. A driver's real backend
(e.g. ``QbloxBackend``) is a drop-in replacement for :class:`SimulatedBackend`.
"""

from __future__ import annotations

import xarray as xr

from .backend import Backend
from .device import ComponentView, DeviceModel
from .experiment import Experiment
from .roster import Component, Roster


def demo_roster(qubits: tuple[str, ...] = ("q0", "q1"), *, pair: bool = True) -> Roster:
    """The chipT-shaped demo roster: per qubit a FixedTransmon/ReadableTransmon
    with a Resonator + ReadoutLine + XYControl satellite set and design values —
    so every core test exercises satellites, topology, and design seeding.
    With ``pair`` (default) the first two qubits also form a Coupling/TransmonPair
    component (name-sorted, roles by demo design frequency: q1 > q0), so pair
    plumbing is exercised end-to-end on the simulated backend too."""
    components: dict[str, Component] = {}
    for i, q in enumerate(qubits):
        components[q] = Component(
            name=q, physical="FixedTransmon", instrument="ReadableTransmon",
            operations=("rx", "readout"),
            design={"f_01_hz": 3.8e9 + i * 0.15e9},
        )
        components[f"{q}_res"] = Component(
            name=f"{q}_res", physical="Resonator",
            design={"f_r_hz": 5.95e9 + i * 0.1e9},
        )
        components[f"{q}_ro"] = Component(
            name=f"{q}_ro", physical="ReadoutLine",
            members={"transmon": q, "resonator": f"{q}_res"},
        )
        components[f"{q}_xy"] = Component(
            name=f"{q}_xy", physical="XYControl", members={"transmon": q},
        )
    if pair and len(qubits) >= 2:
        a, b = sorted(qubits[:2])
        components[f"{a}_{b}"] = Component(
            name=f"{a}_{b}", physical="Coupling", instrument="TransmonPair",
            # demo design freqs grow with the index: the later qubit is "high"
            members={"high": qubits[1], "low": qubits[0]},
            operations=("coupler_bias", "iswap"),
        )
    return Roster(components)


class _InMemoryComponent(ComponentView):
    """A ComponentView backed by a plain dict — tolerates ANY field key so the
    vendor stand-in stays schema-agnostic (the RecordingDevice above it enforces
    the catalog)."""

    def __init__(self, name: str, state: dict, category: str = "ReadableTransmon") -> None:
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "_state", state)
        object.__setattr__(self, "category", category)

    def __getattr__(self, field: str):
        state = object.__getattribute__(self, "_state")
        if field in state:
            return state[field]
        raise AttributeError(field)

    def __setattr__(self, field: str, value) -> None:
        # Deliberately permissive: the simulated vendor accepts whatever the
        # neutral layer pushes (readout_power_dbm stays uncoupled from
        # readout_amp here — no output chain exists).
        self._state[field] = float(value)


class InMemoryDevice(DeviceModel):
    """A DeviceModel held entirely in memory (no JSON files).

    ``categories`` labels non-ReadableTransmon entries (e.g. a demo pair:
    ``{"q0_q1": "TransmonPair"}``) so the derived ``components()`` witness and
    the per-view category stay truthful."""

    def __init__(self, qubits: dict[str, dict],
                 categories: dict[str, str] | None = None) -> None:
        self._components = {name: dict(state) for name, state in qubits.items()}
        self._categories = dict(categories or {})

    def component(self, name: str) -> _InMemoryComponent:
        return _InMemoryComponent(name, self._components[name],
                                  self._categories.get(name, "ReadableTransmon"))

    def components(self) -> dict[str, "ComponentInfo"]:
        from .device import ComponentInfo

        ops = {"ReadableTransmon": ("rx", "readout"),
               "TransmonPair": ("coupler_bias", "iswap")}
        return {
            name: ComponentInfo(cat, operations=ops.get(cat, ()))
            for name in self._components
            for cat in (self._categories.get(name, "ReadableTransmon"),)
        }

    def save(self) -> None:  # nothing to persist
        pass

    def snapshot(self) -> dict:
        return {name: dict(state) for name, state in self._components.items()}


class SimulatedBackend(Backend):
    """Backend that fabricates data from ``experiment.simulate`` (never calls ``probe``)."""

    def __init__(self, device: DeviceModel) -> None:
        self._device = device

    @property
    def device(self) -> DeviceModel:
        return self._device

    def acquire(self, experiment: Experiment) -> xr.Dataset:
        sweep = experiment.sweep_axes
        raw = experiment.simulate(sweep)  # {var: ndarray of shape (n_targets, *sweep)}
        targets = experiment.params.targets  # type: ignore[attr-defined]
        dims = ["target", *sweep.keys()]
        coords = {"target": list(targets), **sweep}
        data_vars = {var: (dims, array) for var, array in raw.items()}
        return xr.Dataset(data_vars, coords=coords)
