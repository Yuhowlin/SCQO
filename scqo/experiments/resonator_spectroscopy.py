"""Resonator spectroscopy — the worked reference experiment (backend-free half).

Sweeps readout frequency around each qubit's current resonator frequency, locates the
transmission dip, and updates ``readout_freq``. A driver only adds ``probe()``.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from lmfit.models import ConstantModel, LorentzianModel
from pydantic import Field

from ..parameters import AveragingParameters, QubitSelection
from ..experiment import Experiment
from ..result import Outcome, Result

# np.trapz was renamed to np.trapezoid in NumPy 2.0 (and the old name later removed);
# np.trapezoid does not exist before 2.0. Support scqo's whole numpy>=1.26 range.
_trapezoid = np.trapezoid if hasattr(np, "trapezoid") else np.trapz


class ResonatorSpectroscopyParameters(QubitSelection, AveragingParameters):
    """Inputs for resonator spectroscopy."""

    frequency_span_hz: float = Field(20e6, gt=0, description="Total sweep width around the current readout freq.")
    num_points: int = Field(101, gt=1, description="Number of frequency points in the sweep.")
    readout_amplitude: float | None = Field(
        None, gt=0, description="Optional readout amplitude override; None keeps the device value."
    )


class ResonatorSpectroscopyResult(Result):
    """Output of resonator spectroscopy.

    ``fit[qubit]`` carries ``readout_freq`` (new absolute Hz), ``dip_detuning_hz`` and
    ``old_readout_freq``.
    """


def _fit_dip(detuning: np.ndarray, magnitude: np.ndarray) -> tuple[float, bool]:
    """Fit a Lorentzian to the transmission dip; return (center_detuning_hz, ok)."""
    peak = magnitude.max() - magnitude  # flip the dip into a positive peak
    model = LorentzianModel() + ConstantModel()
    params = model.make_params()
    params["center"].set(value=float(detuning[np.argmax(peak)]))
    params["sigma"].set(value=float((detuning[-1] - detuning[0]) / 10), min=0)
    params["amplitude"].set(value=float(_trapezoid(peak, detuning)), min=0)
    params["c"].set(value=0.0)
    try:
        out = model.fit(peak, params, x=detuning)
        center = float(out.params["center"].value)
        ok = bool(out.success) and detuning[0] <= center <= detuning[-1]
        return center, ok
    except Exception:
        return float(detuning[np.argmin(magnitude)]), False


class ResonatorSpectroscopy(Experiment):
    """Backend-agnostic resonator spectroscopy. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "resonator_spectroscopy"
    description: ClassVar[str] = (
        "Sweep readout frequency around each resonator and locate the transmission dip; "
        "updates each qubit's readout_freq."
    )
    Parameters: ClassVar[type] = ResonatorSpectroscopyParameters
    Result: ClassVar[type] = ResonatorSpectroscopyResult

    params: ResonatorSpectroscopyParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        span = self.params.frequency_span_hz
        return {"detuning_hz": np.linspace(-span / 2, span / 2, self.params.num_points)}

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        detuning = coords["detuning_hz"]
        qubits = self.params.qubits
        rng = np.random.default_rng(abs(hash(tuple(qubits))) % (2**32))
        span = float(detuning[-1] - detuning[0])
        kappa = span / 15
        i_data = np.empty((len(qubits), detuning.size))
        q_data = np.empty_like(i_data)
        for k in range(len(qubits)):
            true_offset = rng.uniform(-0.15, 0.15) * span  # the dip we should recover
            magnitude = 1.0 - 0.8 / (1.0 + ((detuning - true_offset) / kappa) ** 2)
            noise = 0.01
            i_data[k] = magnitude + rng.normal(0, noise, detuning.size)
            q_data[k] = rng.normal(0, noise, detuning.size)
        return {"I": i_data, "Q": q_data}

    def estimate(self) -> ResonatorSpectroscopyResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        detuning = self.dataset["detuning_hz"].values
        result = ResonatorSpectroscopyResult()
        for qubit in self.params.qubits:
            i_data = self.dataset["I"].sel(qubit=qubit).values
            q_data = self.dataset["Q"].sel(qubit=qubit).values
            magnitude = np.hypot(i_data, q_data)
            center, ok = _fit_dip(detuning, magnitude)
            old = float(self.backend.device.qubit(qubit).readout_freq)
            result.fit[qubit] = {
                "readout_freq": old + center,
                "dip_detuning_hz": center,
                "old_readout_freq": old,
            }
            result.outcomes[qubit] = Outcome.SUCCESSFUL if ok else Outcome.FAILED
        return result

    def update(self) -> None:
        if self.result is None:
            return
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is Outcome.SUCCESSFUL:
                self.backend.device.qubit(qubit).readout_freq = fit["readout_freq"]
