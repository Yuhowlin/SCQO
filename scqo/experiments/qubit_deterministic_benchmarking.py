"""Qubit Deterministic Benchmarking experiment.

Applies repeated sequences of a specified target gate (x180, y180, x90, y90, -x90, -y90)
across repetition counts N and amplitude scaling factors to measure rotation error accumulation
and calibrate gate pulse amplitudes.
"""

from __future__ import annotations

from typing import ClassVar, Dict, Any, Optional, List

import numpy as np
import matplotlib.pyplot as plt
from scipy.optimize import curve_fit
from pydantic import Field

from .._scqat import per_qubit_results
from ..contract import DatasetContract
from ._capabilities.qubit_reset import QubitResetParameters
from ._capabilities.state_readout import (
    STATE_ALT,
    StateReadoutParameters,
    readout_vars,
    state_row,
)
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register


def damped_cosine_zero_phase(N: np.ndarray, A: float, gamma: float, omega: float, C: float) -> np.ndarray:
    """Damped cosine model with fixed zero initial phase (phi_0 = 0)."""
    return A * np.exp(-gamma * N) * np.cos(omega * N) + C


class QubitDeterministicBenchmarkingParameters(
    TargetSelection, AveragingParameters, StateReadoutParameters, QubitResetParameters
):
    """Inputs for Qubit Deterministic Benchmarking."""

    target_gate: str = Field(
        "x180",
        description="Target gate to benchmark: x180, y180, x90, y90, -x90, -y90.",
    )
    max_repetitions: int = Field(
        100,
        gt=0,
        description="Maximum repetition count N.",
    )
    step: Optional[int] = Field(
        None,
        gt=0,
        description="Repetition increment step (defaults to 2 for pi gates, 4 for pi/2 gates).",
    )
    repetitions: Optional[List[int]] = Field(
        None,
        description="Optional explicit array of repetition counts N.",
    )
    use_state_discrimination: bool = Field(
        True,
        description="Default True: use FPGA state discrimination to measure P1/P0.",
    )
    min_amp_factor: float = Field(
        0.9,
        description="Minimum amplitude scaling factor for sweep.",
    )
    max_amp_factor: float = Field(
        1.1,
        description="Maximum amplitude scaling factor for sweep.",
    )
    num_amp_points: int = Field(
        1,
        gt=0,
        description="Number of amplitude scale points (set to 1 for single-amplitude sweep).",
    )
    amp_factors: Optional[List[float]] = Field(
        None,
        description="Optional explicit list of amplitude scale factors.",
    )

    def get_repetitions(self) -> list[int]:
        if self.repetitions is not None:
            return [int(x) for x in self.repetitions]
        tg = str(self.target_gate).strip().lower()
        if self.step is not None:
            st = self.step
        elif tg in ("x90", "y90", "-x90", "-y90", "pi_half"):
            st = 4
        else:
            st = 2
        return list(range(0, self.max_repetitions + 1, st))

    def get_amp_factors(self) -> list[float]:
        if self.amp_factors is not None:
            return [float(x) for x in self.amp_factors]
        if self.num_amp_points <= 1:
            return [1.0]
        return [float(x) for x in np.linspace(self.min_amp_factor, self.max_amp_factor, self.num_amp_points)]


class QubitDeterministicBenchmarkingResult(Result):
    """Result of QubitDeterministicBenchmarking."""
    pass


