"""Qubit Ramsey — greenfield port of :mod:`scqo.experiments.qubit_ramsey`.

The physics half (time sweep, decaying-cosine fit yielding residual detuning +
T2*) is byte-for-byte; what moved is the device surface: the corrected drive
frequency lands on the target's DRIVE CHANNEL (``drive_freq_hz``) while its
measured twin ``f_01_hz`` and ``t2_star_s`` stay as facts on the target MODE.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from pydantic import Field

from .._scqat import per_qubit_results
from ..contract import DatasetContract
from ._capabilities.qubit_reset import QubitResetParameters
from ._capabilities.state_readout import (
    STATE_ALT,
    StateReadoutParameters,
    readout_vars,
    signal_rename,
    state_row,
)
from ._sim import iq_from_population, stable_seed
from ._time_grid import time_axis_ns
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register


class QubitRamseyParameters(TargetSelection, AveragingParameters, StateReadoutParameters, QubitResetParameters):
    """Inputs for a Ramsey experiment."""

    frequency_detuning_hz: float = Field(
        1.0e6, gt=0, description="Artificial drive detuning applied to make the fringe oscillate."
    )
    min_idle_time_ns: float = Field(16, ge=0, description="Shortest idle delay between the two pi/2 pulses.")
    max_idle_time_ns: float = Field(4000, gt=0, description="Longest idle delay.")
    num_points: int = Field(101, gt=1, description="Number of idle-time points.")


class QubitRamseyResult(Result):
    """Output of QubitRamsey.

    ``fit[target]`` carries ``drive_freq_hz`` (new absolute Hz), its measured twin
    ``f_01_hz`` (same value; ``update()`` writes the knob and the fact together),
    ``detuning_error_hz``, ``t2_star_s`` and ``old_drive_freq_hz``.
    """


@register
class QubitRamsey(Experiment):
    """Backend-agnostic Ramsey. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "qubit_ramsey"
    description: ClassVar[str] = (
        "Two pi/2 pulses separated by a swept idle time with an artificial drive detuning; "
        "fits the decaying fringe to correct the drive channel's drive_freq_hz and report "
        "T2*. use_state_discrimination returns the FPGA-discriminated averaged state "
        "instead of I/Q (needs a calibrated discriminator: run single_shot_readout and "
        "accept its readout_rotation_rad / readout_threshold suggestions first)."
    )
    Parameters: ClassVar[type] = QubitRamseyParameters
    Result: ClassVar[type] = QubitRamseyResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("idle_time_ns",), sweep_units=("ns",), variables=("I", "Q"),
        alt_variables=STATE_ALT,
    )
    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout")
    #: stored blob centers ride the dataset -> axial axis = the measured g->e vector
    attach_readout_positions: ClassVar[bool] = True

    params: QubitRamseyParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            "idle_time_ns": time_axis_ns(
                self.params.min_idle_time_ns, self.params.max_idle_time_ns, self.params.num_points
            )
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        t = coords["idle_time_ns"] * 1e-9
        targets = self.params.targets
        rng = np.random.default_rng(stable_seed("qubit_ramsey", *targets))
        applied = self.params.frequency_detuning_hz
        use_state = self.params.use_state_discrimination
        i_data = np.empty((len(targets), t.size))
        q_data = np.empty_like(i_data)
        state = np.empty_like(i_data)
        for k in range(len(targets)):
            err = rng.uniform(-0.2, 0.2) * applied  # residual detuning to recover
            t2_star = rng.uniform(5e-6, 15e-6)
            fringe = 0.5 - 0.5 * np.exp(-t / t2_star) * np.cos(2 * np.pi * (applied + err) * t)
            if use_state:
                state[k] = state_row(fringe, rng)
            else:
                i_data[k], q_data[k] = iq_from_population(fringe, rng)
        return readout_vars(use_state, state, i_data, q_data)

    def estimate(self) -> QubitRamseyResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.ramsey import RamseyEstimator

        # scqat's contract: complex IQ (`I`/`Q`) + coord `idle_time` in seconds. The estimator
        # reduces IQ to the signed axial projection, then selects single/beat/relaxation; the
        # beat (charge dispersion) case calibrates on the mean of the two fringe frequencies.
        # A discriminated probe returns the averaged `state` instead — the estimator's
        # pre-reduced `signal` input.
        rename = signal_rename(self.dataset, {"idle_time_ns": "idle_time"})
        prepared = self.dataset.rename(rename)
        prepared = prepared.assign_coords(idle_time=prepared["idle_time"] * 1e-9)

        results = per_qubit_results(prepared, RamseyEstimator(), artifact_dir=self.artifact_dir)

        applied = self.params.frequency_detuning_hz
        result = QubitRamseyResult()
        for qubit in self.params.targets:
            r = results[qubit]
            model_type = r.get("model_type")
            if model_type == "beat":
                osc_freq = 0.5 * (float(r["f_1"]) + float(r["f_2"]))
            else:
                osc_freq = float(r["f_1"])
            detuning_error = osc_freq - applied
            t2_star = float(r.get("tau_1", float("nan")))
            old = float(self.device.channel(qubit, "drive").drive_freq_hz)
            result.fit[qubit] = {
                "drive_freq_hz": old + detuning_error,
                # the measured FACT twin of the drive_freq_hz knob (same fit)
                "f_01_hz": old + detuning_error,
                "detuning_error_hz": detuning_error,
                "t2_star_s": t2_star,
                "old_drive_freq_hz": old,
            }
            # A fringe is expected: only a converged single/beat fit with a physical T2*
            # counts; the relaxation model (f=0) means no fringe was resolved.
            ok = bool(r["success"]) and model_type in ("single", "beat") and np.isfinite(t2_star) and t2_star > 0
            result.outcomes[qubit] = Outcome.SUCCESSFUL if ok else Outcome.FAILED
        return result

    def update(self) -> None:
        if self.result is None:
            return
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is Outcome.SUCCESSFUL:
                # Knob first as documentation of intent; under the entity
                # split, apply atomicity is per-ENTITY now (q0_xy vs q0), so
                # a vendor rejection of the drive frequency no longer skips
                # the mode facts — they are vendor-free and land regardless.
                self.device.channel(qubit, "drive").drive_freq_hz = (
                    fit["drive_freq_hz"])  # the instrument knob
                mode = self.device.component(qubit)
                mode.f_01_hz = fit["f_01_hz"]  # the measured physical fact (same fit)
                mode.t2_star_s = fit["t2_star_s"]

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
