"""Pi-pulse amplitude error amplification (qubit_pi_pulse_error), greenfield.

Port of :mod:`scqo.experiments.qubit_pi_pulse_error`. The physics half is
byte-for-byte; what moved is the device surface: ``pi_amp`` keeps its name
but now lives on the target's DRIVE CHANNEL
(``self.device.channel(t, "drive").pi_amp``), read in ``estimate()`` and
written in ``update()``.
"""

from __future__ import annotations

from typing import ClassVar, List

import numpy as np
from pydantic import Field

from ..contract import DatasetContract
from ._capabilities.amplitude import (
    ABS_AMP_COORD,
    ABS_AMP_LABEL,
    AMP_AXIS,
    MAX_AMP_FACTOR_DESC,
    MIN_AMP_FACTOR_DESC,
    NUM_AMP_POINTS_DESC,
    AmplitudeSweepParameters,
    amp_anchor,
    amp_sweep,
    attach_absolute_amp,
)
from ._capabilities.qubit_reset import QubitResetParameters
from ._sim import stable_seed
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register


class QubitPiPulseErrorParameters(TargetSelection, AveragingParameters, QubitResetParameters,
                                  AmplitudeSweepParameters):
    """Inputs for pi-pulse error amplification calibration."""

    # a TIGHT window around the standing amplitude: error amplification resolves a
    # small over/under-rotation, it does not search for the pi pulse
    min_amp_factor: float = Field(0.90, ge=0.0, description=MIN_AMP_FACTOR_DESC)
    max_amp_factor: float = Field(1.10, gt=0.0, lt=2.0, description=MAX_AMP_FACTOR_DESC)
    num_amp_points: int = Field(41, gt=1, description=NUM_AMP_POINTS_DESC)
    gate_counts: List[int] = Field(
        default_factory=lambda: [1, 3, 5, 7, 9, 11],
        description="List of odd gate counts (repetitions of X180).",
    )


class QubitPiPulseErrorResult(Result):
    """Fitted optimal pi-pulse amplitude factor."""


@register
class QubitPiPulseError(Experiment):
    """Calibrate pi-pulse amplitude via error amplification across repeated X180 gates."""

    name: ClassVar[str] = "qubit_pi_pulse_error"
    description: ClassVar[str] = (
        "Sweep pi-pulse amplitude factor across repeated X180 gate sequences (X^1, X^3, X^5...) "
        "to amplify and precisely calibrate the pi pulse amplitude."
    )
    Parameters: ClassVar[type] = QubitPiPulseErrorParameters
    Result: ClassVar[type] = QubitPiPulseErrorResult
    # raw (I, Q) or a bare I quadrature — this probe never returns a discriminated
    # `state` (the QM shell hardcodes use_state_discrimination=False), so the
    # contract truthfully omits it.
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("gate_count", AMP_AXIS),
        sweep_units=("", ""),
        variables=("I", "Q"),
        alt_variables=(("I",),),
    )

    params: QubitPiPulseErrorParameters

    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout")

    def amp_reference_field(self) -> str:
        return "pi_amp"

    def attach_acquisition_coords(self) -> None:
        attach_absolute_amp(self)

    def define_sweep(self) -> dict[str, np.ndarray]:
        # dict order IS the contract order: (gate_count, AMP_AXIS)
        return {
            "gate_count": np.array(self.params.gate_counts, dtype=int),
            **amp_sweep(self.params),
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        gate_counts = coords["gate_count"]
        amp_factors = coords[AMP_AXIS]
        qubits = self.params.targets

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
        amp_factors = ds.coords[AMP_AXIS].values
        gate_counts = ds.coords["gate_count"].values

        for qubit in self.params.targets:
            try:
                # this probe never returns a discriminated `state` (its contract
                # accepts I/Q or a bare I), so `I` is the only source
                var_name = "I"
                if "target" in ds.dims:
                    data = ds[var_name].sel(target=qubit).values
                else:
                    data = ds[var_name].values

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

                # the SAME read the attached `digital_amp` axis used
                old_pi_amp = amp_anchor(self, qubit)
                new_pi_amp = old_pi_amp * opt_factor

                # scalars only: the swept axes already live on the dataset, and a
                # list-valued fit entry counts as n_nonscalar in every campaign
                # statistics table (campaign.summarize)
                result.fit[qubit] = {
                    "opt_amp_prefactor": opt_factor,
                    "pi_amp": new_pi_amp,
                    "old_pi_amp": old_pi_amp,
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

                        ax.axvline(1.0, color="gray", linestyle=":",
                                   label="Current (1.00 x)")
                        ax.axvline(
                            opt_factor,
                            color="crimson",
                            linestyle="--",
                            label=f"Optimum ({opt_factor:.4f} x, {new_pi_amp:.6f} abs)",
                        )
                        # pi_amp is DIMENSIONLESS (catalog unit ""): a normalized
                        # fraction of full scale on both backends, never volts.
                        ax.set_title(
                            f"Pi-Pulse Error Amplification ({qubit})\n"
                            f"pi_amp {old_pi_amp:.6f} -> {new_pi_amp:.6f} "
                            f"(normalized amplitude)"
                        )
                        ax.set_xlabel("Amplitude Factor")
                        ax.set_ylabel("Readout Signal (I)")
                        ax.legend()
                        self._add_absolute_axis(ax, qubit, amp_factors)
                        plt.tight_layout()

                        fig.savefig(out_q_dir / "qubit_pi_pulse_error.png", dpi=150)
                        plt.close(fig)
                    except Exception:
                        pass
            except Exception:
                result.outcomes[qubit] = Outcome.FAILED

        return result

    def _add_absolute_axis(self, ax, qubit: str, amp_factors) -> None:
        """Draw the attached ``digital_amp`` row as a secondary top axis, so this
        figure reads in absolute amplitude like its scqat-fitted siblings.

        Reuses scqat's shared helper rather than a local ``secondary_xaxis`` so
        there is ONE "second scale" implementation across the stack. The import
        is lazy and this whole figure block is already wrapped in
        ``except Exception: pass``, so an older scqat degrades to the primary
        axis and never kills a run.
        """
        if self.dataset is None or ABS_AMP_COORD not in self.dataset.coords:
            return
        coord = self.dataset.coords[ABS_AMP_COORD]
        values = coord.sel(target=qubit).values if "target" in coord.dims else coord.values
        values = np.asarray(values, dtype=float)
        if not np.isfinite(values).all():
            return  # this target's reference knob was unreadable — primary axis only
        from scqat.estimators._twin_axis import add_twin_axis

        add_twin_axis(ax, np.asarray(amp_factors, dtype=float), values, ABS_AMP_LABEL)

    def update(self) -> None:
        if self.result is None:
            return
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is Outcome.SUCCESSFUL and fit.get("pi_amp") is not None:
                self.device.channel(qubit, "drive").pi_amp = fit["pi_amp"]

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
