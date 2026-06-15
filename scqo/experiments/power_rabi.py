"""Power Rabi — third worked experiment (backend-free half).

Completes the trio of sweep types: frequency (resonator spec) / time (Ramsey) /
**amplitude** (here). Sweeps the drive amplitude as a factor of the qubit's current
``pi_amp``, fits the Rabi oscillation of excited-state population, and updates ``pi_amp``.

Population model: ``P = 0.5 - 0.5 * cos(pi * factor / factor_pi)`` where ``factor_pi`` is
the amplitude factor giving a full pi rotation (== 1.0 for a perfectly calibrated pulse).
A driver still only adds ``probe()``.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from pydantic import Field

from .._scqat import per_qubit_results
from ..parameters import AveragingParameters, QubitSelection
from ..experiment import Experiment
from ..result import Outcome, Result


class PowerRabiParameters(QubitSelection, AveragingParameters):
    """Inputs for power Rabi."""

    min_amp_factor: float = Field(0.0, ge=0, description="Lowest drive amplitude, as a factor of current pi_amp.")
    max_amp_factor: float = Field(2.0, gt=0, description="Highest drive amplitude, as a factor of current pi_amp.")
    num_points: int = Field(101, gt=1, description="Number of amplitude points.")


class PowerRabiResult(Result):
    """Output of power Rabi.

    ``fit[qubit]`` carries ``pi_amp`` (new absolute), ``pi_amp_factor`` (recovered factor)
    and ``old_pi_amp``.
    """


class PowerRabi(Experiment):
    """Backend-agnostic power Rabi. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "power_rabi"
    description: ClassVar[str] = (
        "Sweep drive amplitude (as a factor of the current pi pulse) and fit the Rabi "
        "oscillation to recalibrate pi_amp."
    )
    Parameters: ClassVar[type] = PowerRabiParameters
    Result: ClassVar[type] = PowerRabiResult

    params: PowerRabiParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            "amp_factor": np.linspace(
                self.params.min_amp_factor, self.params.max_amp_factor, self.params.num_points
            )
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        factor = coords["amp_factor"]
        qubits = self.params.qubits
        rng = np.random.default_rng(abs(hash(("power_rabi", tuple(qubits)))) % (2**32))
        i_data = np.empty((len(qubits), factor.size))
        q_data = np.empty_like(i_data)
        for k in range(len(qubits)):
            factor_pi = rng.uniform(0.85, 1.15)  # miscalibration to recover (1.0 == perfect)
            population = 0.5 - 0.5 * np.cos(np.pi * factor / factor_pi)
            noise = 0.02
            i_data[k] = population + rng.normal(0, noise, factor.size)
            q_data[k] = rng.normal(0, noise, factor.size)
        return {"I": i_data, "Q": q_data}

    def estimate(self) -> PowerRabiResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.power_rabi import PowerRabiEstimator

        # scqat's contract: variable `signal` + coord `amp_prefactor` (the dimensionless
        # amplitude multiplier). It returns `opt_amp_prefactor` == the pi-pulse factor.
        prepared = self.dataset.rename({"I": "signal", "amp_factor": "amp_prefactor"})

        results = per_qubit_results(prepared, PowerRabiEstimator())

        result = PowerRabiResult()
        for qubit in self.params.qubits:
            r = results[qubit]
            factor_pi = float(r["opt_amp_prefactor"])
            old = float(self.backend.device.qubit(qubit).pi_amp)
            result.fit[qubit] = {
                "pi_amp": old * factor_pi,
                "pi_amp_factor": factor_pi,
                "old_pi_amp": old,
            }
            result.outcomes[qubit] = Outcome.SUCCESSFUL if bool(r["success"]) else Outcome.FAILED
        return result

    def update(self) -> None:
        if self.result is None:
            return
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is Outcome.SUCCESSFUL:
                self.backend.device.qubit(qubit).pi_amp = fit["pi_amp"]
