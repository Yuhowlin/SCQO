"""Qubit relaxation — excited-state lifetime T1, greenfield.

Port of :mod:`scqo.experiments.qubit_relaxation`. The physics half is
byte-for-byte; ``t1_s`` is a mode fact whose name and home survive the
model change, so ``update()`` still writes ``self.device.component(q).t1_s``
— only the imports moved (capability helpers from
``scqo.experiments._capabilities.state_readout``) and the driver stub
``probe()`` was added.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from pydantic import Field

from .._scqat import per_qubit_results
from ..contract import DatasetContract
from ._capabilities.state_readout import (
    STATE_ALT,
    StateReadoutParameters,
    readout_vars,
    signal_rename,
    state_row,
)
from ._sim import iq_from_population, stable_seed
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register


class QubitRelaxationParameters(TargetSelection, AveragingParameters, StateReadoutParameters):
    """Inputs for a T1 relaxation measurement."""

    min_wait_ns: float = Field(16, ge=0, description="Shortest delay after the pi pulse.")
    max_wait_ns: float = Field(200_000, gt=0, description="Longest delay (should exceed a few T1).")
    num_points: int = Field(51, gt=1, description="Number of delay points.")


class QubitRelaxationResult(Result):
    """``fit[target]`` carries ``t1_s`` (plus fit amplitude/offset); proposed as a
    physical parameter by ``update()``."""


@register
class QubitRelaxation(Experiment):
    """Backend-agnostic T1: pi pulse -> swept wait -> measure -> exponential fit."""

    name: ClassVar[str] = "qubit_relaxation"
    description: ClassVar[str] = (
        "Excite with a pi pulse, wait a swept delay and measure; fits the exponential "
        "decay and proposes t1_s as a physical parameter (sample physics, no instrument "
        "knob). use_state_discrimination returns the FPGA-discriminated averaged state "
        "instead of I/Q (needs a calibrated discriminator: run single_shot_readout and "
        "accept its readout_rotation_rad / readout_threshold suggestions first)."
    )
    Parameters: ClassVar[type] = QubitRelaxationParameters
    Result: ClassVar[type] = QubitRelaxationResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("wait_time_ns",), sweep_units=("ns",), variables=("I", "Q"),
        alt_variables=STATE_ALT,
    )
    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout")
    #: stored blob centers ride the dataset -> axial axis = the measured g->e vector
    attach_readout_positions: ClassVar[bool] = True

    params: QubitRelaxationParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            "wait_time_ns": np.linspace(self.params.min_wait_ns, self.params.max_wait_ns, self.params.num_points)
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        t = coords["wait_time_ns"] * 1e-9
        targets = self.params.targets
        rng = np.random.default_rng(stable_seed("qubit_relaxation", *targets))
        use_state = self.params.use_state_discrimination
        i_data = np.empty((len(targets), t.size))
        q_data = np.empty_like(i_data)
        state = np.empty_like(i_data)
        for k in range(len(targets)):
            t1 = rng.uniform(20e-6, 60e-6)  # hidden truth the fit must recover
            population = np.exp(-t / t1)
            if use_state:
                state[k] = state_row(population, rng)
            else:
                i_data[k], q_data[k] = iq_from_population(population, rng)
        return readout_vars(use_state, state, i_data, q_data)

    def estimate(self) -> QubitRelaxationResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.qubit_relaxation import QubitRelaxationEstimator

        # scqat's contract: complex IQ (`I`/`Q`) + coord `wait_time` in seconds; the
        # estimator reduces IQ to the signed axial projection before the decay fit.
        # A discriminated probe returns the averaged `state` instead — the estimator's
        # pre-reduced `signal` input.
        rename = signal_rename(self.dataset, {"wait_time_ns": "wait_time"})
        prepared = self.dataset.rename(rename)
        prepared = prepared.assign_coords(wait_time=prepared["wait_time"] * 1e-9)

        results = per_qubit_results(prepared, QubitRelaxationEstimator(), artifact_dir=self.artifact_dir)

        result = QubitRelaxationResult()
        for qubit in self.params.targets:
            r = results[qubit]
            result.fit[qubit] = {
                "t1_s": float(r["t1"]),
                "t1_stderr_s": float(r["t1_stderr"]),
                "amplitude": float(r["amplitude"]),
                "offset": float(r["offset"]),
            }
            result.outcomes[qubit] = Outcome.SUCCESSFUL if bool(r["success"]) else Outcome.FAILED
        return result

    def update(self) -> None:
        # Record T1 as device state (record-only field: history + config, no push).
        if self.result is None:
            return
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is Outcome.SUCCESSFUL:
                self.device.component(qubit).t1_s = fit["t1_s"]

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
