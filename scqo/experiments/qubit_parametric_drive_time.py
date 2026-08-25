"""Parametric-drive chevron — flux-modulation frequency x driving TIME, record-only.

Excite the qubit with a pi pulse, then MODULATE its own flux (z) line with an RF
tone of swept frequency at a FIXED, user-given amplitude, hold the tone for a
SWEPT driving time, and read the qubit back. Where the modulation frequency
matches a sideband condition with a coupled component (the red sideband
``|e,0> <-> |g,1>`` at the qubit-partner detuning, say), the excitation
parametrically exchanges out of the qubit and back, so each frequency row is an
oscillating ``rho_11(t)`` whose rate is the parametric coupling and whose decay
is the loss the exchange picks up. The 2D map over (frequency, time) is the
chevron: fastest, deepest oscillation on resonance, faster-but-shallower off it.

Sibling of ``qubit_parametric_drive_amp``, which fixes the TIME and sweeps the
AMPLITUDE. Run the amp map first — it locates the resonance and a usable drive
strength — then run this one at that frequency window and amplitude to
characterize the coupling it found.

THE TIME AXIS IS ON THE 4 ns GRID, not the neutral 1 ns one. This experiment is
QM-only (the parametric tone has no Qblox realization), QM's ``play(duration=)``
counts 4 ns clock cycles, and the driver sweeps that count as a real-time QUA
variable — so building the axis on the instrument's own grid makes every stored
point exactly what played, and the probe never has to round-and-re-declare the
axis the way the neutral-grid time sweeps do.

RUN THIS DISCRIMINATED. The estimator reconstructs ``rho_11`` as
``(signal - offset) / scale`` with an identity correction, so it needs a real
P(|1>): with ``use_state_discrimination`` off it falls back to the raw in-phase
quadrature, still runs, and reports rates in units of volts-per-nanosecond that
look like numbers and are not physical.

RECORD-ONLY for the DEVICE: there is no ``update()`` and nothing lands on the
device surface; the per-map summary lives in ``result.fit``. The scqat estimator
(``parametric_drive_decoherence``) reconstructs ``rho_11(t)`` per frequency and
runs the three-stage Hankel -> multi-damped-oscillation -> non-Markovian
amplitude-damping pipeline, reporting the loss rate ``gamma``, the coherent
coupling ``lambda`` and the residual detuning ``Delta`` per drive frequency plus
the exceptional-point figure of merit ``8*lambda^2/gamma^2`` (> 1 = the exchange
is coherent, i.e. a usable parametric coupling) — but it only VISUALIZES and
proposes nothing; the SUCCESS verdict (>= 1 converged frequency) is made here in
``estimate()``.
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
from ._time_grid import time_axis_ns
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register


class QubitParametricDriveTimeParameters(TargetSelection, AveragingParameters,
                                         StateReadoutParameters, QubitResetParameters):
    """Inputs for the parametric-drive chevron.

    The frequency window is ABSOLUTE (Hz of the modulation tone), not a detuning:
    the parametric tone has no standing knob to be relative to. ``define_sweep``
    refuses an empty or inverted window by name — the frequency axis must ascend
    (scqat's per-slice reductions mis-fit a descending axis silently).
    """

    parametric_amp_v: float = Field(
        0.15, gt=0.0,
        description="FIXED parametric-drive amplitude (V at the DAC) — user-given, not a "
                    "sweep axis. The tone rides on the standing idle bias, and the backend "
                    "refuses past the port rail. Locate a usable value with "
                    "qubit_parametric_drive_amp first, which sweeps this axis.")
    min_parametric_freq_hz: float = Field(
        180e6, gt=0,
        description="Lowest parametric-drive (flux-modulation) frequency (Hz), absolute. "
                    "This is a ZOOM around a known resonance, not a finder: the window "
                    "must be narrow enough that the chevron's linewidth (~the exchange "
                    "rate) spans several frequency steps, or the sweep steps straight "
                    "over it. Get the centre from qubit_parametric_drive_amp.")
    max_parametric_freq_hz: float = Field(
        220e6, gt=0,
        description="Highest parametric-drive frequency (Hz; the reachable band is the "
                    "flux line's — the instrument refuses past its bandwidth).")
    num_freq_points: int = Field(21, gt=4, description="Number of frequency points.")
    min_drive_time_ns: float = Field(
        16.0, ge=16.0,
        description="Shortest parametric driving time (ns; 16 ns is the QM floor — "
                    "play() counts 4 ns cycles and 16 ns is the shortest pulse).")
    max_drive_time_ns: float = Field(
        3000.0, gt=16.0,
        description="Longest parametric driving time (ns). Cover several exchange "
                    "periods on resonance, or the oscillation rate is unidentifiable.")
    num_time_points: int = Field(
        101, gt=4,
        description="Number of driving-time points. The axis is uniform on the 4 ns "
                    "instrument grid, so a window too narrow to hold this many distinct "
                    "points is refused by name rather than silently collapsed.")


class QubitParametricDriveTimeResult(Result):
    """``fit[target]``: the per-frequency fit census (``n_freq`` / ``n_decoh_ok`` /
    ``n_underdamped`` — how many drive frequencies converged, and at how many the
    exchange came out COHERENT) and, when at least one frequency converged, the
    BEST sideband: the frequency of largest ``8*lambda^2/gamma^2`` and that
    frequency's ``best_ep_metric`` / ``best_gamma_hz`` / ``best_lambda_hz`` /
    ``best_delta_hz`` (rates converted from the estimator's 1/ns to Hz).
    Record-only: no ``update()``, nothing written to the device."""


@register
class QubitParametricDriveTime(Experiment):
    """Backend-agnostic parametric-drive chevron. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "qubit_parametric_drive_time"
    description: ClassVar[str] = (
        "Parametric-drive chevron: excite the qubit, then modulate its own flux (z) line "
        "with an RF tone of swept frequency at a FIXED user-given amplitude, hold it for a "
        "SWEPT driving time, and read the qubit back. Where the modulation frequency meets "
        "a sideband condition with a coupled component the excitation exchanges out of the "
        "qubit and back, so each frequency row is an oscillating rho_11(t) — the map is the "
        "chevron and the fit gives the coherent coupling rate, the loss rate and the "
        "exceptional-point figure of merit per drive frequency. The time-domain "
        "characterization that follows qubit_parametric_drive_amp's resonance finder. "
        "Run it with use_state_discrimination ON: the fit reconstructs rho_11 from a "
        "real P(|1>), and on raw I/Q it silently reports unphysical rates. "
        "Record-only diagnostic: the per-map fit summary lands in result.fit and nothing "
        "is written back to the device."
    )
    Parameters: ClassVar[type] = QubitParametricDriveTimeParameters
    Result: ClassVar[type] = QubitParametricDriveTimeResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("parametric_freq_hz", "drive_time_ns"), sweep_units=("Hz", "ns"),
        variables=("I", "Q"), alt_variables=POPULATION_ALT,
    )
    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout", "flux_bias")

    params: QubitParametricDriveTimeParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        p = self.params
        if not p.max_parametric_freq_hz > p.min_parametric_freq_hz:
            raise ValueError(
                f"the parametric_freq_hz window is empty or inverted "
                f"(min {p.min_parametric_freq_hz} >= max {p.max_parametric_freq_hz}); "
                f"the axis must ascend — swap the edges or widen the window")
        return {
            # dict order IS the contract order: frequency outer, time inner (each
            # map row is one rho_11(t) trace at one drive frequency — the shape the
            # estimator reads, and the one that lets the driver step the z-line
            # oscillator once per frequency instead of once per point).
            "parametric_freq_hz": np.linspace(p.min_parametric_freq_hz,
                                              p.max_parametric_freq_hz,
                                              p.num_freq_points),
            # grid_ns=4, not the neutral 1: QM is the only backend and its
            # play(duration=) counts 4 ns cycles (module docstring).
            "drive_time_ns": time_axis_ns(p.min_drive_time_ns, p.max_drive_time_ns,
                                          p.num_time_points, grid_ns=4),
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """One hidden sideband per target, seen as a chevron — the model the map
        comes from.

        A two-level exchange driven at detuning ``delta = f - f0`` oscillates at
        the generalized rate ``Omega = sqrt(Omega0**2 + delta**2)`` with contrast
        ``(Omega0 / Omega)**2``, and the whole thing decays at ``gamma``. Prepared
        in ``|e>``, that is
        ``rho_11(t) = prep * exp(-gamma t) * (1 - (Omega0/Omega)**2 sin**2(pi Omega t))``:
        full-depth slow oscillation on resonance, shallower and faster off it —
        the chevron. Feature scales are drawn RELATIVE to the grids, the same
        discipline as the swap maps and the amp sibling — but here the two grids
        are NOT independent, and that is the point the parameters have to
        respect: ``Omega0`` is BOTH the on-resonance exchange rate (time axis)
        and the chevron's half-width (frequency axis), so it is drawn from the
        FREQUENCY step — a few steps wide, hence resolved — and the resulting
        oscillation period is what the time window then has to sample. A
        frequency window too wide for its time window produces a chevron narrower
        than one frequency step, which the sweep steps straight over; the default
        windows are chosen to be consistent.
        """
        f = coords["parametric_freq_hz"]
        t = coords["drive_time_ns"]
        targets = self.params.targets
        rng = np.random.default_rng(stable_seed("qubit_parametric_drive_time", *targets))
        use_state = self.params.use_state_discrimination
        f_span = float(np.ptp(f)) or 1.0
        f_step = f_span / max(f.size - 1, 1)
        t_span = float(np.ptp(t)) or 1.0
        i_data = np.empty((len(targets), f.size, t.size))
        q_data = np.empty_like(i_data)
        state = np.empty_like(i_data)
        for k in range(len(targets)):
            f0 = float(rng.uniform(f.min() + 0.3 * f_span, f.min() + 0.7 * f_span))
            # Omega0 in 1/ns (Hz -> 1/ns is 1e-9): 2-4 frequency steps wide, so
            # the chevron is resolved along f AND its period is sampled along t.
            omega0 = float(rng.uniform(2.0, 4.0)) * f_step * 1e-9
            # decay over 1-2 window lengths, so the envelope is visible but the
            # oscillation is not swallowed by it.
            gamma = 1.0 / (float(rng.uniform(1.0, 2.0)) * t_span)
            prep = float(rng.uniform(0.94, 0.99))              # pi-pulse fidelity
            # detuning in the SAME 1/ns units as Omega0 (Hz -> 1/ns is 1e-9).
            delta = (f - f0) * 1e-9
            omega = np.sqrt(omega0 ** 2 + delta ** 2)
            contrast = (omega0 / omega) ** 2
            envelope = np.exp(-gamma * t)[None, :]
            swap = np.sin(np.pi * omega[:, None] * t[None, :]) ** 2
            population = np.clip(
                prep * envelope * (1.0 - contrast[:, None] * swap), 0.0, 1.0)
            if use_state:
                state[k] = np.clip(
                    population + rng.normal(0.0, 0.01, population.shape), 0.0, 1.0)
            else:
                i_row, q_row = iq_from_population(population.ravel(), rng)
                i_data[k] = i_row.reshape(population.shape)
                q_data[k] = q_row.reshape(population.shape)
        return readout_vars(use_state, state, i_data, q_data)

    def estimate(self) -> QubitParametricDriveTimeResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.parametric_drive_decoherence import (
            ParametricDriveDecoherenceEstimator,
        )

        # scqat's contract: coords `driving_frequency` + `driving_time` (ns) and a
        # real state/signal variable carrying P(|1>). The estimator rebuilds
        # rho_11(t) per frequency and runs the Hankel -> multi-damped-oscillation
        # -> non-Markovian pipeline on each trace.
        rename = signal_rename(self.dataset, {
            "parametric_freq_hz": "driving_frequency",
            "drive_time_ns": "driving_time",
        })
        prepared = self.dataset.rename(rename)

        # Identity readout correction, passed EXPLICITLY: these are already scqat's
        # defaults, but ep_pipeline carries commented-out lab-calibrated values
        # right beside them, and scqo's discriminated population is already a true
        # probability that must not be re-scaled.
        results = per_qubit_results(prepared, ParametricDriveDecoherenceEstimator(),
                                    artifact_dir=self.artifact_dir,
                                    rho11_offset=0.0, rho11_scale=1.0)

        result = QubitParametricDriveTimeResult()
        for qubit in self.params.targets:
            r = results[qubit]
            regime = list(r["regime"])
            fit: dict[str, float] = {
                "n_freq": int(r["n_freq"]),
                "n_decoh_ok": int(r["n_decoh_ok"]),
                # underdamped = the exchange oscillates = a usable coupling.
                "n_underdamped": int(sum(1 for g in regime if g == "underdamped")),
            }
            ep = np.asarray(r["ep_metric"], dtype=float)
            if np.isfinite(ep).any():
                # the BEST sideband: largest 8*lambda^2/gamma^2, i.e. the drive
                # frequency where the exchange is most coherent.
                idx = int(np.nanargmax(ep))
                freqs = np.asarray(r["driving_frequency"], dtype=float)
                fit["best_parametric_freq_hz"] = float(freqs[idx])
                fit["best_ep_metric"] = float(ep[idx])
                # the estimator works in 1/ns; report Hz at this boundary.
                for key, field in (("gamma", "best_gamma_hz"),
                                   ("lambda_", "best_lambda_hz"),
                                   ("Delta", "best_delta_hz")):
                    fit[field] = float(np.asarray(r[key], dtype=float)[idx]) * 1e9
            result.fit[qubit] = fit
            result.outcomes[qubit] = (
                Outcome.SUCCESSFUL if fit["n_decoh_ok"] >= 1 else Outcome.FAILED)
        return result

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
