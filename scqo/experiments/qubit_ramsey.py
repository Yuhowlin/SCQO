"""Qubit Ramsey — second worked experiment, proving the pattern generalizes (backend-free half).

Differs from resonator spectroscopy on every axis that matters:
  * sweep is **time** (idle delay), not frequency;
  * fit is a **decaying cosine** yielding **two** quantities (residual detuning + T2*);
  * writeback targets a **different** neutral device field: ``drive_freq``.

A driver still only adds ``probe()``.

Detuning convention: the experiment deliberately detunes the drive by
``frequency_detuning_hz``; the Ramsey fringe then oscillates at
``frequency_detuning_hz + err`` where ``err`` is the residual qubit-drive detuning.
``estimate`` recovers ``err`` and corrects ``drive_freq`` by it.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from pydantic import Field

from .._scqat import per_qubit_results
from ._sim import iq_from_population, stable_seed
from ..contract import DatasetContract
from ..parameters import AveragingParameters, TargetSelection
from ..experiment import Experiment
from ..result import Outcome, Result


class QubitRamseyParameters(TargetSelection, AveragingParameters):
    """Inputs for a Ramsey experiment."""

    frequency_detuning_hz: float = Field(
        1.0e6, gt=0, description="Artificial drive detuning applied to make the fringe oscillate."
    )
    min_idle_time_ns: float = Field(16, ge=0, description="Shortest idle delay between the two pi/2 pulses.")
    max_idle_time_ns: float = Field(4000, gt=0, description="Longest idle delay.")
    num_points: int = Field(101, gt=1, description="Number of idle-time points.")
    use_state_discrimination: bool = Field(
        False,
        description="Discriminate each shot on the FPGA and return the averaged state "
        "(population) instead of I/Q. Requires a calibrated discriminator "
        "(QM: the qualibrate 07_iq_blobs node's integration_weights_angle + threshold).",
    )


class QubitRamseyResult(Result):
    """Output of QubitRamsey.

    ``fit[qubit]`` carries ``drive_freq`` (new absolute Hz), its measured twin
    ``f_01_hz`` (same value; ``update()`` writes the knob and the fact together),
    ``detuning_error_hz``, ``t2_star_s`` and ``old_drive_freq``.
    """


class QubitRamsey(Experiment):
    """Backend-agnostic Ramsey. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "qubit_ramsey"
    description: ClassVar[str] = (
        "Two pi/2 pulses separated by a swept idle time with an artificial drive detuning; "
        "fits the decaying fringe to correct drive_freq and report T2*. "
        "use_state_discrimination returns the FPGA-discriminated averaged state instead "
        "of I/Q (needs a calibrated discriminator: run single_shot_readout with "
        "calibrate_discriminator=true first)."
    )
    Parameters: ClassVar[type] = QubitRamseyParameters
    Result: ClassVar[type] = QubitRamseyResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("idle_time_ns",), sweep_units=("ns",), variables=("I", "Q"),
        alt_variables=(("state",),),
    )
    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout")

    params: QubitRamseyParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            "idle_time_ns": np.linspace(
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
                # FPGA-discriminated averaged state: a population in [0, 1]
                state[k] = np.clip(fringe + rng.normal(0, 0.02, t.size), 0.0, 1.0)
            else:
                i_data[k], q_data[k] = iq_from_population(fringe, rng)
        return {"state": state} if use_state else {"I": i_data, "Q": q_data}

    def estimate(self) -> QubitRamseyResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.ramsey import RamseyEstimator

        # scqat's contract: complex IQ (`I`/`Q`) + coord `idle_time` in seconds. The estimator
        # reduces IQ to the signed axial projection, then selects single/beat/relaxation; the
        # beat (charge dispersion) case calibrates on the mean of the two fringe frequencies.
        # A discriminated probe returns the averaged `state` instead — the estimator's
        # pre-reduced `signal` input.
        rename = {"idle_time_ns": "idle_time"}
        if "state" in self.dataset.data_vars:
            rename["state"] = "signal"
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
            old = float(self.device.component(qubit).drive_freq)
            result.fit[qubit] = {
                "drive_freq": old + detuning_error,
                # the measured FACT twin of the drive_freq knob (same fit)
                "f_01_hz": old + detuning_error,
                "detuning_error_hz": detuning_error,
                "t2_star_s": t2_star,
                "old_drive_freq": old,
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
                # Calibration knob first: applies are per-qubit atomic in capture
                # order, so if the vendor rejects the corrected drive frequency the
                # physical fields (f_01_hz, T2*) are skipped too — no half-applied
                # qubit.
                view = self.device.component(qubit)
                view.drive_freq = fit["drive_freq"]  # the instrument knob
                view.f_01_hz = fit["f_01_hz"]  # the measured physical fact (same fit)
                view.t2_star_s = fit["t2_star_s"]
