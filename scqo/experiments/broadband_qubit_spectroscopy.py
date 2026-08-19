"""Broadband qubit spectroscopy — wideband search across stepped drive LO sub-bands.

Sweeps qubit XY drive frequency over a wide range (e.g. 3.0 GHz to 6.5 GHz) by stepping
drive Local Oscillator (LO) sub-bands while reading out the resonator at its calibrated frequency,
detects candidate qubit transition peaks, and marks the top N candidate frequencies without
updating device state.
"""

from __future__ import annotations

from typing import Any, ClassVar

import numpy as np
from pydantic import Field

from .._scqat import per_qubit_results
from ..contract import DatasetContract
from ..experiment import Experiment
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from . import register
from ._capabilities.qubit_reset import QubitResetParameters
from ._drive_power import drive_power_boundary
from ._sim import stable_seed


class BroadbandQubitSpectroscopyParameters(
    TargetSelection, AveragingParameters, QubitResetParameters
):
    """Inputs for broadband qubit spectroscopy."""

    start_freq_hz: float = Field(
        3.0e9, gt=0, description="Start frequency of the wideband drive sweep in Hz."
    )
    stop_freq_hz: float = Field(
        6.5e9, gt=0, description="Stop frequency of the wideband drive sweep in Hz."
    )
    bandwidth_per_lo_hz: float = Field(
        300.0e6,
        gt=0,
        description="Intermediate frequency (IF) bandwidth per drive LO sub-band in Hz.",
    )
    num_points_per_lo: int = Field(
        201, gt=1, description="Number of frequency sweep points per drive LO segment."
    )
    lo_gap_hz: float = Field(
        10.0e6,
        ge=0,
        description="Frequency hole width around drive LO frequency to skip mixer leakage.",
    )
    drive_power_dbm: float = Field(
        -25.0,
        le=10.0,
        description="Absolute saturation-drive power in dBm at the instrument drive port.",
    )
    drive_len_ns: float | None = Field(
        None,
        gt=0,
        description="Saturation pulse length in ns; None = backend's configured length.",
    )
    max_peaks: int = Field(
        1,
        ge=1,
        description=(
            "Maximum number of candidate qubit transition peaks to return. "
            "Peaks are ranked by Lorentzian area; only the top max_peaks are kept."
        ),
    )
    prominence: float = Field(
        0.1,
        gt=0,
        description=(
            "Minimum peak prominence as a fraction of the baseline-corrected "
            "signal span (e.g. 0.1 = 10%% of span). Passed directly to fit_peaks()."
        ),
    )
    min_snr: float = Field(
        6.0,
        gt=0,
        description=(
            "Minimum peak height in robust noise standard deviations (MAD-based). "
            "A peak must satisfy BOTH the prominence and min_snr criteria."
        ),
    )


class BroadbandQubitSpectroscopyResult(Result):
    """``fit[qubit]``: candidate qubit transition peaks and extracted properties.

    Contains:
    - ``peaks``: list of detected candidate peaks with rank, frequency, FWHM, and prominence.
    - ``candidate_qubit_frequencies_hz``: list of candidate transition frequencies.
    - ``num_peaks_found``: number of detected peaks meeting criteria.
    - ``num_peaks_requested``: number of peaks requested.
    """

    fit: dict[str, dict[str, Any]] = Field(  # type: ignore[assignment]
        default_factory=dict,
        description="Per-qubit extracted quantities and candidate transition peak structures.",
    )


BroadbandQubitSpectroscopyParameters.model_rebuild()
BroadbandQubitSpectroscopyResult.model_rebuild()


