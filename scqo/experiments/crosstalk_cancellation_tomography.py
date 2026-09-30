"""Crosstalk Cancellation Tomography experiment.

Measures quantum state trajectories on the Bloch sphere during microwave crosstalk
cancellation across three conditions:
1. 'noise_only': Probe state evolution driven purely by aggressor microwave leakage.
2. 'cancel_only': Probe state evolution driven purely by active cancellation pulses.
3. 'compensated': Probe state evolution under simultaneous drive and cancellation.

Diagnoses whether residual errors in calibrate mode are coherent (e.g. AC Stark phase shift /
Z rotation) or mixed (incoherent dephasing / heating), and extracts the effective AC Stark shift
per gate to determine necessary virtual Z corrections.
"""

from __future__ import annotations

from typing import Any, ClassVar

import numpy as np
from pydantic import Field, field_validator, model_validator
import xarray as xr

from ..contract import ContractError, DatasetContract
from ._capabilities.qubit_reset import QubitResetParameters
from ._capabilities.state_readout import (
    POPULATION_ALT,
    StateReadoutParameters,
)
from ._sim import stable_seed
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


class CrosstalkCancellationTomographyParameters(
    TargetSelection, AveragingParameters, StateReadoutParameters, QubitResetParameters
):
    """Inputs for Crosstalk Cancellation Tomography."""

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

    target_gate: str = Field(
        "x180",
        description="Target gate repeated on drive qubit and cancel element: x180, y180, x90, y90."
    )

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

    bidirectional: bool = Field(
        False,
        description=(
            "Reproduce the bidirectional crosstalk_compensated_sqrb program context: add the "
            "drive-line cancellation element (aligned, frame-reset, set to drive_init_phase, and "
            "advancing its frame every pulse as it does while the probe idles). It plays no pulse "
            "here - the probe is idle - so this measures the probe-side cancellation inside the "
            "same 4-element program the bidirectional benchmark runs."
        )
    )
    drive_init_phase: float = Field(
        0.0,
        description="Bidirectional only: initial phase in radians of the drive-line cancellation element."
    )
    drive_cancel_amp: float | None = Field(
        None,
        ge=0.0,
        description=(
            "Bidirectional only, diagnostic: if set, the drive-line element PLAYS target_gate at this "
            "amplitude in every compensated pulse slot (0.0 = a silent play) instead of idling, to test "
            "whether a concurrent play on it disturbs the probe-side cancellation."
        )
    )
    read_drive: bool = Field(
        False,
        description=(
            "Diagnostic: also read the drive qubit in the same shot, multiplexed with the probe "
            "(as the simultaneous-readout bidirectional SQRB does). Its data is not returned."
        )
    )
    stark_frame_rad: float = Field(
        0.0,
        description=(
            "Compensated condition: frame rotation (rad) applied to the probe after every drive "
            "pulse (1/4 of it for a 90-type target_gate) - the virtual Z that crosstalk_compensated_sqrb's "
            "stark_frame_rad applies. Scan it here: the compensated Z residual goes to 0 at the right value."
        )
    )

    repetitions: list[int] | str | int | None = Field(
        None,
        description=(
            "Repetition counts N. Supports explicit list [0, 2, 4], "
            "slice string '0:50:2', single int, or None (derived from min/max/step)."
        ),
    )

    @field_validator("repetitions", mode="before")
    @classmethod
    def _parse_repetitions(cls, val: Any) -> Any:
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
        elif isinstance(val, (int, float)):
            return [int(val)]
        return val

    min_repetitions: int = Field(
        0,
        ge=0,
        description="Minimum pulse repetition count N."
    )
    max_repetitions: int = Field(
        20,
        gt=0,
        description="Maximum pulse repetition count N."
    )
    step_repetitions: int = Field(
        1,
        gt=0,
        description="Step increment for repetitions."
    )
    batch_size: int = Field(
        4,
        gt=0,
        description="Number of repetition points per OPX hardware execution batch."
    )

    conditions: list[str] | None = Field(
        None,
        description="Conditions to compare. Defaults to ['noise_only', 'cancel_only', 'compensated']."
    )
    bases: list[str] | None = Field(
        None,
        description="Tomography measurement bases. Defaults to ['z', 'x', 'y']."
    )

    def get_repetitions(self) -> np.ndarray:
        """Generate repetitions array based on explicit list or min/max/step."""
        if self.repetitions is not None and len(self.repetitions) > 0:
            return np.asarray(self.repetitions, dtype=int)
        return np.arange(self.min_repetitions, self.max_repetitions + 1, self.step_repetitions, dtype=int)

    def get_conditions(self) -> list[str]:
        """Get benchmark conditions."""
        if self.conditions is not None and len(self.conditions) > 0:
            return list(self.conditions)
        return ["noise_only", "cancel_only", "compensated"]

    def get_bases(self) -> list[str]:
        """Get measurement bases."""
        if self.bases is not None and len(self.bases) > 0:
            return [b.lower() for b in self.bases]
        return ["z", "x", "y"]

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
    def _ensure_targets(self) -> CrosstalkCancellationTomographyParameters:
        if not self.targets:
            self.targets = [self.probe_qubit, self.drive_qubit]
        return self


class CrosstalkCancellationTomographyContract(DatasetContract):
    """Dataset contract for CrosstalkCancellationTomography."""

    def validate(self, ds: xr.Dataset) -> None:
        required_coords = {"target", "condition", "repetitions", "basis"}
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


class CrosstalkCancellationTomographyResult(Result):
    """Output of CrosstalkCancellationTomography."""

    fit: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Extracted tomography quantities (Bloch trajectories, purity, AC Stark shift)."
    )


