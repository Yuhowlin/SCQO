"""Crosstalk Compensated Benchmark experiment.

Evaluates and mitigates microwave crosstalk during deterministic gate operations
on a probe qubit when an aggressor drive qubit is driven concurrently.

Two modes are supported:
1. 'calibrate':
   Probe qubit remains idle in |0>. Drive qubit executes N = cal_repetitions
   repetitions of target_gate (e.g. x180). Cancel element concurrently plays
   the compensation pulses at probe frequency. Leakage from drive excites
   probe qubit; optimal (cancel_amp, init_phase) minimizes probe excitation
   back to |0> (2D bowl minimum).

2. 'benchmark':
   Probe qubit concurrently executes N repetitions of probe_gate (e.g. x180)
   while drive qubit executes N repetitions of target_gate. We sweep N over
   three conditions:
     - isolated: probe qubit runs alone (drive idle)
     - simultaneous: probe and drive run concurrently without compensation
     - compensated: probe and drive run concurrently with active compensation
   Error accumulation vs N is compared across the three conditions.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

import numpy as np
from pydantic import Field, model_validator
import xarray as xr

from .._scqat import per_qubit_results
from ..contract import ContractError, DatasetContract
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


class CrosstalkCompensatedBenchmarkParameters(
    TargetSelection, AveragingParameters, StateReadoutParameters, QubitResetParameters
):
    """Inputs for Crosstalk Compensated Benchmark."""

    probe_qubit: str = Field(
        "q1",
        description="Target qubit under measurement (e.g. 'q1')."
    )
    drive_qubit: str = Field(
        "q2",
        description="Interfering aggressor qubit running concurrent gates (e.g. 'q2')."
    )
    targets: list[str] = Field(
        default_factory=list,
        description="Target qubits [probe_qubit, drive_qubit]. Auto-derived if omitted."
    )

    # Gate definitions
    target_gate: str = Field(
        "x180",
        description="Target gate repeated on drive qubit and cancel element: x180, y180, x90, y90."
    )
    probe_gate: str = Field(
        "x180",
        description="Gate repeated on probe qubit during benchmark mode."
    )

    # Calibration repetitions
    min_cal_repetitions: int = Field(
        10,
        gt=0,
        description="Minimum pulse repetition count for calibrate mode."
    )
    max_cal_repetitions: int = Field(
        30,
        gt=0,
        description="Maximum pulse repetition count for calibrate mode."
    )
    num_cal_repetitions: int = Field(
        5,
        gt=0,
        description="Number of repetition points to average over in calibrate mode."
    )
    cal_repetitions: int | str | list[int] | None = Field(
        None,
        description=(
            "Repetition counts for calibrate mode. Supports:\n"
            "1. None: generated from min_cal_repetitions, max_cal_repetitions, num_cal_repetitions.\n"
            "2. Slice string: e.g. '10:30:5' -> [10, 15, 20, 25, 30].\n"
            "3. Single int: e.g. 20 -> 5 points centered around 20.\n"
            "4. List of ints: e.g. [10, 15, 20, 25, 30]."
        )
    )

    def get_cal_repetitions(self) -> list[int]:
        """Generate calibration repetition counts based on slice string, single int, list, or min/max/num."""
        if self.cal_repetitions is not None:
            if isinstance(self.cal_repetitions, str):
                parts = self.cal_repetitions.strip().split(":")
                if len(parts) == 1:
                    val = int(parts[0])
                    reps = [max(2, int(round(val * f))) for f in (0.5, 0.75, 1.0, 1.25, 1.5)]
                    return sorted(set(reps))
                elif len(parts) == 2:
                    start, stop = int(parts[0]), int(parts[1])
                    return list(range(start, stop + 1, max(1, (stop - start) // 4)))
                elif len(parts) >= 3:
                    start, stop, step = int(parts[0]), int(parts[1]), int(parts[2])
                    return list(range(start, stop + 1, step))
            elif isinstance(self.cal_repetitions, (int, float)):
                val = int(self.cal_repetitions)
                reps = [max(2, int(round(val * f))) for f in (0.5, 0.75, 1.0, 1.25, 1.5)]
                return sorted(set(reps))
            elif isinstance(self.cal_repetitions, (list, tuple, np.ndarray)):
                return sorted(set(int(x) for x in self.cal_repetitions))

        pts = np.linspace(self.min_cal_repetitions, self.max_cal_repetitions, self.num_cal_repetitions, dtype=int)
        return sorted(set(int(x) for x in pts))

    # Benchmark repetition sweep
    repetitions: list[int] | None = Field(
        None,
        description="Explicit list of repetition counts N for benchmark mode."
    )
    max_repetitions: int = Field(
        100,
        gt=0,
        description="Maximum repetition count N when repetitions is not provided."
    )
    min_repetitions: int = Field(
        2,
        ge=0,
        description="Minimum repetition count N for benchmark sweep (defaults to 2)."
    )
    num_repetitions: int = Field(
        8,
        gt=1,
        description="Number of repetition points to sample in benchmark mode."
    )
    log_scale: bool = Field(
        True,
        description="Whether to distribute repetitions logarithmically between min and max."
    )
    round_to_even: bool = Field(
        True,
        description="Whether to round repetition points to nearest even integers for parity conservation."
    )
    step_repetitions: int = Field(
        4,
        gt=0,
        description="Step increment when log_scale is False."
    )
    benchmark_batch_size: int = Field(
        1,
        gt=0,
        description="Number of repetition points per execution batch to prevent OPX memory overload."
    )

    def get_repetitions(self) -> np.ndarray:
        """Generate repetitions array based on explicit list or log/linear scale."""
        if self.repetitions is not None and len(self.repetitions) > 0:
            return np.asarray(self.repetitions, dtype=int)

        if not self.log_scale:
            reps = list(range(self.min_repetitions, self.max_repetitions + 1, self.step_repetitions))
            if not reps or reps[-1] != self.max_repetitions:
                reps.append(self.max_repetitions)
            return np.array(sorted(set(reps)), dtype=int)

        min_val = max(1, self.min_repetitions)
        max_val = self.max_repetitions
        if min_val >= max_val:
            reps = [max_val]
        else:
            raw_pts = np.geomspace(min_val, max_val, self.num_repetitions)
            if self.round_to_even:
                pts = [max(2, int(round(x / 2.0) * 2)) for x in raw_pts]
                if max_val % 2 == 0:
                    pts[-1] = int(max_val)
                else:
                    pts[-1] = int(round(max_val / 2.0) * 2)
            else:
                pts = [max(1, int(round(x))) for x in raw_pts]
                pts[-1] = int(max_val)
            reps = sorted(set(pts))

        if self.min_repetitions == 0 and (not reps or reps[0] != 0):
            reps = [0] + reps
        return np.array(reps, dtype=int)

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
            "Phase evolution rate per pulse in radians (Delta phi). "
            "If None, computed automatically from theoretical detuning (f_d - f_p) and pulse duration."
        )
    )
    pulse_duration_ns: int = Field(
        16,
        gt=0,
        description="Duration of each pulse in ns for theoretical phase rate calculation."
    )

    # Benchmark alternating pulse options
    alternate_probe: bool = Field(
        False,
        description="Whether to alternate probe gate signs (+probe, -probe...) in benchmark mode.",
    )
    alternate_target: bool = Field(
        False,
        description=(
            "Whether to alternate target (drive) gate signs (+target, -target...) in benchmark mode. "
            "The cancel element automatically tracks the target gate's sign."
        ),
    )

    # Execution mode
    mode: Literal["calibrate", "benchmark"] = Field(
        "calibrate",
        description=(
            "'calibrate' runs multi-stage zoom-in sweeps of cancel_amp and init_phase with probe idle in |0>. "
            "'benchmark' sweeps repetitions N comparing isolated, simultaneous, and compensated conditions."
        )
    )

    # Multi-stage zoom-in calibration parameters
    cal_stage_repetitions: list[int] | str | int | None = Field(
        None,
        description=(
            "Repetition counts for each zoom-in stage in calibrate mode. "
            "E.g. [2, 8, 24] or '2,8,24'. Defaults to [2, 8, 24]."
        )
    )
    zoom_factor: float = Field(
        3.0,
        gt=1.0,
        description="Range contraction factor per zoom-in stage in calibrate mode (span /= zoom_factor)."
    )

    def get_cal_stage_repetitions(self) -> list[int]:
        """Generate list of gate counts for each zoom-in calibration stage."""
        val = self.cal_stage_repetitions if self.cal_stage_repetitions is not None else self.cal_repetitions
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
        return [2, 8, 24]

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

    @model_validator(mode="before")
    @classmethod
    def _resolve_targets_and_qubits(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Map benchmark parameter aliases
            if "max_repeat" in data:
                data["max_repetitions"] = data.pop("max_repeat")
            if "min_repeat" in data:
                data["min_repetitions"] = data.pop("min_repeat")
            if "num_repeat" in data:
                data["num_repetitions"] = data.pop("num_repeat")
            if "num_points" in data:
                data["num_repetitions"] = data.pop("num_points")
            if "batch_size" in data:
                data["benchmark_batch_size"] = data.pop("batch_size")
            if "alter_probe" in data:
                data["alternate_probe"] = data.pop("alter_probe")
            if "alternating_probe" in data:
                data["alternate_probe"] = data.pop("alternating_probe")
            if "alter_target" in data:
                data["alternate_target"] = data.pop("alter_target")
            if "alternating_target" in data:
                data["alternate_target"] = data.pop("alternating_target")
            if "alter_drive" in data:
                data["alternate_target"] = data.pop("alter_drive")
            if "alternating_drive" in data:
                data["alternate_target"] = data.pop("alternating_drive")
            if "alternating" in data:
                val = data.pop("alternating")
                data.setdefault("alternate_probe", val)
                data.setdefault("alternate_target", val)
            if "alter" in data:
                val = data.pop("alter")
                data.setdefault("alternate_probe", val)
                data.setdefault("alternate_target", val)

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
    def _ensure_targets(self) -> CrosstalkCompensatedBenchmarkParameters:
        if not self.targets:
            self.targets = [self.probe_qubit, self.drive_qubit]
        return self


class CrosstalkCompensatedBenchmarkContract(DatasetContract):
    """Dataset contract for CrosstalkCompensatedBenchmark supporting calibrate and benchmark modes."""

    def validate(self, ds: xr.Dataset) -> None:
        if "cancel_amp" in ds.coords and "init_phase" in ds.coords:
            required_coords = {"target", "cancel_amp", "init_phase"}
        else:
            required_coords = {"target", "condition", "repetitions"}

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


class CrosstalkCompensatedBenchmarkResult(Result):
    """Output of CrosstalkCompensatedBenchmark."""

    fit: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Extracted quantities (optimal cancel_amp, init_phase, error reduction)."
    )


@register
class CrosstalkCompensatedBenchmark(Experiment):
    """Crosstalk Compensated Deterministic Benchmark Experiment."""

    name: ClassVar[str] = "crosstalk_compensated_benchmark"
    description: ClassVar[str] = (
        "Deterministic Gate Benchmarking for active crosstalk cancellation, supporting "
        "iterative multi-stage zoom-in calibration with idle probe and benchmark comparison."
    )
    Parameters: ClassVar[type] = CrosstalkCompensatedBenchmarkParameters
    Result: ClassVar[type] = CrosstalkCompensatedBenchmarkResult

    Contract: ClassVar[DatasetContract] = CrosstalkCompensatedBenchmarkContract(
        sweeps=("condition", "repetitions"),
        sweep_units=("", ""),
        variables=("I", "Q"),
        alt_variables=(*POPULATION_ALT, ("I",)),
    )

    params: CrosstalkCompensatedBenchmarkParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        if self.params.mode == "calibrate":
            return {
                "cancel_amp": self.params.get_cancel_amps(),
                "init_phase": self.params.get_init_phases(),
            }
        return {
            "condition": np.array(["isolated", "simultaneous", "compensated"]),
            "repetitions": self.params.get_repetitions(),
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        probe = self.params.probe_qubit
        rng = np.random.default_rng(stable_seed("crosstalk_compensated_benchmark", probe))

        opt_amp = 0.035
        opt_phi = 0.85
        actual_amp = float(self.params.cancel_amp)
        actual_phi = float(self.params.init_phase)

        n_targets = max(1, len(self.params.targets))

        if self.params.mode == "calibrate":
            amps = coords["cancel_amp"]
            phases = coords["init_phase"]
            stage_reps = self.params.get_cal_stage_repetitions()

            # Simulate cancellation response across stage gate repetitions
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

            # Broadcast across targets: shape (n_targets, len(amps), len(phases))
            p1_t = np.repeat(p1_sim[None, ...], n_targets, axis=0)

            if self.params.use_state_discrimination:
                return {"state": (("target", "cancel_amp", "init_phase"), p1_t)}
            return {
                "I": (("target", "cancel_amp", "init_phase"), p1_t),
                "Q": (("target", "cancel_amp", "init_phase"), np.zeros_like(p1_t)),
            }

        else:
            # Benchmark mode: sweep repetitions across conditions
            conds = coords["condition"]
            reps = coords["repetitions"]

            # Quality of compensation: 1.0 = perfect, 0.0 = completely off
            dist_sq = (actual_amp - opt_amp) ** 2 + 0.001 * (actual_phi - opt_phi) ** 2
            comp_factor = float(np.exp(-dist_sq / 0.002))

            # Isolated baseline: pi-pulse error accumulation (e.g. 0.004 per gate)
            # If alternate_probe is True, systematic over-rotation is canceled across pairs
            eps_iso = 0.0005 if self.params.alternate_probe else 0.004
            eps_sim = 0.025  # Crosstalk introduces extra rotation / detuning error
            eps_comp = eps_sim - (eps_sim - eps_iso) * comp_factor

            pop_list = []
            for cond in conds:
                if cond == "isolated":
                    eps = eps_iso
                elif cond == "simultaneous":
                    eps = eps_sim
                else:
                    eps = eps_comp

                # For even N of x180, state should be |0>; error per gate accumulates
                p1_curve = 0.5 * (1.0 - (1.0 - 2.0 * eps) ** reps)
                p1_noisy = np.clip(p1_curve + rng.normal(0, 0.01, len(reps)), 0.0, 1.0)
                pop_list.append(p1_noisy)

            pop_arr = np.array(pop_list)  # (3, len(reps))
            pop_t = np.repeat(pop_arr[None, ...], n_targets, axis=0)

            if self.params.use_state_discrimination:
                return {"state": (("target", "condition", "repetitions"), pop_t)}
            return {
                "I": (("target", "condition", "repetitions"), pop_t),
                "Q": (("target", "condition", "repetitions"), np.zeros_like(pop_t)),
            }

    def estimate(self) -> CrosstalkCompensatedBenchmarkResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.crosstalk_compensated_benchmark import (
            CrosstalkCompensatedBenchmarkEstimator,
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

        estimator = CrosstalkCompensatedBenchmarkEstimator()
        out_dir = str(self.artifact_dir) if self.artifact_dir is not None else None
        fit_res, _figures = estimator.analyze(ds_probe, output_dir=out_dir)

        result = CrosstalkCompensatedBenchmarkResult()
        result.fit[probe] = fit_res
        result.outcomes[probe] = (
            Outcome.SUCCESSFUL if fit_res.get("success", False) else Outcome.FAILED
        )
        return result

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
