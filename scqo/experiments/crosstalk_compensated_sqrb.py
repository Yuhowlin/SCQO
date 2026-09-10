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

    target_gate: str = Field(
        "x180",
        description="Target gate repeated on drive qubit and cancel element in calibrate mode: x180, y180, x90, y90."
    )

    benchmark_batch_size: int = Field(
        1,
        gt=0,
        description="Number of depths per execution batch to prevent OPX memory overload."
    )
    benchmark_sequence_batch_size: int | None = Field(
        None,
        gt=0,
        description="Chunk size for sequences per batch. If None, computed adaptively from max_gates_per_batch and depth."
    )
    max_gates_per_batch: int = Field(
        120,
        gt=0,
        description="Maximum Clifford gates per QUA compilation batch to avoid QOP memory exhaustion and compile timeouts."
    )
    conditions: list[str] | None = Field(
        None,
        description="List of conditions to benchmark (e.g. ['isolated', 'simultaneous', 'compensated'])."
    )

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
            "Phase evolution rate per atomic pulse subslot in radians (Delta phi). "
            "If None, computed automatically from theoretical detuning (f_d - f_p) and atomic pulse duration."
        )
    )
    clifford_duration_ns: int = Field(
        16,
        gt=0,
        description="Duration of each atomic pulse subslot in ns for theoretical phase rate calculation."
    )
    strict_timing: bool = Field(
        True,
        description="Whether to enforce strict_timing_() in QUA to eliminate inter-gate calculation gaps."
    )

    # Execution mode
    mode: Literal["benchmark", "calibrate"] = Field(
        "benchmark",
        description=(
            "'benchmark' runs 3 SQRB curves (isolated, simultaneous, compensated). "
            "'calibrate' runs multi-stage zoom-in sweeps of cancel_amp and init_phase with probe idle in |0>."
        )
    )

    # Multi-stage zoom-in calibration parameters
    cal_stage_repetitions: list[int] | str | int | None = Field(
        default_factory=lambda: [5, 10, 20],
        description="Clifford depths or pulse repetitions for each zoom-in stage in calibrate mode."
    )
    zoom_factor: float = Field(
        2.0,
        gt=1.0,
        description="Range contraction factor per zoom-in stage (span /= zoom_factor)."
    )

    def get_cal_stage_repetitions(self) -> list[int]:
        """Generate list of gate counts for each zoom-in calibration stage."""
        val = self.cal_stage_repetitions
        if val is not None:
            if isinstance(val, str):
                cleaned = val.strip()
                if "," in cleaned:
                    return [max(1, int(x.strip())) for x in cleaned.split(",") if x.strip()]
                elif ":" in cleaned:
                    parts = cleaned.split(":")
                    if len(parts) == 2:
                        return list(range(int(parts[0]), int(parts[1]) + 1, max(1, (int(parts[1]) - int(parts[0])) // 3)))
                    elif len(parts) >= 3:
                        return list(range(int(parts[0]), int(parts[1]) + 1, int(parts[2])))
                try:
                    return [max(1, int(cleaned))]
                except ValueError:
                    pass
            elif isinstance(val, (int, float)):
                return [max(1, int(val))]
            elif isinstance(val, (list, tuple, np.ndarray)):
                return [max(1, int(x)) for x in val]
        return [5, 10, 20]

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
            if "batch_size" in data:
                data["benchmark_batch_size"] = data.pop("batch_size")
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
            required_coords = {"target", "cancel_amp", "init_phase"}
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
            stage_reps = self.params.get_cal_stage_repetitions()

            # Simulate cancellation response across stage gate repetitions with probe idle in |0>
            A_grid, P_grid = np.meshgrid(amps, phases, indexing="ij")
            residual_drive = np.sqrt(
                (A_grid - opt_amp) ** 2 + 0.001 * (np.sin((P_grid - opt_phi) / 2)) ** 2
            )
            p1_acc = np.zeros_like(residual_drive)
            for r in stage_reps:
                theta = 2.0 * np.pi * residual_drive * r
                p1_acc += (np.sin(theta / 2)) ** 2
            p1_sim = p1_acc / len(stage_reps)
            p1_sim = np.clip(p1_sim + rng.normal(0, 0.015, p1_sim.shape), 0.0, 1.0)

            if "sequence_idx" in coords:
                n_seq = len(coords["sequence_idx"])
                pop_seq = np.tile(p1_sim[:, :, None], (1, 1, n_seq))
                p1_t = np.repeat(pop_seq[None, ...], n_targets, axis=0)
                if self.params.use_state_discrimination:
                    return {"state": (("target", "cancel_amp", "init_phase", "sequence_idx"), p1_t)}
                return {
                    "I": (("target", "cancel_amp", "init_phase", "sequence_idx"), p1_t),
                    "Q": (("target", "cancel_amp", "init_phase", "sequence_idx"), np.zeros_like(p1_t)),
                }

            p1_t = np.repeat(p1_sim[None, ...], n_targets, axis=0)
            if self.params.use_state_discrimination:
                return {"state": (("target", "cancel_amp", "init_phase"), p1_t)}
            return {
                "I": (("target", "cancel_amp", "init_phase"), p1_t),
                "Q": (("target", "cancel_amp", "init_phase"), np.zeros_like(p1_t)),
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

        # Populate synthetic stage_history for offline/simulated calibrate runs if not already set by hardware
        if self.params.mode == "calibrate" and "stage_history" not in ds_probe.attrs:
            try:
                import json
                stage_reps = self.params.get_cal_stage_repetitions()
                history = []
                cur_min_a = float(self.params.min_cancel_amp)
                cur_max_a = float(self.params.max_cancel_amp)
                cur_min_p = float(self.params.min_init_phase)
                cur_max_p = float(self.params.max_init_phase)
                zoom = float(self.params.zoom_factor)
                opt_a = 0.035
                opt_phi = 0.85
                for s_idx, r in enumerate(stage_reps):
                    s_amps = np.linspace(cur_min_a, cur_max_a, int(self.params.num_cancel_amps))
                    s_phases = np.linspace(cur_min_p, cur_max_p, int(self.params.num_init_phases))
                    A, P = np.meshgrid(s_amps, s_phases, indexing="ij")
                    res_drive = np.sqrt((A - opt_a) ** 2 + 0.001 * (np.sin((P - opt_phi) / 2)) ** 2)
                    p_val = np.sin(2.0 * np.pi * res_drive * r / 2) ** 2
                    min_idx = np.unravel_index(np.argmin(p_val), p_val.shape)
                    best_a = float(s_amps[min_idx[0]])
                    best_p = float(s_phases[min_idx[1]])
                    history.append({
                        "stage": s_idx + 1,
                        "repetitions": int(r),
                        "cancel_amps": s_amps.tolist(),
                        "init_phases": s_phases.tolist(),
                        "p_vals": p_val.tolist(),
                        "best_cancel_amp": best_a,
                        "best_init_phase": best_p,
                        "min_signal": float(p_val[min_idx]),
                    })
                    span_a = (cur_max_a - cur_min_a) / zoom
                    span_p = (cur_max_p - cur_min_p) / zoom
                    cur_min_a = max(0.0, best_a - span_a / 2.0)
                    cur_max_a = best_a + span_a / 2.0
                    cur_min_p = best_p - span_p / 2.0
                    cur_max_p = best_p + span_p / 2.0
                ds_probe.attrs["stage_history"] = json.dumps(history)
                if self.dataset is not None:
                    self.dataset.attrs["stage_history"] = json.dumps(history)
            except Exception:
                pass

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
