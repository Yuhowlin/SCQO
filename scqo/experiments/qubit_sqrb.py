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
from ..contract import ContractError, DatasetContract
from ..parameters import AveragingParameters, TargetSelection
from ..experiment import Experiment
from ..result import Outcome, Result
from ._sim import stable_seed


class QubitSQRBParameters(TargetSelection, AveragingParameters):
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
    readout_mode: str = Field(
        "raw_iq",
        description="Readout mode: 'raw_iq' (demodulated IQ) or 'hardware_state' (QM readout_state 2D)."
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

    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout")

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            "sequence_idx": np.arange(self.params.num_random_sequences),
            "depth": self.params.get_depths(),
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        depths = coords["depth"]
        seq_idx = coords["sequence_idx"]
        qubits = self.params.targets

        n_qubits = len(qubits)
        n_depths = len(depths)
        n_seqs = len(seq_idx)

        # Axis order MUST match the declared sweep order (sequence_idx, depth):
        # acquire() labels axes ("target", "sequence_idx", "depth").
        I_data = np.zeros((n_qubits, n_seqs, n_depths))
        Q_data = np.zeros((n_qubits, n_seqs, n_depths))

        rng = np.random.default_rng(stable_seed("qubit_sqrb", *qubits))
        for q_idx in range(n_qubits):
            for d_idx, depth in enumerate(depths):
                # Ground state population starting at 1.0 and decaying to 0.5
                p0 = 0.5 + 0.48 * (0.985 ** depth)

                # Add sequence variability
                seq_p0 = p0 + rng.normal(0, 0.03, n_seqs)
                seq_p0 = np.clip(seq_p0, 0.0, 1.0)

                # Shot noise (per sequence, at this depth)
                I_data[q_idx, :, d_idx] = seq_p0 + rng.normal(0, 0.2 / np.sqrt(self.params.num_averages), n_seqs)

        return {
            "I": I_data,
            "Q": Q_data
        }

    def estimate(self) -> QubitSQRBResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.qubit_sqrb import QubitSQRBEstimator
        from scqat.parsers import repetition_data

        prepared = self.dataset.copy()
        if "state" in prepared and "signal" not in prepared:
            prepared["signal"] = prepared["state"]
        elif "I" in prepared and "signal" not in prepared:
            prepared["signal"] = prepared["I"]

        gef_centers_dict: dict[str, list] = {}
        if self.backend is not None and hasattr(self.backend, "machine"):
            machine = getattr(self.backend, "machine", None)
            if machine is not None and hasattr(machine, "qubits"):
                for q_name in self.params.targets:
                    if q_name in machine.qubits:
                        q_obj = machine.qubits[q_name]
                        centers = getattr(q_obj.resonator, "gef_centers", None)
                        if centers is not None:
                            gef_centers_dict[q_name] = centers

        estimator = QubitSQRBEstimator()
        result = QubitSQRBResult()

        for sq in repetition_data(prepared, repetition_dim="target"):
            qubit_name = sq["target"].values.item()
            out_dir = str(self.artifact_dir / str(qubit_name)) if self.artifact_dir is not None else None
            centers = gef_centers_dict.get(qubit_name)

            try:
                results, figures = estimator.analyze(
                    sq, output_dir=out_dir, skip_figures=self.artifact_dir is None,
                    readout_mode=self.params.readout_mode, gef_centers=centers
                )
            except Exception:
                results, figures = estimator.analyze(
                    sq, output_dir=None, skip_figures=True,
                    readout_mode=self.params.readout_mode, gef_centers=centers
                )

            result.fit[qubit_name] = {
                "alpha": float(results["alpha"]),
                "alpha_stderr": float(results["alpha_stderr"]),
                "error_per_clifford": float(results["error_per_clifford"]),
                "error_per_gate": float(results["error_per_gate"]),
                "gate_fidelity": float(results["gate_fidelity"]),
            }
            result.outcomes[qubit_name] = Outcome.SUCCESSFUL if results.get("success", False) else Outcome.FAILED
        return result
