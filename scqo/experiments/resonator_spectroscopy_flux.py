"""Resonator spectroscopy vs flux — the dispersive flux map (backend-free half).

2D map: sweep the flux bias x readout detuning, track the resonator dip at every
flux and fit its flux dependence with a selectable model (``dispersive`` — the
full f_r(flux) = f_r0 + g^2 / (f_r0 - f_q(flux)) transmon arch — or the
model-light ``sine``). Reports the sweet-spot flux (v_offset_v), the
flux period (v_per_phi0_v), and, for the dispersive method, the bare resonator
f_r0 and the coupling g — the resonator-side flux picture that pairs with
qubit_spectroscopy_flux for Phase-3 inference. ``update()`` proposes the
sweet-spot flux + flux period as PHYSICAL parameters on the qubit's ZControl
component (``physical.json`` on accept), and sets up the operating point at the
sweet spot via two pushed instrument knobs on the transmon: ``idle_flux_v`` =
v_offset_v (park at the sweet spot) and ``readout_freq`` = the resonator dip there
(a later resonator_spectroscopy/readout_frequency run refines it for fidelity).
f_r0_hz (on the Resonator component) and g_hz (on the ReadoutLine component) are
proposed only when the dispersive method ran AND ``f_q_max_hz`` was supplied — an
unconstrained fit holds f_q_max at a placeholder assumption, and assumed values
must not enter the measured-physics ledger.
"""

from __future__ import annotations

from typing import ClassVar, Literal

import numpy as np
from pydantic import Field

from .._scqat import per_qubit_results
from ._sim import stable_seed
from ..contract import DatasetContract
from ..experiment import Experiment
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result


class ResonatorSpectroscopyFluxParameters(TargetSelection, AveragingParameters):
    """Inputs for the resonator-vs-flux map."""

    frequency_span_hz: float = Field(20e6, gt=0, description="Total readout-detuning span around the current readout_freq.")
    num_freq_points: int = Field(101, gt=1, description="Number of frequency points.")
    min_flux_v: float = Field(-0.3, ge=-0.5, description="Lowest flux bias (V) on the qubit's own flux line.")
    max_flux_v: float = Field(0.3, le=0.5, description="Highest flux bias (V).")
    num_flux_points: int = Field(21, gt=4, description="Number of flux points (the dispersive fit needs >= 5 good slices).")
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
    flux_component: str | None = Field(
        None,
        description="Roster component whose flux line is swept INSTEAD of each target's "
                    "own z-line: a qubit name (its z) or a pair name (its tunable "
                    "coupler). None = each target fluxes itself. With an assigned "
                    "source the run is RECORD-ONLY (fits saved, zero suggestions): the "
                    "dispersive quantities then describe crosstalk / coupler-induced "
                    "shift, not the target's own flux arch.",
    )


class ResonatorSpectroscopyFluxResult(Result):
    """``fit[qubit]``: ``v_offset_v`` (upper sweet-spot flux), ``sweet_spot_res_hz``
    (resonator centre freq there), ``sweet_spot_low_flux_v``/``sweet_spot_low_res_hz``
    (the LOWER sweet spot — record-only, derivable as v_offset_v ± v_per_phi0_v/2),
    ``v_per_phi0_v`` (flux period), plus
    ``f_r0_hz``/``g_hz`` for the dispersive method only. ``update()`` proposes the
    physical facts on the qubit's ZControl (v_offset_v/v_per_phi0_v), Resonator
    (f_r0_hz) and ReadoutLine (g_hz) components, and two transmon operating-point
    knobs: ``idle_flux_v`` (= v_offset_v; park at the sweet spot) and
    ``readout_freq`` (= sweet_spot_res_hz; read out at the resonator dip there)."""


