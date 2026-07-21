"""Readout-frequency optimization by single-shot fidelity (backend-free half).

Per-shot experiment: for every readout detuning, prepare |g> and |e> and record
each shot's I/Q; a two-Gaussian fit per frequency gives fidelity(freq), and the
fidelity-optimal frequency is written back to ``readout_freq``. This picks the
point of maximum dispersive contrast |alpha_g - alpha_e| — the same goal as a
dispersive-shift (chi) scan (Qblox reference cal13), measured by the criterion
that matters directly: assignment fidelity.
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


class ReadoutFrequencyParameters(TargetSelection, Parameters):
    """Inputs for fidelity-vs-readout-frequency optimization."""

    frequency_span_hz: float = Field(5e6, gt=0, description="Total detuning span around the current readout_freq (chi-scale).")
    num_freq_points: int = Field(21, gt=2, description="Frequency points (one Gaussian-mixture fit per point).")
    num_shots: int = Field(1000, gt=99, description="Shots per prepared state per frequency.")


class ReadoutFrequencyResult(Result):
    """``fit[qubit]``: ``readout_freq`` (new), ``frequency_shift_hz``, ``best_fidelity``,
    ``old_readout_freq``."""


class ReadoutFrequency(Experiment):
    """Backend-agnostic per-shot readout-frequency scan. Probes must not average."""

    name: ClassVar[str] = "readout_frequency"
    description: ClassVar[str] = (
        "Sweep the readout detuning recording every shot's I/Q for |g> and |e>; picks "
        "the fidelity-optimal frequency and updates readout_freq."
    )
    Parameters: ClassVar[type] = ReadoutFrequencyParameters
    Result: ClassVar[type] = ReadoutFrequencyResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("detuning_hz", "prepared_state", "shot_idx"),
        sweep_units=("Hz", "state", "shot"),
        variables=("I", "Q"),
    )
    required_operations: ClassVar[tuple[str, ...]] = ("readout",)

    params: ReadoutFrequencyParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        span = self.params.frequency_span_hz
        return {
            "detuning_hz": np.linspace(-span / 2, span / 2, self.params.num_freq_points),
            "prepared_state": np.array([0, 1]),
            "shot_idx": np.arange(self.params.num_shots),
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        detuning = coords["detuning_hz"]
        n_shots = coords["shot_idx"].size
        targets = self.params.targets
        rng = np.random.default_rng(stable_seed("readout_frequency", *targets))
        span = float(detuning[-1] - detuning[0])
        i_data = np.empty((len(targets), detuning.size, 2, n_shots))
        q_data = np.empty_like(i_data)
        for k in range(len(targets)):
            best_det = rng.uniform(-span / 6, span / 6)  # hidden max-contrast detuning
            sep_max = rng.uniform(3.0, 4.0)
            width = span / 6
            for j, det in enumerate(detuning):
                sep = sep_max * np.exp(-((det - best_det) ** 2) / (2 * width**2))
                for state in (0, 1):
                    flip = 0.02 if state == 0 else 0.05
                    actual = np.where(rng.random(n_shots) < flip, 1 - state, state)
                    i_data[k, j, state] = actual * sep + rng.normal(0, 1.0, n_shots)
                    q_data[k, j, state] = rng.normal(0, 1.0, n_shots)
        return {"I": i_data, "Q": q_data}

    def estimate(self) -> ReadoutFrequencyResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.readout_fidelity import ReadoutFreqFidelityEstimator

        # scqat's sweep coord is named `frequency`; values stay DETUNING (Hz) — the
        # absolute frequency differs per qubit, so the shift is applied per qubit below.
        prepared = self.dataset.rename({"detuning_hz": "frequency"})
        prepared = prepared.transpose("target", "frequency", "prepared_state", "shot_idx")
        old_freqs = {q: float(self.device.component(q).readout_freq) for q in self.params.targets}

        results = per_qubit_results(
            prepared, ReadoutFreqFidelityEstimator(), artifact_dir=self.artifact_dir
        )

        result = ReadoutFrequencyResult()
        for qubit in self.params.targets:
            r = results[qubit]
            best = r.get("best_sweep_value")  # best DETUNING (Hz)
            fidelity = r.get("best_fidelity")
            old_freq = old_freqs[qubit]
            ok = bool(r.get("success")) and best is not None and np.isfinite(best)
            result.fit[qubit] = {
                "readout_freq": old_freq + float(best) if ok else float("nan"),
                "frequency_shift_hz": float(best) if best is not None else float("nan"),
                "best_fidelity": float(fidelity) if fidelity is not None else float("nan"),
                "old_readout_freq": old_freq,
            }
            result.outcomes[qubit] = Outcome.SUCCESSFUL if ok else Outcome.FAILED
        return result

    def update(self) -> None:
        if self.result is None:
            return
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is Outcome.SUCCESSFUL:
                self.device.component(qubit).readout_freq = fit["readout_freq"]