@register
class QubitDeterministicBenchmarking(Experiment):
    """Deterministic Benchmarking for single qubit gates."""

    name: ClassVar[str] = "qubit_deterministic_benchmarking"
    description: ClassVar[str] = (
        "Deterministic Benchmarking by repeatedly applying a target gate across amplitudes to measure overrotation/underrotation error accumulation."
    )
    Parameters: ClassVar[type[QubitDeterministicBenchmarkingParameters]] = QubitDeterministicBenchmarkingParameters
    Result: ClassVar[type[QubitDeterministicBenchmarkingResult]] = QubitDeterministicBenchmarkingResult

    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("amp_factor", "repetitions"),
        sweep_units=("", ""),
        variables=("I", "Q"),
        alt_variables=(*STATE_ALT, ("I",)),
    )

    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout")

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            "amp_factor": np.array(self.params.get_amp_factors(), dtype=float),
            "repetitions": np.array(self.params.get_repetitions(), dtype=int),
        }

    def estimate(self) -> QubitDeterministicBenchmarkingResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        data = self.dataset
        result = QubitDeterministicBenchmarkingResult()
        qubits = list(self.params.targets)
        reps = np.asarray(data.coords["repetitions"].values, dtype=float)
        amp_factors = np.asarray(data.coords["amp_factor"].values, dtype=float)
        num_amps = len(amp_factors)

        for i_q, qubit in enumerate(qubits):
            var_state = f"state{i_q + 1}" if f"state{i_q + 1}" in data.data_vars else ("state" if "state" in data.data_vars else None)
            var_i = f"I{i_q + 1}" if f"I{i_q + 1}" in data.data_vars else ("I" if "I" in data.data_vars else None)
            var_q = f"Q{i_q + 1}" if f"Q{i_q + 1}" in data.data_vars else ("Q" if "Q" in data.data_vars else None)

            # Helper to safely select target and transpose to (amp_factor, repetitions)
            def _extract_2d(var_name: str) -> np.ndarray:
                da = data[var_name]
                if "target" in da.dims:
                    da = da.sel(target=qubit)
                elif "qubit" in da.dims:
                    da = da.sel(qubit=qubit)

                if "amp_factor" in da.dims and "repetitions" in da.dims:
                    da = da.transpose("amp_factor", "repetitions")
                elif "repetitions" in da.dims and "amp_factor" not in da.dims:
                    da = da.expand_dims("amp_factor", axis=0)

                arr = np.asarray(da.values, dtype=float)
                arr = np.squeeze(arr)
                if arr.ndim == 1:
                    arr = arr.reshape((1, -1))
                return arr

            if var_state and var_state in data:
                raw_p1 = _extract_2d(var_state)  # P1 in [0, 1]
                # P0 population: P0 = 1 - P1 (scale 0.0 to 1.0)
                pz_data = 1.0 - raw_p1
                ylabel = "Ground State Population P0"
                unit_str = "P0"
            elif var_i and var_q and var_i in data and var_q in data:
                arr_i = _extract_2d(var_i)
                arr_q = _extract_2d(var_q)
                raw_sig = np.hypot(arr_i, arr_q)
                s_min, s_max = np.min(raw_sig), np.max(raw_sig)
                denom = (s_max - s_min) if (s_max - s_min) > 1e-12 else 1.0
                pz_data = (raw_sig - s_min) / denom
                ylabel = "Normalized Signal P0 (0.0 to 1.0)"
                unit_str = "P0"
            elif var_i and var_i in data:
                raw_sig = _extract_2d(var_i)
                s_min, s_max = np.min(raw_sig), np.max(raw_sig)
                denom = (s_max - s_min) if (s_max - s_min) > 1e-12 else 1.0
                pz_data = (raw_sig - s_min) / denom
                ylabel = "Normalized Voltage I [V] (0.0 to 1.0)"
                unit_str = "P0"
            else:
                pz_data = np.ones((num_amps, len(reps)))
                ylabel = "P0"
                unit_str = "P0"

            # Fit damped cosine with zero initial phase to each amplitude trajectory (in 0.0 to 1.0 scale)
            omegas = []
            signed_omegas = []
            gammas = []

            fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5), dpi=150)
            colors = plt.cm.viridis(np.linspace(0.15, 0.85, num_amps))

            for i_a, a_val in enumerate(amp_factors):
                pz_curve = pz_data[i_a]
                ax1.plot(reps, pz_curve, "o", color=colors[i_a], label=f"amp factor = {a_val:.3f}")

                # Initial guess for P0 scale [0, 1]: A=0.45, gamma=0.01, omega=0.05, C=0.5
                p0_guess = [0.45, 0.01, 0.05, 0.5]
                try:
                    popt, _ = curve_fit(
                        damped_cosine_zero_phase,
                        reps,
                        pz_curve,
                        p0=p0_guess,
                        bounds=([0.0, 0.0, 0.0, 0.0], [0.6, 0.5, np.pi, 1.0]),
                        maxfev=2000,
                    )
                    A_fit, gamma_fit, omega_fit, C_fit = popt
                    reps_fine = np.linspace(reps.min(), reps.max(), 200)
                    curve_fine = damped_cosine_zero_phase(reps_fine, *popt)
                    ax1.plot(reps_fine, curve_fine, "-", color=colors[i_a], alpha=0.8)

                    w_val = float(omega_fit)
                    # Sign convention: positive for a > 1.0 (overrotation), negative for a < 1.0 (underrotation)
                    s_w = w_val if a_val >= 1.0 else -w_val
                    omegas.append(w_val)
                    signed_omegas.append(s_w)
                    gammas.append(float(gamma_fit))
                except Exception:
                    omegas.append(0.0)
                    signed_omegas.append(0.0)
                    gammas.append(0.0)

            # Estimate optimal amplitude scaling factor a_opt where omega(a_opt) = 0
            if num_amps > 1 and len(set(amp_factors)) > 1:
                try:
                    poly = np.polyfit(amp_factors, signed_omegas, 1)
                    k_slope, b_intercept = poly[0], poly[1]
                    if abs(k_slope) > 1e-6:
                        a_opt = float(-b_intercept / k_slope)
                    else:
                        a_opt = float(amp_factors[np.argmin(omegas)])
                except Exception:
                    a_opt = 1.0
            else:
                a_opt = 1.0

            a_opt = float(np.clip(a_opt, 0.5, 1.5))

            ax1.set_xlabel("Number of Gate Repetitions N")
            ax1.set_ylabel(ylabel)
            ax1.set_ylim(-0.05, 1.05)
            ax1.set_title(f"DB Trajectories: {qubit} - {self.params.target_gate}")
            ax1.grid(True, alpha=0.3)
            ax1.legend(loc="best", fontsize="small")

            # Plot 2: Oscillation Frequency omega vs Amplitude Scale Factor a
            ax2.plot(amp_factors, omegas, "o", color="#1f77b4", markersize=7, label="Measured \u03c9")
            if num_amps > 1 and len(set(amp_factors)) > 1:
                a_fine = np.linspace(min(amp_factors.min(), a_opt - 0.02), max(amp_factors.max(), a_opt + 0.02), 100)
                try:
                    poly = np.polyfit(amp_factors, signed_omegas, 1)
                    fit_w_fine = np.abs(poly[0] * a_fine + poly[1])
                    ax2.plot(a_fine, fit_w_fine, "--", color="gray", alpha=0.7, label="Linear Fit |\u03a9(a)|")
                except Exception:
                    pass

            ax2.axvline(a_opt, color="crimson", linestyle=":", label=f"Opt Factor ({a_opt:.4f})")
            ax2.axhline(0.0, color="black", linestyle="-", alpha=0.3)
            ax2.plot(a_opt, 0.0, "*", color="crimson", markersize=12, label=f"Optimum a_opt={a_opt:.4f}")
            ax2.set_xlabel("Amplitude Scaling Factor a")
            ax2.set_ylabel("Oscillation Frequency \u03c9 [rad / gate count]")
            ax2.set_title(f"Frequency vs Amp Factor\nOptimal Scale Factor a_opt = {a_opt:.4f}")
            ax2.grid(True, alpha=0.3)
            ax2.legend(loc="best", fontsize="small")

            fig.tight_layout()

            # Retrieve current amplitude
            tg = str(self.params.target_gate).strip().lower()
            try:
                from customized import quam_fields
                q_obj = self.backend.machine.qubits[qubit]
                if tg in ("x90", "-x90", "y90", "-y90"):
                    old_amp = quam_fields.get_pi_amp(q_obj, operation="x90")
                else:
                    old_amp = quam_fields.get_pi_amp(q_obj, operation="x180")
            except Exception:
                chan = self.device.channel(qubit, "drive")
                old_amp = float(chan.pi_amp / 2.0) if tg in ("x90", "-x90", "y90", "-y90") else float(chan.pi_amp)

            opt_amp = float(old_amp * a_opt)

            fit_dict = {
                "opt_factor": a_opt,
                "opt_amp": opt_amp,
                "old_amp": old_amp,
                "pi_amp_factor": a_opt,
                "repetitions": [int(x) for x in reps],
                "amp_factors": [float(x) for x in amp_factors],
                "omegas": omegas,
                "gammas": gammas,
                "unit": unit_str,
            }

            if tg in ("x90", "-x90"):
                fit_dict["pi_amp_x90"] = opt_amp
                fit_dict["old_pi_amp_x90"] = old_amp
            else:
                fit_dict["pi_amp"] = opt_amp
                fit_dict["old_pi_amp"] = old_amp

            result.fit[qubit] = fit_dict
            result.outcomes[qubit] = Outcome.SUCCESSFUL

            # Save figure artifact if artifact_dir is configured
            if self.artifact_dir is not None:
                try:
                    out_q_dir = self.artifact_dir / str(qubit)
                    out_q_dir.mkdir(parents=True, exist_ok=True)
                    fig.savefig(out_q_dir / "qubit_deterministic_benchmarking.png")
                except Exception:
                    pass
                finally:
                    plt.close(fig)
            else:
                plt.close(fig)

        return result

    def update(self) -> None:
        """Update pulse amplitude via self.device.channel so SCQO SuggestionCapture generates CLI prompts."""
        if self.result is None:
            return
        tg = str(self.params.target_gate).strip().lower()

        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is Outcome.SUCCESSFUL and fit.get("opt_amp") is not None:
                opt_amp = fit["opt_amp"]
                chan = self.device.channel(qubit, "drive")
                if tg in ("x180", "x", "pi", "y180", "y"):
                    chan.pi_amp = opt_amp
                elif tg in ("x90", "-x90", "y90", "-y90"):
                    chan.pi_amp_x90 = opt_amp





    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
