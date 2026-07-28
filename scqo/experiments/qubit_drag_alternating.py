"""Qubit DRAG alternating-pulse calibration, greenfield.

Port of :mod:`scqo.experiments.qubit_drag_alternating`. The physics half is
byte-for-byte; what moved is the device surface: ``drag_beta`` keeps its
name but now lives on the target's DRIVE CHANNEL
(``self.device.channel(t, "drive").drag_beta``), written in ``update()``.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from pydantic import Field

from ..contract import DatasetContract
from ._capabilities.qubit_reset import QubitResetParameters
from ._sim import stable_seed
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register


class QubitDragAlternatingParameters(TargetSelection, AveragingParameters, QubitResetParameters):
    """Inputs for an alternating pulse error amplification DRAG calibration experiment."""

    min_beta: float = Field(-2.0, description="Minimum DRAG beta coefficient / pre-factor.")
    max_beta: float = Field(2.0, description="Maximum DRAG beta coefficient / pre-factor.")
    num_beta_points: int = Field(41, gt=1, description="Number of beta sweep points.")
    max_pulses: int = Field(20, gt=0, description="Maximum number of alternating pulses.")
    num_pulse_points: int = Field(10, gt=1, description="Number of pulse sweep points.")
    target_gate: str = Field("x180", description="Gate to calibrate: 'x180' or 'x90'.")


class QubitDragAlternatingResult(Result):
    """Fitted optimal DRAG beta parameters."""


@register
class QubitDragAlternating(Experiment):
    """Calibrate DRAG parameter using the alternating pulse method."""

    name: ClassVar[str] = "qubit_drag_alternating"
    description: ClassVar[str] = (
        "Sweep DRAG beta coefficient and play alternating pulse sequences. "
        "The DRAG value that minimizes error accumulation (stays flat at "
        "ground state) is the optimal calibration point."
    )
    Parameters: ClassVar[type] = QubitDragAlternatingParameters
    Result: ClassVar[type] = QubitDragAlternatingResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("nb_of_pulses", "beta"),
        sweep_units=("", ""),
        variables=("I", "Q"),
    )

    params: QubitDragAlternatingParameters

    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout")

    def define_sweep(self) -> dict[str, np.ndarray]:
        beta = np.linspace(
            self.params.min_beta,
            self.params.max_beta,
            self.params.num_beta_points,
        )
        nb_of_pulses = np.linspace(
            2,
            self.params.max_pulses,
            self.params.num_pulse_points,
            dtype=int,
        )
        return {
            "nb_of_pulses": nb_of_pulses,
            "beta": beta,
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        nb_of_pulses = coords["nb_of_pulses"]
        beta = coords["beta"]
        qubits = self.params.targets

        n_qubits = len(qubits)
        n_pulses = len(nb_of_pulses)
        n_beta = len(beta)

        i_data = np.zeros((n_qubits, n_pulses, n_beta))
        q_data = np.zeros((n_qubits, n_pulses, n_beta))

        rng = np.random.default_rng(stable_seed("qubit_drag_alternating", *qubits))
        for k, qubit in enumerate(qubits):
            opt_beta = rng.uniform(-1.0, 1.0)
            noise = 0.02
            for p_idx, n_p in enumerate(nb_of_pulses):
                decay = np.exp(-0.05 * n_p)
                i_data[k, p_idx] = 0.5 * (1 - decay * np.cos(n_p * (beta - opt_beta))) + rng.normal(0, noise, n_beta)
                q_data[k, p_idx] = rng.normal(0, noise, n_beta)

        return {"I": i_data, "Q": q_data}

    def estimate(self) -> QubitDragAlternatingResult:
        assert self.dataset is not None
        from scqat.estimators.qubit_drag_alternating import QubitDragAlternatingEstimator
        from .._scqat import per_qubit_results

        # Map variable I as signal to scqat
        prepared = self.dataset.rename({"I": "signal"})

        results = per_qubit_results(
            prepared, QubitDragAlternatingEstimator(), artifact_dir=self.artifact_dir
        )

        result = QubitDragAlternatingResult()
        for qubit in self.params.targets:
            r = results[qubit]
            result.fit[qubit] = {
                "opt_beta": float(r["opt_beta"]),
                "beta": [float(x) for x in r["beta"]],
                "nb_of_pulses": [int(x) for x in r["nb_of_pulses"]],
            }
            result.outcomes[qubit] = Outcome.SUCCESSFUL if r.get("success", False) else Outcome.FAILED
        return result

    def update(self) -> None:
        if self.result is None:
            return
        target_gate = getattr(self.params, "target_gate", "x180")
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is Outcome.SUCCESSFUL and fit.get("opt_beta") is not None:
                chan = self.device.channel(qubit, "drive")
                if target_gate == "x90":
                    if hasattr(chan, "drag_beta_x90"):
                        chan.drag_beta_x90 = fit["opt_beta"]
                    elif hasattr(chan, "set_drag_beta"):
                        chan.set_drag_beta(fit["opt_beta"], operation="x90", lock_x90=False)
                else:
                    chan.drag_beta = fit["opt_beta"]





    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
