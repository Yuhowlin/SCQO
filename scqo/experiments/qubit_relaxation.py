"""Qubit relaxation — excited-state lifetime T1 (backend-free half).

Excite with a pi pulse, wait a swept delay, measure; fit the exponential decay to
extract T1. ``update()`` proposes ``t1_s`` as a PHYSICAL parameter — sample physics
landing in ``physical.json`` on accept (see ``scqo.physical.PHYSICAL_FIELDS``; no
instrument knob involved); the per-run value also lives in the run index
(``fit_trend`` query).

Promoted from scqo-contrib 2026-07-05 (as ``t1_relaxation``; renamed
``qubit_relaxation`` 2026-07-06) — the first Tier-3 promotion.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from pydantic import Field

from .._scqat import per_qubit_results
from ._sim import stable_seed
from ..contract import DatasetContract
from ..experiment import Experiment
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result


class QubitRelaxationParameters(TargetSelection, AveragingParameters):
    """Inputs for a T1 relaxation measurement."""

    min_wait_ns: float = Field(16, ge=0, description="Shortest delay after the pi pulse.")
    max_wait_ns: float = Field(200_000, gt=0, description="Longest delay (should exceed a few T1).")
    num_points: int = Field(51, gt=1, description="Number of delay points.")


class QubitRelaxationResult(Result):
    """``fit[qubit]`` carries ``t1_s`` (plus fit amplitude/offset); proposed as a
    physical parameter by ``update()``."""


class QubitRelaxation(Experiment):
    """Backend-agnostic T1: pi pulse -> swept wait -> measure -> exponential fit."""

    name: ClassVar[str] = "qubit_relaxation"
    description: ClassVar[str] = (
        "Excite with a pi pulse, wait a swept delay and measure; fits the exponential "
        "decay and proposes t1_s as a physical parameter (sample physics, no instrument knob)."
    )
    Parameters: ClassVar[type] = QubitRelaxationParameters
    Result: ClassVar[type] = QubitRelaxationResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("wait_time_ns",), sweep_units=("ns",), variables=("I", "Q")
    )
    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout")

    params: QubitRelaxationParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            "wait_time_ns": np.linspace(self.params.min_wait_ns, self.params.max_wait_ns, self.params.num_points)
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        t = coords["wait_time_ns"] * 1e-9
        targets = self.params.targets
        rng = np.random.default_rng(stable_seed("qubit_relaxation", *targets))
        i_data = np.empty((len(targets), t.size))
        q_data = np.empty_like(i_data)
        for k in range(len(targets)):
            t1 = rng.uniform(20e-6, 60e-6)  # hidden truth the fit must recover
            noise = 0.02
            i_data[k] = np.exp(-t / t1) + rng.normal(0, noise, t.size)
            q_data[k] = rng.normal(0, noise, t.size)
        return {"I": i_data, "Q": q_data}

    def estimate(self) -> QubitRelaxationResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.qubit_relaxation import QubitRelaxationEstimator

        # scqat's contract: variable `signal` + coord `wait_time` in seconds.
        prepared = self.dataset.rename({"I": "signal", "wait_time_ns": "wait_time"})
        prepared = prepared.assign_coords(wait_time=prepared["wait_time"] * 1e-9)

        results = per_qubit_results(prepared, QubitRelaxationEstimator(), artifact_dir=self.artifact_dir)

        result = QubitRelaxationResult()
        for qubit in self.params.targets:
            r = results[qubit]
            result.fit[qubit] = {
                "t1_s": float(r["t1"]),
                "t1_stderr_s": float(r["t1_stderr"]),
                "amplitude": float(r["amplitude"]),
                "offset": float(r["offset"]),
            }
            result.outcomes[qubit] = Outcome.SUCCESSFUL if bool(r["success"]) else Outcome.FAILED
        return result

    def update(self) -> None:
        # Record T1 as device state (record-only field: history + config, no push).
        if self.result is None:
            return
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is Outcome.SUCCESSFUL:
                self.device.component(qubit).t1_s = fit["t1_s"]
