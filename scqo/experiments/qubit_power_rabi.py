"""Qubit power Rabi — amplitude-sweep calibration, greenfield.

Port of :mod:`scqo.experiments.qubit_power_rabi`. The physics half is
byte-for-byte; what moved is the device surface: ``pi_amp`` keeps its name
but now lives on the target's DRIVE CHANNEL
(``self.device.channel(t, "drive").pi_amp``), read in ``estimate()`` and
written in ``update()``.
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
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register


class QubitPowerRabiParameters(TargetSelection, AveragingParameters, StateReadoutParameters, QubitResetParameters):
    """Inputs for power Rabi."""

    min_amp_factor: float = Field(0.0, ge=0, description="Lowest drive amplitude, as a factor of current pi_amp.")
    max_amp_factor: float = Field(2.0, gt=0, description="Highest drive amplitude, as a factor of current pi_amp.")
    num_points: int = Field(101, gt=1, description="Number of amplitude points.")


class QubitPowerRabiResult(Result):
    """Output of QubitPowerRabi.

    ``fit[target]`` carries ``pi_amp`` (new absolute), ``pi_amp_factor``
    (recovered factor) and ``old_pi_amp``.
    """


@register
class QubitPowerRabi(Experiment):
    """Backend-agnostic power Rabi. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "qubit_power_rabi"
    description: ClassVar[str] = (
        "Sweep drive amplitude (as a factor of the current pi pulse) and fit the Rabi "
        "oscillation to recalibrate the drive channel's pi_amp. use_state_discrimination "
        "returns the FPGA-discriminated averaged state instead of I/Q (needs a calibrated "
        "discriminator: run single_shot_readout and accept its readout_rotation_rad / "
        "readout_threshold suggestions first)."
    )
    Parameters: ClassVar[type] = QubitPowerRabiParameters
    Result: ClassVar[type] = QubitPowerRabiResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("amp_factor",), sweep_units=("dimensionless",), variables=("I", "Q"),
        alt_variables=STATE_ALT,
    )
    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout")
    #: stored blob centers ride the dataset -> axial axis = the measured g->e vector
    attach_readout_positions: ClassVar[bool] = True

    params: QubitPowerRabiParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            "amp_factor": np.linspace(
                self.params.min_amp_factor, self.params.max_amp_factor, self.params.num_points
            )
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        factor = coords["amp_factor"]
        targets = self.params.targets
        rng = np.random.default_rng(stable_seed("qubit_power_rabi", *targets))
        use_state = self.params.use_state_discrimination
        i_data = np.empty((len(targets), factor.size))
        q_data = np.empty_like(i_data)
        state = np.empty_like(i_data)
        for k in range(len(targets)):
            factor_pi = rng.uniform(0.85, 1.15)  # miscalibration to recover (1.0 == perfect)
            population = 0.5 - 0.5 * np.cos(np.pi * factor / factor_pi)
            if use_state:
                state[k] = state_row(population, rng)
            else:
                i_data[k], q_data[k] = iq_from_population(population, rng)
        return readout_vars(use_state, state, i_data, q_data)

    def estimate(self) -> QubitPowerRabiResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.power_rabi import PowerRabiEstimator

        # scqat's contract: complex IQ (`I`/`Q`) + coord `amp_prefactor` (the dimensionless
        # amplitude multiplier). The estimator reduces IQ to the signed axial projection
        # onto the |0>-|1> axis and returns `opt_amp_prefactor` == the pi-pulse factor.
        # A discriminated probe returns the averaged `state` instead — the estimator's
        # pre-reduced `signal` input.
        rename = signal_rename(self.dataset, {"amp_factor": "amp_prefactor"})
        prepared = self.dataset.rename(rename)

        results = per_qubit_results(prepared, PowerRabiEstimator(), artifact_dir=self.artifact_dir)

        result = QubitPowerRabiResult()
        for qubit in self.params.targets:
            r = results[qubit]
            factor_pi = float(r["opt_amp_prefactor"])
            old = float(self.device.channel(qubit, "drive").pi_amp)
            result.fit[qubit] = {
                "pi_amp": old * factor_pi,
                "pi_amp_factor": factor_pi,
                "old_pi_amp": old,
            }
            result.outcomes[qubit] = Outcome.SUCCESSFUL if bool(r["success"]) else Outcome.FAILED
        return result

    def update(self) -> None:
        if self.result is None:
            return
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is Outcome.SUCCESSFUL:
                self.device.channel(qubit, "drive").pi_amp = fit["pi_amp"]

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
