"""Readout-frequency optimization by single-shot fidelity, greenfield.

Port of :mod:`scqo.experiments.readout_frequency`. The physics half is
byte-for-byte; what moved is the device surface and field spelling: the
fidelity-optimal frequency lands on the target's READOUT CHANNEL as
``readout_freq_hz`` (was ``readout_freq`` on the component), read and
written via ``self.device.channel(target, "readout")``.
"""

from __future__ import annotations

from typing import ClassVar, Literal

import numpy as np
from pydantic import Field

from .._scqat import per_qubit_results
from ..contract import DatasetContract
from ._capabilities.detuning import (
    END_READOUT_DETUNING_DESC,
    NUM_FREQ_POINTS_DESC,
    START_READOUT_DETUNING_DESC,
    ReadoutDetuningSweepParameters,
    readout_detuning_sweep,
)
from ._capabilities.qubit_reset import QubitResetParameters
from ._capabilities.state_readout import ReadoutModeParameters, discrimination_method
from ._sim import stable_seed
from ..parameters import TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register


class ReadoutFrequencyParameters(TargetSelection, QubitResetParameters,
                                 ReadoutModeParameters,
                                 ReadoutDetuningSweepParameters):
    """Inputs for fidelity-vs-readout-frequency optimization."""

    # capability window re-declared at the CHI scale: this scan resolves the
    # |g>/|e> separation optimum, which sits within a chi of the current
    # readout frequency, not within a resonator linewidth of it.
    start_readout_detuning_hz: float = Field(
        -2.5e6, description=START_READOUT_DETUNING_DESC)
    end_readout_detuning_hz: float = Field(
        2.5e6, description=END_READOUT_DETUNING_DESC)
    num_readout_freq_points: int = Field(
        21, gt=2, description=NUM_FREQ_POINTS_DESC
        + " One Gaussian-mixture fit per point in shot mode.")
    num_shots: int = Field(
        1000, gt=99,
        description="Measurements per prepared state per frequency — kept per "
        "shot in shot mode, averaged on the FPGA in average mode.")
    # re-declared: the mixin defaults to "average", but this is a per-shot
    # FIDELITY calibration by construction and its default must not flip.
    readout_mode: Literal["average", "shot"] = Field(
        "shot",
        description="'shot' keeps every shot's I/Q and fits a Gaussian mixture "
        "per frequency, so the optimum is the FIDELITY maximum. 'average' "
        "returns one FPGA-averaged I/Q point per prepared state: no fit, no "
        "fidelity, and the optimum is the largest blob SEPARATION — a faster "
        "scan that peaks at the same detuning but says nothing about how well "
        "the states can actually be told apart.")


class ReadoutFrequencyResult(Result):
    """``fit[target]``: ``readout_freq_hz`` (new), ``frequency_shift_hz``,
    ``best_fidelity`` (NaN in average mode), ``best_separation``,
    ``old_readout_freq_hz``."""


