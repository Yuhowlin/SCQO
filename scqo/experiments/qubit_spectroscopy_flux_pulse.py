"""Qubit spectroscopy vs PULSED flux — the f01(flux) arch (backend-free half).

2D map: sweep the flux bias on the qubit's own line x the drive detuning around
the current ``drive_freq``, find the 0-1 peak at every flux and fit the transmon
arch ``f = sqrt(8*Ec*Ej_eff) - Ec``. Reports the sweet spot, flux period and
``Ej_sum`` — the f01(flux) inputs that Phase-3 device-level EJ/EC inference
consumes. ``update()`` proposes them as PHYSICAL parameters (sample physics,
``physical.json`` on accept — see scqo.physical): ``ej_sum_hz``/``f_q_max_hz``
on the target transmon, ``v_offset_v``/``v_per_phi0_v`` on the qubit's ZControl
component; nothing is pushed to any instrument, and the fits also stay queryable
in the run index (``fit_trend``).

PROBE CONTRACT — the ``_pulse`` in the name is the acquisition style: the flux
bias is applied ONLY while the saturation drive plays, and the line is back at
its idle value BEFORE every readout. Every slice therefore reads out at the SAME
(idle-flux) condition, so the analysis reduces the whole map against ONE global
radial reference (``ref_scope="global"``: the complex median over every point —
n_flux x more samples than a per-slice median, and the readout condition equals
the one single_shot_readout calibrates). A probe that HOLDS the flux through
readout must NOT register under this name — there the resonator is pulled during
the measurement, the ground point moves with flux, and a global reference can
null a weak line (a DC-held variant belongs to a separate experiment with the
per-slice reduction).

Flux safety: the flux axis is in volts on the qubit's flux line, bounded to
|V| <= 0.5 by the parameter schema; probes must return the line to idle before
each readout (the contract above) and after the sweep.

Drive power: the weak saturation drive is a per-run STIMULUS set via
``drive_power_dbm`` (a recorded boundary write, reverted after — the same
discipline as ``qubit_spectroscopy``); both backends play a saturation drive at
that absolute power (no calibrated pi pulse is needed).
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from pydantic import Field

from .._scqat import per_qubit_results
from ._capabilities import (
    NUM_FLUX_DESC,
    FluxComponentParameters,
    FluxSweepParameters,
    flux_sweep,
    foreign_flux_source,
)
from ._drive_power import drive_power_boundary
from ._sim import stable_seed
from ..contract import DatasetContract
from ..experiment import Experiment
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result


class QubitSpectroscopyFluxPulseParameters(
    TargetSelection, AveragingParameters, FluxSweepParameters, FluxComponentParameters
):
    """Inputs for the qubit-frequency-vs-flux map."""

    frequency_span_hz: float = Field(400e6, gt=0, description="Total drive-detuning span around the current drive_freq.")
    num_freq_points: int = Field(101, gt=1, description="Number of frequency points.")
    # capability default narrowed: the arch fit needs >= 5 good slices
    num_flux_points: int = Field(21, gt=4, description=NUM_FLUX_DESC + " (the arch fit needs >= 5 good slices).")
    ec_ghz: float = Field(0.2, gt=0, description="Charging energy (GHz) held fixed in the arch model.")
    drive_power_dbm: float = Field(
        -25.0,
        le=10.0,
        description="Absolute saturation-drive power (dBm at the instrument drive port), set "
        "as a recorded boundary write through the drive chain before the flux map and reverted "
        "after. QM caps at +10 dBm; Qblox above ~-1 dBm needs amplitude > 0.5.",
    )


class QubitSpectroscopyFluxPulseResult(Result):
    """``fit[qubit]``: ``v_offset_v``, ``f01_at_sweet_spot_hz``, ``v_per_phi0_v``,
    ``ej_sum_hz`` (+ stderrs). ``update()`` proposes them as physical parameters:
    ej_sum_hz/f_q_max_hz on the transmon, v_offset_v/v_per_phi0_v on the qubit's
    ZControl component."""


class QubitSpectroscopyFluxPulse(Experiment):
    """Backend-agnostic f01(flux) arch. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "qubit_spectroscopy_flux_pulse"
    description: ClassVar[str] = (
        "2D qubit spectroscopy vs PULSED flux (bias applied only during the drive; "
        "readout at idle flux every slice, reduced against one global IQ reference): "
        "finds the 0-1 peak at every flux and fits the transmon arch; proposes sweet "
        "spot (v_offset_v), flux period (v_per_phi0_v) on the qubit's ZControl "
        "component and ej_sum_hz/f_q_max_hz on the transmon as physical parameters "
        "(the Phase-3 EJ/EC inference inputs — no instrument knob). flux_component "
        "may name a qubit z-line only (the probe plays z pulses) — coupler maps "
        "belong to resonator_spectroscopy_flux."
    )
    Parameters: ClassVar[type] = QubitSpectroscopyFluxPulseParameters
    Result: ClassVar[type] = QubitSpectroscopyFluxPulseResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("flux_bias_v", "detuning_hz"), sweep_units=("V", "Hz"), variables=("I", "Q")
    )
    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout", "flux_bias")
    #: the probe plays z PULSES — only a qubit z-line is sweepable here
    flux_component_categories: ClassVar[tuple[str, ...]] = ("ReadableTransmon",)

    params: QubitSpectroscopyFluxPulseParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        span = self.params.frequency_span_hz
        return {
            **flux_sweep(self.params),
            "detuning_hz": np.linspace(-span / 2, span / 2, self.params.num_freq_points),
        }

    def run(self) -> Result:
        """Boundary-recorded drive-chain set -> acquire -> revert (shared helper).

        Same saturation-power stimulus discipline as ``qubit_spectroscopy``:
        ``drive_power_boundary`` parks ``drive_power_dbm`` before the flux map and
        reverts it exactly afterwards, so both backends drive the arch sweep at the
        requested absolute power (QM: the saturation op; Qblox: the CW spec drive).
        The arch proposals are physical facts, unrelated to this stimulus.
        """
        self.sweep_axes = self.define_sweep()
        with drive_power_boundary(self, self.params.drive_power_dbm):
            self.dataset = self.backend.acquire(self)
        self.Contract.validate(self.dataset)
        self.result = self.estimate()
        return self.result

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        flux = coords["flux_bias_v"]
        detuning = coords["detuning_hz"]
        targets = self.params.targets
        rng = np.random.default_rng(stable_seed("qubit_spectroscopy_flux_pulse", *targets))
        ec = self.params.ec_ghz
        i_data = np.empty((len(targets), flux.size, detuning.size))
        q_data = np.empty_like(i_data)
        for k, q in enumerate(targets):
            # hidden arch: sweet spot inside the swept window, top of the arch at
            # the current drive_freq (detuning 0) so the peak stays in-window
            f01_now = float(self.device.component(q).drive_freq)
            sweet = rng.uniform(0.3 * flux.min(), 0.3 * flux.max())
            period = rng.uniform(1.5, 2.5) * (flux.max() - flux.min())
            ej_sum = ((f01_now * 1e-9 + ec) ** 2) / (8.0 * ec)  # arch top == f01_now
            quan = (flux - sweet) / period
            f01_ghz = np.sqrt(8.0 * ec * ej_sum * np.abs(np.cos(np.pi * quan))) - ec
            centers = f01_ghz * 1e9 - f01_now  # as detuning
            fwhm = (detuning[-1] - detuning[0]) / 40
            noise = 0.02
            for j in range(flux.size):
                peak = 1.0 / (1.0 + ((detuning - centers[j]) / (fwhm / 2)) ** 2)
                i_data[k, j] = peak + rng.normal(0, noise, detuning.size)
                q_data[k, j] = rng.normal(0, noise, detuning.size)
        return {"I": i_data, "Q": q_data}

    def estimate(self) -> QubitSpectroscopyFluxPulseResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.qubit_flux_arch import QubitFluxArchEstimator

        # scqat's contract: coords `flux_bias` + `detuning` + absolute `full_freq`
        # (the arch model is absolute-scale), vars I/Q; dims (flux_bias, detuning).
        targets = list(self.dataset["target"].values)
        old_freqs = {q: float(self.device.component(q).drive_freq) for q in targets}
        prepared = self.dataset.rename({"flux_bias_v": "flux_bias", "detuning_hz": "detuning"})
        prepared = prepared.transpose("target", "flux_bias", "detuning")
        detuning = prepared["detuning"].values
        full_freq = np.array([detuning + old_freqs[q] for q in targets])
        prepared = prepared.assign_coords(full_freq=(("target", "detuning"), full_freq))

        results = per_qubit_results(
            prepared,
            QubitFluxArchEstimator(),
            artifact_dir=self.artifact_dir,
            ec_ghz=self.params.ec_ghz,
            # the _pulse probe contract: readout at idle flux every slice -> the
            # ground point is common across the map, so ONE global complex median
            # is the reference (stabler than per-slice; see the module docstring)
            ref_scope="global",
        )

        result = QubitSpectroscopyFluxPulseResult()
        for qubit in self.params.targets:
            arch = results[qubit]["arch"]
            fit: dict[str, float] = {"ec_ghz_assumed": float(arch["ec_ghz"])}
            for src, dst in (
                ("sweet_spot_flux", "v_offset_v"),
                ("flux_period", "v_per_phi0_v"),
                ("f01_max_hz", "f01_at_sweet_spot_hz"),
                ("offset_stderr", "v_offset_stderr_v"),
            ):
                if src in arch:
                    fit[dst] = float(arch[src])
            # the estimator reports Ej_sum in GHz; the physical field is in Hz
            if "ej_sum_ghz" in arch:
                fit["ej_sum_hz"] = float(arch["ej_sum_ghz"]) * 1e9
            if "ej_sum_stderr_ghz" in arch:
                fit["ej_sum_stderr_hz"] = float(arch["ej_sum_stderr_ghz"]) * 1e9
            fit["old_drive_freq"] = old_freqs[qubit]
            result.fit[qubit] = fit
            result.outcomes[qubit] = Outcome.SUCCESSFUL if bool(arch["success"]) else Outcome.FAILED
        return result

    def update(self) -> None:
        """Propose the arch parameters as PHYSICAL fields (sample physics, no vendor).

        ``ej_sum_hz`` and ``f01_at_sweet_spot_hz`` (as ``f_q_max_hz``) land on the
        target transmon; the volts-to-flux transfer quantities ``v_offset_v`` /
        ``v_per_phi0_v`` land on the qubit's ZControl component (the same
        quantities the resonator flux map measures).
        """
        if self.result is None:
            return
        if foreign_flux_source(self.params):
            # Flux CROSSTALK data, not the target's own arch — record-only.
            return
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is not Outcome.SUCCESSFUL:
                continue
            view = self.device.component(qubit)
            for fit_key, field in (
                ("ej_sum_hz", "ej_sum_hz"),
                ("f01_at_sweet_spot_hz", "f_q_max_hz"),
            ):
                if fit_key in fit:  # the estimator adds arch keys conditionally
                    setattr(view, field, fit[fit_key])
            z_view = self.device.component(self.device.one(qubit, "ZControl"))
            for field in ("v_offset_v", "v_per_phi0_v"):
                if field in fit:
                    setattr(z_view, field, fit[field])
