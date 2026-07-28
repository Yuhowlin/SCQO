"""Qubit Deterministic Benchmarking experiment.

Applies repeated sequences of a specified target gate (x180, y180, x90, y90, -x90, -y90)
across repetition counts N and amplitude scaling factors to measure rotation error accumulation
and calibrate gate pulse amplitudes.
"""

from __future__ import annotations

from typing import ClassVar, List, Optional

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

#: the pi/2 gates. Their amplitude lives on the x90 storage node (y90/-y90 are
#: reference aliases that follow it), so they calibrate `pi_amp_x90`; everything
#: else is a pi gate and calibrates `pi_amp`.
X90_GATES = frozenset({"x90", "-x90", "y90", "-y90"})


def amp_knob(target_gate: str) -> str:
    """Which neutral drive-channel knob this target gate calibrates."""
    return "pi_amp_x90" if str(target_gate).strip().lower() in X90_GATES else "pi_amp"


class QubitDeterministicBenchmarkingParameters(
    TargetSelection, AveragingParameters, StateReadoutParameters, QubitResetParameters
):
    """Inputs for Qubit Deterministic Benchmarking."""

    target_gate: str = Field(
        "x180",
        description="Target gate to benchmark: x180, y180, x90, y90, -x90, -y90.",
    )
    max_repetitions: int = Field(
        100,
        gt=0,
        description="Maximum repetition count N.",
    )
    step: Optional[int] = Field(
        None,
        gt=0,
        description="Repetition increment step (defaults to 2 for pi gates, 4 for pi/2 gates).",
    )
    repetitions: Optional[List[int]] = Field(
        None,
        description="Optional explicit array of repetition counts N.",
    )
    min_amp_factor: float = Field(
        0.9,
        description="Minimum amplitude scaling factor for sweep.",
    )
    max_amp_factor: float = Field(
        1.1,
        description="Maximum amplitude scaling factor for sweep.",
    )
    num_amp_points: int = Field(
        1,
        gt=0,
        description="Number of amplitude scale points (set to 1 for single-amplitude sweep).",
    )
    amp_factors: Optional[List[float]] = Field(
        None,
        description="Optional explicit list of amplitude scale factors.",
    )

    def get_repetitions(self) -> list[int]:
        if self.repetitions is not None:
            return [int(x) for x in self.repetitions]
        tg = str(self.target_gate).strip().lower()
        if self.step is not None:
            st = self.step
        elif tg in ("x90", "y90", "-x90", "-y90", "pi_half"):
            st = 4
        else:
            st = 2
        return list(range(0, self.max_repetitions + 1, st))

    def get_amp_factors(self) -> list[float]:
        if self.amp_factors is not None:
            return [float(x) for x in self.amp_factors]
        if self.num_amp_points <= 1:
            return [1.0]
        return [float(x) for x in np.linspace(self.min_amp_factor, self.max_amp_factor, self.num_amp_points)]


class QubitDeterministicBenchmarkingResult(Result):
    """``fit[qubit]``: ``opt_factor`` (the amplitude MULTIPLIER where the rotation
    error crosses zero), ``opt_amp`` (that factor applied to the live amplitude),
    plus the calibrated knob and its previous value under their own names —
    ``pi_amp``/``old_pi_amp`` for a pi gate, ``pi_amp_x90``/``old_pi_amp_x90`` for
    a pi/2 one. ``update()`` writes the knob the benchmarked gate belongs to."""


