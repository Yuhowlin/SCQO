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
from ._capabilities.amplitude import (
    ABS_AMP_COORD,
    ABS_AMP_LABEL,
    AMP_AXIS,
    MAX_AMP_FACTOR_DESC,
    MIN_AMP_FACTOR_DESC,
    NUM_AMP_POINTS_DESC,
    AmplitudeSweepParameters,
    amp_anchor,
    amp_sweep,
    attach_absolute_amp,
)
from ._capabilities.qubit_reset import QubitResetParameters
from ._capabilities.state_readout import (
    POPULATION_ALT,
    StateReadoutParameters,
    readout_vars,
    signal_rename,
    population_row,
)
from ._sim import iq_from_population, stable_seed
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register


class QubitPowerRabiParameters(TargetSelection, AveragingParameters, StateReadoutParameters,
                               QubitResetParameters, AmplitudeSweepParameters):
    """Inputs for power Rabi."""

    # a full Rabi arch from zero, so the first extremum IS the pi pulse. The top
    # stops below 2.0: QUA's dynamic amplitude_scale is fixed-point on (-2, 2),
    # so a sweep that INCLUDES 2.0 emits an unrepresentable last point.
    min_amp_factor: float = Field(0.0, ge=0.0, description=MIN_AMP_FACTOR_DESC)
    max_amp_factor: float = Field(1.9, gt=0.0, lt=2.0, description=MAX_AMP_FACTOR_DESC)
    num_amp_points: int = Field(101, gt=1, description=NUM_AMP_POINTS_DESC)


class QubitPowerRabiResult(Result):
    """Output of QubitPowerRabi.

    ``fit[target]`` carries ``pi_amp`` (new absolute), ``opt_amp_prefactor``
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
        sweeps=(AMP_AXIS,), sweep_units=("",), variables=("I", "Q"),
        alt_variables=POPULATION_ALT,
    )
    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout")
    #: stored blob centers ride the dataset -> axial axis = the measured g->e vector
    attach_readout_positions: ClassVar[bool] = True
    params: QubitPowerRabiParameters

    def amp_reference_field(self) -> str:
        return "pi_amp"

    def attach_acquisition_coords(self) -> None:
        attach_absolute_amp(self)

    def define_sweep(self) -> dict[str, np.ndarray]:
        return amp_sweep(self.params)

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        factor = coords[AMP_AXIS]
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
                state[k] = population_row(population, rng)
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
        # No axis rename: AMP_AXIS IS scqat's coord name (that is why it was the one
        # chosen). `digital_amp` is a coord over the swept dim, so per_qubit_results'
        # per-target split reduces it to the 1-D companion scale the estimator draws
        # as a second x-axis.
        prepared = self.dataset.rename(signal_rename(self.dataset))

        results = per_qubit_results(
            prepared, PowerRabiEstimator(), artifact_dir=self.artifact_dir,
            twin_coord=ABS_AMP_COORD, twin_label=ABS_AMP_LABEL,
        )

        result = QubitPowerRabiResult()
        for qubit in self.params.targets:
            r = results[qubit]
            factor_pi = float(r["opt_amp_prefactor"])
            # the SAME read the attached axis used, so the two cannot disagree
            old = amp_anchor(self, qubit)
            result.fit[qubit] = {
                "pi_amp": old * factor_pi,
                "opt_amp_prefactor": factor_pi,
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
