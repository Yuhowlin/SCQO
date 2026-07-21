"""Qubit echo — Hahn-echo coherence time T2_echo (backend-free half).

X90 - tau/2 - X - tau/2 - X90, sweeping the total idle time tau: the central pi
pulse refocuses quasi-static dephasing, so the envelope decays with T2_echo
(T2* <= T2_echo <= 2*T1). Like T1, ``update()`` proposes ``t2_echo_s`` as a
PHYSICAL parameter (``physical.json`` on accept); the daily value also lives in
the run index (``fit_trend`` query).

Renamed from ``t2_echo`` 2026-07-06.
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


class QubitEchoParameters(TargetSelection, AveragingParameters):
    """Inputs for a Hahn-echo measurement."""

    min_wait_ns: float = Field(32, ge=0, description="Shortest total echo idle time.")
    max_wait_ns: float = Field(400_000, gt=0, description="Longest total idle time (should exceed a few T2_echo).")
    num_points: int = Field(51, gt=1, description="Number of idle-time points.")


class QubitEchoResult(Result):
    """``fit[qubit]`` carries ``t2_echo_s`` (plus fit amplitude/offset); proposed as a
    physical parameter by ``update()``."""


class QubitEcho(Experiment):
    """Backend-agnostic Hahn echo: X90 - tau/2 - X - tau/2 - X90 -> exponential fit."""

    name: ClassVar[str] = "qubit_echo"
    description: ClassVar[str] = (
        "Hahn echo (X90 - tau/2 - X - tau/2 - X90) over a swept total idle time; fits "
        "the exponential envelope and proposes t2_echo_s as a physical parameter "
        "(sample physics, no instrument knob)."
    )
    Parameters: ClassVar[type] = QubitEchoParameters
    Result: ClassVar[type] = QubitEchoResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("wait_time_ns",), sweep_units=("ns",), variables=("I", "Q")
    )
    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout")

    params: QubitEchoParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            "wait_time_ns": np.linspace(self.params.min_wait_ns, self.params.max_wait_ns, self.params.num_points)
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        t = coords["wait_time_ns"] * 1e-9
        targets = self.params.targets
        rng = np.random.default_rng(stable_seed("qubit_echo", *targets))
        i_data = np.empty((len(targets), t.size))
        q_data = np.empty_like(i_data)
        for k in range(len(targets)):
            t2e = rng.uniform(30e-6, 80e-6)  # hidden truth the fit must recover
            noise = 0.02
            i_data[k] = np.exp(-t / t2e) + rng.normal(0, noise, t.size)
            q_data[k] = rng.normal(0, noise, t.size)
        return {"I": i_data, "Q": q_data}

    def estimate(self) -> QubitEchoResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.qubit_echo import QubitEchoEstimator

        # scqat's contract: variable `signal` + coord `idle_time` in seconds.
        prepared = self.dataset.rename({"I": "signal", "wait_time_ns": "idle_time"})
        prepared = prepared.assign_coords(idle_time=prepared["idle_time"] * 1e-9)

        results = per_qubit_results(prepared, QubitEchoEstimator(), artifact_dir=self.artifact_dir)

        result = QubitEchoResult()
        for qubit in self.params.targets:
            r = results[qubit]
            result.fit[qubit] = {
                "t2_echo_s": float(r["t2_echo"]),
                "t2_echo_stderr_s": float(r["t2_echo_stderr"]),
                "amplitude": float(r["amplitude"]),
                "offset": float(r["offset"]),
            }
            result.outcomes[qubit] = Outcome.SUCCESSFUL if bool(r["success"]) else Outcome.FAILED
        return result

    def update(self) -> None:
        # Record T2_echo as device state (record-only field: history + config, no push).
        if self.result is None:
            return
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is Outcome.SUCCESSFUL:
                self.device.component(qubit).t2_echo_s = fit["t2_echo_s"]