@register
class BroadbandQubitSpectroscopy(Experiment):
    """Backend-agnostic broadband qubit spectroscopy; a driver adds ``probe()``."""

    name: ClassVar[str] = "broadband_qubit_spectroscopy"
    description: ClassVar[str] = (
        "Sweep qubit XY drive frequency across a wideband range by stepping drive "
        "LO sub-bands, detect candidate qubit transition peaks, and mark candidate "
        "frequencies without updating device state."
    )
    Parameters: ClassVar[type] = BroadbandQubitSpectroscopyParameters
    Result: ClassVar[type] = BroadbandQubitSpectroscopyResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("frequency_hz",), sweep_units=("Hz",), variables=("I", "Q")
    )
    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout")

    params: BroadbandQubitSpectroscopyParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        start = float(self.params.start_freq_hz)
        stop = float(self.params.stop_freq_hz)
        bw = float(self.params.bandwidth_per_lo_hz)
        pts_per_lo = int(self.params.num_points_per_lo)
        gap = float(self.params.lo_gap_hz)

        if stop <= start:
            raise ValueError(
                f"stop_freq_hz ({stop}) must be greater than start_freq_hz ({start})"
            )

        lo_step = bw
        n_segments = max(1, int(np.ceil((stop - start) / lo_step)))
        lo_centers = [start + (i + 0.5) * lo_step for i in range(n_segments)]

        sweep_segments = []
        for lo in lo_centers:
            sub_min = lo - bw / 2.0
            sub_max = lo + bw / 2.0
            if gap > 0 and sub_min < lo < sub_max:
                n_half = max(2, pts_per_lo // 2)
                f1 = np.linspace(sub_min, lo - gap / 2.0, n_half)
                f2 = np.linspace(lo + gap / 2.0, sub_max, n_half)
                sub_freqs = np.concatenate([f1, f2])
            else:
                sub_freqs = np.linspace(sub_min, sub_max, pts_per_lo)
            valid = sub_freqs[(sub_freqs >= start) & (sub_freqs <= stop)]
            if valid.size > 0:
                sweep_segments.append(valid)

        all_freqs = np.unique(np.concatenate(sweep_segments))
        return {"frequency_hz": all_freqs}

    def run(self) -> Result:
        """Execute broadband qubit spectroscopy with recorded drive power boundary."""
        self.sweep_axes = self.define_sweep()
        with drive_power_boundary(self, self.params.drive_power_dbm):
            self.dataset = self.backend.acquire(self)
        self.Contract.validate(self.dataset)
        self.result = self.estimate()
        return self.result

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        freqs = coords["frequency_hz"]
        targets = self.params.targets
        rng = np.random.default_rng(
            stable_seed("broadband_qubit_spectroscopy", *targets)
        )

        n_targets = len(targets)
        n_freqs = freqs.size

        # Baseline transmission
        baseline_mag = 0.5
        i_data = np.full((n_targets, n_freqs), baseline_mag, dtype=float)
        q_data = np.zeros((n_targets, n_freqs), dtype=float)

        for target_idx, target in enumerate(targets):
            # Target simulated qubit transition frequencies
            qubit_f01 = 5.0e9 + (target_idx + 1) * 200.0e6
            qubit_f02_half = qubit_f01 - 100.0e6

            for q_f in (qubit_f01, qubit_f02_half):
                if freqs[0] <= q_f <= freqs[-1]:
                    gamma = 10.0e6
                    lorentzian = gamma**2 / ((freqs - q_f) ** 2 + gamma**2)
                    i_data[target_idx] += 0.25 * lorentzian
                    q_data[target_idx] += 0.15 * lorentzian

            # Add Gaussian noise
            i_data[target_idx] += rng.normal(0, 0.01, size=n_freqs)
            q_data[target_idx] += rng.normal(0, 0.01, size=n_freqs)

        return {"I": i_data, "Q": q_data}

    def estimate(self) -> BroadbandQubitSpectroscopyResult:
        """Run estimator on the dataset."""
        assert self.dataset is not None
        from scqat.estimators import BroadbandQubitSpectroscopyEstimator

        targets = list(self.dataset["target"].values)
        prepared = self.dataset.rename({"frequency_hz": "frequency"})

        fit_dict = per_qubit_results(
            prepared,
            BroadbandQubitSpectroscopyEstimator(),
            artifact_dir=self.artifact_dir,
            max_peaks=self.params.max_peaks,
            prominence=self.params.prominence,
            min_snr=self.params.min_snr,
        )

        result = BroadbandQubitSpectroscopyResult()
        for target in targets:
            raw_target_fit = fit_dict.get(target, {})
            success = bool(raw_target_fit.get("success", False))
            result.outcomes[target] = Outcome.SUCCESSFUL if success else Outcome.FAILED

            clean_peaks = []
            for i, p in enumerate(raw_target_fit.get("peaks", [])):
                clean_peaks.append({
                    "rank": int(p.get("rank", i + 1)),
                    "frequency_hz": float(p.get("frequency_hz", 0.0)),
                    "fwhm_hz": float(p.get("fwhm", p.get("fwhm_hz", 0.0))),
                    "amplitude": float(p.get("amplitude", 0.0)),
                    "success": bool(p.get("success", True)),
                })

            result.fit[target] = {
                "peaks": clean_peaks,
                "candidate_qubit_frequencies_hz": [
                    float(f) for f in raw_target_fit.get("candidate_qubit_frequencies_hz", [])
                ],
                "num_peaks_found": int(raw_target_fit.get("num_peaks_found", 0)),
                "num_peaks_requested": int(self.params.max_peaks),
            }

        return result

    def update(self) -> None:
        """Exploratory spectroscopy does not mutate state directly."""
        pass

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
