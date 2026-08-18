"""Resonator spectroscopy vs flux — the dispersive flux map, greenfield.

Port of :mod:`scqo.experiments.resonator_spectroscopy_flux`. The physics
half is byte-for-byte; what moved is the device surface and the field
spellings: the flux-transfer facts land on the target's FLUX CHANNEL
under their new names (``v_offset_v`` -> ``flux_offset``,
``v_per_phi0_v`` -> ``flux_per_phi0``), the sweet-spot operating point is
two channel knobs (``idle_flux`` on the flux channel, ``readout_freq_hz``
on the readout channel), and the dispersive physics (``f_r0_hz``,
``g_hz``) goes on the attached RESONATOR mode.

FRAME (no ``_pulse`` in the name, plain ``FluxSweepParameters``): the probe
SETS the line's DC offset to each swept value, so the window is ABSOLUTE DAC
volts and its fitted ``flux_offset`` is already an absolute set-point — the
plane the ``_pulse`` experiments re-reference themselves onto. See
:mod:`._capabilities.flux`.
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
    flux_anchor_v,
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
        None,
        description=(
            "Qubit maximum frequency (Hz) to hold fixed in the dispersive fit. "
            "None (the normal case) AUTO-SOURCES it per target from the drive "
            "channel's standing drive_freq_hz — the arch-top proxy, exact while the "
            "qubit is parked at its sweet spot, and itself design-seeded at bring-up. "
            "Only when that is unavailable too (true bring-up, or a foreign flux "
            "source) does the estimator fall back to its placeholder heuristic, which "
            "assumes a fixed 1.5 GHz sweet-spot detuning and therefore yields a g that "
            "is NOT physics. Set this explicitly to override both. Ignored by the "
            "'sine' method."
        ),
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


#: where the dispersive fit's f_q_max came from, recorded per target in ``fit``.
#: Only a MEASURED origin makes the fitted f_r0/g physics — see :func:`_f_q_max_hz`.
F_Q_MAX_PARAM = "param"        # the caller supplied f_q_max_hz outright
F_Q_MAX_DRIVE = "drive_freq"   # the target's standing drive_freq_hz (arch-top proxy)
F_Q_MAX_ASSUMED = "assumed"    # nothing known — the estimator's placeholder heuristic

#: code-default terminals of the fact -> design -> default precedence for the two
#: fixed/seed inputs of the dispersive arch (the estimator carries the same
#: numbers as its own backstop; kept equal on purpose).
_DEFAULT_EC_HZ = 0.2e9         # typical transmon charging energy E_C
_DEFAULT_G_INIT_HZ = 50e6      # physical seed for the fitted coupling g


def _f_q_max_hz(experiment, target: str) -> tuple[float | None, str]:
    """The arch-top qubit frequency to hold FIXED in the dispersive fit, and where
    it came from.

    WHY THIS MATTERS MORE THAN IT LOOKS: ``f_q_max`` is not fitted (the trace fixes
    only the PRODUCT ``g^2 * f_q_max`` — see the estimator's degeneracy note), so an
    assumed value cannot be corrected by the data. The error lands in ``g``
    (``g^2 ~ max_pull * detuning``, so an overstated detuning inflates it) and in
    ``f_r0``, which the fit must push further below the trace to keep the model's
    top:bottom pull ratio; that is what makes the fitted curve dive under the data at
    the LOW sweet spot. Sourcing a measured value fixes both at once.

    Precedence: the explicit ``f_q_max_hz`` parameter, else the target's standing
    ``drive_freq_hz``, else ``None`` (the estimator applies its own placeholder).
    ``drive_freq_hz`` is the arch top exactly while the qubit is parked at its sweet
    spot, and its catalog ``design_source`` already hops to the mode's designed
    ``f_q_max_hz``, so :meth:`~scqo.experiment.Experiment.anchor` seeds it from
    design.toml at bring-up — the same proxy, and the same reason,
    ``qubit_spectroscopy_cryoscope`` uses for its arch curvature. A FOREIGN flux
    source is excluded: the arch being swept is then not the target's own, so the
    target's drive frequency says nothing about it.

    The auto-source is also declined for a qubit sitting ABOVE its resonator: the
    model pulls the resonator UP (``g^2 / (f_r0 - f_q)`` with ``f_r0 > f_q``, a bound
    the estimator enforces), so such a device is outside the dispersive method
    altogether and 'assumed' — which withholds f_r0/g rather than reporting a
    clamped fit as physics — is the honest answer. An EXPLICIT f_q_max_hz is never
    second-guessed.
    """
    if experiment.params.f_q_max_hz is not None:
        return float(experiment.params.f_q_max_hz), F_Q_MAX_PARAM
    if foreign_flux_source(experiment.params):
        return None, F_Q_MAX_ASSUMED
    try:
        value = float(experiment.anchor(target, "drive_freq_hz"))
    except (ValueError, KeyError, AttributeError):
        return None, F_Q_MAX_ASSUMED
    if not np.isfinite(value) or value <= 0.0:
        return None, F_Q_MAX_ASSUMED
    try:  # best-effort sanity gate; a missing readout frequency does not block
        resonator = float(experiment.anchor(target, "readout_freq_hz"))
    except (ValueError, KeyError, AttributeError):
        return value, F_Q_MAX_DRIVE
    if np.isfinite(resonator) and value >= resonator:
        return None, F_Q_MAX_ASSUMED
    return value, F_Q_MAX_DRIVE


def _arch_anchored(experiment, target: str, fit: dict) -> bool:
    """True when this target's dispersive fit was pinned to a MEASURED arch top —
    the gate on proposing ``f_r0_hz``/``g_hz``.

    Normally reads the ``f_q_max_source`` tag ``estimate()`` recorded. A CAMPAIGN
    finalize replays ``update()`` over the aggregated fit, which keeps only numeric
    quantities, so the tag is gone there — re-derive it from the same precedence
    rather than silently withholding physics the per-repeat runs did propose. That
    re-derivation reads device state through whatever surface ``update()`` is running
    against; any failure degrades to the withholding answer (the safe direction — an
    assumed value must never enter the ledger), never a finalize crash.
    """
    source = fit.get("f_q_max_source")
    if source is None:
        try:
            source = _f_q_max_hz(experiment, target)[1]
        except Exception:  # noqa: BLE001 - never fail a finalize over provenance
            source = F_Q_MAX_ASSUMED
    return source != F_Q_MAX_ASSUMED


class ResonatorSpectroscopyFluxResult(Result):
    """``fit[qubit]``: ``flux_offset`` (upper sweet-spot flux), ``sweet_spot_res_hz``
    (resonator centre freq there), ``sweet_spot_low_flux_v``/``sweet_spot_low_res_hz``
    (the LOWER sweet spot — record-only, derivable as flux_offset ± flux_per_phi0/2),
    ``flux_per_phi0`` (flux period), plus
    ``f_r0_hz``/``g_hz``/``f_q_max_hz`` and the ``f_q_max_source`` provenance tag
    (``param``/``drive_freq``/``assumed``) for the dispersive method only —
    ``assumed`` means f_r0_hz/g_hz are conditional on a placeholder detuning and are
    NOT proposed. ``update()`` proposes the
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
        "2D resonator spectroscopy vs ABSOLUTE flux bias (the probe sets the line's "
        "DC offset per point, so the window is DAC volts, not an excursion from "
        "idle_flux): tracks the dip at every flux and fits "
        "its flux dependence with a selectable model (analysis_method='dispersive' or "
        "'sine'); proposes the sweet-spot flux (flux_offset) + flux period "
        "(flux_per_phi0) as physical facts on the qubit's flux channel, and "
        "sets the operating point at the sweet spot (the flux channel's "
        "idle_flux = flux_offset, the readout channel's readout_freq_hz = "
        "resonator dip there). This is the BRING-UP seed for idle_flux, found "
        "without needing a prior operating point; once the qubit answers, "
        "qubit_spectroscopy_flux_pulse is the authority for that knob. "
        "Plus bare f_r0_hz and coupling g_hz on the "
        "attached resonator mode "
        "when the dispersive method ran against a MEASURED arch top — f_q_max_hz "
        "supplied, or auto-sourced from the target's standing drive_freq_hz. With "
        "neither the fit only ASSUMES f_q_max, and assumptions are not recorded as "
        "physics."
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
        i_data = np.empty((len(targets), flux.size, detuning.size))
        q_data = np.empty_like(i_data)
        for k, q in enumerate(targets):
            # centers generated FROM the dispersive model the estimator fits
            readout_now = float(self.device.channel(q, "readout").readout_freq_hz)
            f_q_max = float(self.device.channel(q, "drive").drive_freq_hz)
            # E_c from the same fact -> design -> default source estimate() uses,
            # so the forward model matches the arch the estimator fits (GHz here).
            ec = self.fact(q, "ec_hz", _DEFAULT_EC_HZ) * 1e-9
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
        # f_q_max, E_c and the g seed are per TARGET (each qubit sits at its own
        # detuning), so they ride per_target_kwargs rather than the shared set.
        # E_c and the g seed follow the fact -> design -> code-default precedence
        # (Experiment.fact): a stored ec_hz/g_hz wins, else design.toml, else the
        # code default. g_hz is re-seeded from what THIS experiment last wrote.
        sources: dict[str, str] = {}
        per_target: dict[str, dict] = {}
        for qubit in targets:
            q = str(qubit)
            value, source = _f_q_max_hz(self, q)
            sources[q] = source
            ptk: dict = {
                "ec": self.fact(q, "ec_hz", _DEFAULT_EC_HZ),
                "g_init": self.fact(q, "g_hz", _DEFAULT_G_INIT_HZ),
            }
            if value is not None:
                ptk["f_q_max"] = value
            per_target[q] = ptk
        results = per_qubit_results(
            prepared, ResonatorSpectroscopyFluxEstimator(), artifact_dir=self.artifact_dir,
            per_target_kwargs=per_target, **kwargs
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
                # The frame origin, 0.0 here: this probe SETS the DC offset, so
                # the window is measured from the DAC zero and whatever the line
                # was parked at is overridden point by point. Recorded anyway so
                # `flux_offset == old_idle_flux + <fitted in frame>` is one
                # invariant across the whole flux capability, both frames.
                "old_idle_flux": flux_anchor_v(self, qubit),
            }
            # Dispersive-only physics — the sine method produces no f_r0/g/f_q_max.
            # ec_hz is the sourced FIXED input (record-only provenance, never
            # proposed — an assumed/derived input is not measured physics).
            for src, dst in (("f_r0", "f_r0_hz"), ("g", "g_hz"),
                             ("f_q_max", "f_q_max_hz"), ("ec", "ec_hz")):
                if src in disp:
                    fit[dst] = float(disp[src])
            if "f_q_max_hz" in fit:
                # provenance of the fit's FIXED input: only a measured origin makes
                # the fitted f_r0_hz/g_hz physics (see update()).
                fit["f_q_max_source"] = sources.get(qubit, F_Q_MAX_ASSUMED)
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
        only when the DISPERSIVE method ran AND its ``f_q_max`` came from a MEASURED
        origin — the explicit ``f_q_max_hz`` parameter or the target's standing
        ``drive_freq_hz`` (``fit["f_q_max_source"]``, see :func:`_f_q_max_hz`). The
        sine method yields no such physics, and when nothing was known the estimator
        holds f_q_max at a placeholder guess that the trace cannot correct, so g is
        conditional on that assumption — an assumed value must never enter the
        measured-physics ledger. The gate is per QUBIT: on a multi-target run one
        qubit may be anchored and another not. ``f_q_max_hz`` itself is never
        proposed here (it is an INPUT of the dispersive fit;
        qubit_spectroscopy_flux_pulse measures it).
        """
        if self.result is None:
            return
        if foreign_flux_source(self.params):
            # Crosstalk / coupler-shift data, not the target's own arch — record-only.
            return
        # f_r0/g are physical only from the dispersive model with a MEASURED f_q_max.
        dispersive = self.params.analysis_method == "dispersive"
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
            if dispersive and _arch_anchored(self, qubit, fit):
                res_view = self.device.component(self.device.resonator_of(qubit))
                if "f_r0_hz" in fit:
                    res_view.f_r0_hz = fit["f_r0_hz"]
                if "g_hz" in fit:
                    res_view.g_hz = fit["g_hz"]

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
