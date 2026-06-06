"""Power Rabi — third worked protocol (backend-free half).

Completes the trio of sweep types: frequency (resonator spec) / time (Ramsey) /
**amplitude** (here). Sweeps the drive amplitude as a factor of the qubit's current
``pi_amp``, fits the Rabi oscillation of excited-state population, and updates ``pi_amp``.

Population model: ``P = 0.5 - 0.5 * cos(pi * factor / factor_pi)`` where ``factor_pi`` is
the amplitude factor giving a full pi rotation (== 1.0 for a perfectly calibrated pulse).
A driver still only adds ``build()``.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from lmfit import Model
from pydantic import Field

from ..parameters import AveragingParameters, QubitSelection
from ..protocol import Protocol
from ..result import Outcome, Result


class PowerRabiParameters(QubitSelection, AveragingParameters):
    """Inputs for power Rabi."""

    min_amp_factor: float = Field(0.0, ge=0, description="Lowest drive amplitude, as a factor of current pi_amp.")
    max_amp_factor: float = Field(2.0, gt=0, description="Highest drive amplitude, as a factor of current pi_amp.")
    num_points: int = Field(101, gt=1, description="Number of amplitude points.")


class PowerRabiResult(Result):
    """Output of power Rabi.

    ``fit[qubit]`` carries ``pi_amp`` (new absolute), ``pi_amp_factor`` (recovered factor)
    and ``old_pi_amp``.
    """


def _cosine(factor, offset, amp, freq, phase):
    return offset + amp * np.cos(2 * np.pi * freq * factor + phase)


def _fit_rabi(factor: np.ndarray, signal: np.ndarray) -> tuple[float, bool]:
    """Fit a cosine vs amplitude factor; return (factor_pi, ok).

    The oscillation frequency ``f`` in the factor domain relates to a full pi rotation by
    ``factor_pi = 1 / (2 f)`` (half a period of the population oscillation).
    """
    # The population first maximizes at factor == factor_pi, so the peak location gives a
    # robust frequency guess even when the sweep spans well under one full oscillation
    # (FFT bins are too coarse there to seed the fit).
    peak_factor = float(factor[np.argmax(signal)])
    freq_guess = 1.0 / (2.0 * peak_factor) if peak_factor > 0 else 1.0 / (factor[-1] - factor[0])

    model = Model(_cosine)
    params = model.make_params(
        offset=float(signal.mean()),
        amp=float((signal.max() - signal.min()) / 2),
        freq=freq_guess,
        phase=0.0,
    )
    params["freq"].set(min=0)
    params["amp"].set(min=0)
    try:
        out = model.fit(signal, params, factor=factor)
        freq = float(out.params["freq"].value)
        if freq <= 0:
            return float("nan"), False
        factor_pi = 1.0 / (2.0 * freq)
        ok = bool(out.success) and 0.3 < factor_pi < 3.0
        return factor_pi, ok
    except Exception:
        return float("nan"), False


class PowerRabi(Protocol):
    """Backend-agnostic power Rabi. ``build()`` is supplied by a driver."""

    name: ClassVar[str] = "power_rabi"
    description: ClassVar[str] = (
        "Sweep drive amplitude (as a factor of the current pi pulse) and fit the Rabi "
        "oscillation to recalibrate pi_amp."
    )
    Parameters: ClassVar[type] = PowerRabiParameters
    Result: ClassVar[type] = PowerRabiResult

    params: PowerRabiParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            "amp_factor": np.linspace(
                self.params.min_amp_factor, self.params.max_amp_factor, self.params.num_points
            )
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        factor = coords["amp_factor"]
        qubits = self.params.qubits
        rng = np.random.default_rng(abs(hash(("power_rabi", tuple(qubits)))) % (2**32))
        i_data = np.empty((len(qubits), factor.size))
        q_data = np.empty_like(i_data)
        for k in range(len(qubits)):
            factor_pi = rng.uniform(0.85, 1.15)  # miscalibration to recover (1.0 == perfect)
            population = 0.5 - 0.5 * np.cos(np.pi * factor / factor_pi)
            noise = 0.02
            i_data[k] = population + rng.normal(0, noise, factor.size)
            q_data[k] = rng.normal(0, noise, factor.size)
        return {"I": i_data, "Q": q_data}

    def analyze(self) -> PowerRabiResult:
        assert self.dataset is not None, "run() populates self.dataset before analyze()"
        factor = self.dataset["amp_factor"].values
        result = PowerRabiResult()
        for qubit in self.params.qubits:
            signal = self.dataset["I"].sel(qubit=qubit).values
            factor_pi, ok = _fit_rabi(factor, signal)
            old = float(self.backend.device.qubit(qubit).pi_amp)
            result.fit[qubit] = {
                "pi_amp": old * factor_pi,
                "pi_amp_factor": factor_pi,
                "old_pi_amp": old,
            }
            result.outcomes[qubit] = Outcome.SUCCESSFUL if ok else Outcome.FAILED
        return result

    def update(self) -> None:
        if self.result is None:
            return
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is Outcome.SUCCESSFUL:
                self.backend.device.qubit(qubit).pi_amp = fit["pi_amp"]
