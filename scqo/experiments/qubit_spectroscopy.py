"""Qubit spectroscopy — coarse two-tone search for the 0->1 transition (backend-free half).

The natural SECOND bring-up step: after resonator spectroscopy fixes the readout,
sweep a weak saturation drive around the assumed qubit frequency and fit the response
peak(s); the strongest peak recalibrates ``drive_freq``. It runs before any pi pulse
exists — unlike Ramsey, which needs calibrated pi/2 pulses and a near-correct drive
frequency (Ramsey is the fine-tuning follow-up, not the bring-up tool).
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from pydantic import Field

from .._scqat import per_qubit_results
from ._sim import stable_seed
from ..contract import DatasetContract
from ..experiment import Experiment
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result


class QubitSpectroscopyParameters(TargetSelection, AveragingParameters):
    """Inputs for a qubit-spectroscopy (two-tone) measurement."""

    frequency_span_hz: float = Field(
        60.0e6, gt=0, description="Full drive-detuning span swept around the current drive_freq."
    )
    num_points: int = Field(201, gt=1, description="Number of frequency points.")
    drive_amp: float = Field(
        0.1, gt=0, description="Saturation drive amplitude (backend units: amplitude factor on QM, voltage offset on Qblox)."
    )
    drive_len_ns: float | None = Field(
        None, gt=0, description="Saturation pulse length in ns where the backend needs one (QM); None = backend's configured length. Continuous-drive backends ignore it."
    )


class QubitSpectroscopyResult(Result):
    """``fit[qubit]`` carries ``drive_freq`` (new absolute Hz), its measured twin
    ``f_01_hz`` (same value; ``update()`` writes the knob and the fact together),
    ``peak_detuning_hz``, ``fwhm_hz``, ``n_peaks`` and ``old_drive_freq``."""


class QubitSpectroscopy(Experiment):
    """Backend-agnostic two-tone spectroscopy. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "qubit_spectroscopy"
    description: ClassVar[str] = (
        "Sweep a weak saturation drive around drive_freq and fit the response peaks; the "
        "strongest peak recalibrates drive_freq (coarse two-tone — run after resonator "
        "spectroscopy and before power Rabi / Ramsey)."
    )
    Parameters: ClassVar[type] = QubitSpectroscopyParameters
    Result: ClassVar[type] = QubitSpectroscopyResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("detuning_hz",), sweep_units=("Hz",), variables=("I", "Q")
    )
    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout")

    params: QubitSpectroscopyParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        span = self.params.frequency_span_hz
        return {"detuning_hz": np.linspace(-span / 2, span / 2, self.params.num_points)}

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        detuning = coords["detuning_hz"]
        targets = self.params.targets
        rng = np.random.default_rng(stable_seed("qubit_spectroscopy", *targets))
        i_data = np.empty((len(targets), detuning.size))
        q_data = np.empty_like(i_data)
        for k in range(len(targets)):
            err = rng.uniform(-0.3, 0.3) * self.params.frequency_span_hz  # hidden truth
            fwhm = rng.uniform(2e6, 5e6)
            peak = 0.5 * (fwhm / 2) ** 2 / ((detuning - err) ** 2 + (fwhm / 2) ** 2)
            noise = 0.02
            i_data[k] = peak + rng.normal(0, noise, detuning.size)
            q_data[k] = rng.normal(0, noise, detuning.size)
        return {"I": i_data, "Q": q_data}

    def estimate(self) -> QubitSpectroscopyResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.qubit_spectroscopy import QubitSpectroscopyEstimator

        # scqat's contract: coord `detuning` + vars I/Q (it derives IQdata = I + iQ);
        # optional per-qubit `full_freq` lets it report absolute peak positions.
        targets = list(self.dataset["target"].values)
        old_freqs = {q: self.anchor(q, "drive_freq") for q in targets}
        prepared = self.dataset.rename({"detuning_hz": "detuning"})
        detuning = prepared["detuning"].values
        full_freq = np.array([detuning + old_freqs[q] for q in targets])
        prepared = prepared.assign_coords(full_freq=(("target", "detuning"), full_freq))

        results = per_qubit_results(prepared, QubitSpectroscopyEstimator(), artifact_dir=self.artifact_dir)

        result = QubitSpectroscopyResult()
        for qubit in self.params.targets:
            peaks = results[qubit].get("peaks") or []
            old = old_freqs[qubit]
            if peaks:
                # strongest physical line = largest Lorentzian area
                best = max(peaks, key=lambda p: abs(p["amplitude"]) * p["fwhm"])
                det = float(best["detuning"])
                result.fit[qubit] = {
                    "drive_freq": old + det,
                    # the measured FACT twin of the drive_freq knob (same fit)
                    "f_01_hz": old + det,
                    "peak_detuning_hz": det,
                    "fwhm_hz": float(best["fwhm"]),
                    "n_peaks": float(len(peaks)),
                    "old_drive_freq": old,
                }
                ok = np.isfinite(det) and abs(det) <= self.params.frequency_span_hz
            else:
                result.fit[qubit] = {"n_peaks": 0.0, "old_drive_freq": old}
                ok = False
            result.outcomes[qubit] = Outcome.SUCCESSFUL if ok else Outcome.FAILED
        return result

    def update(self) -> None:
        if self.result is None:
            return
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is Outcome.SUCCESSFUL:
                view = self.device.component(qubit)
                view.drive_freq = fit["drive_freq"]  # the instrument knob
                view.f_01_hz = fit["f_01_hz"]  # the measured physical fact (same fit)
