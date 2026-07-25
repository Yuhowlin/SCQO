"""Qubit echo — Hahn-echo coherence time T2_echo, greenfield.

Port of :mod:`scqo.experiments.qubit_echo`. The physics half is
preserved (cosmetically reflowed); the fitted ``t2_echo_s`` is a MODE fact and keeps its
attribute-style landing on ``self.device.component(target)`` — what moved
is only the import surface (capability helpers from their current
locations) and the kind-based gating inherited from the greenfield base.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from pydantic import Field

from ..._scqat import per_qubit_results
from ...contract import DatasetContract
from ...experiments._capabilities.state_readout import (
    STATE_ALT,
    StateReadoutParameters,
    readout_vars,
    signal_rename,
    state_row,
)
from ...experiments._sim import iq_from_population, stable_seed
from ...parameters import AveragingParameters, TargetSelection
from ...result import Outcome, Result
from ..experiment import Experiment
from . import register


class QubitEchoParameters(TargetSelection, AveragingParameters, StateReadoutParameters):
    """Inputs for a Hahn-echo measurement."""

    min_wait_ns: float = Field(32, ge=0, description="Shortest total echo idle time.")
    max_wait_ns: float = Field(400_000, gt=0, description="Longest total idle time (should exceed a few T2_echo).")
    num_points: int = Field(51, gt=1, description="Number of idle-time points.")


class QubitEchoResult(Result):
    """``fit[target]`` carries ``t2_echo_s`` (plus fit amplitude/offset); proposed as a
    physical parameter by ``update()``."""


@register
class QubitEcho(Experiment):
    """Backend-agnostic Hahn echo: X90 - tau/2 - X - tau/2 - X90 -> exponential fit."""

    name: ClassVar[str] = "qubit_echo"
    description: ClassVar[str] = (
        "Hahn echo (X90 - tau/2 - X - tau/2 - X90) over a swept total idle time; fits "
        "the exponential envelope and proposes t2_echo_s as a physical parameter "
        "(sample physics, no instrument knob). use_state_discrimination returns the "
        "FPGA-discriminated averaged state instead of I/Q (needs a calibrated "
        "discriminator: run single_shot_readout and accept its readout_rotation_rad / "
        "readout_threshold suggestions first)."
    )
    Parameters: ClassVar[type] = QubitEchoParameters
    Result: ClassVar[type] = QubitEchoResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("wait_time_ns",), sweep_units=("ns",), variables=("I", "Q"),
        alt_variables=STATE_ALT,
    )
    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout")
    #: stored blob centers ride the dataset -> axial axis = the measured g->e vector
    attach_readout_positions: ClassVar[bool] = True

    params: QubitEchoParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            "wait_time_ns": np.linspace(self.params.min_wait_ns, self.params.max_wait_ns, self.params.num_points)
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        t = coords["wait_time_ns"] * 1e-9
        targets = self.params.targets
        rng = np.random.default_rng(stable_seed("qubit_echo", *targets))
        use_state = self.params.use_state_discrimination
        i_data = np.empty((len(targets), t.size))
        q_data = np.empty_like(i_data)
        state = np.empty_like(i_data)
        for k in range(len(targets)):
            t2e = rng.uniform(30e-6, 80e-6)  # hidden truth the fit must recover
            population = np.exp(-t / t2e)
            if use_state:
                state[k] = state_row(population, rng)
            else:
                i_data[k], q_data[k] = iq_from_population(population, rng)
        return readout_vars(use_state, state, i_data, q_data)

    def estimate(self) -> QubitEchoResult:
        assert self.dataset is not None
        from scqat.estimators.qubit_echo import QubitEchoEstimator

        # scqat's contract: complex IQ (`I`/`Q`) + coord `idle_time` in seconds; the
        # estimator reduces IQ to the signed axial projection before the decay fit.
        # A discriminated probe returns the averaged `state` instead — the estimator's
        # pre-reduced `signal` input.
        rename = signal_rename(self.dataset, {"wait_time_ns": "idle_time"})
        prepared = self.dataset.rename(rename)
        prepared = prepared.assign_coords(idle_time=prepared["idle_time"] * 1e-9)

        results = per_qubit_results(prepared, QubitEchoEstimator(), artifact_dir=self.artifact_dir)

        result = QubitEchoResult()
        for target in self.params.targets:
            r = results[target]
            result.fit[target] = {
                "t2_echo_s": float(r["t2_echo"]),
                "t2_echo_stderr_s": float(r["t2_echo_stderr"]),
                "amplitude": float(r["amplitude"]),
                "offset": float(r["offset"]),
            }
            result.outcomes[target] = Outcome.SUCCESSFUL if bool(r["success"]) else Outcome.FAILED
        return result

    def update(self) -> None:
        # Record T2_echo as device state (record-only field: history + config, no push).
        if self.result is None:
            return
        for target, fit in self.result.fit.items():
            if self.result.outcomes[target] is Outcome.SUCCESSFUL:
                self.device.component(target).t2_echo_s = fit["t2_echo_s"]

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
