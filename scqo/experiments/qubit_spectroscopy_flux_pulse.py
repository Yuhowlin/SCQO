"""Qubit spectroscopy vs PULSED flux — the f01(flux) arch, greenfield.

Port of :mod:`scqo.experiments.qubit_spectroscopy_flux_pulse` (the probe
contract — flux applied only while the drive plays, readout at idle flux
every slice, one global IQ reference — is documented there and unchanged).
The physics half is byte-for-byte; what moved is the device surface: the
drive anchor is read from the target's DRIVE CHANNEL (``drive_freq_hz``),
the arch facts (``ej_sum_hz``, ``f_q_max_hz``) land on the target mode, and
the volts-to-flux transfer function lands on the target's FLUX CHANNEL as
``flux_offset`` / ``flux_per_phi0`` (formerly ``v_offset_v`` /
``v_per_phi0_v`` on the ZControl component).

FRAME (``_pulse`` in the name, ``FluxPulseSweepParameters`` in the schema):
the flux is a PULSE the DAC adds to the standing bias, so the window is
measured from the channel's ``idle_flux`` and 0 means "stay parked".
``estimate()`` re-references the fitted excursion to an absolute set-point
before writing ``flux_offset`` — see :mod:`._capabilities.flux`.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from pydantic import Field

from .._scqat import per_qubit_results
from ..contract import DatasetContract
from ._capabilities.flux import (
    NUM_FLUX_DESC,
    FluxPulseSweepParameters,
    flux_anchor_v,
    flux_sweep,
    foreign_flux_source,
)
from ._sim import stable_seed
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register
from ._drive_power import drive_power_boundary
from ._flux_component import FluxComponentParameters


class QubitSpectroscopyFluxPulseParameters(
    TargetSelection, AveragingParameters, FluxPulseSweepParameters, FluxComponentParameters
):
    """Inputs for the qubit-frequency-vs-flux map.

    The flux window is RELATIVE to the swept channel's ``idle_flux`` (the probe
    plays a z pulse on top of the standing bias), so 0 means "stay parked".
    """

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
    """``fit[target]``: ``flux_offset``, ``f01_at_sweet_spot_hz``,
    ``flux_per_phi0``, ``ej_sum_hz`` (+ stderrs). ``update()`` proposes them
    as physical facts: ej_sum_hz/f_q_max_hz on the target mode,
    flux_offset/flux_per_phi0 on the target's flux channel — plus ``idle_flux``
    on that channel, the one knob.

    The window is idle-relative, so the sweet spot is reported TWICE, in the
    two frames, exactly as ``qubit_ramsey`` reports old/delta/new for the drive
    frequency: ``old_idle_flux`` (the standing bias the pulse rode on),
    ``flux_offset_from_idle`` (the fitted excursion, this run's own frame) and
    ``flux_offset = old_idle_flux + flux_offset_from_idle`` (ABSOLUTE — the
    value written to the fact and proposed as the new ``idle_flux``).
    ``flux_per_phi0`` is a period and every frequency is frame-invariant, so
    neither is re-referenced.
    """


@register
class QubitSpectroscopyFluxPulse(Experiment):
    """Backend-agnostic f01(flux) arch. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "qubit_spectroscopy_flux_pulse"
    description: ClassVar[str] = (
        "2D qubit spectroscopy vs PULSED flux (bias applied only during the drive; "
        "readout at idle flux every slice, reduced against one global IQ reference): "
        "finds the 0-1 peak at every flux and fits the transmon arch. The flux "
        "window is RELATIVE to the channel's idle_flux (0 = stay parked), so a "
        "well-tuned qubit maps an arch centred on 0. Proposes sweet spot "
        "(flux_offset, absolute), flux period (flux_per_phi0) on the target's flux "
        "channel and ej_sum_hz/f_q_max_hz on the target mode as physical facts "
        "(the Phase-3 EJ/EC inference inputs), plus idle_flux on the flux channel "
        "— accepting it RE-PARKS the qubit at the measured sweet spot, so the next "
        "map centres at 0. On a flux-tunable qubit this experiment is the "
        "AUTHORITY for idle_flux (it measures the qubit itself); "
        "resonator_spectroscopy_flux only seeds it during bring-up. A foreign "
        "flux_component (another qubit's z, a coupler's z) makes the run a "
        "RECORD-ONLY crosstalk/coupler-shift map — fits saved, zero "
        "suggestions."
    )
    Parameters: ClassVar[type] = QubitSpectroscopyFluxPulseParameters
    Result: ClassVar[type] = QubitSpectroscopyFluxPulseResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("flux_bias_v", "detuning_hz"), sweep_units=("V", "Hz"), variables=("I", "Q")
    )
    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout", "flux_bias")

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
            # the current drive_freq (detuning 0) so the peak stays in-window.
            # The window is idle-relative, so `sweet` reads as "this far from the
            # standing bias" — a qubit parked slightly off its sweet spot, which
            # is exactly the condition this experiment exists to correct.
            f01_now = float(self.device.channel(q, "drive").drive_freq_hz)
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
        old_freqs = {q: float(self.device.channel(q, "drive").drive_freq_hz) for q in targets}
        # The window rode on this standing bias (0.0 would be the absolute frame);
        # it is what turns the fitted excursion back into an absolute set-point.
        old_idle = {q: flux_anchor_v(self, q) for q in targets}
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
                # the estimator fits in the SWEPT frame, so its sweet spot is an
                # excursion from the standing bias until re-referenced below
                ("sweet_spot_flux", "flux_offset_from_idle"),
                ("flux_period", "flux_per_phi0"),
                ("f01_max_hz", "f01_at_sweet_spot_hz"),
                ("offset_stderr", "flux_offset_stderr"),
            ):
                if src in arch:
                    fit[dst] = float(arch[src])
            # the estimator reports Ej_sum in GHz; the physical field is in Hz
            if "ej_sum_ghz" in arch:
                fit["ej_sum_hz"] = float(arch["ej_sum_ghz"]) * 1e9
            if "ej_sum_stderr_ghz" in arch:
                fit["ej_sum_stderr_hz"] = float(arch["ej_sum_stderr_ghz"]) * 1e9
            fit["old_drive_freq_hz"] = old_freqs[qubit]
            # The frame declaration, as a number: unconditional, so a run always
            # says where it was parked even when the arch fit found nothing.
            fit["old_idle_flux"] = old_idle[qubit]
            if "flux_offset_from_idle" in fit:  # the arch keys are conditional
                fit["flux_offset"] = old_idle[qubit] + fit["flux_offset_from_idle"]
            result.fit[qubit] = fit
            result.outcomes[qubit] = Outcome.SUCCESSFUL if bool(arch["success"]) else Outcome.FAILED
        return result

    def update(self) -> None:
        """Propose the arch parameters as PHYSICAL facts, then re-park the qubit.

        ``ej_sum_hz`` and ``f01_at_sweet_spot_hz`` (as ``f_q_max_hz``) land on the
        target mode; the volts-to-flux transfer quantities ``flux_offset`` /
        ``flux_per_phi0`` land on the target's flux channel (the same
        quantities the resonator flux map measures).

        Facts first, then the one KNOB: ``idle_flux`` takes the same number as
        ``flux_offset``, because the measured sweet spot IS where the qubit
        should stand — the identical move ``resonator_spectroscopy_flux`` makes.
        Accepting therefore re-parks the operating point, and the next run of
        this experiment maps an arch centred on 0. Both are absolute (the
        re-reference happened in ``estimate()``), so the knob and the fact stay
        on one plane.
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
            flux_view = self.device.channel(qubit, "flux")
            for field in ("flux_offset", "flux_per_phi0"):
                if field in fit:
                    setattr(flux_view, field, fit[field])
            # ... and the operating point itself: park at the measured sweet spot.
            if "flux_offset" in fit:
                flux_view.idle_flux = fit["flux_offset"]

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
