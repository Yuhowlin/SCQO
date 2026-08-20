"""Parametric-drive resonance map — flux-modulation frequency x amplitude, record-only.

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

The drive time is deliberately NOT a sweep axis: this is the parameter-FINDING
map (where is the resonance, how hard can I drive it), run before any
time-domain characterization of the induced coupling.

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
from pydantic import Field

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
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register


class QubitParametricDriveParameters(TargetSelection, AveragingParameters,
                                     StateReadoutParameters, QubitResetParameters):
    """Inputs for the parametric-drive resonance map.

    Both windows are ABSOLUTE (volts at the DAC / Hz of the modulation tone),
    not factors or detunings: the parametric tone has no standing knob to be
    relative to. ``define_sweep`` refuses an empty or inverted window by name —
    the frequency axis must ascend (scqat's per-slice peak fit mis-fits a
    descending axis silently).
    """

    min_parametric_amp_v: float = Field(
        0.0, ge=0.0,
        description="Lowest parametric-drive amplitude (V at the DAC; the tone rides on "
                    "the standing idle bias, and the backend refuses past the port rail). "
                    "0 is the no-drive baseline row.")
    max_parametric_amp_v: float = Field(
        0.3, gt=0.0, description="Highest parametric-drive amplitude (V).")
    num_amp_points: int = Field(21, gt=4, description="Number of amplitude points.")
    min_parametric_freq_hz: float = Field(
        50e6, gt=0,
        description="Lowest parametric-drive (flux-modulation) frequency (Hz), absolute.")
    max_parametric_freq_hz: float = Field(
        300e6, gt=0,
        description="Highest parametric-drive frequency (Hz; the reachable band is the "
                    "flux line's — the instrument refuses past its bandwidth).")
    num_freq_points: int = Field(51, gt=4, description="Number of frequency points.")
    drive_time_ns: int = Field(
        2000, ge=16,
        description="FIXED parametric driving time (ns) — user-given, not a sweep axis. "
                    "Longer drives narrow the resonance features and deepen weak ones. "
                    "The QM backend requires a multiple of 4 ns and refuses otherwise.")


class QubitParametricDriveResult(Result):
    """``fit[target]``: the pooled peak-cloud counts (``n_peaks`` / ``n_good`` /
    ``n_outlier``) and, when at least one resonance was kept, the STRONGEST kept
    peak's coordinates — ``best_parametric_freq_hz`` / ``best_parametric_amp_v``
    plus its ``best_fwhm_hz`` and ``best_peak_amplitude`` (signed: negative =
    a population dip, the prepared-excited signature). Record-only: no
    ``update()``, nothing written to the device."""


@register
class QubitParametricDrive(Experiment):
    """Backend-agnostic parametric-drive map. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "qubit_parametric_drive"
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
    Parameters: ClassVar[type] = QubitParametricDriveParameters
    Result: ClassVar[type] = QubitParametricDriveResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("parametric_amp_v", "parametric_freq_hz"), sweep_units=("V", "Hz"),
        variables=("I", "Q"), alt_variables=POPULATION_ALT,
    )
    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout", "flux_bias")

    params: QubitParametricDriveParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        p = self.params
        for lo, hi, what in (
            (p.min_parametric_amp_v, p.max_parametric_amp_v, "parametric_amp_v"),
            (p.min_parametric_freq_hz, p.max_parametric_freq_hz, "parametric_freq_hz"),
        ):
            if not hi > lo:
                raise ValueError(
                    f"the {what} window is empty or inverted (min {lo} >= max {hi}); "
                    f"the axis must ascend — swap the edges or widen the window")
        return {
            # dict order IS the contract order: amplitude outer, frequency inner
            # (each map row is a spectrum at one drive amplitude).
            "parametric_amp_v": np.linspace(p.min_parametric_amp_v,
                                            p.max_parametric_amp_v,
                                            p.num_amp_points),
            "parametric_freq_hz": np.linspace(p.min_parametric_freq_hz,
                                              p.max_parametric_freq_hz,
                                              p.num_freq_points),
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
        rng = np.random.default_rng(stable_seed("qubit_parametric_drive", *targets))
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

    def estimate(self) -> QubitParametricDriveResult:
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

        result = QubitParametricDriveResult()
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
