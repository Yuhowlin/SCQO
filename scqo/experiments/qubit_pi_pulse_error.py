"""Pi-pulse amplitude error amplification experiment (qubit_pi_pulse_error).

Sweeps drive amplitude factor (e.g. 0.90 to 1.10) across repeated odd gate counts
(X^1, X^3, X^5, X^7, X^9, X^11...) to amplify and fit small pi-pulse rotation errors.
"""

from __future__ import annotations

from typing import ClassVar, Dict, Any, List

import numpy as np
import xarray as xr
from pydantic import Field

from scqo.contract import DatasetContract
from scqo.experiment import Experiment
from scqo.result import Outcome, Result
from scqo.parameters import AveragingParameters, QubitSelection
from scqo.experiments._sim import stable_seed


class QubitPiPulseErrorParameters(QubitSelection, AveragingParameters):
    """Inputs for pi-pulse error amplification calibration."""

    min_amp_factor: float = Field(0.90, ge=0, description="Minimum amplitude factor relative to current pi_amp.")
    max_amp_factor: float = Field(1.10, gt=0, description="Maximum amplitude factor relative to current pi_amp.")
    num_amp_points: int = Field(41, gt=1, description="Number of amplitude factor sweep points.")
    gate_counts: List[int] = Field(
        default_factory=lambda: [1, 3, 5, 7, 9, 11],
        description="List of odd gate counts (repetitions of X180).",
    )


class QubitPiPulseErrorResult(Result):
    """Fitted optimal pi-pulse amplitude factor."""


class QubitPiPulseError(Experiment):
    """Calibrate pi-pulse amplitude via error amplification across repeated X180 gates."""

    name: ClassVar[str] = "qubit_pi_pulse_error"
    description: ClassVar[str] = (
        "Sweep pi-pulse amplitude factor across repeated X180 gate sequences (X^1, X^3, X^5...) "
        "to amplify and precisely calibrate the pi pulse amplitude."
    )
    Parameters: ClassVar[type] = QubitPiPulseErrorParameters
    Result: ClassVar[type] = QubitPiPulseErrorResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("gate_count", "amp_factor"),
        sweep_units=("", "dimensionless"),
        variables=("I", "Q"),
    )

    params: QubitPiPulseErrorParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            "gate_count": np.array(self.params.gate_counts, dtype=int),
            "amp_factor": np.linspace(
                self.params.min_amp_factor,
                self.params.max_amp_factor,
                self.params.num_amp_points,
            ),
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        gate_counts = coords["gate_count"]
        amp_factors = coords["amp_factor"]
        qubits = self.params.qubits

        n_qubits = len(qubits)
        n_gc = len(gate_counts)
        n_amp = len(amp_factors)

        i_data = np.zeros((n_qubits, n_gc, n_amp))
        q_data = np.zeros((n_qubits, n_gc, n_amp))

        rng = np.random.default_rng(stable_seed("qubit_pi_pulse_error", *qubits))
        for k, qubit in enumerate(qubits):
            opt_factor = rng.uniform(0.97, 1.03)
            noise = 0.015
            for i_g, N in enumerate(gate_counts):
                # P_e = sin^2(N * pi * amp_factor / (2 * opt_factor))
                i_data[k, i_g] = np.sin(N * np.pi * amp_factors / (2.0 * opt_factor)) ** 2 + rng.normal(0, noise, n_amp)
                q_data[k, i_g] = rng.normal(0, noise, n_amp)

        return {"I": i_data, "Q": q_data}

    def estimate(self) -> QubitPiPulseErrorResult:
        assert self.dataset is not None
        result = QubitPiPulseErrorResult()

        ds = self.dataset
        amp_factors = ds.coords["amp_factor"].values
        gate_counts = ds.coords["gate_count"].values

        for qubit in self.params.qubits:
            try:
                # Signal for qubit (use I variable)
                if "qubit" in ds.dims:
                    data = ds["I"].sel(qubit=qubit).values
                else:
                    data = ds["I"].values

                # data shape: (len(gate_counts), len(amp_factors))
                # For odd gate counts, peak signal occurs at optimal factor where rotation = N*pi
                # We fit a parabola to the high-N curve or weighted sum of curves
                weights = np.array(gate_counts, dtype=float) ** 2
                weights /= weights.sum()

                weighted_signal = np.zeros_like(amp_factors)
                for i_g, w in enumerate(weights):
                    weighted_signal += w * data[i_g]

                # For odd gate counts, raw I signal for state |1> is more negative than |0>,
                # so the optimal factor is a valley (minimum) in raw I.
                # The vertex of y = a*x^2 + b*x + c is x = -b / (2*a) for both min and max.
                poly = np.polyfit(amp_factors, weighted_signal, 2)
                a_coef, b_coef, _ = poly

                if abs(a_coef) > 1e-12:
                    opt_factor = float(-b_coef / (2.0 * a_coef))
                else:
                    opt_factor = float(amp_factors[np.argmin(weighted_signal)])

                # Clamp factor within sweep bounds
                opt_factor = float(np.clip(opt_factor, amp_factors.min(), amp_factors.max()))

                old_pi_amp = float(self.device.qubit(qubit).pi_amp)
                new_pi_amp = old_pi_amp * opt_factor

                result.fit[qubit] = {
                    "opt_amp_factor": opt_factor,
                    "pi_amp": new_pi_amp,
                    "old_pi_amp": old_pi_amp,
                    "amp_factors": [float(x) for x in amp_factors],
                    "gate_counts": [int(x) for x in gate_counts],
                }
                result.outcomes[qubit] = Outcome.SUCCESSFUL

                # Generate plot artifact if artifact_dir is configured
                if self.artifact_dir is not None:
                    try:
                        import matplotlib
                        matplotlib.use("Agg")
                        import matplotlib.pyplot as plt

                        out_q_dir = self.artifact_dir / str(qubit)
                        out_q_dir.mkdir(parents=True, exist_ok=True)

                        fig, ax = plt.subplots(figsize=(8, 5), dpi=150)
                        colors = plt.cm.viridis(np.linspace(0.15, 0.85, len(gate_counts)))
                        for i_gc, N in enumerate(gate_counts):
                            ax.plot(
                                amp_factors,
                                data[i_gc],
                                "o-",
                                label=f"N={N}",
                                color=colors[i_gc],
                                markersize=4,
                                alpha=0.85,
                            )

                        ax.axvline(1.0, color="gray", linestyle=":", label="Current (1.00)")
                        ax.axvline(
                            opt_factor,
                            color="crimson",
                            linestyle="--",
                            label=f"Optimum ({opt_factor:.3f})",
                        )
                        ax.set_title(
                            f"Pi-Pulse Error Amplification ({qubit})\n"
                            f"Current pi_amp={old_pi_amp:.6f}V -> Opt pi_amp={new_pi_amp:.6f}V"
                        )
                        ax.set_xlabel("Amplitude Factor")
                        ax.set_ylabel("Readout Signal (I)")
                        ax.legend()
                        plt.tight_layout()

                        fig.savefig(out_q_dir / "qubit_pi_pulse_error.png", dpi=150)
                        plt.close(fig)
                    except Exception:
                        pass
            except Exception:
                result.outcomes[qubit] = Outcome.FAILED

        return result

    def update(self) -> None:
        if self.result is None:
            return
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is Outcome.SUCCESSFUL and fit.get("pi_amp") is not None:
                self.device.qubit(qubit).pi_amp = fit["pi_amp"]
