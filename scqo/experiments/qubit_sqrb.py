"""Qubit Single Qubit Randomized Benchmarking (SQRB).

Plays random Clifford sequences of increasing depths, fitting the depolarizing
decay curve to determine the average single-qubit gate fidelity.
"""

from __future__ import annotations

from typing import ClassVar, Dict, Any, Optional

import numpy as np
from pydantic import Field
import xarray as xr

from .._scqat import per_qubit_results
from ..contract import DatasetContract
from ..parameters import AveragingParameters, QubitSelection
from ..experiment import Experiment
from ..result import Outcome, Result
from ._sim import stable_seed


class QubitSQRBParameters(QubitSelection, AveragingParameters):
    """Inputs for a Qubit SQRB experiment."""

    num_random_sequences: int = Field(
        30,
        gt=0,
        description="Number of distinct random Clifford sequences."
    )
    max_circuit_depth: int = Field(
        200,
        gt=0,
        description="Maximum Clifford depth to sweep."
    )
    delta_clifford: int = Field(
        20,
        gt=0,
        description="Depth increment between points (if not log scale)."
    )
    log_scale: bool = Field(
        True,
        description="Use logarithmic depth scaling (1, 2, 4, 8, 16...)."
    )
    use_state_discrimination: bool = Field(
        False,
        description="Use state discrimination to classify |0> vs |1> population."
    )
    seed: Optional[int] = Field(
        None,
        description="Optional seed for the sequence generator."
    )

    def get_depths(self) -> np.ndarray:
        """Generate depths based on log_scale and max_circuit_depth."""
        if self.log_scale:
            depths = [1]
            current_depth = 2
            while current_depth <= self.max_circuit_depth:
                depths.append(current_depth)
                current_depth *= 2
            return np.array(depths, dtype=int)
        else:
            assert (
                self.max_circuit_depth / self.delta_clifford
            ).is_integer(), "max_circuit_depth / delta_clifford must be an integer."
            depths = np.arange(0, self.max_circuit_depth + 0.1, self.delta_clifford, dtype=int)
            depths[0] = 1
            return depths


class SQRBContract(DatasetContract):
    """Custom contract for SQRB supporting either raw (I, Q) or state classification (state)."""

    def validate(self, ds: xr.Dataset) -> None:
        problems: list[str] = []
        for dim in self.dims:
            if dim not in ds.dims:
                problems.append(f"missing dimension {dim!r}")
            if dim not in ds.coords:
                problems.append(f"missing coordinate {dim!r}")
        
        has_iq = "I" in ds.data_vars and "Q" in ds.data_vars
        has_state = "state" in ds.data_vars
        has_i = "I" in ds.data_vars
        
        if not (has_iq or has_state or has_i):
            problems.append("dataset must contain data variables ('I', 'Q') or ('state',)")
        
        if problems:
            raise ContractError(
                f"dataset does not conform to SQRB contract: " + "; ".join(problems)
            )


class QubitSQRBResult(Result):
    """Result of QubitSQRB."""
    pass


class QubitSQRB(Experiment):
    """Single Qubit Randomized Benchmarking."""

    name: ClassVar[str] = "qubit_sqrb"
    description: ClassVar[str] = (
        "Single Qubit Randomized Benchmarking (SQRB) to measure average gate fidelity."
    )
    Parameters: ClassVar[type[QubitSQRBParameters]] = QubitSQRBParameters
    Result: ClassVar[type[QubitSQRBResult]] = QubitSQRBResult
    Contract: ClassVar[DatasetContract] = SQRBContract(
        sweeps=("sequence_idx", "depth"),
        sweep_units=("", ""),
        variables=("I", "Q")
    )

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            "sequence_idx": np.arange(self.params.num_random_sequences),
            "depth": self.params.get_depths(),
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        depths = coords["depth"]
        seq_idx = coords["sequence_idx"]
        qubits = self.params.qubits

        n_qubits = len(qubits)
        n_depths = len(depths)
        n_seqs = len(seq_idx)

        I_data = np.zeros((n_qubits, n_depths, n_seqs))
        Q_data = np.zeros((n_qubits, n_depths, n_seqs))

        rng = np.random.default_rng(stable_seed("qubit_sqrb", *qubits))
        for q_idx in range(n_qubits):
            for d_idx, depth in enumerate(depths):
                # Ground state population starting at 1.0 and decaying to 0.5
                p0 = 0.5 + 0.48 * (0.985 ** depth)
                
                # Add sequence variability
                seq_p0 = p0 + rng.normal(0, 0.03, n_seqs)
                seq_p0 = np.clip(seq_p0, 0.0, 1.0)
                
                # Shot noise
                I_data[q_idx, d_idx] = seq_p0 + rng.normal(0, 0.2 / np.sqrt(self.params.num_averages), n_seqs)

        return {
            "I": I_data,
            "Q": Q_data
        }

    def estimate(self) -> QubitSQRBResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.qubit_sqrb import QubitSQRBEstimator

        results = per_qubit_results(
            self.dataset, QubitSQRBEstimator(), artifact_dir=self.artifact_dir
        )

        result = QubitSQRBResult()
        for qubit in self.params.qubits:
            r = results[qubit]
            result.fit[qubit] = {
                "alpha": float(r["alpha"]),
                "alpha_stderr": float(r["alpha_stderr"]),
                "error_per_clifford": float(r["error_per_clifford"]),
                "error_per_gate": float(r["error_per_gate"]),
                "gate_fidelity": float(r["gate_fidelity"]),
            }
            result.outcomes[qubit] = Outcome.SUCCESSFUL if r.get("success", False) else Outcome.FAILED
        return result
