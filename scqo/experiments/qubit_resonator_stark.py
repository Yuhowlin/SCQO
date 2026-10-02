"""Qubit spectroscopy under a resonator Stark tone — the AC-Stark photon map.

``qubit_spectroscopy`` with one more axis: at every amplitude of a tone parked
on the readout resonator, the saturation drive sweeps the qubit line. The tone
fills the resonator with photons, each of which pulls the qubit by ``-2 chi``
(AC-Stark) and dephases it (measurement-induced broadening); the readout comes
after the photons have left, so every row is measured the same way. The
sequence — and why ring-up and depletion are one number — lives in
:mod:`._stark_tone`, the one timing authority both driver probes play.

The amplitude axis is the amplitude capability's ``amp_prefactor``: a FACTOR of
each target's standing ``readout_amp``, so a multiplexed run drives every
resonator around its own readout point, and ``amp_prefactor = 1`` IS the
calibrated readout amplitude — the fitted shift per ``amp_prefactor**2`` is
therefore the Stark shift the readout itself causes. ``digital_amp`` rides the
dataset with the absolute amplitude.

The reading (scqat ``ac_stark_shift``, bound here 1:1): the line in each row,
then ``f = f0 + s a**2`` and ``fwhm = w0 + b a**2``. With the resonator's
``chi_hz`` fact (``readout_frequency`` with a dip fit writes it) the shift is
also a photon number, ``n = (f - f0) / (-2 chi_hz)``.

Record-only: nothing is written back. ``f0`` is the zero-photon line, but
``qubit_spectroscopy`` owns ``drive_freq_hz``; ``n`` at the readout amplitude is
a property of the CURRENT readout knobs with no catalog home yet.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import ClassVar, Optional

import numpy as np
from pydantic import Field

from .._scqat import per_qubit_results
from ..contract import DatasetContract
from ..estimate_inputs import note_acquisition
from ..experiment import Experiment
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from . import register
from ._capabilities.amplitude import (
    ABS_AMP_COORD,
    ABS_AMP_LABEL,
    AMP_AXIS,
    END_AMP_FACTOR_DESC,
    START_AMP_FACTOR_DESC,
    NUM_AMP_POINTS_DESC,
    AmplitudeSweepParameters,
    amp_anchor,
    amp_sweep,
    attach_absolute_amp,
)
from ._capabilities.detuning import (
    DETUNING_AXIS,
    END_DRIVE_DETUNING_DESC,
    START_DRIVE_DETUNING_DESC,
    DriveDetuningSweepParameters,
    drive_detuning_sweep,
)
from ._capabilities.qubit_reset import QubitResetParameters
from ._depletion import READOUT_DEPLETION_NS_DESC
from ._drive_power import drive_power_boundary
from ._sim import stable_seed
from ._stark_tone import StarkWindows, stark_windows
from ._window import window_bounds


class QubitResonatorStarkParameters(
    TargetSelection, AveragingParameters, DriveDetuningSweepParameters,
    AmplitudeSweepParameters, QubitResetParameters,
):
    """Inputs for the Stark-tone map: the drive-detuning window, the Stark-tone
    amplitude window (a factor of each target's ``readout_amp``) and the
    saturation drive.

    The tone runs the resolved depletion wait before the drive, ends with it,
    and the readout follows one more depletion wait later (:mod:`._stark_tone`).
    """

    # One-sided below the drive by default: the Stark photons pull a transmon
    # that sits under its resonator DOWN, so the upper half of a symmetric
    # window would be spent on empty detuning.
    start_drive_detuning_hz: float = Field(
        -40.0e6,
        description=START_DRIVE_DETUNING_DESC + " The default sits mostly BELOW "
        "the drive because the Stark photons pull a transmon under its resonator "
        "down; flip it for a qubit above its resonator.")
    end_drive_detuning_hz: float = Field(10.0e6, description=END_DRIVE_DETUNING_DESC)
    # from ZERO: the zero-amplitude row is the bare line, the anchor of the shift
    # (scqat fits the shift against amp**2, so the sweep order does not matter)
    start_amp_factor: float = Field(0.0, ge=0.0, lt=2.0, description=START_AMP_FACTOR_DESC)
    end_amp_factor: float = Field(1.5, ge=0.0, lt=2.0, description=END_AMP_FACTOR_DESC)
    num_amp_points: int = Field(16, gt=1, description=NUM_AMP_POINTS_DESC)
    drive_power_dbm: float = Field(
        -25.0,
        le=10.0,
        description="Absolute saturation-drive power (dBm at the instrument drive port), "
        "applied as a recorded boundary write through the drive chain and reverted after "
        "the run. QM caps at +10 dBm; Qblox above ~-1 dBm needs amplitude > 0.5. Keep it "
        "weak: a saturating drive makes the photon number depend on the qubit state.",
    )
    drive_len_ns: float = Field(
        20000.0,
        ge=4,
        multiple_of=4,
        description="Saturation-drive length in ns (multiple of 4). The drive ENDS with "
        "the Stark tone and starts one depletion wait (the tone's ring-up) after it, so "
        "the tone is that wait plus this long.",
    )
    readout_depletion_ns: Optional[float] = Field(
        None,
        ge=0,
        description=READOUT_DEPLETION_NS_DESC + " Here the same wait is also the Stark "
        "tone's ring-up lead before the drive starts.",
    )


class QubitResonatorStarkResult(Result):
    """``fit[target]``: ``stark_shift_at_readout_hz`` (+ ``_err_hz``) — the fitted
    shift per ``amp_prefactor**2``, i.e. at the calibrated ``readout_amp``;
    ``stark_hz_per_amp2`` — the same per absolute amplitude squared;
    ``zero_photon_freq_hz`` / ``zero_photon_detuning_hz`` — the line with no
    photons; ``broadening_at_readout_hz`` / ``fwhm_zero_photon_hz`` — the width
    fit; ``n_readout`` / ``photons_per_amp2`` — photons at ``amp_prefactor = 1``
    and per amplitude squared (NaN without ``chi_hz``); ``chi_hz_used``;
    ``n_rows_fit``; ``rms_residual_hz``; ``old_drive_freq_hz``;
    ``old_readout_amp``."""


@register
class QubitResonatorStark(Experiment):
    """Backend-agnostic Stark-tone spectroscopy map. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "qubit_resonator_stark"
    description: ClassVar[str] = (
        "Qubit spectroscopy under a Stark tone: a tone on the readout channel "
        "(readout_freq_hz, amplitude = amp_prefactor x readout_amp) fills the "
        "resonator; one depletion wait later the saturation drive sweeps around "
        "drive_freq_hz and ends with the tone; one more depletion wait later the "
        "STANDARD readout measures, so every row is read the same way. The line moves "
        "by -2 chi per photon (AC-Stark) and broadens (measurement-induced "
        "dephasing). Reports the shift and the broadening per amp_prefactor**2 — the "
        "shift at factor 1 is the Stark shift of the calibrated readout_amp — and, "
        "when the resonator's chi_hz is known, the photon number. Record-only: "
        "nothing is written back. Needs readout_depletion_s (resonator_spectroscopy "
        "proposes it) or readout_depletion_ns=; chi_hz comes from readout_frequency "
        "with dip_fit_method=lorentzian or circle. Keep the saturation drive weak. "
        "Photon-number-split lines (2 chi > kappa) and the saturating high-power "
        "shift are outside the linear model: read the per-row curve."
    )
    Parameters: ClassVar[type] = QubitResonatorStarkParameters
    Result: ClassVar[type] = QubitResonatorStarkResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=(AMP_AXIS, DETUNING_AXIS), sweep_units=("", "Hz"), variables=("I", "Q")
    )
    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout")
    #: the readout runs at its calibrated amplitude in EVERY row, so the stored
    #: ground blob is the radial reference for the whole map
    attach_readout_positions: ClassVar[bool] = True

    params: QubitResonatorStarkParameters
    #: set by define_sweep(), played by the probes (see resolved_windows)
    _windows: Optional[StarkWindows] = None

    def amp_reference_field(self) -> str:
        return "readout_amp"

    def attach_acquisition_coords(self) -> None:
        attach_absolute_amp(self)

    def define_sweep(self) -> dict[str, np.ndarray]:
        # refuse an ungoverned depletion before the drive-power boundary and
        # before any instrument time — preview included
        self._windows = stark_windows(self)
        # dict order IS the contract order: amplitude outer, detuning inner
        return {**amp_sweep(self.params), **drive_detuning_sweep(self.params)}

    def resolved_windows(self) -> StarkWindows:
        """The timing a probe must play (``define_sweep`` resolved it)."""
        if self._windows is None:
            raise RuntimeError(
                f"{self.name}: define_sweep() resolves the Stark-tone timing and "
                f"must run before probe()")
        return self._windows

    def run(self) -> Result:
        """Boundary-recorded drive-chain set -> acquire -> revert, as in
        ``qubit_spectroscopy``; then the provenance ``qubit_spectroscopy`` does not
        carry: the absolute amplitude axis and the timing that played."""
        self.sweep_axes = self.define_sweep()
        with drive_power_boundary(self, self.params.drive_power_dbm):
            self.dataset = self.backend.acquire(self)
        self.Contract.validate(self.dataset)
        self._attach_reference_positions()
        self.attach_acquisition_coords()
        note_acquisition(self.dataset, "stark_windows", asdict(self.resolved_windows()))
        return self.run_estimate()

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """One hidden line per target, pulled down and broadened by the tone power.

        The zero-photon line sits in the upper part of the window and moves
        ``-pull * (a / a_max)**2`` — the farthest row lands 25-45% of the window
        lower, inside it — while its FWHM grows by the same power law. Scales
        are drawn relative to the window, never from device state.
        """
        amps = coords[AMP_AXIS]
        detuning = coords[DETUNING_AXIS]
        targets = self.params.targets
        rng = np.random.default_rng(stable_seed("qubit_resonator_stark", *targets))
        low, high = window_bounds(self.params.start_drive_detuning_hz,
                                  self.params.end_drive_detuning_hz)
        width = high - low
        a_max = float(np.max(np.abs(amps))) or 1.0
        power = (amps / a_max) ** 2
        i_data = np.empty((len(targets), amps.size, detuning.size))
        q_data = np.empty_like(i_data)
        for k in range(len(targets)):
            f0 = high - rng.uniform(0.2, 0.35) * width     # zero-photon line
            pull = rng.uniform(0.25, 0.45) * width          # downward shift at a_max
            fwhm0 = rng.uniform(2e6, 4e6)
            broaden = rng.uniform(0.5, 1.0)                 # relative width growth at a_max
            centres = f0 - pull * power
            fwhms = fwhm0 * (1.0 + broaden * power)
            line = 0.5 / (1.0 + ((detuning[None, :] - centres[:, None])
                                 / (fwhms[:, None] / 2)) ** 2)
            i_data[k] = line + rng.normal(0, 0.02, line.shape)
            q_data[k] = rng.normal(0, 0.02, line.shape)
        return {"I": i_data, "Q": q_data}

    def estimate(self) -> QubitResonatorStarkResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.ac_stark_shift import AcStarkShiftEstimator

        # scqat's contract: coords `amp_prefactor` + `detuning` + vars I/Q; the
        # per-target `full_freq` makes it report the absolute zero-photon line and
        # `digital_amp` (already attached in run()) draws the absolute axis.
        targets = [str(t) for t in self.dataset["target"].values]
        old_freqs = {q: self.anchor(q, "drive_freq_hz") for q in targets}
        # the SAME read the attached `digital_amp` axis used, so the two cannot disagree
        old_amps = {q: amp_anchor(self, q) for q in targets}
        chis = {q: self.fact(q, "chi_hz") for q in targets}
        prepared = self.dataset.rename({DETUNING_AXIS: "detuning"})
        detuning = prepared["detuning"].values
        full_freq = np.array([detuning + old_freqs[q] for q in targets])
        prepared = prepared.assign_coords(full_freq=(("target", "detuning"), full_freq))

        results = per_qubit_results(
            prepared, AcStarkShiftEstimator(), artifact_dir=self.artifact_dir,
            twin_coord=ABS_AMP_COORD, twin_label=ABS_AMP_LABEL,
            per_target_kwargs={q: {"amp_ref": old_amps[q], "chi_hz": chis[q]}
                               for q in targets},
        )

        result = QubitResonatorStarkResult()
        for qubit in self.params.targets:
            r = results[qubit]
            result.fit[qubit] = {
                "stark_shift_at_readout_hz": float(r["stark_slope_hz"]),
                "stark_shift_at_readout_err_hz": float(r["stark_slope_err_hz"]),
                "stark_hz_per_amp2": float(r["stark_hz_per_amp2"]),
                "zero_photon_freq_hz": float(r["intercept_freq_hz"]),
                "zero_photon_detuning_hz": float(r["intercept_detuning_hz"]),
                "broadening_at_readout_hz": float(r["broadening_slope_hz"]),
                "fwhm_zero_photon_hz": float(r["fwhm_intercept_hz"]),
                "n_readout": float(r["photons_per_prefactor2"]),
                "photons_per_amp2": float(r["photons_per_amp2"]),
                "chi_hz_used": float(r["chi_hz"]),
                "n_rows_fit": float(r["n_rows_fit"]),
                "rms_residual_hz": float(r["rms_residual_hz"]),
                "old_drive_freq_hz": old_freqs[qubit],
                "old_readout_amp": old_amps[qubit],
            }
            result.outcomes[qubit] = (
                Outcome.SUCCESSFUL if r.get("success") else Outcome.FAILED)
        return result

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
