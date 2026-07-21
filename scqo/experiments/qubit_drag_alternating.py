from __future__ import annotations

from typing import ClassVar, Dict, Any, List

import numpy as np
import xarray as xr
from pydantic import Field

from scqo.contract import DatasetContract
from scqo.experiment import Experiment
from scqo.result import Outcome, Result
from scqo.parameters import AveragingParameters, QubitSelection
from scqo.experiments._sim import stable_seed


class QubitDragAlternatingParameters(QubitSelection, AveragingParameters):
    """Inputs for an alternating pulse error amplification DRAG calibration experiment."""

    min_beta: float = Field(-2.0, description="Minimum DRAG beta coefficient / pre-factor.")
    max_beta: float = Field(2.0, description="Maximum DRAG beta coefficient / pre-factor.")
    num_beta_points: int = Field(41, gt=1, description="Number of beta sweep points.")
    max_pulses: int = Field(20, gt=0, description="Maximum number of alternating pulses.")
    num_pulse_points: int = Field(10, gt=1, description="Number of pulse sweep points.")


class QubitDragAlternatingResult(Result):
    """Fitted optimal DRAG beta parameters."""


class QubitDragAlternating(Experiment):
    """Calibrate DRAG parameter using the alternating pulse (180/-180) error amplification method."""

    name: ClassVar[str] = "qubit_drag_alternating"
    description: ClassVar[str] = (
        "Sweep DRAG beta coefficient and play alternating pulse sequences (x180 -x180) "
        "repeated N times. The DRAG value that minimizes error accumulation (stays flat "
        "at ground state) is the optimal calibration point."
    )
    Parameters: ClassVar[type] = QubitDragAlternatingParameters
    Result: ClassVar[type] = QubitDragAlternatingResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("nb_of_pulses", "beta"),
        sweep_units=("", ""),
        variables=("I", "Q"),
    )

    params: QubitDragAlternatingParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        beta = np.linspace(
            self.params.min_beta,
            self.params.max_beta,
            self.params.num_beta_points,
        )
        nb_pulses = np.linspace(
            1,
            self.params.max_pulses,
            self.params.num_pulse_points,
            dtype=int,
        )
        return {
            "nb_of_pulses": nb_pulses,
            "beta": beta,
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        npi = coords["nb_of_pulses"]
        beta = coords["beta"]
        qubits = self.params.qubits

        n_qubits = len(qubits)
        n_npi = len(npi)
        n_beta = len(beta)

        i_data = np.zeros((n_qubits, n_npi, n_beta))
        q_data = np.zeros((n_qubits, n_npi, n_beta))

        rng = np.random.default_rng(stable_seed("qubit_drag_alternating", *qubits))
        for k, qubit in enumerate(qubits):
            opt_beta = rng.uniform(-0.5, 0.5)
            noise = 0.015
            
            for p_idx, p_count in enumerate(npi):
                # Error scales with pulse count * offset from opt_beta
                error_phase = p_count * (beta - opt_beta) * 0.15
                population = 1.0 - (np.sin(error_phase) ** 2)
                i_data[k, p_idx] = population + rng.normal(0, noise, n_beta)
                q_data[k, p_idx] = rng.normal(0, noise, n_beta)

        return {"I": i_data, "Q": q_data}

    def estimate(self) -> QubitDragAlternatingResult:
        assert self.dataset is not None
        from scqat.estimators.qubit_drag_alternating import QubitDragAlternatingEstimator
        from scqo._scqat import per_qubit_results

        # Map variable I as signal to scqat
        prepared = self.dataset.rename({"I": "signal"})

        results = per_qubit_results(
            prepared, QubitDragAlternatingEstimator(), artifact_dir=self.artifact_dir
        )

        result = QubitDragAlternatingResult()
        for qubit in self.params.qubits:
            r = results[qubit]
            result.fit[qubit] = {
                "opt_beta": float(r["opt_beta"]),
                "beta": [float(x) for x in r["beta"]],
                "nb_of_pulses": [int(x) for x in r["nb_of_pulses"]],
            }
            result.outcomes[qubit] = Outcome.SUCCESSFUL if r.get("success", False) else Outcome.FAILED
        return result

    def update(self) -> None:
        if self.result is None:
            return
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is Outcome.SUCCESSFUL and fit.get("opt_beta") is not None:
                self.device.qubit(qubit).drag_beta = fit["opt_beta"]
