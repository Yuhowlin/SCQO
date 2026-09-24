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
from pydantic import Field, model_validator

from .._scqat import per_qubit_results
from ..contract import DatasetContract
from ._capabilities.detuning import (
    DriveDetuningSweepParameters,
    drive_detuning_sweep,
)
from ._capabilities.qubit_reset import QubitResetParameters
from ._overlap import OVERLAP_FIELD_DESCS
from ._window import window_bounds
from ._sim import stable_seed
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register
from ._drive_power import drive_power_boundary


class QubitSpectroscopyParameters(
    TargetSelection, AveragingParameters, DriveDetuningSweepParameters, QubitResetParameters
):
    """Inputs for a qubit-spectroscopy (two-tone) measurement.

    The frequency window is the drive_detuning capability's
    ``[start_drive_detuning_hz, end_drive_detuning_hz]`` pair, relative to the
    target's current ``drive_freq_hz``; the mixin defaults ARE this
    experiment's window.

    The three timing fields are one rule: the drive ENDS at an anchor and starts
    ``drive_len_ns`` earlier; ``readout_overlap`` picks the anchor. See
    :mod:`._overlap`, which owns the arithmetic for both backends.
    """

    drive_power_dbm: float = Field(
        -25.0,
        le=10.0,
        description="Absolute saturation-drive power (dBm at the instrument drive port), "
        "applied as a recorded boundary write through the drive chain and reverted after "
        "the run. QM caps at +10 dBm; Qblox above ~-1 dBm needs amplitude > 0.5.",
    )
    # 20 us is the saturation length the QM configs were carrying as device
    # state before this became a parameter, so a defaulted run reproduces it.
    drive_len_ns: float = Field(
        20000.0, ge=4, multiple_of=4, description=OVERLAP_FIELD_DESCS["drive_len_ns"]
    )
    readout_overlap: bool = Field(
        False,
        description="Where the saturation drive ends. false = at the readout "
        "tone's START (the drive is over before the tone, so the line is "
        "measured with no readout photons present); true = at the tone's END, so "
        "the ADC window is covered by a live drive and what you get is the line "
        "under measurement conditions — AC-Stark shifted. See the experiment "
        "description before trusting a true run's writeback.",
    )
    acq_start_ns: float = Field(
        0.0, ge=0, multiple_of=4, description=OVERLAP_FIELD_DESCS["acq_start_ns"]
    )

    @model_validator(mode="after")
    def _acq_start_needs_the_overlap(self) -> "QubitSpectroscopyParameters":
        if self.acq_start_ns and not self.readout_overlap:
            raise ValueError(
                f"acq_start_ns={self.acq_start_ns:g} ns is only meaningful with "
                f"readout_overlap=true. With the drive already over when the tone "
                f"starts there is no steady state to wait for, so delaying the ADC "
                f"would just push it further into the readout pulse. Set "
                f"readout_overlap=true, or leave acq_start_ns at 0."
            )
        return self


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
        "two-tone — run after resonator spectroscopy and before power Rabi / Ramsey). "
        "readout_overlap=false (the default) ends the drive before the readout tone, "
        "which is the bare f_01. readout_overlap=true instead ends it WITH the tone, so "
        "the ADC integrates a steady state under a live drive — faster (no drive-then-"
        "readout dead time) and it shows the line under measurement conditions, but the "
        "readout photons AC-Stark shift the qubit and THE FREQUENCY IT WRITES BACK "
        "CARRIES THAT SHIFT. Keep readout_amp low enough that the shift is under your "
        "tolerance and watch peak_detuning_hz: if it moves when you change the readout "
        "power you are reading photons, not the qubit."
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
        return drive_detuning_sweep(self.params)

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
        return self.run_estimate()

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """A Lorentzian line, pulled low and broadened when the readout tone is live.

        The two modes keep SEPARATE seeds and separate draw orders. Separate
        seeds because offline overlap data has to be distinguishable from
        offline sequential data; separate draw orders because the extra draws
        belong on the overlap branch only, which leaves the default path
        byte-identical to what it produced before the overlap experiment was
        merged in here. The shift scales with the window rather than with
        anything physical — this is an offline placeholder, so it only has to be
        deterministic, distinguishable, and inside the window ``estimate()``
        accepts.
        """
        overlap = self.params.readout_overlap
        detuning = coords["detuning_hz"]
        targets = self.params.targets
        rng = np.random.default_rng(stable_seed(
            "qubit_spectroscopy_overlap" if overlap else "qubit_spectroscopy", *targets))
        low, high = window_bounds(self.params.start_drive_detuning_hz,
                                  self.params.end_drive_detuning_hz)
        width = high - low
        center = (low + high) / 2
        i_data = np.empty((len(targets), detuning.size))
        q_data = np.empty_like(i_data)
        for k in range(len(targets)):
            if overlap:
                err = center + rng.uniform(-0.2, 0.2) * width  # hidden truth, in-window
                err -= rng.uniform(0.02, 0.08) * width  # readout-photon Stark pull
                fwhm = rng.uniform(2e6, 5e6) * rng.uniform(1.5, 2.5)  # dephasing-broadened
            else:
                err = center + rng.uniform(-0.3, 0.3) * width  # hidden truth, in-window
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
                # window_bounds, never a chained start <= det <= end: the edges
                # may arrive in either order and a reversed pair would make the
                # chain always-False, failing every good fit.
                low, high = window_bounds(self.params.start_drive_detuning_hz,
                                          self.params.end_drive_detuning_hz)
                ok = np.isfinite(det) and low <= det <= high
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
