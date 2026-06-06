"""Protocol — one experiment, split into a shared physics half and a backend half.

* physics half (defined here / in ``scqo.protocols``, shared by every backend):
  ``define_sweep`` -> ``simulate`` (optional) -> ``analyze`` -> ``update``.
* backend half (implemented by a driver subclass, e.g. LCHQBDriver):
  ``build`` -> turns ``params`` + device state into a native program.

A driver subclass therefore only writes ``build``; Parameters, Result, the fit in
``analyze`` and the writeback in ``update`` are inherited unchanged. Because the
simulated backend never calls ``build``, the physics half is fully runnable and
testable with no instrument installed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

import numpy as np
import xarray as xr

from .backend import Backend
from .parameters import Parameters
from .result import Result


class Protocol(ABC):
    """Base class for every experiment."""

    #: Stable identifier used in the registry and shown to humans/AI.
    name: ClassVar[str]
    #: One-line description of what the experiment does and what it updates.
    description: ClassVar[str]
    #: pydantic schema for this experiment's inputs.
    Parameters: ClassVar[type[Parameters]]
    #: pydantic schema for this experiment's structured output.
    Result: ClassVar[type[Result]]

    def __init__(self, backend: Backend, params: Parameters) -> None:
        self.backend = backend
        self.params = params
        self.sweep_axes: dict[str, np.ndarray] = {}
        self.dataset: xr.Dataset | None = None
        self.result: Result | None = None

    # ------------------------------------------------------------------ physics
    @abstractmethod
    def define_sweep(self) -> dict[str, np.ndarray]:
        """Return the swept axes as ``{axis_name: 1d array}`` (no backend calls)."""

    @abstractmethod
    def analyze(self) -> Result:
        """Fit ``self.dataset`` and return a structured :class:`Result`."""

    def update(self) -> None:
        """Write fitted quantities back into ``self.backend.device``. Default: no-op."""

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """Return synthetic ``{var: ndarray}`` of shape ``(n_qubits, *sweep)``.

        Optional. Enables the simulated backend, offline tests, and AI dry-runs.
        """
        raise NotImplementedError(f"{type(self).__name__} provides no simulator.")

    # ------------------------------------------------------------------ backend
    @abstractmethod
    def build(self) -> Any:
        """Compile ``params`` + device state into a native program (QUA / Schedule)."""

    # ------------------------------------------------------------ orchestration
    def run(self) -> Result:
        """define_sweep -> acquire (via backend) -> analyze. Does not auto-update."""
        self.sweep_axes = self.define_sweep()
        self.dataset = self.backend.acquire(self)
        self.result = self.analyze()
        return self.result
