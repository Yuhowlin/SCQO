"""Experiment — one experiment, split into a shared physics half and a backend half.

* physics half (defined here / in ``scqo.experiments``, shared by every backend):
  ``define_sweep`` -> ``simulate`` (optional) -> ``estimate`` -> ``update``.
* backend half (implemented by a driver subclass, e.g. LCHQBDriver):
  ``probe`` -> turns ``params`` + device state into a native program.

A driver subclass therefore only writes ``probe``; Parameters, Result, the fit in
``estimate`` and the writeback in ``update`` are inherited unchanged. Because the
simulated backend never calls ``probe``, the physics half is fully runnable and
testable with no instrument installed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

import numpy as np
import xarray as xr

from .backend import Backend
from .contract import DatasetContract
from .parameters import Parameters
from .result import Result


class Experiment(ABC):
    """Base class for every experiment."""

    #: Stable identifier used in the registry and shown to humans/AI.
    name: ClassVar[str]
    #: One-line description of what the experiment does and what it updates.
    description: ClassVar[str]
    #: pydantic schema for this experiment's inputs.
    Parameters: ClassVar[type[Parameters]]
    #: pydantic schema for this experiment's structured output.
    Result: ClassVar[type[Result]]
    #: canonical dataset every backend's probe must emit (and estimate() consumes).
    Contract: ClassVar[DatasetContract]

    def __init__(self, backend: Backend, params: Parameters) -> None:
        self.backend = backend
        self.params = params
        #: device state experiments read/write. Defaults to the backend's vendor device
        #: (standalone use, unrecorded); the Session swaps in a RecordingDevice so its
        #: runs are recorded into the authoritative SCQO config + history.
        self.device = backend.device
        self.sweep_axes: dict[str, np.ndarray] = {}
        self.dataset: xr.Dataset | None = None
        self.result: Result | None = None

    # ------------------------------------------------------------------ physics
    @abstractmethod
    def define_sweep(self) -> dict[str, np.ndarray]:
        """Return the swept axes as ``{axis_name: 1d array}`` (no backend calls)."""

    @abstractmethod
    def estimate(self) -> Result:
        """Fit ``self.dataset`` and return a structured :class:`Result`."""

    def update(self) -> None:
        """Write fitted quantities back into ``self.device``. Default: no-op."""

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """Return synthetic ``{var: ndarray}`` of shape ``(n_qubits, *sweep)``.

        Optional. Enables the simulated backend, offline tests, and AI dry-runs.
        """
        raise NotImplementedError(f"{type(self).__name__} provides no simulator.")

    # ------------------------------------------------------------------ backend
    @abstractmethod
    def probe(self) -> Any:
        """Compile ``params`` + device state into a native program (QUA / Schedule)."""

    # ------------------------------------------------------------ orchestration
    def run(self) -> Result:
        """define_sweep -> acquire -> verify contract -> estimate. Does not auto-update."""
        self.sweep_axes = self.define_sweep()
        self.dataset = self.backend.acquire(self)
        # Certify the probe emitted the method's canonical dataset before analysing it:
        # this is the runtime form of "the instrument supports this method".
        self.Contract.validate(self.dataset)
        self.result = self.estimate()
        return self.result
