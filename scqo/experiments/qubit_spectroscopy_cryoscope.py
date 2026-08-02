"""Qubit spectroscopy cryoscope — LONG-TIME flux step response via spectroscopy.

The complement of :mod:`qubit_ramsey_cryoscope`. Where the Ramsey cryoscope
reconstructs a phase and so is limited to the short (ns) transients before the
phase wraps, this experiment measures the qubit frequency DIRECTLY by
spectroscopy at each wait-time INTO a parked flux pulse, reaching the
microsecond tails (bias-tee droop, slow wiring transients). The sequence per
(detuning, wait) point: reset -> set the drive near the parked qubit frequency
-> play a long flux ``const`` pulse -> wait ``t`` into it -> play an x180
spectroscopy pulse -> read out. Per wait-time the spectroscopy peak center gives
the qubit's frequency, which maps to the delivered flux; the flux settling is fit
to a sum of exponentials -> the predistortion tap ``(amplitude, tau)`` components.

BAND-LIMITED v1: the drive is centered on the arch-predicted parked detuning
(``resolved_center_offset_hz``, derived from the catalogued arch facts, or a
nominal fallback), NOT by shifting the LO. It therefore reaches only excursions
whose detuning stays in the analog band; a gate-sized excursion needs an LO
shift, which is deferred. The detuning window is a symmetric span around that
center.

FRAME (deliberately NOT a ``_pulse`` experiment): the flux amplitude is a SCALAR
parameter ``flux_pulse_amp_v`` (volts RELATIVE to idle_flux — the pulse rides on
the standing bias), not a swept window, so this experiment does not subclass the
flux capability mixins and carries no ``flux`` tag. The swept axes are the drive
detuning and the (log-spaced) wait time.

WRITEBACK: accepting proposes the SAME paired flux-channel FACTS the Ramsey
cryoscope writes — ``distortion_amp`` (dimensionless, ``amp / a_dc``) and
``distortion_tau_s`` (seconds) — with whole-array REPLACE semantics. The two
cryoscopes are INDEPENDENT full measurements (short vs long timescale); accepting
one overwrites the other's taps (last-writer-wins), it does NOT compose them.
Nothing is pushed to the vendor: applying the taps to the instrument's output
filter stays a manual vendor-config step.

QM-only today: the base ``probe()`` raises, and only LCHQMDriver supplies one.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from pydantic import Field, field_validator, model_validator

from ..contract import DatasetContract
from ..experiment import Experiment
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from ._capabilities.qubit_reset import QubitResetParameters
from ._capabilities.state_readout import (
    STATE_ALT,
    StateReadoutParameters,
    readout_vars,
    signal_rename,
    state_row,
)
from ._sim import iq_from_population, stable_seed
from . import register

#: axis names crossing the probe <-> estimator boundary.
DETUNING_AXIS = "detuning_hz"
WAIT_AXIS = "wait_time_ns"


def _log_time_ns(min_ns: int, max_ns: int, num_points: int, *, grid_ns: int = 4) -> np.ndarray:
    """Log-spaced wait times in ns, snapped to the QM grid (>= 16 ns, a multiple
    of ``grid_ns``) and de-duplicated.

    Non-uniform on purpose: the estimator fits exponentials (no derivative, unlike
    the Ramsey cryoscope's Savitzky-Golay step), so a log axis can span the ns
    transients and the microsecond tails in one sweep. The QM 4 ns / 16 ns wait
    floor still binds — the probe plays ``xy.wait`` in cycles — so the axis is
    kept integer ns.
    """
    lo = max(16, int(min_ns))
    raw = np.logspace(np.log10(lo), np.log10(int(max_ns)), int(num_points))
    snapped = np.maximum(16, (np.round(raw / grid_ns) * grid_ns).astype(int))
    return np.unique(snapped).astype(float)


class QubitSpectroscopyCryoscopeParameters(
    TargetSelection, AveragingParameters, StateReadoutParameters, QubitResetParameters
):
    """Parameters for the long-time (spectroscopy) cryoscope."""

    flux_pulse_amp_v: float = Field(
        0.1,
        description="Parked flux-pulse amplitude in volts, RELATIVE to the flux "
        "channel's idle_flux (the pulse rides on the standing bias). The qubit "
        "sits at this excursion for the whole wait sweep; the drive is centered on "
        "the resulting detuning. Band-limited v1: keep it small enough that the "
        "detuning stays in the analog band (no LO shift).",
    )
    frequency_span_hz: float = Field(
        200e6, gt=0,
        description="Total drive-detuning span swept around the arch-predicted "
        "parked frequency (symmetric). Widen it if the peak is not captured "
        "(e.g. when the arch facts are unset and the center is only nominal).",
    )
    num_freq_points: int = Field(101, gt=1, description="Number of detuning points.")
    min_wait_ns: int = Field(
        16, ge=16,
        description="Shortest wait into the flux pulse, ns (>= the 16 ns QM floor).",
    )
    max_wait_ns: int = Field(
        20000, gt=16,
        description="Longest wait into the flux pulse, ns — set it a few times the "
        "slowest expected tail (bias-tee droop reaches microseconds).",
    )
    num_wait_points: int = Field(
        50, gt=4, description="Number of LOG-spaced wait points (deduplicated on the 4 ns grid)."
    )
    ec_ghz: float = Field(
        0.2, gt=0,
        description="Charging energy (GHz) used with the arch facts to predict the "
        "parked detuning that centers the drive.",
    )
    fallback_curvature_hz_per_v2: float = Field(
        -1.0e9,
        description="Nominal flux-frequency curvature (Hz per V^2) used to center "
        "the drive when the arch facts (f_q_max_hz, flux_per_phi0) are unset. Only "
        "a rough centering; measure the arch (qubit_spectroscopy_flux_pulse) for an "
        "accurate one, or widen frequency_span_hz.",
    )
    num_averages: int = Field(
        100, gt=0, description="Number of shots to average per sweep point."
    )
    fit_start_fractions: list[float] = Field(
        default_factory=lambda: [0.5, 0.05],
        description="DESCENDING 0-1 fractions of the record where each exponential "
        "component's fit starts (slowest first). Forwarded to the estimator; length "
        "sets the number of tap components.",
    )

    @field_validator("fit_start_fractions")
    @classmethod
    def _descending_fractions(cls, value: list[float]) -> list[float]:
        if not value or any(b >= a for a, b in zip(value, value[1:])):
            raise ValueError(
                "fit_start_fractions must be non-empty and strictly DESCENDING "
                f"(slowest component first), got {value}"
            )
        if any(not (0.0 < f < 1.0) for f in value):
            raise ValueError(f"fit_start_fractions must lie in (0, 1), got {value}")
        return value

    @model_validator(mode="after")
    def _wait_window_orders(self) -> "QubitSpectroscopyCryoscopeParameters":
        if self.max_wait_ns <= self.min_wait_ns:
            raise ValueError(
                f"max_wait_ns ({self.max_wait_ns}) must exceed min_wait_ns "
                f"({self.min_wait_ns})"
            )
        return self


class QubitSpectroscopyCryoscopeResult(Result):
    """Fitted long-time step-response results.

    ``fit[target]``: the paired tap arrays ``distortion_amp`` (relative,
    ``amp / a_dc``) and ``distortion_tau_s`` (seconds), the settled level
    ``a_dc``, the fit ``rms_residual``, the ``center_offset_hz`` the drive was
    parked on, the scalar ``old_idle_flux`` and ``flux_pulse_amp_v``.
    """


@register
class QubitSpectroscopyCryoscope(Experiment):
    """Measure the long-time flux step response by spectroscopy and propose taps."""

    name: ClassVar[str] = "qubit_spectroscopy_cryoscope"
    description: ClassVar[str] = (
        "Reconstruct the flux line's LONG-TIME (microsecond) step response by qubit "
        "spectroscopy vs wait-time into a parked flux pulse, and fit it to a sum of "
        "exponentials. The complement of qubit_ramsey_cryoscope: spectroscopy reads "
        "the frequency directly, so it reaches the slow bias-tee/wiring tails a "
        "phase measurement cannot. Proposes the flux channel's paired distortion "
        "facts (distortion_amp, relative; distortion_tau_s, seconds) — the same "
        "facts the Ramsey cryoscope writes, as an INDEPENDENT full measurement "
        "(REPLACE, not composed). Band-limited v1: the drive is centered on the "
        "arch-predicted parked detuning without an LO shift, so keep the excursion "
        "modest. Prerequisite: a rough arch (qubit_spectroscopy_flux_pulse) for "
        "accurate centering. QM-only probe today."
    )
    Parameters: ClassVar[type] = QubitSpectroscopyCryoscopeParameters
    Result: ClassVar[type] = QubitSpectroscopyCryoscopeResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=(DETUNING_AXIS, WAIT_AXIS),
        sweep_units=("Hz", "ns"),
        variables=("I", "Q"),
        alt_variables=STATE_ALT,
    )

    params: QubitSpectroscopyCryoscopeParameters

    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout", "flux_bias")

    # ------------------------------------------------------------ arch centering
    def _quad_term_hz_per_v2(self, target: str) -> float | None:
        """Local flux-frequency curvature d^2 f_01 / d v^2 (Hz/V^2) at the sweet
        spot, from CHANNEL fields, or None when they are unavailable.

        Near the sweet spot ``f_01 ~ f_max - (f_max + E_C) * pi^2 / 2 *
        (v / flux_per_phi0)^2``, so the curvature is
        ``-(f_max + E_C) * pi^2 / (2 * flux_per_phi0^2)``. The arch-top ``f_max``
        is a MODE fact, but modes carry no readable view here, so the drive
        channel's current ``drive_freq_hz`` is used as the proxy (exact when the
        qubit is parked at its sweet spot); ``flux_per_phi0`` is a flux-channel
        fact. A missing field falls back to the nominal curvature.
        """
        try:
            f01 = self.anchor(target, "drive_freq_hz")
            per_phi0 = self.anchor(target, "flux_per_phi0")
        except (ValueError, KeyError, AttributeError):
            return None
        if not (np.isfinite(f01) and np.isfinite(per_phi0)) or per_phi0 == 0.0:
            return None
        ec_hz = self.params.ec_ghz * 1e9
        return -(f01 + ec_hz) * np.pi ** 2 / (2.0 * per_phi0 ** 2)

    def resolved_center_offset_hz(self, target: str) -> float:
        """The parked detuning the drive is centered on: ``quad * flux_amp^2``,
        with ``quad`` arch-derived (:meth:`_quad_term_hz_per_v2`) or the nominal
        ``fallback_curvature_hz_per_v2``. Probe and estimator share this one value
        (the probe shifts the drive by it; the estimator adds it back to the fitted
        peak to recover the absolute detuning)."""
        quad = self._quad_term_hz_per_v2(target)
        if quad is None:
            quad = self.params.fallback_curvature_hz_per_v2
        return float(quad * self.params.flux_pulse_amp_v ** 2)

    def define_sweep(self) -> dict[str, np.ndarray]:
        span = self.params.frequency_span_hz
        detuning = np.linspace(-span / 2.0, span / 2.0, self.params.num_freq_points)
        wait = _log_time_ns(
            self.params.min_wait_ns, self.params.max_wait_ns, self.params.num_wait_points
        )
        return {DETUNING_AXIS: detuning, WAIT_AXIS: wait}

    def attach_acquisition_coords(self) -> None:
        """Snapshot the per-target parked detuning onto the dataset so the estimator
        reconstructs absolute detuning from the fitted (drive-relative) peak."""
        if self.dataset is None:
            return
        targets = [str(t) for t in self.dataset["target"].values]
        offsets = np.array(
            [self.resolved_center_offset_hz(q) for q in targets], dtype=float
        )
        self.dataset["center_offset_hz"] = ("target", offsets)

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """Synthesize a spectroscopy map whose reconstructed step response is a
        known two-exponential — the inverse of the estimator chain."""
        detuning = coords[DETUNING_AXIS]
        wait_ns = coords[WAIT_AXIS]
        qubits = self.params.targets

        n_q, n_d, n_w = len(qubits), detuning.size, wait_ns.size
        i_data = np.zeros((n_q, n_d, n_w))
        q_data = np.zeros((n_q, n_d, n_w))
        state = np.zeros((n_q, n_d, n_w))

        use_state = self.params.use_state_discrimination
        rng = np.random.default_rng(stable_seed("qubit_spectroscopy_cryoscope", *qubits))
        fwhm = (detuning[-1] - detuning[0]) / 25.0
        for k, q in enumerate(qubits):
            # the drive is parked here; the peak position is measured relative to it
            offset = self.resolved_center_offset_hz(q)
            # physics draws first (the stable-seed draw-order contract), taus
            # spanning the log wait window so the offline fit converges.
            a1 = rng.uniform(0.03, 0.07)
            tau1 = rng.uniform(3000.0, 8000.0)
            a2 = rng.uniform(0.02, 0.05)
            tau2 = rng.uniform(150.0, 600.0)
            step = 1.0 + a1 * np.exp(-wait_ns / tau1) + a2 * np.exp(-wait_ns / tau2)
            # detuning from the sweet spot ~ quad * flux^2 = offset * step^2; the
            # peak sits at that minus the parked offset the drive already carries.
            center = offset * (step ** 2 - 1.0)  # (n_w,)
            peak = 1.0 / (1.0 + ((detuning[:, None] - center[None, :]) / (fwhm / 2.0)) ** 2)

            if use_state:
                state[k] = state_row(peak.ravel(), rng).reshape(n_d, n_w)
            else:
                i_row, q_row = iq_from_population(peak.ravel(), rng)
                i_data[k] = i_row.reshape(n_d, n_w)
                q_data[k] = q_row.reshape(n_d, n_w)

        return readout_vars(use_state, state, i_data, q_data)

    def estimate(self) -> QubitSpectroscopyCryoscopeResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        # Loud coupling: an under-versioned scqat lacks this module and fails with
        # ImportError here rather than silently reading NaNs downstream.
        from scqat.estimators.spectroscopy_cryoscope import SpectroscopyCryoscopeEstimator
        from .._scqat import per_qubit_results

        # scqat's contract: `signal` (or complex I/Q) + coords `detuning` (Hz) &
        # `wait_time` (s), plus the scalar `center_offset_hz` attached above.
        rename = signal_rename(self.dataset, {DETUNING_AXIS: "detuning", WAIT_AXIS: "wait_time"})
        prepared = self.dataset.rename(rename)
        prepared = prepared.assign_coords(wait_time=prepared["wait_time"] * 1e-9)

        results = per_qubit_results(
            prepared,
            SpectroscopyCryoscopeEstimator(),
            artifact_dir=self.artifact_dir,
            start_fractions=list(self.params.fit_start_fractions),
        )

        result = QubitSpectroscopyCryoscopeResult()
        for qubit in self.params.targets:
            r = results[qubit]
            a_dc = float(r["a_dc"])
            amps = [float(a) for a in r["component_amps"]]
            taus = [float(t) for t in r["component_taus_s"]]
            ok = (
                bool(r["success"])
                and len(amps) == len(taus) > 0
                and a_dc != 0.0
                and all(np.isfinite(t) and t > 0 for t in taus)
                and all(np.isfinite(a) for a in amps)
            )
            result.fit[qubit] = {
                # relative taps: A_i = amp_i / a_dc (the vendor tap convention)
                "distortion_amp": [a / a_dc for a in amps],
                "distortion_tau_s": taus,
                "a_dc": a_dc,
                "rms_residual": float(r["rms_residual"]),
                "n_components": int(r["n_components"]),
                "center_offset_hz": float(r["center_offset_hz"]),
                "n_peaks_found": int(r["n_peaks_found"]),
                # the frame declaration: the excursion rode on this bias.
                "old_idle_flux": self.anchor(qubit, "idle_flux"),
                "flux_pulse_amp_v": float(self.params.flux_pulse_amp_v),
            }
            result.outcomes[qubit] = Outcome.SUCCESSFUL if ok else Outcome.FAILED
        return result

    def update(self) -> None:
        """Propose the measured taps as the flux channel's paired distortion FACTS.

        REPLACE semantics, identical to qubit_ramsey_cryoscope: the two cryoscopes
        are independent full measurements of the same step response (short vs long
        timescale), so the newer accepted run overwrites the taps rather than
        composing them. Nothing is pushed to the vendor (facts).
        """
        if self.result is None:
            return
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is not Outcome.SUCCESSFUL:
                continue
            flux_view = self.device.channel(qubit, "flux")
            flux_view.distortion_amp = [float(a) for a in fit["distortion_amp"]]
            flux_view.distortion_tau_s = [float(t) for t in fit["distortion_tau_s"]]

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