class ResonatorSpectroscopyFlux(Experiment):
    """Backend-agnostic resonator flux map. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "resonator_spectroscopy_flux"
    description: ClassVar[str] = (
        "2D resonator spectroscopy vs flux bias: tracks the dip at every flux and fits "
        "its flux dependence with a selectable model (analysis_method='dispersive' or "
        "'sine'); proposes the sweet-spot flux (v_offset_v) + flux period "
        "(v_per_phi0_v) as physical parameters on the qubit's ZControl component, and "
        "sets the transmon operating point at the sweet spot (idle_flux_v=v_offset_v, "
        "readout_freq=resonator dip there) — plus bare f_r0_hz (Resonator) and "
        "coupling g_hz (ReadoutLine) "
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
            "flux_bias_v": np.linspace(self.params.min_flux_v, self.params.max_flux_v, self.params.num_flux_points),
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
            readout_now = float(self.device.component(q).readout_freq)
            f_q_max = float(self.device.component(q).drive_freq)
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
        old_freqs = {q: float(self.device.component(q).readout_freq) for q in targets}
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
                "v_offset_v": float(disp["sweet_spot_flux"]),
                "sweet_spot_res_hz": float(disp["sweet_spot_res"]),
                "sweet_spot_low_flux_v": float(disp["sweet_spot_low_flux"]),
                "sweet_spot_low_res_hz": float(disp["sweet_spot_low_res"]),
                "v_per_phi0_v": float(disp["dv_phi0"]),
                "n_good_flux": int(vs["n_good"]),
                "old_readout_freq": old_freqs[qubit],
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
        produced by every method) as ``v_offset_v``/``v_per_phi0_v`` on the qubit's
        ZControl component (PHYSICAL facts). Two pushed instrument knobs on the
        transmon set the operating point at the sweet spot: ``idle_flux_v`` =
        ``v_offset_v`` (park at the upper sweet spot) and ``readout_freq`` =
        ``sweet_spot_res_hz`` (read out at the resonator dip there — a later
        readout_frequency run refines it for fidelity).
        ``f_r0_hz`` (Resonator) / ``g_hz`` (ReadoutLine) are proposed only when the
        DISPERSIVE method ran AND the caller supplied ``f_q_max_hz``: the sine
        method yields no such physics, and without a known f_q_max the estimator
        holds it at a placeholder guess and g is conditional on that assumption —
        an assumed value must never enter the measured-physics ledger.
        ``f_q_max_hz`` itself is never proposed here (it is an INPUT of the
        dispersive fit; qubit_spectroscopy_flux measures it).
        """
        if self.result is None:
            return
        if self.params.flux_component is not None:
            # Foreign flux source (neighbor z or a pair's coupler): the fitted
            # quantities are crosstalk / coupler-shift data, NOT the target's own
            # arch — record-only, the fits stay findable/trendable in result.fit.
            return
        # f_r0/g are physical only from the dispersive model with a known f_q_max.
        constrained = (
            self.params.analysis_method == "dispersive"
            and self.params.f_q_max_hz is not None
        )
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is not Outcome.SUCCESSFUL:
                continue
            z_view = self.device.component(self.device.one(qubit, "ZControl"))
            for field in ("v_offset_v", "v_per_phi0_v"):
                if field in fit:
                    setattr(z_view, field, fit[field])
            # Set up the operating point at the sweet spot — two pushed knobs on
            # the transmon: park the standing idle flux at the sweet-spot voltage
            # (idle_flux_v = v_offset_v), and read out at the resonator dip there
            # (readout_freq = sweet_spot_res_hz). idle_flux_v exists because an
            # own-flux run requires the flux_bias operation (the target is a
            # FluxTunableTransmon); a later readout_frequency run refines readout_freq.
            q_view = self.device.component(qubit)
            if "v_offset_v" in fit:
                q_view.idle_flux_v = fit["v_offset_v"]
            if "sweet_spot_res_hz" in fit:
                q_view.readout_freq = fit["sweet_spot_res_hz"]
            if constrained:
                if "f_r0_hz" in fit:
                    res_view = self.device.component(self.device.one(qubit, "Resonator"))
                    res_view.f_r0_hz = fit["f_r0_hz"]
                if "g_hz" in fit:
                    ro_view = self.device.component(self.device.one(qubit, "ReadoutLine"))
                    ro_view.g_hz = fit["g_hz"]
