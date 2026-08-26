"""Parametric-drive resonance map — flux-modulation frequency x AMPLITUDE, record-only.

Excite the qubit with a pi pulse, then MODULATE its own flux (z) line with an RF
tone of swept frequency and amplitude for a FIXED, user-given driving time, and
read the qubit back. Modulating the flux modulates f_01, and when the modulation
frequency matches a sideband condition with another component the qubit is
coupled to (its readout resonator, a coupler, a neighbouring qubit — e.g. the
red sideband ``|e,0> <-> |g,1>`` at the qubit-partner detuning), the excitation
parametrically transfers out of the qubit. The 2D population map over
(amplitude, frequency) draws the resonance line(s): the feature's frequency
locates the coupling condition and its amplitude dependence (drift + growing
width/depth) locates a usable operating amplitude for a parametric gate or
reset.

The drive time is deliberately NOT a sweep axis here — it is the fixed scalar,
and the swept second axis is the AMPLITUDE (hence the ``_amp`` name). This is
the parameter-FINDING map: where is the resonance, and how hard can I drive it.
Its sibling ``qubit_parametric_drive_time`` mirrors the split — it fixes the
amplitude and sweeps the driving TIME — and is the time-domain
characterization of the coupling this map locates, run second.

RECORD-ONLY for the DEVICE: there is no ``update()`` and nothing lands on the
device surface; the per-map summary lives in ``result.fit``. The scqat
estimator (``parametric_drive_resonance``) fits every amplitude slice with the
family-shared peak reduction, pools the peaks into a point-cloud over the map,
and draws the heat-map figure with the kept resonances overlaid — but it only
VISUALIZES and proposes nothing; the SUCCESS verdict (>= 1 kept resonance peak)
is made here in ``estimate()``.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from pydantic import Field, model_validator

from .._scqat import per_qubit_results
from ..contract import DatasetContract
from ._capabilities.qubit_reset import QubitResetParameters
from ._capabilities.state_readout import (
    POPULATION_ALT,
    StateReadoutParameters,
    readout_vars,
    signal_rename,
)
from ._sim import iq_from_population, stable_seed
from ._window import refuse_zero_width, window_bounds
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register


class QubitParametricDriveAmpParameters(TargetSelection, AveragingParameters,
                                     StateReadoutParameters, QubitResetParameters):
    """Inputs for the parametric-drive resonance map.

    Both windows are ABSOLUTE (volts at the DAC / Hz of the modulation tone),
    not factors or detunings: the parametric tone has no standing knob to be
    relative to. Each is a ``[start, end]`` pair whose edges may be given in
    EITHER order — they define the window, not a sweep direction, and the axis
    is always swept ascending (``_window.py``; scqat's per-slice peak fit
    mis-fits a descending axis silently). Only a zero-width window is refused.
    """

    start_parametric_amp_v: float = Field(
        0.0, ge=0.0,
        description="One edge of the swept parametric-drive amplitude window (V at the "
                    "DAC; the tone rides on the standing idle bias, and the backend "
                    "refuses past the port rail). 0 is the no-drive baseline row. The two "
                    "edges may be given in EITHER order — they define the window, not a "
                    "sweep direction, and the axis is always swept ascending.")
    end_parametric_amp_v: float = Field(
        0.3, ge=0.0,
        description="The other edge of the amplitude window (V). May be above or below "
                    "start_parametric_amp_v; only a zero-width window (both edges equal) "
                    "is refused.")
    num_amp_points: int = Field(21, gt=4, description="Number of amplitude points.")
    start_parametric_freq_hz: float = Field(
        50e6, gt=0,
        description="One edge of the swept parametric-drive (flux-modulation) frequency "
                    "window (Hz), absolute. The two edges may be given in EITHER order — "
                    "they define the window, not a sweep direction, and the axis is always "
                    "swept ascending.")
    end_parametric_freq_hz: float = Field(
        300e6, gt=0,
        description="The other edge of the frequency window (Hz; the reachable band is the "
                    "flux line's — the instrument refuses past its bandwidth). May be above "
                    "or below start_parametric_freq_hz; only a zero-width window is refused.")
    num_freq_points: int = Field(51, gt=4, description="Number of frequency points.")

    @model_validator(mode="after")
    def _windows_span(self) -> "QubitParametricDriveAmpParameters":
        refuse_zero_width(
            self.start_parametric_amp_v, self.end_parametric_amp_v,
            start_name="start_parametric_amp_v", end_name="end_parametric_amp_v",
            points_name="num_amp_points", quantity="amplitude")
        refuse_zero_width(
            self.start_parametric_freq_hz, self.end_parametric_freq_hz,
            start_name="start_parametric_freq_hz", end_name="end_parametric_freq_hz",
            points_name="num_freq_points")
        return self
    drive_time_ns: int = Field(
        2000, ge=16,
        description="FIXED parametric driving time (ns) — user-given, not a sweep axis. "
                    "Longer drives narrow the resonance features and deepen weak ones. "
                    "The QM backend requires a multiple of 4 ns and refuses otherwise. "
                    "Sweep this axis instead with qubit_parametric_drive_time.")


class QubitParametricDriveAmpResult(Result):
    """``fit[target]``: the pooled peak-cloud counts (``n_peaks`` / ``n_good`` /
    ``n_outlier``) and, when at least one resonance was kept, the STRONGEST kept
    peak's coordinates — ``best_parametric_freq_hz`` / ``best_parametric_amp_v``
    plus its ``best_fwhm_hz`` and ``best_peak_amplitude``. That amplitude is
    POLARITY-NORMALIZED, not signed physics: scqat's ``fit_peaks`` picks the
    stronger polarity per slice and inverts a dip trace before fitting, so a
    well-fit resonance reports a POSITIVE amplitude whether it is a dip or a
    peak (the dip-ness lives in that fitter's ``inverted`` flag, which the
    tracker does not propagate). Rank and gate on ``best_fwhm_hz`` instead — a
    real line is a small fraction of the swept window. Record-only: no
    ``update()``, nothing written to the device."""


@register
class QubitParametricDriveAmp(Experiment):
    """Backend-agnostic parametric-drive map. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "qubit_parametric_drive_amp"
    description: ClassVar[str] = (
        "Parametric-drive resonance map: excite the qubit, then modulate its own flux (z) "
        "line with an RF tone of swept frequency and amplitude for a FIXED user-given "
        "driving time, and read the qubit back. Where the modulation frequency matches a "
        "sideband condition with a coupled component the excitation parametrically "
        "transfers out of the qubit, so the 2D population map draws the resonance line(s) "
        "— the operating-parameter finder for parametric coupling (frequency locates the "
        "coupling condition, amplitude dependence locates a usable drive strength). "
        "use_state_discrimination returns the averaged population instead of I/Q. "
        "Record-only diagnostic: the per-map peak summary lands in result.fit and nothing "
        "is written back to the device."
    )
    Parameters: ClassVar[type] = QubitParametricDriveAmpParameters
    Result: ClassVar[type] = QubitParametricDriveAmpResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("parametric_amp_v", "parametric_freq_hz"), sweep_units=("V", "Hz"),
        variables=("I", "Q"), alt_variables=POPULATION_ALT,
    )
    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout", "flux_bias")

    params: QubitParametricDriveAmpParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        p = self.params
        # window_bounds is the ONE ordering point: the edges arrive in either
        # order and every axis leaves ascending. Zero width was already refused
        # at Parameters construction, so nothing here can raise.
        amp_lo, amp_hi = window_bounds(p.start_parametric_amp_v, p.end_parametric_amp_v)
        f_lo, f_hi = window_bounds(p.start_parametric_freq_hz, p.end_parametric_freq_hz)
        return {
            # dict order IS the contract order: amplitude outer, frequency inner
            # (each map row is a spectrum at one drive amplitude).
            "parametric_amp_v": np.linspace(amp_lo, amp_hi, p.num_amp_points),
            "parametric_freq_hz": np.linspace(f_lo, f_hi, p.num_freq_points),
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """One hidden sideband resonance per target — the model the map comes from.

        The resonance sits at ``f0`` at zero drive and drifts quadratically with
        amplitude (the parametric analogue of an AC-Stark shift); its depth also
        grows quadratically (transfer rate ~ drive power) and saturates. With
        the qubit prepared in ``|e>`` the transfer is a DIP in the excited-state
        population. Feature scales are drawn RELATIVE to the frequency grid so
        the sweep resolves the line — the same discipline as the swap-map sims.
        """
        a = coords["parametric_amp_v"]
        f = coords["parametric_freq_hz"]
        targets = self.params.targets
        rng = np.random.default_rng(stable_seed("qubit_parametric_drive_amp", *targets))
        use_state = self.params.use_state_discrimination
        f_span = float(np.ptp(f)) or 1.0
        f_step = f_span / max(f.size - 1, 1)
        a_ref = float(np.max(np.abs(a))) or 1.0
        i_data = np.empty((len(targets), a.size, f.size))
        q_data = np.empty_like(i_data)
        state = np.empty_like(i_data)
        for k in range(len(targets)):
            f0 = float(rng.uniform(f.min() + 0.3 * f_span, f.min() + 0.7 * f_span))
            drift = float(rng.uniform(3.0, 8.0)) * f_step      # full-amp quadratic shift
            fwhm = float(rng.uniform(3.0, 6.0)) * f_step
            strength = float(rng.uniform(1.0, 1.6))            # depth at full amplitude
            prep = float(rng.uniform(0.94, 0.99))              # pi-pulse fidelity
            rel = (a / a_ref) ** 2
            centers = f0 + drift * rel                         # per-amplitude line centre
            depth = np.minimum(0.9, strength * rel)
            dip = 1.0 / (1.0 + ((f[None, :] - centers[:, None]) / (fwhm / 2)) ** 2)
            population = np.clip(prep * (1.0 - depth[:, None] * dip), 0.0, 1.0)
            if use_state:
                state[k] = np.clip(
                    population + rng.normal(0.0, 0.02, population.shape), 0.0, 1.0)
            else:
                i_row, q_row = iq_from_population(population.ravel(), rng)
                i_data[k] = i_row.reshape(population.shape)
                q_data[k] = q_row.reshape(population.shape)
        return readout_vars(use_state, state, i_data, q_data)

    def estimate(self) -> QubitParametricDriveAmpResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.parametric_drive_resonance import (
            ParametricDriveResonanceEstimator,
        )

        # scqat's contract: coords `drive_amp` + `driving_frequency`, a real
        # `signal` (the discriminated population) or raw I/Q (reduced against
        # the complex median per slice). The estimator fits every amplitude
        # slice with the family-shared peak reduction — dips and peaks alike —
        # and pools the fits into a point-cloud over the map.
        rename = signal_rename(self.dataset, {
            "parametric_amp_v": "drive_amp",
            "parametric_freq_hz": "driving_frequency",
        })
        prepared = self.dataset.rename(rename)

        results = per_qubit_results(prepared, ParametricDriveResonanceEstimator(),
                                    artifact_dir=self.artifact_dir)

        result = QubitParametricDriveAmpResult()
        for qubit in self.params.targets:
            r = results[qubit]
            good = np.asarray(r["good"], dtype=bool)
            fit: dict[str, float] = {
                "n_peaks": int(r["n_peaks"]),
                "n_good": int(r["n_good"]),
                "n_outlier": int(r["n_outlier"]),
            }
            if good.any():
                # the strongest KEPT resonance: rank by |fitted amplitude|,
                # keep the sign in the report (negative = population dip).
                amps = np.asarray(r["peak_amplitude"], dtype=float)
                idx = int(np.flatnonzero(good)[np.argmax(np.abs(amps[good]))])
                fit["best_parametric_freq_hz"] = float(
                    np.asarray(r["peak_frequency"], dtype=float)[idx])
                fit["best_parametric_amp_v"] = float(
                    np.asarray(r["peak_drive_amp"], dtype=float)[idx])
                fit["best_fwhm_hz"] = float(
                    np.asarray(r["peak_fwhm"], dtype=float)[idx])
                fit["best_peak_amplitude"] = float(amps[idx])
            result.fit[qubit] = fit
            result.outcomes[qubit] = (
                Outcome.SUCCESSFUL if fit["n_good"] >= 1 else Outcome.FAILED)
        return result

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
