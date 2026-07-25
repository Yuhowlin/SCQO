"""Resonator spectroscopy vs flux — the dispersive flux map, greenfield.

Port of :mod:`scqo.experiments.resonator_spectroscopy_flux`. The physics
half is byte-for-byte; what moved is the device surface and the field
spellings: the flux-transfer facts land on the target's FLUX CHANNEL
under their new names (``v_offset_v`` -> ``flux_offset``,
``v_per_phi0_v`` -> ``flux_per_phi0``), the sweet-spot operating point is
two channel knobs (``idle_flux`` on the flux channel, ``readout_freq_hz``
on the readout channel), and the dispersive physics (``f_r0_hz``,
``g_hz``) goes on the attached RESONATOR mode.
"""

from __future__ import annotations

from typing import ClassVar, Literal

import numpy as np
from pydantic import Field

from .._scqat import per_qubit_results
from ..contract import DatasetContract
from ._capabilities.flux import (
    NUM_FLUX_DESC,
    FluxSweepParameters,
    flux_sweep,
    foreign_flux_source,
)
from ._sim import stable_seed
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register
from ._flux_component import FluxComponentParameters


class ResonatorSpectroscopyFluxParameters(
    TargetSelection, AveragingParameters, FluxSweepParameters, FluxComponentParameters
):
    """Inputs for the resonator-vs-flux map."""

    frequency_span_hz: float = Field(20e6, gt=0, description="Total readout-detuning span around the current readout_freq.")
    num_freq_points: int = Field(101, gt=1, description="Number of frequency points.")
    # capability default narrowed: the dispersive fit needs >= 5 good slices
    num_flux_points: int = Field(21, gt=4, description=NUM_FLUX_DESC + " (the dispersive fit needs >= 5 good slices).")
    f_q_max_hz: float | None = Field(
        None, description="Qubit maximum frequency (Hz) to hold fixed in the dispersive fit; None = estimator heuristic. Ignored by the 'sine' method."
    )
    analysis_method: Literal["dispersive", "sine"] = Field(
        "dispersive",
        description=(
            "Flux-model fit: 'dispersive' = full flux-tunable-transmon model "
            "f_r = f_r0 + g^2/(f_r0 - f_q(flux)) — yields bare f_r0 and (conditional) "
            "coupling g on top of the sweet-spot flux + period. 'sine' = a bare "
            "cosine of the flux — model-light and robust when only ~one arch is "
            "visible or the trace is noisy, but yields no f_r0/g (so f_r0_hz/g_hz are "
            "never proposed)."
        ),
    )
    edge_margin_frac: float = Field(
        0.06, ge=0, lt=0.5,
        description=(
            "Reject per-flux dip centres pinned within this fraction of the swept "
            "detuning window of either edge before the flux-model fit. Edge-pinned "
            "centres from low-SNR slices otherwise capture the fit seed and pull the "
            "sweet spot to the wrong flux. 0 disables."
        ),
    )
    dip_method: Literal["lorentzian", "circle"] = Field(
        "lorentzian",
        description=(
            "Per-slice dip fit: 'lorentzian' = joint Lorentzian + background fit of "
            "|IQ|^2 (fast, magnitude-only). 'circle' = Probst notch-model fit of the "
            "complex S21 — handles Fano-asymmetric dips, but needs meaningful phase "
            "data (on the simulated backend, whose Q quadrature is noise, slices fall "
            "back to the coarse argmin centre)."
        ),
    )


class ResonatorSpectroscopyFluxResult(Result):
    """``fit[qubit]``: ``flux_offset`` (upper sweet-spot flux), ``sweet_spot_res_hz``
    (resonator centre freq there), ``sweet_spot_low_flux_v``/``sweet_spot_low_res_hz``
    (the LOWER sweet spot — record-only, derivable as flux_offset ± flux_per_phi0/2),
    ``flux_per_phi0`` (flux period), plus
    ``f_r0_hz``/``g_hz`` for the dispersive method only. ``update()`` proposes the
    physical facts on the qubit's flux channel (flux_offset/flux_per_phi0) and
    resonator mode (f_r0_hz/g_hz), and two operating-point channel knobs:
    ``idle_flux`` on the flux channel (= flux_offset; park at the sweet spot) and
    ``readout_freq_hz`` on the readout channel (= sweet_spot_res_hz; read out at
    the resonator dip there)."""


