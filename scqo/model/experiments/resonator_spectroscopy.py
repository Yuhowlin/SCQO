"""Resonator spectroscopy — the worked reference experiment, greenfield.

Port of :mod:`scqo.experiments.resonator_spectroscopy`. The physics half is
preserved (cosmetically reflowed); what moved is the device surface in ``update()`` and the
anchor spelling: the operating choice lands on the target's READOUT CHANNEL
(``readout_freq_hz``) and the fit's physical content (``f_r_hz``,
``kappa_tot_hz``) on the attached RESONATOR mode.
"""

from __future__ import annotations

from typing import ClassVar, Literal

import numpy as np
from pydantic import Field

from ..._scqat import per_qubit_results
from ...contract import DatasetContract
from ...experiments._sim import stable_seed
from ...parameters import AveragingParameters, TargetSelection
from ...result import Outcome, Result
from ..experiment import Experiment
from . import register


class ResonatorSpectroscopyParameters(TargetSelection, AveragingParameters):
    """Inputs for resonator spectroscopy."""

    frequency_span_hz: float = Field(
        20e6, gt=0, description="Total sweep width around the current "
                                "readout frequency.")
    num_points: int = Field(101, gt=1,
                            description="Number of frequency points.")
    readout_amplitude: float | None = Field(
        None, gt=0, description="Optional readout amplitude override; None "
                                "keeps the device value.")
    analysis_method: Literal["lorentzian", "circle"] = Field(
        "lorentzian",
        description=(
            "Fit model: 'lorentzian' = joint Lorentzian + polynomial-"
            "background fit of the power |IQ|^2. 'circle' = Probst notch-"
            "model fit of the complex S21 (needs meaningful phase data)."),
    )
    baseline_order: int = Field(
        1, ge=0, le=2,
        description="lorentzian only: polynomial background order.")


class ResonatorSpectroscopyResult(Result):
    """``fit[target]``: readout_freq_hz (new absolute), dip_detuning_hz,
    old_readout_freq_hz, plus the physical f_r_hz / kappa_tot_hz that
    update() proposes on the target's resonator mode."""


@register
class ResonatorSpectroscopy(Experiment):
    """Backend-agnostic resonator spectroscopy; a driver adds ``probe()``."""

    name: ClassVar[str] = "resonator_spectroscopy"
    description: ClassVar[str] = (
        "Sweep readout frequency around each resonator and locate the "
        "transmission dip; updates each target's readout channel "
        "readout_freq_hz and proposes the dip position (f_r_hz) and "
        "linewidth (kappa_tot_hz) on the attached resonator mode.")
    Parameters: ClassVar[type] = ResonatorSpectroscopyParameters
    Result: ClassVar[type] = ResonatorSpectroscopyResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("detuning_hz",), sweep_units=("Hz",), variables=("I", "Q"))
    required_operations: ClassVar[tuple[str, ...]] = ("readout",)

    params: ResonatorSpectroscopyParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        span = self.params.frequency_span_hz
        return {"detuning_hz": np.linspace(-span / 2, span / 2,
                                           self.params.num_points)}

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        detuning = coords["detuning_hz"]
        targets = self.params.targets
        rng = np.random.default_rng(
            stable_seed("resonator_spectroscopy", *targets))
        span = float(detuning[-1] - detuning[0])
        kappa = span / 15
        i_data = np.empty((len(targets), detuning.size))
        q_data = np.empty_like(i_data)
        for k in range(len(targets)):
            true_offset = rng.uniform(-0.15, 0.15) * span
            magnitude = 1.0 - 0.8 / (1.0 + ((detuning - true_offset)
                                            / kappa) ** 2)
            noise = 0.01
            i_data[k] = magnitude + rng.normal(0, noise, detuning.size)
            q_data[k] = rng.normal(0, noise, detuning.size)
        return {"I": i_data, "Q": q_data}

    def estimate(self) -> ResonatorSpectroscopyResult:
        assert self.dataset is not None
        from scqat.estimators.resonator_spectroscopy import (
            ResonatorSpectroscopyEstimator,
        )

        targets = list(self.dataset["target"].values)
        old_freqs = {q: self.anchor(q, "readout_freq_hz") for q in targets}
        prepared = self.dataset.rename({"detuning_hz": "detuning"})
        detuning = prepared["detuning"].values
        full_freq = np.array([detuning + old_freqs[q] for q in targets])
        prepared = prepared.assign_coords(
            full_freq=(("target", "detuning"), full_freq))

        results = per_qubit_results(
            prepared,
            ResonatorSpectroscopyEstimator(),
            artifact_dir=self.artifact_dir,
            method=self.params.analysis_method,
            baseline_order=self.params.baseline_order,
        )

        result = ResonatorSpectroscopyResult()
        for target in self.params.targets:
            r = results[target]
            old = old_freqs[target]
            center = float(r["detuning"])
            new_freq = float(r.get("full_freq", old + center))
            result.fit[target] = {
                "readout_freq_hz": new_freq,
                "dip_detuning_hz": center,
                "old_readout_freq_hz": old,
                # the same fit, under its physical names: the dip IS the
                # dressed resonator frequency, the FWHM IS kappa
                "f_r_hz": new_freq,
                "kappa_tot_hz": float(r["fwhm"]),
            }
            result.outcomes[target] = (Outcome.SUCCESSFUL if bool(r["success"])
                                       else Outcome.FAILED)
        return result

    def update(self) -> None:
        if self.result is None:
            return
        for target, fit in self.result.fit.items():
            if self.result.outcomes[target] is not Outcome.SUCCESSFUL:
                continue
            self.device.channel(target, "readout").readout_freq_hz = (
                fit["readout_freq_hz"])
            res_view = self.device.component(self.device.resonator_of(target))
            for field in ("f_r_hz", "kappa_tot_hz"):
                if field in fit:
                    setattr(res_view, field, fit[field])

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
