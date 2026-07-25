"""Qubit single-qubit randomized benchmarking (SQRB), greenfield.

Port of :mod:`scqo.experiments.qubit_sqrb`. The physics half is
byte-for-byte; nothing on the device surface moved — the module reads and
writes no device fields (its fit keys are purely descriptive gate-fidelity
quantities), so only the import paths and registration changed.
"""

from __future__ import annotations

from typing import ClassVar, Dict, Any, Optional

import numpy as np
from pydantic import Field

from ..._scqat import per_qubit_results
from ...contract import DatasetContract
from ...experiments._capabilities.state_readout import (
    STATE_ALT,
    StateReadoutParameters,
    readout_vars,
    state_row,
)
from ...experiments._sim import stable_seed
from ...parameters import AveragingParameters, TargetSelection
from ...result import Outcome, Result
from ..experiment import Experiment
from . import register


class QubitSQRBParameters(TargetSelection, AveragingParameters, StateReadoutParameters):
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


class QubitSQRBResult(Result):
    """Result of QubitSQRB."""
    pass


@register
class QubitSQRB(Experiment):
    """Single Qubit Randomized Benchmarking."""

    name: ClassVar[str] = "qubit_sqrb"
    description: ClassVar[str] = (
        "Single Qubit Randomized Benchmarking (SQRB) to measure average gate fidelity."
    )
    Parameters: ClassVar[type[QubitSQRBParameters]] = QubitSQRBParameters
    Result: ClassVar[type[QubitSQRBResult]] = QubitSQRBResult
    # raw (I, Q), or the FPGA-discriminated state, or a bare I quadrature — the
    # alt sets are checked with the same dims rigor as the primary set.
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("sequence_idx", "depth"),
        sweep_units=("", ""),
        variables=("I", "Q"),
        alt_variables=(*STATE_ALT, ("I",)),
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
        state = np.zeros_like(I_data)

        use_state = self.params.use_state_discrimination
        shot_noise = 0.2 / np.sqrt(self.params.num_averages)
        rng = np.random.default_rng(stable_seed("qubit_sqrb", *qubits))
        for q_idx in range(n_qubits):
            for d_idx, depth in enumerate(depths):
                # Ground state population starting at 1.0 and decaying to 0.5
                p0 = 0.5 + 0.48 * (0.985 ** depth)

                # Add sequence variability
                seq_p0 = p0 + rng.normal(0, 0.03, n_seqs)
                seq_p0 = np.clip(seq_p0, 0.0, 1.0)

                # Shot noise (per sequence, at this depth) — one draw in either mode,
                # so the I/Q stream is unchanged when discrimination is off.
                if use_state:
                    state[q_idx, :, d_idx] = state_row(seq_p0, rng, noise=shot_noise)
                else:
                    I_data[q_idx, :, d_idx] = seq_p0 + rng.normal(0, shot_noise, n_seqs)

        return readout_vars(use_state, state, I_data, Q_data)

    def estimate(self) -> QubitSQRBResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.qubit_sqrb import QubitSQRBEstimator

        # No rename needed: the scqat estimator natively prefers a `state` data var
        # and falls back to `I`/`signal` itself.
        results = per_qubit_results(
            self.dataset, QubitSQRBEstimator(), artifact_dir=self.artifact_dir
        )

        result = QubitSQRBResult()
        for qubit in self.params.targets:
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

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
