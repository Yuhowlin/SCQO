"""Resonator spectroscopy — the worked reference experiment (backend-free half).

Sweeps readout frequency around each qubit's current resonator frequency, locates the
transmission dip, and updates ``readout_freq``. A driver only adds ``probe()``.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from pydantic import Field

from .._scqat import per_qubit_results
from ..parameters import AveragingParameters, QubitSelection
from ..experiment import Experiment
from ..result import Outcome, Result


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
        from scqat.estimators.resonator_spectroscopy import ResonatorSpectroscopyEstimator

        # scqat's contract: coord `detuning` + (I, Q); optional `full_freq` lets it report
        # the absolute resonance. full_freq is per-qubit (each has its own readout_freq), so
        # attach it as a (qubit, detuning) coord before the per-qubit split.
        qubits = list(self.dataset["qubit"].values)
        old_freqs = {q: float(self.backend.device.qubit(q).readout_freq) for q in qubits}
        prepared = self.dataset.rename({"detuning_hz": "detuning"})
        detuning = prepared["detuning"].values
        full_freq = np.array([detuning + old_freqs[q] for q in qubits])
        prepared = prepared.assign_coords(full_freq=(("qubit", "detuning"), full_freq))

        results = per_qubit_results(prepared, ResonatorSpectroscopyEstimator())

        result = ResonatorSpectroscopyResult()
        for qubit in self.params.qubits:
            r = results[qubit]
            old = old_freqs[qubit]
            center = float(r["detuning"])  # fitted dip detuning (Hz, relative)
            result.fit[qubit] = {
                "readout_freq": float(r.get("full_freq", old + center)),
                "dip_detuning_hz": center,
                "old_readout_freq": old,
            }
            result.outcomes[qubit] = Outcome.SUCCESSFUL if bool(r["success"]) else Outcome.FAILED
        return result

    def update(self) -> None:
        if self.result is None:
            return
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is Outcome.SUCCESSFUL:
                self.backend.device.qubit(qubit).readout_freq = fit["readout_freq"]
