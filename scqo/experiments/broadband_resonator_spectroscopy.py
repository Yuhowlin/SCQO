"""Broadband resonator spectroscopy — wideband search across stepped LO sub-bands.

Sweeps transmission over a wide frequency range by stepping Local Oscillator (LO)
sub-bands, stitches the full spectrum, detects candidate resonator dips, and marks
the top N candidate frequencies (determined from components.toml) without updating
device state.
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
from ._sim import stable_seed


class BroadbandResonatorSpectroscopyParameters(TargetSelection, AveragingParameters):
    """Inputs for broadband resonator spectroscopy."""

    start_freq_hz: float = Field(
        4.0e9, gt=0, description="Start frequency of the wideband sweep in Hz."
    )
    stop_freq_hz: float = Field(
        8.0e9, gt=0, description="Stop frequency of the wideband sweep in Hz."
    )
    bandwidth_per_lo_hz: float = Field(
        400.0e6,
        gt=0,
        description="Intermediate frequency (IF) bandwidth per LO sub-band in Hz.",
    )
    num_points_per_lo: int = Field(
        201, gt=1, description="Number of frequency sweep points per LO segment."
    )
    lo_gap_hz: float = Field(
        10.0e6,
        ge=0,
        description="Frequency hole width around LO frequency to skip mixer leakage.",
    )
    num_dips: int | None = Field(
        None,
        ge=1,
        description="Number of candidate resonator dips to detect. If None, "
        "automatically derived from components.toml.",
    )
    readout_amplitude: float | None = Field(
        None, gt=0, description="Optional readout amplitude override."
    )
    readout_power_dbm: float | None = Field(
        None, description="Optional readout power override in dBm."
    )
    min_prominence_db: float = Field(
        0.5, gt=0, description="Minimum dip prominence in dB."
    )
    min_snr: float = Field(
        2.5, gt=0, description="Minimum peak height in robust noise standard deviations."
    )


class BroadbandResonatorSpectroscopyResult(Result):
    """``fit[target]``: candidate resonator transmission dips and extracted properties.

    Contains:
    - ``dips``: list of detected candidate dips with rank, frequency, FWHM, Q, and depth.
    - ``resonator_frequencies_hz``: list of candidate resonance frequencies.
    - ``num_dips_found``: number of detected dips meeting criteria.
    - ``num_dips_requested``: number of dips requested / expected.
    """

    fit: dict[str, dict[str, Any]] = Field(  # type: ignore[assignment]
        default_factory=dict,
        description="Per-qubit extracted quantities and candidate dip structures.",
    )


BroadbandResonatorSpectroscopyParameters.model_rebuild()
BroadbandResonatorSpectroscopyResult.model_rebuild()


@register
class BroadbandResonatorSpectroscopy(Experiment):
    """Backend-agnostic broadband resonator spectroscopy; a driver adds ``probe()``."""

    name: ClassVar[str] = "broadband_resonator_spectroscopy"
    description: ClassVar[str] = (
        "Sweep readout frequency across a wideband range by stepping LO "
        "sub-bands, detect transmission dips, and mark the candidate resonator "
        "frequencies determined from components.toml without updating device state."
    )
    Parameters: ClassVar[type] = BroadbandResonatorSpectroscopyParameters
    Result: ClassVar[type] = BroadbandResonatorSpectroscopyResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("frequency_hz",), sweep_units=("Hz",), variables=("I", "Q")
    )
    required_operations: ClassVar[tuple[str, ...]] = ("readout",)

    params: BroadbandResonatorSpectroscopyParameters

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
            # Clip within total span
            valid = sub_freqs[(sub_freqs >= start) & (sub_freqs <= stop)]
            if valid.size > 0:
                sweep_segments.append(valid)

        all_freqs = np.unique(np.concatenate(sweep_segments))
        return {"frequency_hz": all_freqs}

    def _expected_dips(self, targets) -> int:
        """How many resonator dips the feedline should show: the explicit
        ``num_dips`` when given, else the ROSTER's resonator modes, else the
        target count.

        Shared by ``simulate()`` and ``estimate()`` on purpose — the number the
        offline trace PLANTS and the number the dip finder is ASKED for have to
        be the same, or an offline run reports a mismatch that is an artifact of
        the two disagreeing rather than anything about the chip. They diverged
        silently (planted ``max(2, len(targets))``, requested the roster count)
        for as long as the demo device happened to carry one resonator per
        target."""
        if self.params.num_dips is not None:
            return int(self.params.num_dips)
        try:
            roster = getattr(self.device, "roster", None)
            modes = ([m for m in roster.modes().values()
                      if getattr(m, "kind", None) == "resonator"]
                     if roster is not None else [])
        except Exception:
            modes = []
        return len(modes) or len(list(targets))

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        freqs = coords["frequency_hz"]
        targets = self.params.targets
        rng = np.random.default_rng(
            stable_seed("broadband_resonator_spectroscopy", *targets)
        )

        n_targets = len(targets)
        n_freqs = freqs.size
        i_data = np.empty((n_targets, n_freqs))
        q_data = np.empty((n_targets, n_freqs))

        # Baseline ripple and phase delay
        baseline_mag = 0.8 + 0.08 * np.sin(2.0 * np.pi * freqs / 1.2e9)
        baseline_phase = -2.0 * np.pi * freqs * 30e-9

        s21 = baseline_mag * np.exp(1j * baseline_phase)

        # Plant the candidate dips across the swept span. Like every other
        # simulate() (resonator_spectroscopy is the sibling), the offline model
        # invents its own physics from the sweep and the stable seed rather than
        # reading device state: f_r_hz is a resonator-MODE fact, and a mode
        # carries no knob view to read it through (device.component refuses one
        # by name — knobs live on channels).
        sim_dips: list[tuple[float, float, float]] = []
        span = freqs[-1] - freqs[0]
        n_dips = max(2, self._expected_dips(targets))
        for i in range(n_dips):
            offset = freqs[0] + span * (0.15 + 0.7 * (i + 0.5) / n_dips)
            sim_dips.append((offset, 4.0e6, 0.8))

        for f0, kappa, depth in sim_dips:
            if freqs[0] <= f0 <= freqs[-1]:
                dip_resp = 1.0 - depth / (1.0 + 2j * (freqs - f0) / kappa)
                s21 *= dip_resp

        noise_level = 0.008
        noise = rng.normal(0, noise_level, n_freqs) + 1j * rng.normal(
            0, noise_level, n_freqs
        )
        s21 += noise

        # Broadcast identical feedline transmission to all targets
        for k in range(n_targets):
            i_data[k] = np.real(s21)
            q_data[k] = np.imag(s21)

        return {"I": i_data, "Q": q_data}

    def estimate(self) -> BroadbandResonatorSpectroscopyResult:
        assert self.dataset is not None
        from scqat.estimators.broadband_resonator_spectroscopy import (
            BroadbandResonatorSpectroscopyEstimator,
        )

        targets = list(self.dataset["target"].values)
        prepared = self.dataset.rename({"frequency_hz": "frequency"})

        # Resolve expected number of dips from components.toml / roster
        num_dips = self._expected_dips(targets)

        results = per_qubit_results(
            prepared,
            BroadbandResonatorSpectroscopyEstimator(),
            artifact_dir=self.artifact_dir,
            num_dips=num_dips,
            min_prominence_db=self.params.min_prominence_db,
            min_snr=self.params.min_snr,
        )

        result = BroadbandResonatorSpectroscopyResult()
        for target in targets:
            r = results[target]
            clean_dips = []
            for d in r.get("dips", []):
                clean_dips.append({
                    "rank": int(d.get("rank", 0)),
                    "frequency_hz": float(d.get("frequency_hz", 0.0)),
                    "fwhm_hz": float(d.get("fwhm_hz", 0.0)),
                    "ql": float(d.get("ql", 0.0)),
                    "depth_db": float(d.get("depth_db", 0.0)),
                    "prominence_db": float(d.get("prominence_db", 0.0)),
                    "success": bool(d.get("success", False)),
                })
            resonator_freqs = [float(f) for f in r.get("resonator_frequencies_hz", [])]
            result.fit[target] = {
                "dips": clean_dips,
                "resonator_frequencies_hz": resonator_freqs,
                "num_dips_found": len(clean_dips),
                "num_dips_requested": num_dips,
            }
            # FAILED, not a "warning": the Outcome vocabulary is
            # successful/failed/no_data, and the missing member used to raise
            # AttributeError the moment a fit did NOT succeed — i.e. exactly
            # when this branch was needed.
            result.outcomes[target] = (
                Outcome.SUCCESSFUL if bool(r.get("success", False)) else Outcome.FAILED
            )
        return result

    def update(self) -> None:
        """No-op: broadband resonator spectroscopy is a diagnostic search tool."""

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
