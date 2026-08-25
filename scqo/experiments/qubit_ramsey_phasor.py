"""Qubit phasor Ramsey — T2* and the residual drive detuning from a frame lock-in.

A Ramsey sequence (``x90`` - idle - ``frame_rotation`` - ``x90``) in which the
closing pulse's FRAME is swept through a full turn at every idle time. The
fringe therefore lives in the FRAME axis rather than in the time axis, and a
complex lock-in at one cycle per turn collapses each fringe to a single phasor:
its MAGNITUDE is the coherence envelope and its ANGLE the accumulated phase.

Two consequences, and they are the whole point of the experiment:

* Because the time axis no longer has to resolve a fringe, it is free to be
  LOG-spaced. That is what makes the stretch exponent ``p`` of the decay
  measurable — inside a fraction of one decay time every exponent fits equally
  well — so this experiment reports the SHAPE of the dephasing (``p = 1``
  Markovian/white, ``p = 2`` the Gaussian decay of a 1/f-dominated environment)
  as well as its time constant. Plain ``qubit_ramsey`` cannot: its linear axis
  is spent resolving the fringe.
* NO ARTIFICIAL DETUNING is applied, and none is offered. The contrast is
  detuning-independent (the fringe is in the frame axis), so an artificial
  detuning would buy nothing and only alias the phase; with it at zero the
  phase slope IS the residual frequency error. This also drops the negative
  virtual-detuning ramp — and its sign trap — from both probes.

The lock-in is blind to the readout rotation by construction (a constant
rotation is a global phase on the phasor), so unlike ``qubit_ramsey`` this
experiment does NOT attach stored blob centres: there is no axial projection to
resolve. It also cancels the readout offset exactly and rejects the ``m = 2``
harmonic an over-rotated pi/2 pulse produces — the latter only for
``num_frames >= 4``, which is why that is the floor rather than the lock-in's
bare minimum of 3.

WRITEBACK: accepting proposes the mode fact ``t2_star_s`` (the same free-
induction dephasing quantity ``qubit_ramsey`` measures — identical sequence,
different readout of the envelope) and, when the phase fit is valid, corrects
the drive channel's ``drive_freq_hz`` knob together with its measured twin
``f_01_hz``. The stretch exponent is reported in the result but not written
back: there is no catalogued field for it yet. The detuning is NaN-gated — the
estimator refuses to unwrap past the point where the branch is ambiguous — so
the frequency proposal is skipped rather than poisoned when the phase record
runs out.

The base ``probe()`` raises; both drivers supply one.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from pydantic import Field

from .._scqat import per_qubit_results
from ..contract import DatasetContract
from ..experiment import Experiment
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from ._capabilities.qubit_reset import QubitResetParameters
from ._capabilities.state_readout import (
    POPULATION_ALT,
    StateReadoutParameters,
    readout_vars,
    signal_rename,
    population_row,
)
from ._sim import iq_from_population, stable_seed
from ._time_grid import log_time_axis_ns
from . import register

#: axis names crossing the probe <-> estimator boundary.
IDLE_AXIS = "idle_time_ns"
FRAME_AXIS = "frame"


class QubitRamseyPhasorParameters(
    TargetSelection, AveragingParameters, StateReadoutParameters, QubitResetParameters
):
    """Inputs for a phasor Ramsey."""

    min_idle_time_ns: float = Field(
        16, ge=16,
        description="Shortest idle delay. The 16 ns floor is the instrument's — QM "
                    "plays the idle as 4 ns wait cycles and refuses anything shorter.")
    max_idle_time_ns: float = Field(
        200_000, gt=0,
        description="Longest idle delay. Reach for several times the expected T2* so "
                    "the decay is resolved over enough of a dynamic range for the "
                    "stretch exponent to mean something.")
    num_points: int = Field(
        60, gt=4,
        description="Number of LOG-spaced idle points requested. The realized axis is "
                    "SHORTER: points are snapped to the 4 ns grid and de-duplicated, "
                    "which collides neighbours at the dense end.")
    num_frames: int = Field(
        16, ge=4,
        description="Closing-pulse frame points over one full turn (endpoint-exclusive). "
                    "The lock-in recovers contrast and phase exactly on this grid. The "
                    "floor is 4, not 3: a real 2f contaminant from an over-rotated pi/2 "
                    "pulse carries both +2 and -2 harmonics, and at 3 points the -2 "
                    "component aliases onto the signal and leaks through at full weight.")
    fix_p: float | None = Field(
        None, gt=0,
        description="Freeze the stretch exponent instead of fitting it. Leave unset for "
                    "the free fit a log axis affords; fix_p=1.0 recovers a plain "
                    "exponential and is the right choice when the idle window spans "
                    "well under a decade.")


class QubitRamseyPhasorResult(Result):
    """Output of QubitRamseyPhasor.

    ``fit[target]`` carries ``t2_star_s`` and ``stretch_p`` (with their errors),
    plus ``drive_freq_hz`` / ``f_01_hz`` / ``detuning_error_hz`` /
    ``old_drive_freq_hz`` when the phase fit was valid.
    """


@register
class QubitRamseyPhasor(Experiment):
    """Backend-agnostic phasor Ramsey. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "qubit_ramsey_phasor"
    description: ClassVar[str] = (
        "Two pi/2 pulses separated by a LOG-spaced idle time, with the closing pulse's "
        "phase swept through a full turn at every idle point. A lock-in over that frame "
        "axis reads the coherence envelope off the phasor magnitude and the accumulated "
        "phase off its angle, so T2* comes straight from the envelope instead of being "
        "fitted out of a decaying oscillation. Reports the stretch exponent p of the "
        "decay (1 = Markovian/white noise, 2 = the Gaussian decay of a 1/f-dominated "
        "environment) alongside T2*, which the log axis is what makes measurable; "
        "corrects drive_freq_hz from the phase slope. Prefer plain qubit_ramsey for a "
        "fast frequency correction — this costs num_frames times as many points and "
        "buys the decay SHAPE. No artificial detuning is applied: the fringe is in the "
        "frame axis, so the phase slope is the residual frequency error directly. "
        "use_state_discrimination returns the FPGA-discriminated averaged state instead "
        "of I/Q (needs a calibrated discriminator: run single_shot_readout and accept "
        "its readout_rotation_rad / readout_threshold suggestions first)."
    )
    Parameters: ClassVar[type] = QubitRamseyPhasorParameters
    Result: ClassVar[type] = QubitRamseyPhasorResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=(IDLE_AXIS, FRAME_AXIS), sweep_units=("ns", "turn"),
        variables=("I", "Q"), alt_variables=POPULATION_ALT,
    )
    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout")
    # attach_readout_positions stays at the base False: the lock-in is blind to
    # the readout rotation, so there is no axial axis to resolve from blob centres.

    params: QubitRamseyPhasorParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        # Idle OUTER, frame INNER — the dict order IS the contract order, and it
        # matches both probes' loop nesting.
        idle = log_time_axis_ns(
            self.params.min_idle_time_ns,
            self.params.max_idle_time_ns,
            self.params.num_points,
        )
        # endpoint-EXCLUSIVE: the lock-in's offset cancellation and harmonic
        # rejection both hold only on this grid.
        frame = np.linspace(0.0, 1.0, self.params.num_frames, endpoint=False)
        return {IDLE_AXIS: idle, FRAME_AXIS: frame}

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """Synthesize a fringe whose lock-in magnitude is a known stretched
        exponential and whose angle is a known detuning — the inverse of the
        estimator chain."""
        idle_s = coords[IDLE_AXIS] * 1e-9
        frame = coords[FRAME_AXIS]
        qubits = self.params.targets

        n_qubits, n_idle, n_frame = len(qubits), len(idle_s), len(frame)
        i_data = np.zeros((n_qubits, n_idle, n_frame))
        q_data = np.zeros((n_qubits, n_idle, n_frame))
        state = np.zeros_like(i_data)

        use_state = self.params.use_state_discrimination
        rng = np.random.default_rng(stable_seed("qubit_ramsey_phasor", *qubits))
        for k in range(n_qubits):
            # physics draws first (the stable-seed draw-order contract)
            t2_star = rng.uniform(8e-6, 25e-6)
            stretch = rng.uniform(1.0, 2.0)
            detuning = rng.uniform(-2e5, 2e5)  # residual error to recover

            envelope = np.exp(-((idle_s / t2_star) ** stretch))
            phase = 2 * np.pi * detuning * idle_s
            population = 0.5 + 0.5 * envelope[:, None] * np.cos(
                2 * np.pi * frame[None, :] - phase[:, None]
            )

            # exactly one readout draw per target on the raveled 2-D fringe
            if use_state:
                state[k] = population_row(population.ravel(), rng).reshape(n_idle, n_frame)
            else:
                i_row, q_row = iq_from_population(population.ravel(), rng)
                i_data[k] = i_row.reshape(n_idle, n_frame)
                q_data[k] = q_row.reshape(n_idle, n_frame)

        return readout_vars(use_state, state, i_data, q_data)

    def estimate(self) -> QubitRamseyPhasorResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        # Loud coupling: an under-versioned scqat lacks this module and fails
        # with ImportError here rather than silently reading NaNs downstream.
        from scqat.estimators.ramsey_phasor import RamseyPhasorEstimator

        # scqat's contract: `signal` (or complex I/Q) + coords `idle_time` (s) &
        # `frame` (turns). Keep I/Q complex — the estimator's lock-in uses the
        # full IQ contrast and is blind to the readout rotation.
        rename = signal_rename(self.dataset, {IDLE_AXIS: "idle_time"})
        prepared = self.dataset.rename(rename)
        prepared = prepared.assign_coords(idle_time=prepared["idle_time"] * 1e-9)

        results = per_qubit_results(
            prepared,
            RamseyPhasorEstimator(),
            artifact_dir=self.artifact_dir,
            fix_p=self.params.fix_p,
        )

        result = QubitRamseyPhasorResult()
        for qubit in self.params.targets:
            r = results[qubit]
            t2_star = float(r.get("t2_star_s", float("nan")))
            ok = (bool(r["success"]) and np.isfinite(t2_star) and t2_star > 0)
            fit: dict = {
                "t2_star_s": t2_star,
                "t2_star_err_s": float(r.get("t2_star_err_s", float("nan"))),
                "stretch_p": float(r.get("stretch_p", float("nan"))),
                "stretch_p_err": float(r.get("stretch_p_err", float("nan"))),
                "var_explained": float(r.get("var_explained", float("nan"))),
                "n_phase_valid": int(r.get("n_phase_valid", 0)),
            }
            # The phase half fails INDEPENDENTLY of the envelope: a record the
            # unwrap could not follow yields NaN here, and the frequency
            # proposal is simply skipped rather than poisoned.
            detuning_error = float(r.get("detuning_error_hz", float("nan")))
            if np.isfinite(detuning_error):
                old = float(self.device.channel(qubit, "drive").drive_freq_hz)
                fit.update({
                    "drive_freq_hz": old + detuning_error,
                    # the measured FACT twin of the drive_freq_hz knob (same fit)
                    "f_01_hz": old + detuning_error,
                    "detuning_error_hz": detuning_error,
                    "old_drive_freq_hz": old,
                })
            result.fit[qubit] = fit
            result.outcomes[qubit] = Outcome.SUCCESSFUL if ok else Outcome.FAILED
        return result

    def update(self) -> None:
        if self.result is None:
            return
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is not Outcome.SUCCESSFUL:
                continue
            mode = self.device.component(qubit)
            mode.t2_star_s = fit["t2_star_s"]
            # Only when the gated phase fit produced a finite slope. stretch_p is
            # reported in the result but has no catalogued field, so it is not
            # written back.
            new_freq = fit.get("drive_freq_hz")
            if new_freq is not None and np.isfinite(new_freq):
                self.device.channel(qubit, "drive").drive_freq_hz = new_freq  # the knob
                mode.f_01_hz = fit["f_01_hz"]  # the measured fact (same fit)

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
