"""Resonator spectroscopy vs readout power — the punchout scan (backend-free half).

The stack's first 2D experiment: sweep readout detuning x readout power (relative dB,
0 dB = the current ``readout_amp``), track the dip vs power, and pick the highest
power still in the dispersive regime (before the dip starts shifting toward the bare
cavity). Writes back BOTH neutral fields: ``readout_amp`` (optimal power as a pulse
amplitude) and ``readout_freq`` (dip position at that power).

Power axis convention: relative dB. Amplitude prefactors are ``10**(power_db/20)``,
so 0 dB keeps the current readout amplitude and -30 dB is ~3% of it. The scqat
estimator's optimal-power logic is derivative-based (Hz per dB step), so a relative
axis works exactly like an absolute dBm axis.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from pydantic import Field

from .._scqat import per_qubit_results
from ._sim import stable_seed
from ..contract import DatasetContract
from ..experiment import Experiment
from ..parameters import AveragingParameters, QubitSelection
from ..result import Outcome, Result


class ResonatorSpectroscopyPowerParameters(QubitSelection, AveragingParameters):
    """Inputs for the readout-power punchout scan."""

    frequency_span_hz: float = Field(20e6, gt=0, description="Total detuning span around the current readout_freq.")
    num_freq_points: int = Field(101, gt=1, description="Number of frequency points.")
    min_power_db: float = Field(-30.0, description="Lowest readout power, dB relative to the current readout_amp.")
    max_power_db: float = Field(0.0, le=6.0, description="Highest readout power (0 dB = current readout_amp).")
    num_power_points: int = Field(
        21, gt=1, description="Number of power points (the optimal-power derivative uses a ~10-point smoothing window, so keep this comfortably above 10)."
    )


class ResonatorSpectroscopyPowerResult(Result):
    """``fit[qubit]``: ``readout_amp`` (new), ``readout_freq`` (new), ``optimal_power_db``,
    ``frequency_shift_hz``, ``readout_amp_factor``, plus the old values."""


class ResonatorSpectroscopyPower(Experiment):
    """Backend-agnostic punchout. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "resonator_spectroscopy_power"
    description: ClassVar[str] = (
        "2D punchout: sweep readout detuning x readout power, track the dip vs power and "
        "pick the highest dispersive-regime power; updates readout_amp and readout_freq."
    )
    Parameters: ClassVar[type] = ResonatorSpectroscopyPowerParameters
    Result: ClassVar[type] = ResonatorSpectroscopyPowerResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("detuning_hz", "power_db"), sweep_units=("Hz", "dB"), variables=("I", "Q")
    )

    params: ResonatorSpectroscopyPowerParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        span = self.params.frequency_span_hz
        return {
            "detuning_hz": np.linspace(-span / 2, span / 2, self.params.num_freq_points),
            "power_db": np.linspace(self.params.min_power_db, self.params.max_power_db, self.params.num_power_points),
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        detuning = coords["detuning_hz"]
        power = coords["power_db"]
        qubits = self.params.qubits
        rng = np.random.default_rng(stable_seed("resonator_spectroscopy_power", *qubits))
        span = float(detuning[-1] - detuning[0])
        kappa = span / 15
        i_data = np.empty((len(qubits), detuning.size, power.size))
        q_data = np.empty_like(i_data)
        for k in range(len(qubits)):
            dressed = rng.uniform(-0.1, 0.1) * span  # dispersive dip position (low power)
            knee_db = rng.uniform(-12.0, -8.0)  # punchout onset
            for j, p in enumerate(power):
                # above the knee the dip walks DOWN toward the bare cavity (the
                # estimator's derivative threshold is negative) and washes out
                walk = max(0.0, p - knee_db)
                center = dressed - walk * 0.8e6  # ~-0.8 MHz per dB past the knee
                depth = 0.8 / (1.0 + walk / 4.0)
                magnitude = 1.0 - depth / (1.0 + ((detuning - center) / kappa) ** 2)
                noise = 0.01
                i_data[k, :, j] = magnitude + rng.normal(0, noise, detuning.size)
                q_data[k, :, j] = rng.normal(0, noise, detuning.size)
        return {"I": i_data, "Q": q_data}

    def estimate(self) -> ResonatorSpectroscopyPowerResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.resonator_spectroscopy_power import ResonatorSpectroscopyPowerEstimator

        # scqat's contract: coords `power` + `detuning`, vars I/Q (IQdata derived),
        # optional per-qubit `full_freq`; dims ordered (power, detuning).
        qubits = list(self.dataset["qubit"].values)
        old_freqs = {q: float(self.device.qubit(q).readout_freq) for q in qubits}
        old_amps = {q: float(self.device.qubit(q).readout_amp) for q in qubits}
        prepared = self.dataset.rename({"detuning_hz": "detuning", "power_db": "power"})
        prepared = prepared.transpose("qubit", "power", "detuning")
        detuning = prepared["detuning"].values
        full_freq = np.array([detuning + old_freqs[q] for q in qubits])
        prepared = prepared.assign_coords(full_freq=(("qubit", "detuning"), full_freq))

        results = per_qubit_results(
            prepared, ResonatorSpectroscopyPowerEstimator(), artifact_dir=self.artifact_dir
        )

        result = ResonatorSpectroscopyPowerResult()
        for qubit in self.params.qubits:
            r = results[qubit]
            old_freq, old_amp = old_freqs[qubit], old_amps[qubit]
            optimal_db = float(r["optimal_power"])
            shift = float(r["frequency_shift"])
            factor = float(10.0 ** (optimal_db / 20.0))
            result.fit[qubit] = {
                "readout_amp": old_amp * factor,
                "readout_amp_factor": factor,
                "optimal_power_db": optimal_db,
                "readout_freq": old_freq + shift,
                "frequency_shift_hz": shift,
                "old_readout_amp": old_amp,
                "old_readout_freq": old_freq,
            }
            ok = bool(r["optimal_success"]) and np.isfinite(optimal_db) and np.isfinite(shift)
            result.outcomes[qubit] = Outcome.SUCCESSFUL if ok else Outcome.FAILED
        return result

    def update(self) -> None:
        if self.result is None:
            return
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is Outcome.SUCCESSFUL:
                view = self.device.qubit(qubit)
                view.readout_amp = fit["readout_amp"]
                view.readout_freq = fit["readout_freq"]
