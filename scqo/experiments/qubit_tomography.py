"""Qubit Tomography (backend-free half).

Performs state tomography by applying init states, target gates, and sweeping
basis rotations to measure populations and gate error trajectory.
"""

from __future__ import annotations

import json
from typing import ClassVar, Dict, Any

import numpy as np
from pydantic import Field, field_validator
import xarray as xr

from .._scqat import per_qubit_results
from ..contract import DatasetContract, ContractError
from ..parameters import AveragingParameters, TargetSelection
from ..experiment import Experiment
from ..result import Outcome, Result
from ._sim import stable_seed


class TomographyContract(DatasetContract):
    """Custom dataset contract for tomography experiments."""

    def validate(self, ds: xr.Dataset) -> None:
        problems: list[str] = []
        required_dims = ["target", "basis", "sym", "gate_count", "shot_idx", "prepared_state", "train_shot_idx"]
        for dim in required_dims:
            if dim not in ds.dims:
                problems.append(f"missing dimension {dim!r}")
            if dim not in ds.coords:
                problems.append(f"missing coordinate {dim!r}")
        
        tomo_vars = ["I_tomo", "Q_tomo"]
        train_vars = ["I_train", "Q_train"]
        
        tomo_dims = {"target", "basis", "sym", "gate_count", "shot_idx"}
        train_dims = {"target", "prepared_state", "train_shot_idx"}
        
        for var in tomo_vars:
            if var not in ds.data_vars:
                problems.append(f"missing variable {var!r}")
            elif set(ds[var].dims) != tomo_dims:
                problems.append(f"variable {var!r} has dims {tuple(ds[var].dims)}, expected {tomo_dims}")
                
        for var in train_vars:
            if var not in ds.data_vars:
                problems.append(f"missing variable {var!r}")
            elif set(ds[var].dims) != train_dims:
                problems.append(f"variable {var!r} has dims {tuple(ds[var].dims)}, expected {train_dims}")
                
        if problems:
            raise ContractError("dataset does not conform to Tomography contract: " + "; ".join(problems))


class QubitTomographyParameters(TargetSelection, AveragingParameters):
    """Inputs for a Qubit Tomography experiment."""

    qubit_configs: dict[str, dict[str, str]] = Field(
        default_factory=dict,
        description="Qubit configurations mapping qubit name to init_state ('0','1','+','-','+i','-i') and target_gate ('I','X','X90','Y','Y90')"
    )
    gate_counts: list[int] = Field(
        default_factory=lambda: list(range(0, 11)),
        description="Gate counts to sweep from 0 to 10 inclusive."
    )

    @field_validator("gate_counts", mode="before")
    @classmethod
    def parse_gate_counts(cls, val: Any) -> Any:
        if isinstance(val, str):
            parts = val.strip().split(":")
            if len(parts) in (2, 3):
                try:
                    start = int(parts[0])
                    stop = int(parts[1])
                    step = int(parts[2]) if len(parts) == 3 else 1
                    return list(range(start, stop + 1, step))
                except ValueError:
                    pass
        elif isinstance(val, dict):
            if "start" in val and "stop" in val:
                try:
                    start = int(val["start"])
                    stop = int(val["stop"])
                    step = int(val.get("step", 1))
                    return list(range(start, stop + 1, step))
                except ValueError:
                    pass
        return val
    symmetrized_readout: bool = Field(
        False,
        description="Whether to perform symmetrized (inverted) readout for error mitigation."
    )
    num_training_shots: int = Field(
        2000,
        description="Number of shots for training GMM classifier."
    )

class QubitTomographyResult(Result):
    """Output of QubitTomography."""
    pass


