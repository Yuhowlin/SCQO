"""Qubit Ramsey cryoscope — flux-line step response via Ramsey phase tomography.

Measure the flux line's transient (step) response so its distortions can be
compensated. The probe plays a flux pulse of swept DURATION inside a Ramsey
sequence (``x90`` - flux pulse - fixed total idle - ``frame_rotation`` - ``x90``)
and sweeps the closing pulse's FRAME through a full turn; the qubit's
flux-dependent detuning imprints a phase that grows with the accumulated flux.
The estimator reconstructs that phase per duration, differentiates it into a
detuning, maps it to the delivered flux (``sqrt`` of the quadratic arch),
NORMALIZES by the settled tail, and fits the resulting step response to a sum of
exponentials — the predistortion tap ``(amplitude, tau)`` components.

FRAME (deliberately NOT a ``_pulse`` experiment): the flux-pulse amplitude is a
SCALAR parameter ``flux_pulse_amp_v`` (volts RELATIVE to the flux channel's
``idle_flux`` — the pulse rides on the standing bias), not a swept window, so this
experiment does not subclass the flux capability mixins and carries no ``flux``
tag. The swept axis is the pulse DURATION. The amplitude only sets SNR: the
estimator normalizes by the settled tail, so the recorded taps are relative and
no flux-frequency curvature calibration is needed.

WRITEBACK: accepting proposes the paired flux-channel FACTS ``distortion_amp``
(dimensionless, ``amp / a_dc``) and ``distortion_tau_s`` (seconds) — the measured
cryo-wiring physics. They land in physical.json and are NEVER pushed to the
vendor: applying them to the instrument's output filter (e.g. the OPX
``exponential_filter``) stays a manual vendor-config step. Semantics are REPLACE
— each accepted run records the full step response measured that run.

QM-only today: the base ``probe()`` raises, and only LCHQMDriver supplies one.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from pydantic import Field, field_validator

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
DURATION_AXIS = "duration_ns"
FRAME_AXIS = "frame"


class QubitRamseyCryoscopeParameters(
    TargetSelection, AveragingParameters, StateReadoutParameters, QubitResetParameters
):
    """Parameters for the cryoscope (flux-line step response)."""

    flux_pulse_amp_v: float = Field(
        0.1,
        description="Flux-pulse amplitude in volts, RELATIVE to the flux channel's "
        "idle_flux (the pulse rides on the standing bias). Choose it to detune the "
        "qubit by a few hundred MHz; the step response is normalized by its settled "
        "tail, so this only sets SNR and the recorded taps are relative.",
    )
    max_duration_ns: int = Field(
        240,
        ge=32,
        le=2000,
        multiple_of=4,
        description="Longest flux-pulse duration in ns. The duration axis is EVERY "
        "nanosecond from 1 to this value (1 ns resolution); the fixed post-pulse "
        "idle is derived from it. Needs enough length for the response to settle.",
    )
    num_frames: int = Field(
        16,
        ge=4,
        description="Number of closing-pulse frame points over one full turn "
        "(endpoint-exclusive); the phase is recovered by a lock-in at one cycle "
        "per turn, exact on this grid.",
    )
    num_averages: int = Field(
        2000, gt=0, description="Number of shots to average per sweep point."
    )
    fit_start_fractions: list[float] = Field(
        default_factory=lambda: [0.5, 0.01],
        description="DESCENDING 0-1 fractions of the record where each exponential "
        "component's fit starts (slowest component first). Forwarded to the "
        "estimator; length sets the number of tap components.",
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


class QubitRamseyCryoscopeResult(Result):
    """Fitted step-response results.

    ``fit[target]``: the paired tap arrays ``distortion_amp`` (relative,
    ``amp / a_dc``) and ``distortion_tau_s`` (seconds), the settled level
    ``a_dc``, the fit ``rms_residual``, the scalar ``old_idle_flux`` (the standing
    bias the pulse rode on) and ``flux_pulse_amp_v`` (the excursion used).
    """


@register
class QubitRamseyCryoscope(Experiment):
    """Measure the flux-line step response and propose predistortion taps."""

    name: ClassVar[str] = "qubit_ramsey_cryoscope"
    description: ClassVar[str] = (
        "Reconstruct the flux line's step response with a Ramsey phase-tomography "
        "sequence — a flux pulse of swept DURATION (1 ns resolution) between two "
        "x90 pulses, the second's FRAME swept through a turn — and fit it to a sum "
        "of exponentials. Proposes the flux channel's paired distortion facts "
        "(distortion_amp, relative; distortion_tau_s, seconds) that describe the "
        "cryo-wiring transient; applying them to the instrument's output filter is "
        "a manual vendor step. The flux-pulse amplitude is a volts parameter "
        "RELATIVE to idle_flux and only sets SNR (the response is tail-normalized). "
        "Prerequisite: calibrated x90 (spectroscopy, power_rabi, Ramsey). QM-only "
        "probe today."
    )
    Parameters: ClassVar[type] = QubitRamseyCryoscopeParameters
    Result: ClassVar[type] = QubitRamseyCryoscopeResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=(DURATION_AXIS, FRAME_AXIS),
        sweep_units=("ns", "turn"),
        variables=("I", "Q"),
        alt_variables=STATE_ALT,
    )

    params: QubitRamseyCryoscopeParameters

    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout", "flux_bias")

    def define_sweep(self) -> dict[str, np.ndarray]:
        # Every nanosecond 1..max: the QUA loop enumerates each ns and the
        # Savitzky-Golay derivative assumes a uniform 1 ns step.
        duration = np.arange(1, self.params.max_duration_ns + 1, dtype=float)
        frame = np.linspace(0.0, 1.0, self.params.num_frames, endpoint=False)
        return {DURATION_AXIS: duration, FRAME_AXIS: frame}

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """Synthesize a fringe whose reconstructed step response is a known
        two-exponential — the inverse of the estimator chain."""
        duration_ns = coords[DURATION_AXIS]
        frame = coords[FRAME_AXIS]
        qubits = self.params.targets

        n_qubits, n_dur, n_frame = len(qubits), len(duration_ns), len(frame)
        i_data = np.zeros((n_qubits, n_dur, n_frame))
        q_data = np.zeros((n_qubits, n_dur, n_frame))
        state = np.zeros_like(i_data)

        use_state = self.params.use_state_discrimination
        rng = np.random.default_rng(stable_seed("qubit_ramsey_cryoscope", *qubits))
        dt_s = 1e-9  # the duration axis is whole nanoseconds
        for k in range(n_qubits):
            # physics draws first (the stable-seed draw-order contract), all
            # comfortably within a 240 ns record so the offline fit converges.
            a1 = rng.uniform(0.04, 0.10)
            tau1 = rng.uniform(30.0, 90.0)
            a2 = rng.uniform(0.01, 0.04)
            tau2 = rng.uniform(2.0, 8.0)
            f_step = rng.uniform(80e6, 200e6)  # settled detuning, Hz

            step = 1.0 + a1 * np.exp(-duration_ns / tau1) + a2 * np.exp(-duration_ns / tau2)
            freq = f_step * step ** 2  # detuning ~ quad arch of the delivered flux
            phase = 2 * np.pi * np.cumsum(freq) * dt_s
            population = 0.5 + 0.5 * np.cos(2 * np.pi * frame[None, :] - phase[:, None])

            # exactly one readout draw per target on the raveled 2-D fringe
            if use_state:
                state[k] = state_row(population.ravel(), rng).reshape(n_dur, n_frame)
            else:
                i_row, q_row = iq_from_population(population.ravel(), rng)
                i_data[k] = i_row.reshape(n_dur, n_frame)
                q_data[k] = q_row.reshape(n_dur, n_frame)

        return readout_vars(use_state, state, i_data, q_data)

    def estimate(self) -> QubitRamseyCryoscopeResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        # Loud coupling: an under-versioned scqat lacks this module and fails
        # with ImportError here rather than silently reading NaNs downstream.
        from scqat.estimators.ramsey_cryoscope import RamseyCryoscopeEstimator
        from .._scqat import per_qubit_results

        # scqat's contract: `signal` (or complex I/Q) + coords `duration` (s) &
        # `frame` (turns). Keep I/Q complex (iq_fallback=None) — the estimator's
        # lock-in uses the full IQ contrast, blind to the readout rotation.
        rename = signal_rename(self.dataset, {DURATION_AXIS: "duration"})
        prepared = self.dataset.rename(rename)
        prepared = prepared.assign_coords(duration=prepared["duration"] * 1e-9)

        results = per_qubit_results(
            prepared,
            RamseyCryoscopeEstimator(),
            artifact_dir=self.artifact_dir,
            start_fractions=list(self.params.fit_start_fractions),
        )

        result = QubitRamseyCryoscopeResult()
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
                # the frame declaration: the excursion rode on this bias.
                "old_idle_flux": self.anchor(qubit, "idle_flux"),
                "flux_pulse_amp_v": float(self.params.flux_pulse_amp_v),
            }
            result.outcomes[qubit] = Outcome.SUCCESSFUL if ok else Outcome.FAILED
        return result

    def update(self) -> None:
        """Propose the measured taps as the flux channel's paired distortion FACTS.

        REPLACE semantics: each accepted run records the whole step response it
        measured. Both arrays are written through the one flux-channel view so the
        accept batches them together — the paired-length invariant is checked at
        accept, not here. Nothing is pushed to the vendor (facts): applying the
        taps to the instrument's output filter is a manual vendor-config step.
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