@register
class QubitDeterministicBenchmarking(Experiment):
    """Deterministic Benchmarking for single qubit gates."""

    name: ClassVar[str] = "qubit_deterministic_benchmarking"
    description: ClassVar[str] = (
        "Amplitude error amplification: plays ONE target gate N times and sweeps N, "
        "so a small per-gate over/under-rotation accumulates into a resolvable "
        "fringe. Repeated across an amplitude-scale sweep, the fringe rate crosses "
        "zero at the correct amplitude. Calibrates whichever gate is benchmarked — "
        "pi (x180/y180) proposes pi_amp, pi/2 (x90/y90/-x90/-y90) proposes "
        "pi_amp_x90 — so it is how the pi/2 gets calibrated in its own right rather "
        "than assumed to be half the pi. Far more sensitive than power Rabi, which "
        "sees the rotation error once; use it after power Rabi has the amplitude "
        "roughly right."
    )
    Parameters: ClassVar[type[QubitDeterministicBenchmarkingParameters]] = QubitDeterministicBenchmarkingParameters
    Result: ClassVar[type[QubitDeterministicBenchmarkingResult]] = QubitDeterministicBenchmarkingResult

    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("amp_factor", "repetitions"),
        sweep_units=("", ""),
        variables=("I", "Q"),
        alt_variables=(*STATE_ALT, ("I",)),
    )

    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout")

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            "amp_factor": np.array(self.params.get_amp_factors(), dtype=float),
            "repetitions": np.array(self.params.get_repetitions(), dtype=int),
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """Repeated-gate error accumulation: an amplitude factor off the ideal one
        leaves a fixed per-gate over/under-rotation, so the population oscillates in
        the REPETITION count at a rate proportional to (a - a_ideal), under a
        depolarizing envelope. At N=0 nothing has been played, so P(excited) = 0."""
        amp = coords["amp_factor"]
        reps = coords["repetitions"].astype(float)
        targets = self.params.targets
        rng = np.random.default_rng(stable_seed("qubit_deterministic_benchmarking", *targets))
        use_state = self.params.use_state_discrimination
        # a pi/2 gate accumulates half the rotation error per repetition of a pi gate
        per_factor = np.pi / 2 if amp_knob(self.params.target_gate) == "pi_amp_x90" else np.pi
        i_data = np.empty((len(targets), amp.size, reps.size))
        q_data = np.empty_like(i_data)
        state = np.empty_like(i_data)
        for k in range(len(targets)):
            a_ideal = rng.uniform(0.97, 1.03)  # the factor a perfect pulse would need
            decay = rng.uniform(2e-3, 8e-3)    # per-gate depolarization
            for j, a in enumerate(amp):
                omega = per_factor * (float(a) - a_ideal)
                population = 0.5 - 0.45 * np.exp(-decay * reps) * np.cos(omega * reps)
                if use_state:
                    state[k, j] = state_row(population, rng)
                else:
                    i_data[k, j], q_data[k, j] = iq_from_population(population, rng)
        return readout_vars(use_state, state, i_data, q_data)

    def estimate(self) -> QubitDeterministicBenchmarkingResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.qubit_deterministic_benchmarking import (
            QubitDeterministicBenchmarkingEstimator,
        )

        # scqat's contract: coord `repetition` (singular), and either a pre-reduced
        # `signal` or raw I/Q that it reduces onto the |0>-|1> axis. It returns
        # `opt_factor` — the MULTIPLIER on the current amplitude, never an absolute
        # amplitude, so applying it against the live knob is ours to do.
        rename = signal_rename(self.dataset, {"repetitions": "repetition"})
        prepared = self.dataset.rename(rename).transpose("target", "amp_factor", "repetition")
        results = per_qubit_results(
            prepared, QubitDeterministicBenchmarkingEstimator(), artifact_dir=self.artifact_dir
        )

        knob = amp_knob(self.params.target_gate)
        result = QubitDeterministicBenchmarkingResult()
        for qubit in self.params.targets:
            a_opt = float(results[qubit]["opt_factor"])
            old_amp = float(getattr(self.device.channel(qubit, "drive"), knob))
            opt_amp = old_amp * a_opt
            result.fit[qubit] = {
                "opt_factor": a_opt,
                "opt_amp": opt_amp,
                knob: opt_amp,
                f"old_{knob}": old_amp,
                "unit": results[qubit]["unit"],
            }
            result.outcomes[qubit] = Outcome.SUCCESSFUL
        return result

    def update(self) -> None:
        """Propose the rescaled amplitude on the target's DRIVE channel. Which knob
        it lands on depends only on whether the benchmarked gate is a pi or a pi/2
        rotation — `amp_knob` is the single place that decides, shared with
        estimate() so the read and the write can never disagree."""
        if self.result is None:
            return
        knob = amp_knob(self.params.target_gate)
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is not Outcome.SUCCESSFUL:
                continue
            setattr(self.device.channel(qubit, "drive"), knob, fit["opt_amp"])

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