class QubitTomography(Experiment):
    """Backend-agnostic Qubit Tomography. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "qubit_tomography"
    description: ClassVar[str] = (
        "Performs state tomography by applying init states, target gates, "
        "and sweeping basis rotations to measure populations and gate error trajectory."
    )
    Parameters: ClassVar[type] = QubitTomographyParameters
    Result: ClassVar[type] = QubitTomographyResult
    Contract: ClassVar[DatasetContract] = TomographyContract(
        sweeps=("basis", "sym", "gate_count", "shot_idx", "prepared_state", "train_shot_idx"),
        sweep_units=("", "", "", "", "", ""),
        variables=("I_tomo", "Q_tomo", "I_train", "Q_train")
    )

    params: QubitTomographyParameters

    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout")

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            "basis": np.array(["x", "y", "z"]),
            "sym": np.array(["reg", "inv"]) if self.params.symmetrized_readout else np.array(["reg"]),
            "gate_count": np.array(self.params.gate_counts),
            "shot_idx": np.arange(self.params.num_averages),
            "prepared_state": np.array([0, 1]),
            "train_shot_idx": np.arange(self.params.num_training_shots)
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        qubits = self.params.targets
        n_qubits = len(qubits)
        
        bases = coords["basis"]
        syms = coords["sym"]
        gate_counts = coords["gate_count"]
        shot_idx = coords["shot_idx"]
        prepared_states = coords["prepared_state"]
        train_shot_idx = coords["train_shot_idx"]
        
        # 1. Simulate training data
        I_train = np.empty((n_qubits, len(prepared_states), len(train_shot_idx)))
        Q_train = np.empty_like(I_train)
        
        rng = np.random.default_rng(stable_seed("qubit_tomography", *qubits))
        for q_idx in range(n_qubits):
            for s_idx, state in enumerate(prepared_states):
                center_x = 0.0 if state == 0 else 4.0
                I_train[q_idx, s_idx] = center_x + rng.normal(0, 0.8, len(train_shot_idx))
                Q_train[q_idx, s_idx] = rng.normal(0, 0.8, len(train_shot_idx))
                
        # 2. Simulate tomography data
        I_tomo = np.empty((n_qubits, len(bases), len(syms), len(gate_counts), len(shot_idx)))
        Q_tomo = np.empty_like(I_tomo)
        
        for q_idx, qubit in enumerate(qubits):
            config = self.params.qubit_configs.get(qubit, {"init_state": "0", "target_gate": "X"})
            for b_idx, basis in enumerate(bases):
                for s_idx, sym in enumerate(syms):
                    for g_idx, gc in enumerate(gate_counts):
                        if basis == "x":
                            p = 0.5 + 0.5 * np.exp(-gc / 10.0) * np.cos(gc * 0.1)
                        elif basis == "y":
                            p = 0.5 + 0.5 * np.exp(-gc / 10.0) * np.sin(gc * 0.1)
                        else:
                            p = 0.5 - 0.5 * np.exp(-gc / 10.0)
                        
                        if sym == "inv":
                            p = 1.0 - p
                            
                        actual_states = rng.binomial(1, p, len(shot_idx))
                        cx = np.where(actual_states == 1, 4.0, 0.0)
                        I_tomo[q_idx, b_idx, s_idx, g_idx] = cx + rng.normal(0, 0.8, len(shot_idx))
                        Q_tomo[q_idx, b_idx, s_idx, g_idx] = rng.normal(0, 0.8, len(shot_idx))
                        
        return {
            "I_tomo": (("target", "basis", "sym", "gate_count", "shot_idx"), I_tomo),
            "Q_tomo": (("target", "basis", "sym", "gate_count", "shot_idx"), Q_tomo),
            "I_train": (("target", "prepared_state", "train_shot_idx"), I_train),
            "Q_train": (("target", "prepared_state", "train_shot_idx"), Q_train)
        }
    def estimate(self) -> QubitTomographyResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.qubit_tomography import QubitTomographyEstimator
        
        # Split along qubit dimension and analyze
        results = per_qubit_results(
            self.dataset, QubitTomographyEstimator(), artifact_dir=self.artifact_dir
        )
        
        result = QubitTomographyResult()
        for qubit in self.params.targets:
            r = results.get(qubit, {})
            result.fit[qubit] = r
            result.outcomes[qubit] = Outcome.SUCCESSFUL if (r and r.get("success", False)) else Outcome.FAILED
        return result
