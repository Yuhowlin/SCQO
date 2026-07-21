"""Readout-amplitude optimization by single-shot fidelity (backend-free half).

Per-shot experiment: for every readout-amplitude prefactor, prepare |g> and |e>
and record each shot's I/Q; a two-Gaussian fit per amplitude gives fidelity(amp),
and the best amplitude (highest fidelity, outlier-fraction guarded) is written
back to ``readout_amp``. Complements the punchout (which picks the dispersive-
regime power from averaged S21): this one optimizes the same knob by what
actually matters — assignment fidelity.

Method note: the Qblox reference cal17 calibrates readout amplitude via the
AC-Stark shift of the QUBIT frequency — a different (averaged) method; both
backends here follow the QM ``readout_power`` probe's per-shot fidelity approach.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from pydantic import Field

from .._scqat import per_qubit_results
from ._sim import stable_seed
from ..contract import DatasetContract
from ..experiment import Experiment
from ..parameters import Parameters, TargetSelection
from ..result import Outcome, Result


class ReadoutPowerParameters(TargetSelection, Parameters):
    """Inputs for fidelity-vs-readout-amplitude optimization."""

    min_amp_factor: float = Field(0.4, gt=0, description="Lowest amplitude prefactor (x current readout_amp).")
    max_amp_factor: float = Field(1.8, le=2.0, description="Highest prefactor (QUA amplitude_scale cap: 2).")
    num_amp_points: int = Field(16, gt=2, description="Amplitude points (one Gaussian-mixture fit per point).")
    num_shots: int = Field(1000, gt=99, description="Shots per prepared state per amplitude.")


class ReadoutPowerResult(Result):
    """``fit[qubit]``: ``readout_amp`` (new), ``best_amp_factor``, ``best_fidelity``,
    ``old_readout_amp``."""


class ReadoutPower(Experiment):
    """Backend-agnostic per-shot readout-amplitude scan. Probes must not average."""

    name: ClassVar[str] = "readout_power"
    description: ClassVar[str] = (
        "Sweep the readout-amplitude prefactor recording every shot's I/Q for |g> and "
        "|e>; picks the fidelity-optimal amplitude and updates readout_amp."
    )
    Parameters: ClassVar[type] = ReadoutPowerParameters
    Result: ClassVar[type] = ReadoutPowerResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("amp_prefactor", "prepared_state", "shot_idx"),
        sweep_units=("x", "state", "shot"),
        variables=("I", "Q"),
    )
    required_operations: ClassVar[tuple[str, ...]] = ("readout",)

    params: ReadoutPowerParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            "amp_prefactor": np.linspace(self.params.min_amp_factor, self.params.max_amp_factor, self.params.num_amp_points),
            "prepared_state": np.array([0, 1]),
            "shot_idx": np.arange(self.params.num_shots),
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        amps = coords["amp_prefactor"]
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
        prepared = self.dataset.transpose("target", "amp_prefactor", "prepared_state", "shot_idx")
        old_amps = {q: float(self.device.component(q).readout_amp) for q in self.params.targets}

        results = per_qubit_results(
            prepared, ReadoutPowerFidelityEstimator(), artifact_dir=self.artifact_dir
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
                "best_amp_factor": float(best) if best is not None else float("nan"),
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
                self.device.component(qubit).readout_amp = fit["readout_amp"]