@register
class CrosstalkCancellationTomography(Experiment):
    """Crosstalk Cancellation Tomography Experiment."""

    name: ClassVar[str] = "crosstalk_cancellation_tomography"
    description: ClassVar[str] = (
        "Measures quantum state trajectories on the Bloch sphere during crosstalk cancellation, "
        "comparing noise_only, cancel_only, and compensated conditions to diagnose AC Stark shift."
    )
    Parameters: ClassVar[type] = CrosstalkCancellationTomographyParameters
    Result: ClassVar[type] = CrosstalkCancellationTomographyResult

    Contract: ClassVar[DatasetContract] = CrosstalkCancellationTomographyContract(
        sweeps=("condition", "repetitions", "basis"),
        sweep_units=("", "", ""),
        variables=("I", "Q"),
        alt_variables=(*POPULATION_ALT, ("I",)),
    )

    params: CrosstalkCancellationTomographyParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            "condition": np.asarray(self.params.get_conditions(), dtype=str),
            "repetitions": self.params.get_repetitions(),
            "basis": np.asarray(self.params.get_bases(), dtype=str),
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        probe = self.params.probe_qubit
        rng = np.random.default_rng(stable_seed("crosstalk_cancellation_tomography", probe))

        conditions = coords["condition"]
        reps = coords["repetitions"]
        bases = coords["basis"]
        n_targets = max(1, len(self.params.targets))

        # Model parameters
        opt_amp = 0.035
        opt_phi = 0.85
        actual_amp = float(self.params.cancel_amp)
        actual_phi = float(self.params.init_phase)

        # Crosstalk leakage vector per pulse
        # leakage drives along axis phi_leak = opt_phi
        omega_leak = 0.05
        # Cancellation drive vector per pulse
        omega_cancel = omega_leak * (actual_amp / opt_amp if opt_amp > 0 else 1.0)
        phi_cancel = actual_phi

        pop_data = np.zeros((len(conditions), len(reps), len(bases)), dtype=float)

        for c_idx, cond in enumerate(conditions):
            for r_idx, n in enumerate(reps):
                if cond == "noise_only":
                    # Rotation around phi_leak axis in XY plane
                    theta = omega_leak * n
                    rx = np.sin(theta) * np.sin(opt_phi)
                    ry = -np.sin(theta) * np.cos(opt_phi)
                    rz = np.cos(theta)
                elif cond == "cancel_only":
                    # Rotation around phi_cancel axis
                    theta = omega_cancel * n
                    rx = -np.sin(theta) * np.sin(phi_cancel)
                    ry = np.sin(theta) * np.cos(phi_cancel)
                    rz = np.cos(theta)
                else:
                    # Compensated: residual vector + AC Stark shift (Z precession)
                    dx = omega_leak * np.sin(opt_phi) - omega_cancel * np.sin(phi_cancel)
                    dy = -omega_leak * np.cos(opt_phi) + omega_cancel * np.cos(phi_cancel)
                    stark_phi = 0.06 * n
                    theta_res = np.sqrt(dx**2 + dy**2) * n
                    rx = theta_res * np.cos(stark_phi)
                    ry = theta_res * np.sin(stark_phi)
                    rz = np.sqrt(max(0.0, 1.0 - rx**2 - ry**2))

                # Tomography measurement:
                # basis 'z': P1 = (1 - rz) / 2
                # basis 'x': P1 = (1 - rx) / 2
                # basis 'y': P1 = (1 - ry) / 2
                for b_idx, b in enumerate(bases):
                    b_str = str(b).lower()
                    if b_str == "z":
                        p1 = 0.5 * (1.0 - rz)
                    elif b_str == "x":
                        p1 = 0.5 * (1.0 - rx)
                    elif b_str == "y":
                        p1 = 0.5 * (1.0 - ry)
                    else:
                        p1 = 0.5
                    p1_noisy = np.clip(p1 + rng.normal(0, 0.005), 0.0, 1.0)
                    pop_data[c_idx, r_idx, b_idx] = p1_noisy

        pop_t = np.repeat(pop_data[None, ...], n_targets, axis=0)
        if self.params.use_state_discrimination:
            return {"state": (("target", "condition", "repetitions", "basis"), pop_t)}
        return {
            "I": (("target", "condition", "repetitions", "basis"), pop_t),
            "Q": (("target", "condition", "repetitions", "basis"), np.zeros_like(pop_t)),
        }

    def estimate(self) -> CrosstalkCancellationTomographyResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.crosstalk_cancellation_tomography import (
            CrosstalkCancellationTomographyEstimator,
        )

        probe = self.params.probe_qubit
        if "qubit" in self.dataset.coords and probe in self.dataset.coords["qubit"].values:
            ds_probe = self.dataset.sel(qubit=probe)
        elif "target" in self.dataset.coords and probe in self.dataset.coords["target"].values:
            ds_probe = self.dataset.sel(target=probe)
        else:
            ds_probe = self.dataset


        estimator = CrosstalkCancellationTomographyEstimator()
        out_dir = str(self.artifact_dir) if self.artifact_dir is not None else None
        # The run's settings turn the rotation-vector fit into absolute suggestions
        fit_res, _figures = estimator.analyze(
            ds_probe,
            output_dir=out_dir,
            cancel_amp=float(self.params.cancel_amp),
            init_phase=float(self.params.init_phase),
        )

        result = CrosstalkCancellationTomographyResult()
        result.fit[probe] = fit_res
        result.outcomes[probe] = (
            Outcome.SUCCESSFUL if fit_res.get("success", False) else Outcome.FAILED
        )
        return result

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")

