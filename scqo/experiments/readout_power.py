"""Readout-amplitude optimization by single-shot fidelity, greenfield.

Port of :mod:`scqo.experiments.readout_power`. The physics half is
byte-for-byte; what moved is the device surface: the ``readout_amp``
knob is read and written on the target's READOUT CHANNEL
(``self.device.channel(target, "readout").readout_amp``) instead of the
old component view. Fit keys are unchanged (``readout_amp`` keeps its
spelling on the channel).
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
from ._sim import stable_seed
from ..parameters import TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register


class ReadoutPowerParameters(TargetSelection, QubitResetParameters, AmplitudeSweepParameters):
    """Inputs for fidelity-vs-readout-amplitude optimization."""

    min_amp_factor: float = Field(0.4, gt=0.0, description=MIN_AMP_FACTOR_DESC)
    max_amp_factor: float = Field(1.8, gt=0.0, lt=2.0, description=MAX_AMP_FACTOR_DESC)
    # one Gaussian-mixture fit per point, so this is the cost driver
    num_amp_points: int = Field(16, gt=2, description=NUM_AMP_POINTS_DESC)
    num_shots: int = Field(1000, gt=99, description="Shots per prepared state per amplitude.")


class ReadoutPowerResult(Result):
    """``fit[target]``: ``readout_amp`` (new), ``opt_amp_prefactor``,
    ``best_fidelity``, ``old_readout_amp``."""


@register
class ReadoutPower(Experiment):
    """Backend-agnostic per-shot readout-amplitude scan. Probes must not average."""

    name: ClassVar[str] = "readout_power"
    description: ClassVar[str] = (
        "Sweep the readout-amplitude prefactor recording every shot's I/Q for |g> and "
        "|e>; picks the fidelity-optimal amplitude and updates the readout channel's "
        "readout_amp."
    )
    Parameters: ClassVar[type] = ReadoutPowerParameters
    Result: ClassVar[type] = ReadoutPowerResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=(AMP_AXIS, "prepared_state", "shot_idx"),
        sweep_units=("", "state", "shot"),
        variables=("I", "Q"),
    )
    required_operations: ClassVar[tuple[str, ...]] = ("readout",)

    params: ReadoutPowerParameters

    def amp_reference_field(self) -> str:
        return "readout_amp"

    def attach_acquisition_coords(self) -> None:
        attach_absolute_amp(self)

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            **amp_sweep(self.params),
            "prepared_state": np.array([0, 1]),
            "shot_idx": np.arange(self.params.num_shots),
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        amps = coords[AMP_AXIS]
        n_shots = coords["shot_idx"].size
        targets = self.params.targets
        rng = np.random.default_rng(stable_seed("readout_power", *targets))
        i_data = np.empty((len(targets), amps.size, 2, n_shots))
        q_data = np.empty_like(i_data)
        for k in range(len(targets)):
            sep_unit = rng.uniform(2.2, 3.0)  # blob separation per unit prefactor (sigma units)
            knee = rng.uniform(1.0, 1.5)  # measurement-induced transitions set in above this
            for j, a in enumerate(amps):
                sep = sep_unit * a
                extra = 0.25 * max(0.0, a - knee)
                for state in (0, 1):
                    flip = (0.02 if state == 0 else 0.05) + extra
                    actual = np.where(rng.random(n_shots) < flip, 1 - state, state)
                    i_data[k, j, state] = actual * sep + rng.normal(0, 1.0, n_shots)
                    q_data[k, j, state] = rng.normal(0, 1.0, n_shots)
        return {"I": i_data, "Q": q_data}

    def estimate(self) -> ReadoutPowerResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.readout_fidelity import ReadoutPowerFidelityEstimator

        # scqat's contract: I/Q over (amp_prefactor, prepared_state, shot_idx) — names match.
        prepared = self.dataset.transpose("target", AMP_AXIS, "prepared_state", "shot_idx")
        # the SAME read the attached `digital_amp` axis used, so the two cannot disagree
        old_amps = {q: amp_anchor(self, q) for q in self.params.targets}

        results = per_qubit_results(
            prepared, ReadoutPowerFidelityEstimator(), artifact_dir=self.artifact_dir,
            twin_coord=ABS_AMP_COORD, twin_label=ABS_AMP_LABEL,
        )

        result = ReadoutPowerResult()
        for qubit in self.params.targets:
            r = results[qubit]
            best = r.get("best_sweep_value")
            fidelity = r.get("best_fidelity")
            old_amp = old_amps[qubit]
            ok = bool(r.get("success")) and best is not None and np.isfinite(best)
            result.fit[qubit] = {
                "readout_amp": old_amp * float(best) if ok else float("nan"),
                "opt_amp_prefactor": float(best) if best is not None else float("nan"),
                "best_fidelity": float(fidelity) if fidelity is not None else float("nan"),
                "old_readout_amp": old_amp,
            }
            result.outcomes[qubit] = Outcome.SUCCESSFUL if ok else Outcome.FAILED
        return result

    def update(self) -> None:
        if self.result is None:
            return
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is Outcome.SUCCESSFUL:
                self.device.channel(qubit, "readout").readout_amp = fit["readout_amp"]

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
