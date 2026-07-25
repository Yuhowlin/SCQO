"""Qubit spectroscopy — coarse two-tone 0->1 search, greenfield.

Port of :mod:`scqo.experiments.qubit_spectroscopy`. The physics half is
byte-for-byte; what moved is the device surface and spellings: the anchor
and the recalibrated knob land on the target's DRIVE CHANNEL
(``drive_freq_hz``), while its measured twin ``f_01_hz`` stays a fact on
the target mode — ``update()`` writes both from the one fit. The
saturation-power boundary is the ported :mod:`._drive_power` helper.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from pydantic import Field

from .._scqat import per_qubit_results
from ..contract import DatasetContract
from ._sim import stable_seed
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register
from ._drive_power import drive_power_boundary


class QubitSpectroscopyParameters(TargetSelection, AveragingParameters):
    """Inputs for a qubit-spectroscopy (two-tone) measurement."""

    frequency_span_hz: float = Field(
        60.0e6, gt=0, description="Full drive-detuning span swept around the current drive_freq_hz."
    )
    num_points: int = Field(201, gt=1, description="Number of frequency points.")
    drive_power_dbm: float = Field(
        -25.0,
        le=10.0,
        description="Absolute saturation-drive power (dBm at the instrument drive port), "
        "applied as a recorded boundary write through the drive chain and reverted after "
        "the run. QM caps at +10 dBm; Qblox above ~-1 dBm needs amplitude > 0.5.",
    )
    drive_len_ns: float | None = Field(
        None, gt=0, description="Saturation pulse length in ns where the backend needs one (QM); None = backend's configured length. Continuous-drive backends ignore it."
    )


class QubitSpectroscopyResult(Result):
    """``fit[qubit]`` carries ``drive_freq_hz`` (new absolute Hz), its measured twin
    ``f_01_hz`` (same value; ``update()`` writes the knob and the fact together),
    ``peak_detuning_hz``, ``fwhm_hz``, ``n_peaks`` and ``old_drive_freq_hz``."""


@register
class QubitSpectroscopy(Experiment):
    """Backend-agnostic two-tone spectroscopy. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "qubit_spectroscopy"
    description: ClassVar[str] = (
        "Sweep a weak saturation drive around drive_freq_hz and fit the response peaks; "
        "the strongest peak recalibrates the drive channel's drive_freq_hz (coarse "
        "two-tone — run after resonator spectroscopy and before power Rabi / Ramsey)."
    )
    Parameters: ClassVar[type] = QubitSpectroscopyParameters
    Result: ClassVar[type] = QubitSpectroscopyResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("detuning_hz",), sweep_units=("Hz",), variables=("I", "Q")
    )
    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout")
    #: stored blob centers ride the dataset -> radial ref = the measured ground point
    attach_readout_positions: ClassVar[bool] = True

    params: QubitSpectroscopyParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        span = self.params.frequency_span_hz
        return {"detuning_hz": np.linspace(-span / 2, span / 2, self.params.num_points)}

    def run(self) -> Result:
        """Boundary-recorded drive-chain set -> acquire -> revert (shared helper).

        The saturation power is a per-run STIMULUS, not a calibration proposal:
        ``drive_power_boundary`` writes ``drive_power_dbm`` through ``self.device``
        (the Session's RecordingDevice, on each target's drive channel) and reverts
        it exactly afterwards — 2 ChangeRecords + coupled ``drive_amp`` echoes per
        qubit, the punchout discipline of ``resonator_spectroscopy_power_amp``.
        """
        self.sweep_axes = self.define_sweep()
        with drive_power_boundary(self, self.params.drive_power_dbm):
            self.dataset = self.backend.acquire(self)
        self.Contract.validate(self.dataset)
        self._attach_reference_positions()
        self.result = self.estimate()
        return self.result

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
        old_freqs = {q: self.anchor(q, "drive_freq_hz") for q in targets}
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
                    "drive_freq_hz": old + det,
                    # the measured FACT twin of the drive_freq_hz knob (same fit)
                    "f_01_hz": old + det,
                    "peak_detuning_hz": det,
                    "fwhm_hz": float(best["fwhm"]),
                    "n_peaks": float(len(peaks)),
                    "old_drive_freq_hz": old,
                }
                ok = np.isfinite(det) and abs(det) <= self.params.frequency_span_hz
            else:
                result.fit[qubit] = {"n_peaks": 0.0, "old_drive_freq_hz": old}
                ok = False
            result.outcomes[qubit] = Outcome.SUCCESSFUL if ok else Outcome.FAILED
        return result

    def update(self) -> None:
        if self.result is None:
            return
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is Outcome.SUCCESSFUL:
                # the instrument knob, on the drive channel
                self.device.channel(qubit, "drive").drive_freq_hz = fit["drive_freq_hz"]
                # the measured physical fact (same fit), on the target mode
                self.device.component(qubit).f_01_hz = fit["f_01_hz"]

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
