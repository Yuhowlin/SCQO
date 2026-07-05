"""T1 relaxation — excited-state lifetime (backend-free half).

Excite with a pi pulse, wait a swept delay, measure; fit the exponential decay to
extract T1. A *reported* quantity: there is no calibration writeback (T1 is not a
tracked device field — that is a Phase-3 schema discussion), so ``update()`` is a
no-op and the daily value lives in the run index (``fit_trend`` query).

Promoted from scqo-contrib 2026-07-05 — the first Tier-3 promotion.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from pydantic import Field

from .._scqat import per_qubit_results
from ._sim import stable_seed
from ..contract import DatasetContract
from ..experiment import Experiment
from ..parameters import AveragingParameters, QubitSelection
from ..result import Outcome, Result


class T1RelaxationParameters(QubitSelection, AveragingParameters):
    """Inputs for a T1 relaxation measurement."""

    min_wait_ns: float = Field(16, ge=0, description="Shortest delay after the pi pulse.")
    max_wait_ns: float = Field(200_000, gt=0, description="Longest delay (should exceed a few T1).")
    num_points: int = Field(51, gt=1, description="Number of delay points.")


class T1RelaxationResult(Result):
    """``fit[qubit]`` carries ``t1_s`` (plus fit amplitude/offset). No writeback."""


class T1Relaxation(Experiment):
    """Backend-agnostic T1: pi pulse -> swept wait -> measure -> exponential fit."""

    name: ClassVar[str] = "t1_relaxation"
    description: ClassVar[str] = (
        "Excite with a pi pulse, wait a swept delay and measure; fits the exponential "
        "decay to report T1 (diagnostics — no device writeback)."
    )
    Parameters: ClassVar[type] = T1RelaxationParameters
    Result: ClassVar[type] = T1RelaxationResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("wait_time_ns",), sweep_units=("ns",), variables=("I", "Q")
    )

    params: T1RelaxationParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            "wait_time_ns": np.linspace(self.params.min_wait_ns, self.params.max_wait_ns, self.params.num_points)
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        t = coords["wait_time_ns"] * 1e-9
        qubits = self.params.qubits
        rng = np.random.default_rng(stable_seed("t1_relaxation", *qubits))
        i_data = np.empty((len(qubits), t.size))
        q_data = np.empty_like(i_data)
        for k in range(len(qubits)):
            t1 = rng.uniform(20e-6, 60e-6)  # hidden truth the fit must recover
            noise = 0.02
            i_data[k] = np.exp(-t / t1) + rng.normal(0, noise, t.size)
            q_data[k] = rng.normal(0, noise, t.size)
        return {"I": i_data, "Q": q_data}

    def estimate(self) -> T1RelaxationResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.t1_relaxation import T1RelaxationEstimator

        # scqat's contract: variable `signal` + coord `wait_time` in seconds.
        prepared = self.dataset.rename({"I": "signal", "wait_time_ns": "wait_time"})
        prepared = prepared.assign_coords(wait_time=prepared["wait_time"] * 1e-9)

        results = per_qubit_results(prepared, T1RelaxationEstimator(), artifact_dir=self.artifact_dir)

        result = T1RelaxationResult()
        for qubit in self.params.qubits:
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
        # T1 is reported, not written back (not a tracked device field).
        return
