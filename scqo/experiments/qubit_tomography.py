"""Qubit tomography — greenfield port.

Port of :mod:`scqo.experiments.qubit_tomography`. The physics half
(custom tomography contract, define_sweep/simulate/estimate) is
byte-for-byte; this module touches no device fields (no anchors, no
update()), so only the registry surface moved: kind-based gating via the
default qubit-like ``target_kinds``, ``@register`` into the greenfield
registry, and the driver-supplied ``probe()`` stub.
"""

from __future__ import annotations

from typing import Any, ClassVar

import numpy as np
from pydantic import Field, field_validator
import xarray as xr

from .._scqat import per_qubit_results
from ..contract import ContractError, DatasetContract
from ._capabilities.qubit_reset import QubitResetParameters
from ._sim import stable_seed
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register


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

        has_nc = "noise_condition" in ds.dims or "noise_condition" in ds.coords
        if has_nc:
            tomo_dims = {"target", "noise_condition", "basis", "sym", "gate_count", "shot_idx"}
        else:
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


class QubitTomographyParameters(TargetSelection, AveragingParameters, QubitResetParameters):
    """Inputs for a Qubit Tomography experiment."""

    qubit_configs: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description=(
            "Qubit configurations mapping qubit name to init_state "
            "('0','1','+','-','+i','-i'), target_gate ('I','X','X90','Y','Y90'), "
            "amp (float, amplitude scaling factor, default 1.0), "
            "detuning / frequency_shift (float in Hz, default 0.0), "
            "and noise_mode (bool, default False). A noise_mode qubit is a "
            "spectator noise source: it still plays its target gates but skips "
            "init, basis rotation and measurement — its record holds dummy "
            "zero I/Q and the estimator marks it success=0."
        )
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

    interleave_noise: bool = Field(
        True,
        description="Whether to interleave Noise OFF (baseline) and Noise ON conditions within each shot."
    )
    symmetrized_readout: bool = Field(
        True,
        description="Whether to perform symmetrized (inverted) readout for error mitigation."
    )
    num_training_shots: int = Field(
        2000,
        description="Number of shots for training GMM classifier."
    )


class QubitTomographyResult(Result):
    """Output of QubitTomography."""

    fit: dict[str, dict[str, Any]] = Field(
        default_factory=dict, description="Per-qubit extracted quantities, keyed by qubit name."
    )


@register
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
        sweeps=("noise_condition", "basis", "sym", "gate_count", "shot_idx", "prepared_state", "train_shot_idx"),
        sweep_units=("", "", "", "", "", "", ""),
        variables=("I_tomo", "Q_tomo", "I_train", "Q_train")
    )

    params: QubitTomographyParameters

    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout")

    def define_sweep(self) -> dict[str, np.ndarray]:
        has_noise = any(
            bool(cfg.get("noise_mode", False))
            for cfg in self.params.qubit_configs.values()
        )
        sweeps = {}
        if has_noise and self.params.interleave_noise:
            sweeps["noise_condition"] = np.array(["off", "on"])
        else:
            sweeps["noise_condition"] = np.array(["off"])
        sweeps.update({
            "basis": np.array(["x", "y", "z"]),
            "sym": np.array(["reg", "inv"]) if self.params.symmetrized_readout else np.array(["reg"]),
            "gate_count": np.array(self.params.gate_counts),
            "shot_idx": np.arange(self.params.num_averages),
            "prepared_state": np.array([0, 1]),
            "train_shot_idx": np.arange(self.params.num_training_shots)
        })
        return sweeps

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        qubits = self.params.targets
        n_qubits = len(qubits)

        noise_conds = coords.get("noise_condition", np.array(["off"]))
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
        I_tomo = np.empty((n_qubits, len(noise_conds), len(bases), len(syms), len(gate_counts), len(shot_idx)))
        Q_tomo = np.empty_like(I_tomo)

        for q_idx, qubit in enumerate(qubits):
            config = self.params.qubit_configs.get(qubit, {"init_state": "0", "target_gate": "X"})
            for nc_idx, nc in enumerate(noise_conds):
                decay_rate = 12.0 if nc == "off" else 9.0
                xtalk_phase = 0.0 if nc == "off" else 0.05
                for b_idx, basis in enumerate(bases):
                    for s_idx, sym in enumerate(syms):
                        for g_idx, gc in enumerate(gate_counts):
                            if basis == "x":
                                p = 0.5 + 0.5 * np.exp(-gc / decay_rate) * np.cos(gc * 0.1 + xtalk_phase * gc)
                            elif basis == "y":
                                p = 0.5 + 0.5 * np.exp(-gc / decay_rate) * np.sin(gc * 0.1 + xtalk_phase * gc)
                            else:
                                p = 0.5 - 0.5 * np.exp(-gc / decay_rate)

                            if sym == "inv":
                                p = 1.0 - p

                            actual_states = rng.binomial(1, np.clip(p, 0.0, 1.0), len(shot_idx))
                            cx = np.where(actual_states == 1, 4.0, 0.0)
                            I_tomo[q_idx, nc_idx, b_idx, s_idx, g_idx] = cx + rng.normal(0, 0.8, len(shot_idx))
                            Q_tomo[q_idx, nc_idx, b_idx, s_idx, g_idx] = rng.normal(0, 0.8, len(shot_idx))

        return {
            "I_tomo": (("target", "noise_condition", "basis", "sym", "gate_count", "shot_idx"), I_tomo),
            "Q_tomo": (("target", "noise_condition", "basis", "sym", "gate_count", "shot_idx"), Q_tomo),
            "I_train": (("target", "prepared_state", "train_shot_idx"), I_train),
            "Q_train": (("target", "prepared_state", "train_shot_idx"), Q_train)
        }

    def estimate(self) -> QubitTomographyResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.qubit_tomography import QubitTomographyEstimator

        # Analyze only target qubits, skipping spectator noise-mode qubits
        measured_targets = [
            q for q in self.params.targets
            if not bool(self.params.qubit_configs.get(q, {}).get("noise_mode", False))
        ]
        if not measured_targets:
            measured_targets = list(self.params.targets)

        ds_to_analyze = self.dataset.sel(target=measured_targets)

        # Split along qubit dimension and analyze
        results = per_qubit_results(
            ds_to_analyze, QubitTomographyEstimator(), artifact_dir=self.artifact_dir
        )

        result = QubitTomographyResult()
        for qubit in measured_targets:
            r = results.get(qubit, {})
            result.fit[qubit] = r
            result.outcomes[qubit] = Outcome.SUCCESSFUL if (r and r.get("success", False)) else Outcome.FAILED
        return result

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