@register
class ResonatorSpectroscopyFlux(Experiment):
    """Backend-agnostic resonator flux map. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "resonator_spectroscopy_flux"
    description: ClassVar[str] = (
        "2D resonator spectroscopy vs flux bias: tracks the dip at every flux and fits "
        "its flux dependence with a selectable model (analysis_method='dispersive' or "
        "'sine'); proposes the sweet-spot flux (flux_offset) + flux period "
        "(flux_per_phi0) as physical facts on the qubit's flux channel, and "
        "sets the operating point at the sweet spot (the flux channel's "
        "idle_flux = flux_offset, the readout channel's readout_freq_hz = "
        "resonator dip there) — plus bare f_r0_hz and coupling g_hz on the "
        "attached resonator mode "
        "when the dispersive method ran with f_q_max_hz supplied (an unconstrained fit "
        "only ASSUMES f_q_max; assumptions are not recorded as physics)."
    )
    Parameters: ClassVar[type] = ResonatorSpectroscopyFluxParameters
    Result: ClassVar[type] = ResonatorSpectroscopyFluxResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("flux_bias_v", "detuning_hz"), sweep_units=("V", "Hz"), variables=("I", "Q")
    )
    required_operations: ClassVar[tuple[str, ...]] = ("readout", "flux_bias")

    params: ResonatorSpectroscopyFluxParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        span = self.params.frequency_span_hz
        return {
            **flux_sweep(self.params),
            "detuning_hz": np.linspace(-span / 2, span / 2, self.params.num_freq_points),
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        flux = coords["flux_bias_v"]
        detuning = coords["detuning_hz"]
        targets = self.params.targets
        rng = np.random.default_rng(stable_seed("resonator_spectroscopy_flux", *targets))
        kappa = (detuning[-1] - detuning[0]) / 40
        ec = 0.2  # GHz
        i_data = np.empty((len(targets), flux.size, detuning.size))
        q_data = np.empty_like(i_data)
        for k, q in enumerate(targets):
            # centers generated FROM the dispersive model the estimator fits
            readout_now = float(self.device.channel(q, "readout").readout_freq_hz)
            f_q_max = float(self.device.channel(q, "drive").drive_freq_hz)
            sweet = rng.uniform(0.3 * flux.min(), 0.3 * flux.max())
            period = rng.uniform(1.8, 2.6) * (flux.max() - flux.min())
            g = rng.uniform(70e6, 100e6)
            f_r0 = readout_now - 2e6  # bare resonator (dressed sits above for f_q < f_r0)
            ej_sum = ((f_q_max * 1e-9 + ec) ** 2) / (8.0 * ec)
            quan = (flux - sweet) / period
            f_q = (np.sqrt(8.0 * ec * ej_sum * np.abs(np.cos(np.pi * quan))) - ec) * 1e9
            centers = (f_r0 + g**2 / (f_r0 - f_q)) - readout_now  # as detuning
            noise = 0.01
            for j in range(flux.size):
                magnitude = 1.0 - 0.75 / (1.0 + ((detuning - centers[j]) / kappa) ** 2)
                i_data[k, j] = magnitude + rng.normal(0, noise, detuning.size)
                q_data[k, j] = rng.normal(0, noise, detuning.size)
        return {"I": i_data, "Q": q_data}

    def estimate(self) -> ResonatorSpectroscopyFluxResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.resonator_spectroscopy_flux import ResonatorSpectroscopyFluxEstimator

        targets = list(self.dataset["target"].values)
        old_freqs = {
            q: float(self.device.channel(q, "readout").readout_freq_hz) for q in targets
        }
        prepared = self.dataset.rename({"flux_bias_v": "flux_bias", "detuning_hz": "detuning"})
        prepared = prepared.transpose("target", "flux_bias", "detuning")
        detuning = prepared["detuning"].values
        full_freq = np.array([detuning + old_freqs[q] for q in targets])
        prepared = prepared.assign_coords(full_freq=(("target", "detuning"), full_freq))

        kwargs = {
            "method": self.params.analysis_method,
            "dip_method": self.params.dip_method,
            "edge_margin_frac": float(self.params.edge_margin_frac),
        }
        if self.params.f_q_max_hz is not None:
            kwargs["f_q_max"] = float(self.params.f_q_max_hz)
        results = per_qubit_results(
            prepared, ResonatorSpectroscopyFluxEstimator(), artifact_dir=self.artifact_dir, **kwargs
        )

        result = ResonatorSpectroscopyFluxResult()
        for qubit in self.params.targets:
            disp = results[qubit]["dispersion"]
            vs = results[qubit]["vs_flux"]
            fit = {
                "flux_offset": float(disp["sweet_spot_flux"]),
                "sweet_spot_res_hz": float(disp["sweet_spot_res"]),
                "sweet_spot_low_flux_v": float(disp["sweet_spot_low_flux"]),
                "sweet_spot_low_res_hz": float(disp["sweet_spot_low_res"]),
                "flux_per_phi0": float(disp["dv_phi0"]),
                "n_good_flux": int(vs["n_good"]),
                "old_readout_freq_hz": old_freqs[qubit],
            }
            # Dispersive-only physics — the sine method produces no f_r0/g/f_q_max.
            for src, dst in (("f_r0", "f_r0_hz"), ("g", "g_hz"), ("f_q_max", "f_q_max_hz")):
                if src in disp:
                    fit[dst] = float(disp[src])
            result.fit[qubit] = fit
            result.outcomes[qubit] = Outcome.SUCCESSFUL if bool(disp["success"]) else Outcome.FAILED
        return result

    def update(self) -> None:
        """Propose the flux-model quantities: physical facts + operating-point knobs.

        Sweet-spot flux + flux period are always proposed (robust flux-periodicity,
        produced by every method) as ``flux_offset``/``flux_per_phi0`` on the
        qubit's flux CHANNEL (PHYSICAL facts). Two pushed channel knobs set the
        operating point at the sweet spot: the flux channel's ``idle_flux`` =
        ``flux_offset`` (park at the upper sweet spot) and the readout channel's
        ``readout_freq_hz`` = ``sweet_spot_res_hz`` (read out at the resonator dip
        there — a later readout_frequency run refines it for fidelity).
        ``f_r0_hz`` / ``g_hz`` (both on the attached resonator mode) are proposed
        only when the DISPERSIVE method ran AND the caller supplied ``f_q_max_hz``:
        the sine method yields no such physics, and without a known f_q_max the
        estimator holds it at a placeholder guess and g is conditional on that
        assumption — an assumed value must never enter the measured-physics ledger.
        ``f_q_max_hz`` itself is never proposed here (it is an INPUT of the
        dispersive fit; qubit_spectroscopy_flux_pulse measures it).
        """
        if self.result is None:
            return
        if foreign_flux_source(self.params):
            # Crosstalk / coupler-shift data, not the target's own arch — record-only.
            return
        # f_r0/g are physical only from the dispersive model with a known f_q_max.
        constrained = (
            self.params.analysis_method == "dispersive"
            and self.params.f_q_max_hz is not None
        )
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is not Outcome.SUCCESSFUL:
                continue
            flux_view = self.device.channel(qubit, "flux")
            for field in ("flux_offset", "flux_per_phi0"):
                if field in fit:
                    setattr(flux_view, field, fit[field])
            # Set up the operating point at the sweet spot — two pushed channel
            # knobs: park the standing idle flux at the sweet-spot bias
            # (idle_flux = flux_offset on the flux channel), and read out at the
            # resonator dip there (readout_freq_hz = sweet_spot_res_hz on the
            # readout channel). The flux channel exists because an own-flux run
            # requires the flux_bias operation (the target is flux-tunable); a
            # later readout_frequency run refines readout_freq_hz.
            if "flux_offset" in fit:
                flux_view.idle_flux = fit["flux_offset"]
            if "sweet_spot_res_hz" in fit:
                self.device.channel(qubit, "readout").readout_freq_hz = (
                    fit["sweet_spot_res_hz"])
            if constrained:
                res_view = self.device.component(self.device.resonator_of(qubit))
                if "f_r0_hz" in fit:
                    res_view.f_r0_hz = fit["f_r0_hz"]
                if "g_hz" in fit:
                    res_view.g_hz = fit["g_hz"]

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
