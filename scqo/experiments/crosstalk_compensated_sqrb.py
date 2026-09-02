"""Crosstalk Compensated Simultaneous SQRB experiment.

Evaluates and mitigates microwave crosstalk during simultaneous single-qubit
randomized benchmarking (SQRB) on a probe qubit when an aggressor drive qubit
is running concurrent Clifford gates.

Three conditions are compared:
1. Isolated: Probe qubit runs SQRB alone (drive qubit idle).
2. Simultaneous: Probe qubit and drive qubit run SQRB concurrently without compensation.
3. Compensated: Probe qubit and drive qubit run SQRB concurrently, while active
   cancellation pulses (at probe frequency) are injected to cancel crosstalk.

The phase of the compensation pulse evolves gate-by-gate:
    Phi_k = init_phase + k * phase_rate + gate_phase(G_d,k)
where phase_rate = 2 * pi * (f_d - f_p) * tau_slot is derived from the inter-qubit
detuning and Clifford time slot duration.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

import numpy as np
from pydantic import Field, model_validator
import xarray as xr

from .._scqat import per_qubit_results
from ..contract import DatasetContract
from ._capabilities.qubit_reset import QubitResetParameters
from ._capabilities.state_readout import (
    POPULATION_ALT,
    StateReadoutParameters,
    population_row,
    readout_vars,
    signal_rename,
)
from ._sim import iq_from_population, stable_seed
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register


def compute_theoretical_phase_rate(
    f_drive_hz: float, f_probe_hz: float, duration_ns: float
) -> float:
    """Calculate theoretical phase accumulation per gate slot in radians.

    Formula: Delta phi = 2 * pi * (f_d - f_p) * tau_slot.
    """
    delta_f = f_drive_hz - f_probe_hz
    return float(2.0 * np.pi * delta_f * (duration_ns * 1e-9))


class CrosstalkCompensatedSQRBParameters(
    TargetSelection, AveragingParameters, StateReadoutParameters, QubitResetParameters
):
    """Inputs for Crosstalk Compensated Simultaneous SQRB."""

    probe_qubit: str = Field(
        "q0",
        description="Target qubit under SQRB measurement (e.g. 'q1')."
    )
    drive_qubit: str = Field(
        "q1",
        description="Interfering aggressor qubit running concurrent Cliffords (e.g. 'q2')."
    )
    targets: list[str] = Field(
        default_factory=list,
        description="Target qubits [probe_qubit, drive_qubit]. Auto-derived if omitted."
    )

    num_random_sequences: int = Field(
        30,
        gt=0,
        description="Number of distinct random Clifford sequences."
    )
    # Clifford sequence settings
    max_circuit_depth: int = Field(
        256,
        gt=0,
        description="Maximum Clifford circuit depth to sweep in benchmark mode."
    )
    delta_clifford: int = Field(
        20,
        gt=0,
        description="Step size between depths when log_scale is False."
    )
    log_scale: bool = Field(
        True,
        description="Whether to use logarithmic (powers of 2) depth spacing."
    )
    depths: list[int] | None = Field(
        None,
        description="Explicit list of Clifford circuit depths to sweep. If None, derived from max_circuit_depth and log_scale."
    )

    def get_depths(self) -> np.ndarray:
        """Generate depths based on explicit list or log_scale / max_circuit_depth."""
        if self.depths is not None and len(self.depths) > 0:
            return np.asarray(self.depths, dtype=int)
        if self.log_scale:
            depth_list = [1]
            cur = 2
            while cur <= self.max_circuit_depth:
                depth_list.append(cur)
                cur *= 2
            return np.array(depth_list, dtype=int)
        else:
            arr = np.arange(0, self.max_circuit_depth + 0.1, self.delta_clifford, dtype=int)
            arr[0] = 1
            return arr

    # Active compensation knobs
    cancel_amp: float = Field(
        0.0,
        ge=0.0,
        description="Compensation pulse amplitude scale relative to probe drive amplitude."
    )
    init_phase: float = Field(
        0.0,
        description="Initial phase offset in radians."
    )
    phase_rate: float | None = Field(
        None,
        description=(
            "Phase evolution rate per gate slot in radians (Delta phi). "
            "If None, computed automatically from theoretical detuning (f_d - f_p) and slot duration."
        )
    )
    clifford_duration_ns: int = Field(
        40,
        gt=0,
        description="Duration of each Clifford slot in ns for theoretical phase rate calculation."
    )

    # Execution mode
    mode: Literal["benchmark", "calibrate"] = Field(
        "benchmark",
        description=(
            "'benchmark' runs 3 SQRB curves (isolated, simultaneous, compensated). "
            "'calibrate' sweeps cancel_amp and init_phase at a fixed cal_depth to find optimal values."
        )
    )

    # Calibration mode sweep parameters
    min_cancel_amp: float = Field(
        0.0,
        ge=0.0,
        description="Minimum compensation amplitude scale for calibrate mode."
    )
    max_cancel_amp: float = Field(
        0.1,
        ge=0.0,
        description="Maximum compensation amplitude scale for calibrate mode."
    )
    num_cancel_amps: int = Field(
        21,
        gt=1,
        description="Number of amplitude sweep points for calibrate mode."
    )
    cancel_amps: list[float] | None = Field(
        None,
        description="Explicit list of amplitude sweep points for calibrate mode. Overrides min/max/num."
    )

    min_init_phase: float = Field(
        -np.pi,
        description="Minimum initial phase in radians for calibrate mode."
    )
    max_init_phase: float = Field(
        np.pi,
        description="Maximum initial phase in radians for calibrate mode."
    )
    num_init_phases: int = Field(
        25,
        gt=1,
        description="Number of initial phase sweep points for calibrate mode."
    )
    init_phases: list[float] | None = Field(
        None,
        description="Explicit list of initial phase sweep points for calibrate mode in radians. Overrides min/max/num."
    )

    def get_cancel_amps(self) -> np.ndarray:
        """Generate amplitude sweep array based on explicit list or min/max/num."""
        if self.cancel_amps is not None and len(self.cancel_amps) > 0:
            return np.asarray(self.cancel_amps, dtype=float)
        return np.linspace(self.min_cancel_amp, self.max_cancel_amp, self.num_cancel_amps, dtype=float)

    def get_init_phases(self) -> np.ndarray:
        """Generate initial phase sweep array based on explicit list or min/max/num."""
        if self.init_phases is not None and len(self.init_phases) > 0:
            return np.asarray(self.init_phases, dtype=float)
        return np.linspace(self.min_init_phase, self.max_init_phase, self.num_init_phases, dtype=float)

    cal_depth: int = Field(
        20,
        gt=0,
        description="Fixed Clifford depth used for parameter scan in calibrate mode."
    )

    seed: int | None = Field(
        None,
        description="Optional random seed for reproducible sequence generation."
    )

    @model_validator(mode="before")
    @classmethod
    def _resolve_targets_and_qubits(cls, data: Any) -> Any:
        if isinstance(data, dict):
            targets = data.get("targets")
            if targets:
                if isinstance(targets, str):
                    targets = [t.strip() for t in targets.split(",")]
                valid = [t for t in targets if not t.endswith("_c")]
                if len(valid) == 1 and "_" in valid[0]:
                    parts = valid[0].split("_")
                    data["probe_qubit"] = parts[0]
                    data["drive_qubit"] = parts[1]
                    data["targets"] = [parts[0], parts[1]]
                elif len(valid) == 1:
                    data.setdefault("probe_qubit", valid[0])
                    if "drive_qubit" not in data:
                        data["drive_qubit"] = "q2" if valid[0] != "q2" else "q1"
                    data["targets"] = [data["probe_qubit"], data["drive_qubit"]]
                elif len(valid) >= 2:
                    data["probe_qubit"] = data.get("probe_qubit", valid[0])
                    data["drive_qubit"] = data.get("drive_qubit", valid[1])
                    data["targets"] = [data["probe_qubit"], data["drive_qubit"]]
            elif "probe_qubit" in data and "drive_qubit" in data:
                data["targets"] = [data["probe_qubit"], data["drive_qubit"]]
        return data

    @model_validator(mode="after")
    def _ensure_targets(self) -> CrosstalkCompensatedSQRBParameters:
        if not self.targets:
            self.targets = [self.probe_qubit, self.drive_qubit]
        return self


from ..contract import ContractError, DatasetContract


class CrosstalkCompensatedSQRBContract(DatasetContract):
    """Custom dataset contract for CrosstalkCompensatedSQRB supporting benchmark and calibrate modes."""

    def validate(self, ds: xr.Dataset) -> None:
        if "cancel_amp" in ds.coords and "init_phase" in ds.coords:
            required_coords = {"target", "cancel_amp", "init_phase", "sequence_idx"}
        else:
            required_coords = {"target", "condition", "sequence_idx", "depth"}

        problems: list[str] = []
        for coord in required_coords:
            if coord not in ds.dims:
                problems.append(f"missing dimension {coord!r}")
            if coord not in ds.coords:
                problems.append(f"missing coordinate {coord!r}")

        if not any(v in ds.data_vars for v in ("I", "state", "signal", "population")):
            problems.append("missing data variable ('I', 'state', 'signal', or 'population')")

        if problems:
            raise ContractError("; ".join(problems))


class CrosstalkCompensatedSQRBResult(Result):
    """Output of CrosstalkCompensatedSQRB."""

    fit: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Extracted quantities (fidelities, error rates, optimal compensation)."
    )


@register
class CrosstalkCompensatedSQRB(Experiment):
    """Crosstalk Compensated Simultaneous SQRB Experiment."""

    name: ClassVar[str] = "crosstalk_compensated_sqrb"
    description: ClassVar[str] = (
        "Simultaneous Single Qubit Randomized Benchmarking comparing isolated, simultaneous, "
        "and active crosstalk-compensated conditions using gate-by-gate dynamic phase tracking."
    )
    Parameters: ClassVar[type] = CrosstalkCompensatedSQRBParameters
    Result: ClassVar[type] = CrosstalkCompensatedSQRBResult

    Contract: ClassVar[DatasetContract] = CrosstalkCompensatedSQRBContract(
        sweeps=("condition", "sequence_idx", "depth"),
        sweep_units=("", "", ""),
        variables=("I", "Q"),
        alt_variables=(*POPULATION_ALT, ("I",)),
    )

    params: CrosstalkCompensatedSQRBParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        if self.params.mode == "calibrate":
            return {
                "cancel_amp": self.params.get_cancel_amps(),
                "init_phase": self.params.get_init_phases(),
                "sequence_idx": np.arange(self.params.num_random_sequences, dtype=int),
            }
        return {
            "condition": np.array(["isolated", "simultaneous", "compensated"]),
            "sequence_idx": np.arange(self.params.num_random_sequences, dtype=int),
            "depth": self.params.get_depths(),
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        probe = self.params.probe_qubit
        rng = np.random.default_rng(stable_seed("crosstalk_compensated_sqrb", probe))

        # Base decay parameters
        p_iso = 0.995  # Isolated Clifford survival factor
        p_sim = 0.970  # Degraded survival factor under uncompensated crosstalk

        # Quality of compensation depends on distance from optimal (alpha=0.035, phi=0.85)
        opt_amp = 0.035
        opt_phi = 0.85
        actual_amp = float(self.params.cancel_amp)
        actual_phi = float(self.params.init_phase)
        dist_sq = (actual_amp - opt_amp) ** 2 + 0.001 * (actual_phi - opt_phi) ** 2
        comp_quality = np.exp(-dist_sq / 0.002)
        p_comp = p_sim + (p_iso - 0.002 - p_sim) * comp_quality

        n_targets = max(1, len(self.params.targets))

        if self.params.mode == "calibrate":
            amps = coords["cancel_amp"]
            phases = coords["init_phase"]
            n_seq = len(coords["sequence_idx"])
            cal_depth = self.params.cal_depth

            # Simulate P0 as 2D bowl centered at opt_amp, opt_phi
            A_grid, P_grid = np.meshgrid(amps, phases, indexing="ij")
            err_dist = (A_grid - opt_amp) ** 2 + 0.0005 * (np.sin((P_grid - opt_phi) / 2)) ** 2
            p_effective = p_sim + (p_iso - p_sim) * np.exp(-err_dist / 0.002)
            base_pop = 0.5 + 0.5 * (p_effective ** cal_depth)

            # Broadcast across sequence_idx: shape (len(amps), len(phases), n_seq)
            pop = np.tile(base_pop[:, :, None], (1, 1, n_seq))
            noise = rng.normal(0, 0.01, pop.shape)
            pop = np.clip(pop + noise, 0.0, 1.0)

            # Target dimension: (n_targets, cancel_amp, init_phase, sequence_idx)
            pop_t = np.repeat(pop[None, ...], n_targets, axis=0)
            if self.params.use_state_discrimination:
                return {"state": (("target", "cancel_amp", "init_phase", "sequence_idx"), 1.0 - pop_t)}
            return {
                "I": (("target", "cancel_amp", "init_phase", "sequence_idx"), pop_t),
                "Q": (("target", "cancel_amp", "init_phase", "sequence_idx"), np.zeros_like(pop_t)),
            }

        # Benchmark mode: 3 conditions over depths
        conditions = coords["condition"]
        depths = coords["depth"]
        n_seq = len(coords["sequence_idx"])

        p_map = {
            "isolated": p_iso,
            "simultaneous": p_sim,
            "compensated": p_comp,
        }

        pop_data = np.empty((len(conditions), n_seq, len(depths)), dtype=float)
        for c_idx, c_name in enumerate(conditions):
            p_val = p_map.get(str(c_name), p_iso)
            for d_idx, d in enumerate(depths):
                mean_p0 = 0.5 + 0.48 * (p_val ** d)
                pop_data[c_idx, :, d_idx] = np.clip(
                    mean_p0 + rng.normal(0, 0.005, n_seq), 0.0, 1.0
                )

        pop_t = np.repeat(pop_data[None, ...], n_targets, axis=0)  # (n_targets, cond, seq, depth)
        if self.params.use_state_discrimination:
            return {"state": (("target", "condition", "sequence_idx", "depth"), pop_t)}
        return {
            "I": (("target", "condition", "sequence_idx", "depth"), pop_t),
            "Q": (("target", "condition", "sequence_idx", "depth"), np.zeros_like(pop_t)),
        }

    def estimate(self) -> CrosstalkCompensatedSQRBResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.crosstalk_compensated_sqrb import (
            CrosstalkCompensatedSQRBEstimator,
        )

        probe = self.params.probe_qubit
        if "target" in self.dataset.coords and probe in self.dataset.coords["target"].values:
            ds_probe = self.dataset.sel(target=probe)
        else:
            ds_probe = self.dataset

        estimator = CrosstalkCompensatedSQRBEstimator()
        out_dir = str(self.artifact_dir) if self.artifact_dir is not None else None
        fit_res, _figures = estimator.analyze(ds_probe, output_dir=out_dir)

        result = CrosstalkCompensatedSQRBResult()
        result.fit[probe] = fit_res
        result.outcomes[probe] = (
            Outcome.SUCCESSFUL if fit_res.get("success", False) else Outcome.FAILED
        )
        return result

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
