"""Ramsey — second worked protocol, proving the pattern generalizes (backend-free half).

Differs from resonator spectroscopy on every axis that matters:
  * sweep is **time** (idle delay), not frequency;
  * fit is a **decaying cosine** yielding **two** quantities (residual detuning + T2*);
  * writeback targets a **different** neutral device field: ``drive_freq``.

A driver still only adds ``build()``.

Detuning convention: the experiment deliberately detunes the drive by
``frequency_detuning_hz``; the Ramsey fringe then oscillates at
``frequency_detuning_hz + err`` where ``err`` is the residual qubit-drive detuning.
``analyze`` recovers ``err`` and corrects ``drive_freq`` by it.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from lmfit import Model
from pydantic import Field

from ..parameters import AveragingParameters, QubitSelection
from ..protocol import Protocol
from ..result import Outcome, Result


class RamseyParameters(QubitSelection, AveragingParameters):
    """Inputs for a Ramsey experiment."""

    frequency_detuning_hz: float = Field(
        1.0e6, gt=0, description="Artificial drive detuning applied to make the fringe oscillate."
    )
    min_idle_time_ns: float = Field(16, ge=0, description="Shortest idle delay between the two pi/2 pulses.")
    max_idle_time_ns: float = Field(4000, gt=0, description="Longest idle delay.")
    num_points: int = Field(101, gt=1, description="Number of idle-time points.")


class RamseyResult(Result):
    """Output of Ramsey.

    ``fit[qubit]`` carries ``drive_freq`` (new absolute Hz), ``detuning_error_hz``,
    ``t2_star_s`` and ``old_drive_freq``.
    """


def _decaying_cosine(t, offset, amp, tau, freq, phase):
    return offset + amp * np.exp(-t / tau) * np.cos(2 * np.pi * freq * t + phase)


def _fit_ramsey(t: np.ndarray, signal: np.ndarray) -> tuple[float, float, bool]:
    """Fit a decaying cosine; return (osc_freq_hz, t2_star_s, ok)."""
    centered = signal - signal.mean()
    dt = float(t[1] - t[0])
    spectrum = np.abs(np.fft.rfft(centered))
    freqs = np.fft.rfftfreq(t.size, dt)
    freq_guess = float(freqs[1:][np.argmax(spectrum[1:])]) if t.size > 2 else 1.0 / (t[-1] - t[0])

    model = Model(_decaying_cosine)
    params = model.make_params(
        offset=float(signal.mean()),
        amp=float((signal.max() - signal.min()) / 2),
        tau=float((t[-1] - t[0]) / 2),
        freq=freq_guess,
        phase=0.0,
    )
    params["tau"].set(min=0)
    params["freq"].set(min=0)
    params["amp"].set(min=0)
    try:
        out = model.fit(signal, params, t=t)
        return float(out.params["freq"].value), float(out.params["tau"].value), bool(out.success)
    except Exception:
        return freq_guess, float("nan"), False


class Ramsey(Protocol):
    """Backend-agnostic Ramsey. ``build()`` is supplied by a driver."""

    name: ClassVar[str] = "ramsey"
    description: ClassVar[str] = (
        "Two pi/2 pulses separated by a swept idle time with an artificial drive detuning; "
        "fits the decaying fringe to correct drive_freq and report T2*."
    )
    Parameters: ClassVar[type] = RamseyParameters
    Result: ClassVar[type] = RamseyResult

    params: RamseyParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            "idle_time_ns": np.linspace(
                self.params.min_idle_time_ns, self.params.max_idle_time_ns, self.params.num_points
            )
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        t = coords["idle_time_ns"] * 1e-9
        qubits = self.params.qubits
        rng = np.random.default_rng(abs(hash(("ramsey", tuple(qubits)))) % (2**32))
        applied = self.params.frequency_detuning_hz
        i_data = np.empty((len(qubits), t.size))
        q_data = np.empty_like(i_data)
        for k in range(len(qubits)):
            err = rng.uniform(-0.2, 0.2) * applied  # residual detuning to recover
            t2_star = rng.uniform(5e-6, 15e-6)
            fringe = 0.5 - 0.5 * np.exp(-t / t2_star) * np.cos(2 * np.pi * (applied + err) * t)
            noise = 0.02
            i_data[k] = fringe + rng.normal(0, noise, t.size)
            q_data[k] = rng.normal(0, noise, t.size)
        return {"I": i_data, "Q": q_data}

    def analyze(self) -> RamseyResult:
        assert self.dataset is not None, "run() populates self.dataset before analyze()"
        t = self.dataset["idle_time_ns"].values * 1e-9
        applied = self.params.frequency_detuning_hz
        result = RamseyResult()
        for qubit in self.params.qubits:
            signal = self.dataset["I"].sel(qubit=qubit).values
            osc_freq, t2_star, ok = _fit_ramsey(t, signal)
            detuning_error = osc_freq - applied
            old = float(self.backend.device.qubit(qubit).drive_freq)
            result.fit[qubit] = {
                "drive_freq": old + detuning_error,
                "detuning_error_hz": detuning_error,
                "t2_star_s": t2_star,
                "old_drive_freq": old,
            }
            result.outcomes[qubit] = Outcome.SUCCESSFUL if ok and t2_star > 0 else Outcome.FAILED
        return result

    def update(self) -> None:
        if self.result is None:
            return
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is Outcome.SUCCESSFUL:
                self.backend.device.qubit(qubit).drive_freq = fit["drive_freq"]
