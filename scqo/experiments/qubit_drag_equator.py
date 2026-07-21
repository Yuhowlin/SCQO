from __future__ import annotations

from typing import ClassVar, Dict, Any, List

import numpy as np
import xarray as xr
from pydantic import Field, field_validator

from scqo.contract import DatasetContract
from scqo.experiment import Experiment
from scqo.result import Outcome, Result
from scqo.parameters import AveragingParameters, QubitSelection
from scqo.experiments._sim import stable_seed


class QubitDragEquatorParameters(QubitSelection, AveragingParameters):
    """Inputs for a 3-line symmetric equator DRAG calibration experiment."""

    min_beta: float = Field(-0.5, description="Minimum DRAG beta coefficient.")
    max_beta: float = Field(0.5, description="Maximum DRAG beta coefficient.")
    num_beta_points: int = Field(41, gt=1, description="Number of beta sweep points.")
    pulse_repetitions: int = Field(3, gt=0, description="Number of alternating pi pulses. Must be odd.")

    @field_validator("pulse_repetitions")
    @classmethod
    def check_odd(cls, val: int) -> int:
        if val % 2 == 0:
            raise ValueError("pulse_repetitions must be an odd number (1, 3, 5...) to land on the equator.")
        return val


class QubitDragEquatorResult(Result):
    """Fitted optimal DRAG beta parameters."""


class QubitDragEquator(Experiment):
    """Calibrate DRAG beta parameter using the 3-line symmetric equator method."""

    name: ClassVar[str] = "qubit_drag_equator"
    description: ClassVar[str] = (
        "Sweep the DRAG beta coefficient and play three sequences (Seq 0: X90-(Y180)^N, "
        "Seq 1: X90-(-Y180)^N, Seq 2: X90-(X180)^N). The intersection of the three lines "
        "determines the optimal DRAG beta."
    )
    Parameters: ClassVar[type] = QubitDragEquatorParameters
    Result: ClassVar[type] = QubitDragEquatorResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("seq_idx", "beta"),
        sweep_units=("", ""),
        variables=("I", "Q"),
    )

    params: QubitDragEquatorParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        beta = np.linspace(
            self.params.min_beta,
            self.params.max_beta,
            self.params.num_beta_points,
        )
        return {
            "seq_idx": np.array([0, 1]),
            "beta": beta,
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        seq_idx = coords["seq_idx"]
        beta = coords["beta"]
        qubits = self.params.qubits

        n_qubits = len(qubits)
        n_seq = len(seq_idx)
        n_beta = len(beta)

        i_data = np.zeros((n_qubits, n_seq, n_beta))
        q_data = np.zeros((n_qubits, n_seq, n_beta))

        rng = np.random.default_rng(stable_seed("qubit_drag_equator", *qubits))
        for k, qubit in enumerate(qubits):
            opt_beta = rng.uniform(-0.5, 0.5)
            noise = 0.015
            
            # Seq 0: Rx(pi) - Ry(pi/2)
            i_data[k, 0] = 0.5 + 0.3 * np.tanh(beta - opt_beta) + rng.normal(0, noise, n_beta)
            # Seq 1: Ry(pi) - Rx(pi/2)
            i_data[k, 1] = 0.5 - 0.3 * np.tanh(beta - opt_beta) + rng.normal(0, noise, n_beta)
            
            q_data[k, :] = rng.normal(0, noise, (n_seq, n_beta))

        return {"I": i_data, "Q": q_data}

    def estimate(self) -> QubitDragEquatorResult:
        assert self.dataset is not None
        from scqat.estimators.qubit_drag_equator import QubitDragEquatorEstimator
        from scqo._scqat import per_qubit_results

        # Map variable I as signal to scqat
        prepared = self.dataset.rename({"I": "signal"})

        results = per_qubit_results(
            prepared, QubitDragEquatorEstimator(), artifact_dir=self.artifact_dir
        )

        result = QubitDragEquatorResult()
        for qubit in self.params.qubits:
            r = results[qubit]
            result.fit[qubit] = {
                "opt_beta": float(r["opt_beta"]) if r.get("opt_beta") is not None else None,
                "beta": [float(x) for x in r["beta"]],
                "seq0": [float(x) for x in r["seq0"]],
                "seq1": [float(x) for x in r["seq1"]],
            }
            result.outcomes[qubit] = Outcome.SUCCESSFUL if r.get("success", False) else Outcome.FAILED
        return result

    def update(self) -> None:
        if self.result is None:
            return
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is Outcome.SUCCESSFUL and fit.get("opt_beta") is not None:
                self.device.qubit(qubit).drag_beta = fit["opt_beta"]