@register
class ReadoutFrequency(Experiment):
    """Backend-agnostic readout-frequency scan, per shot or FPGA-averaged."""

    name: ClassVar[str] = "readout_frequency"
    description: ClassVar[str] = (
        "Sweep the readout detuning reading |g> and |e>; picks the best frequency and "
        "updates the readout channel's readout_freq_hz. readout_mode='shot' records "
        "every shot's I/Q and optimizes single-shot FIDELITY; readout_mode='average' "
        "takes one FPGA-averaged I/Q point per prepared state and optimizes blob "
        "SEPARATION instead — faster, and it peaks at the same detuning."
    )
    Parameters: ClassVar[type] = ReadoutFrequencyParameters
    Result: ClassVar[type] = ReadoutFrequencyResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("detuning_hz", "prepared_state"),
        sweep_units=("Hz", "state"),
        # readout_mode="shot": every shot's I/Q on a trailing shot axis...
        variables=("I", "Q"), readout_dims=("shot_idx",),
        # ...readout_mode="average": the same variables, FPGA-averaged, so the
        # shot axis is gone entirely.
        alt_variables=(("I", "Q"),),
        alt_readout_dims=((),),
    )
    required_operations: ClassVar[tuple[str, ...]] = ("readout",)

    params: ReadoutFrequencyParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            **readout_detuning_sweep(self.params),
            "prepared_state": np.array([0, 1]),
        }

    def readout_coords(self) -> dict:
        if self.params.readout_mode == "shot":
            return {"shot_idx": np.arange(self.params.num_shots)}
        return {}

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        detuning = coords["detuning_hz"]
        n_shots = int(self.params.num_shots)
        targets = self.params.targets
        rng = np.random.default_rng(stable_seed("readout_frequency", *targets))
        span = float(detuning[-1] - detuning[0])
        center = float(detuning[0] + detuning[-1]) / 2
        i_data = np.empty((len(targets), detuning.size, 2, n_shots))
        q_data = np.empty_like(i_data)
        for k in range(len(targets)):
            # hidden max-contrast detuning, relative to the window MIDPOINT —
            # an asymmetric window must not re-center the truth
            best_det = center + rng.uniform(-span / 6, span / 6)
            sep_max = rng.uniform(3.0, 4.0)
            width = span / 6
            for j, det in enumerate(detuning):
                sep = sep_max * np.exp(-((det - best_det) ** 2) / (2 * width**2))
                for state in (0, 1):
                    flip = 0.02 if state == 0 else 0.05
                    actual = np.where(rng.random(n_shots) < flip, 1 - state, state)
                    i_data[k, j, state] = actual * sep + rng.normal(0, 1.0, n_shots)
                    q_data[k, j, state] = rng.normal(0, 1.0, n_shots)
        if self.params.readout_mode == "average":
            # the FPGA averages the same shots away; nothing but the mean survives
            return {"I": i_data.mean(axis=-1), "Q": q_data.mean(axis=-1)}
        dims = ("target", "detuning_hz", "prepared_state", "shot_idx")
        return {"I": (dims, i_data), "Q": (dims, q_data)}

    def estimate(self) -> ReadoutFrequencyResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.readout_fidelity import ReadoutFreqFidelityEstimator

        # scqat's sweep coord is named `frequency`; values stay DETUNING (Hz) — the
        # absolute frequency differs per qubit, so the shift is applied per qubit below.
        prepared = self.dataset.rename({"detuning_hz": "frequency"})
        axes = ["target", "frequency", "prepared_state"]
        if "shot_idx" in prepared.dims:  # shot mode only
            axes.append("shot_idx")
        prepared = prepared.transpose(*axes)
        old_freqs = {
            q: float(self.device.channel(q, "readout").readout_freq_hz)
            for q in self.params.targets
        }

        results = per_qubit_results(
            prepared, ReadoutFreqFidelityEstimator(), artifact_dir=self.artifact_dir,
            # DERIVED from the acquisition, never a second knob: averaged data
            # has one point per prepared state, which no mixture can be fitted to
            method=discrimination_method(self.params.readout_mode),
        )

        result = ReadoutFrequencyResult()
        for qubit in self.params.targets:
            r = results[qubit]
            best = r.get("best_sweep_value")  # best DETUNING (Hz)
            fidelity = r.get("best_fidelity")  # None in average mode: nothing was fitted
            separation = r.get("best_separation")
            old_freq = old_freqs[qubit]
            ok = bool(r.get("success")) and best is not None and np.isfinite(best)
            result.fit[qubit] = {
                "readout_freq_hz": old_freq + float(best) if ok else float("nan"),
                "frequency_shift_hz": float(best) if best is not None else float("nan"),
                "best_fidelity": float(fidelity) if fidelity is not None else float("nan"),
                "best_separation": float(separation) if separation is not None else float("nan"),
                "old_readout_freq_hz": old_freq,
            }
            result.outcomes[qubit] = Outcome.SUCCESSFUL if ok else Outcome.FAILED
        return result

    def update(self) -> None:
        if self.result is None:
            return
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is Outcome.SUCCESSFUL:
                self.device.channel(qubit, "readout").readout_freq_hz = (
                    fit["readout_freq_hz"])

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
