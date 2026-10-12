"""Crosstalk Compensated Simultaneous SQRB experiment.

Measures how much a neighbouring qubit's drive ("aggressor", drive_qubit) degrades single-qubit
randomized benchmarking of a "victim" (probe_qubit), and how much of it active cancellation
recovers. Three conditions are compared, each a full SQRB decay:

1. isolated:     the victim runs SQRB alone.
2. simultaneous: both qubits run independent random Clifford sequences at the same time.
3. compensated:  as simultaneous, plus a cancellation element on the victim's line that plays a
                 scaled, phase-shifted copy of the aggressor's pulses at the AGGRESSOR's frequency,
                 so the leaked field and its copy cancel on the chip.

With ``bidirectional`` both qubits are victim and aggressor: each line gets its own cancellation
element and each qubit its own three RB curves, both qubits read out together.

The cancellation amplitude / phase (cancel_amp, init_phase and the drive_* pair) come from
crosstalk_cancellation_tomography, calibrated in the same program context.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

import numpy as np
from pydantic import Field, model_validator
import xarray as xr

from ..contract import ContractError, DatasetContract
from ._capabilities.qubit_reset import QubitResetParameters
from ._capabilities.state_readout import POPULATION_ALT, StateReadoutParameters
from ._sim import stable_seed
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register


CONDITIONS = ("isolated", "simultaneous", "compensated")


class CrosstalkCompensatedSQRBParameters(
    TargetSelection, AveragingParameters, StateReadoutParameters, QubitResetParameters
):
    """Inputs for Crosstalk Compensated Simultaneous SQRB."""

    probe_qubit: str = Field(
        "q0",
        description="Victim qubit whose SQRB is measured (e.g. 'q1')."
    )
    drive_qubit: str = Field(
        "q1",
        description="Aggressor qubit running concurrent Cliffords (e.g. 'q3')."
    )
    targets: list[str] = Field(
        default_factory=list,
        description="Target qubits [probe_qubit, drive_qubit]. Auto-derived if omitted."
    )

    # Sequences and depths
    num_random_sequences: int = Field(
        30,
        gt=0,
        description="Number of distinct random Clifford sequences."
    )
    max_circuit_depth: int = Field(
        256,
        gt=0,
        description="Maximum Clifford depth when `depths` is not given."
    )
    delta_clifford: int = Field(
        20,
        gt=0,
        description="Step between depths when log_scale is False."
    )
    log_scale: bool = Field(
        True,
        description="Powers-of-2 depths (1, 2, 4, ...) up to max_circuit_depth."
    )
    depths: list[int] | None = Field(
        None,
        description="Explicit Clifford depths (>= 1). If None, derived from max_circuit_depth and log_scale."
    )
    conditions: list[str] | None = Field(
        None,
        description="Conditions to benchmark, a subset of ['isolated', 'simultaneous', 'compensated'] (default: all)."
    )
    sequences_per_program: int = Field(
        7,
        ge=1,
        description=(
            "Max sequences per QUA program (each program runs one condition, every depth). Keep "
            "programs short: on chipQM the simultaneous/compensated error grew steadily during a "
            "single 30-sequence program (~1.3 min) and stayed flat with 7 per program (2026-10-01)."
        )
    )
    seed: int | None = Field(
        None,
        description="Random seed of the Clifford sequences (None: 42 for the probe, 1042 for the drive)."
    )

    # Cancellation (from crosstalk_cancellation_tomography)
    cancel_amp: float = Field(
        0.0,
        ge=0.0,
        description="Amplitude scale of the cancellation copy of the drive qubit's pulses, on the probe's line."
    )
    init_phase: float = Field(
        0.0,
        description="Phase (rad) of that cancellation copy."
    )
    bidirectional: bool = Field(
        False,
        description=(
            "Compensate both directions: a second cancellation element on the drive qubit's line "
            "copies the probe's pulses, both qubits are read out together, and each qubit gets its "
            "own isolated / simultaneous / compensated RB."
        )
    )
    drive_cancel_amp: float = Field(
        0.0,
        ge=0.0,
        description="Bidirectional: amplitude scale of the cancellation copy of the probe's pulses, on the drive qubit's line."
    )
    drive_init_phase: float = Field(
        0.0,
        description="Bidirectional: phase (rad) of that cancellation copy."
    )
    cancel_tone: Literal["aggressor", "victim"] = Field(
        "aggressor",
        description=(
            "Frequency of the cancellation copy. 'aggressor': at the aggressor's frequency on the "
            "victim's line, a copy of the leaked field itself. 'victim': at the victim's frequency, "
            "following the leak's phase advance in 16 ns steps. Calibrate it with "
            "crosstalk_cancellation_tomography run with the same cancel_tone."
        )
    )
    line_delay_ns: int = Field(
        0,
        ge=-16,
        le=16,
        description=(
            "Timing shift (ns) of the probe's line against the drive's, on top of the configured port "
            "delays: > 0 delays the probe qubit's xy output port (its own pulses and the cancellation on "
            "it), < 0 delays the drive qubit's. Shifts the cancellation against the leak in 1 ns steps; "
            "a bidirectional reverse cancellation sees the opposite shift. Use the value the cancellation "
            "was calibrated with (crosstalk_cancellation_tomography line_delay_ns)."
        ),
    )

    def get_depths(self) -> np.ndarray:
        """Depths from the explicit list, or from log_scale / max_circuit_depth."""
        if self.depths is not None and len(self.depths) > 0:
            return np.asarray(self.depths, dtype=int)
        if self.log_scale:
            depth_list = [1]
            cur = 2
            while cur <= self.max_circuit_depth:
                depth_list.append(cur)
                cur *= 2
            return np.array(depth_list, dtype=int)
        arr = np.arange(0, self.max_circuit_depth + 0.1, self.delta_clifford, dtype=int)
        arr[0] = 1
        return arr

    def get_conditions(self) -> list[str]:
        return [c for c in (self.conditions or CONDITIONS) if c in CONDITIONS]

    @model_validator(mode="before")
    @classmethod
    def _resolve_targets_and_qubits(cls, data: Any) -> Any:
        if isinstance(data, dict):
            targets = data.get("targets")
            if targets:
                if isinstance(targets, str):
                    targets = [t.strip() for t in targets.split(",")]
                valid = [t for t in targets if not t.endswith("_c")]
                if len(valid) == 1 and "_" in valid[0]:
                    parts = valid[0].split("_")
                    data["probe_qubit"] = parts[0]
                    data["drive_qubit"] = parts[1]
                    data["targets"] = [parts[0], parts[1]]
                elif len(valid) == 1:
                    data.setdefault("probe_qubit", valid[0])
                    if "drive_qubit" not in data:
                        data["drive_qubit"] = "q2" if valid[0] != "q2" else "q1"
                    data["targets"] = [data["probe_qubit"], data["drive_qubit"]]
                elif len(valid) >= 2:
                    data["probe_qubit"] = data.get("probe_qubit", valid[0])
                    data["drive_qubit"] = data.get("drive_qubit", valid[1])
                    data["targets"] = [data["probe_qubit"], data["drive_qubit"]]
            elif "probe_qubit" in data and "drive_qubit" in data:
                data["targets"] = [data["probe_qubit"], data["drive_qubit"]]
        return data

    @model_validator(mode="after")
    def _check(self) -> CrosstalkCompensatedSQRBParameters:
        if not self.targets:
            self.targets = [self.probe_qubit, self.drive_qubit]
        if self.conditions is not None:
            unknown = [c for c in self.conditions if c not in CONDITIONS]
            if unknown or not self.conditions:
                raise ValueError(f"conditions must be a non-empty subset of {list(CONDITIONS)}, got {self.conditions}")
        if int(np.min(self.get_depths())) < 1:
            raise ValueError("depths must be >= 1")
        return self


class CrosstalkCompensatedSQRBContract(DatasetContract):
    """Dataset contract: (target, condition, sequence_idx, depth) + a readout variable."""

    def validate(self, ds: xr.Dataset) -> None:
        problems: list[str] = []
        for coord in ("target", "condition", "sequence_idx", "depth"):
            if coord not in ds.dims:
                problems.append(f"missing dimension {coord!r}")
            if coord not in ds.coords:
                problems.append(f"missing coordinate {coord!r}")
        if not any(v in ds.data_vars for v in ("I", "state", "signal", "population")):
            problems.append("missing data variable ('I', 'state', 'signal', or 'population')")
        if problems:
            raise ContractError("; ".join(problems))


class CrosstalkCompensatedSQRBResult(Result):
    """Output of CrosstalkCompensatedSQRB."""

    fit: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Per victim qubit: error per Clifford of each condition and the recovered fraction."
    )


@register
class CrosstalkCompensatedSQRB(Experiment):
    """Crosstalk Compensated Simultaneous SQRB Experiment."""

    name: ClassVar[str] = "crosstalk_compensated_sqrb"
    description: ClassVar[str] = (
        "Simultaneous single-qubit randomized benchmarking comparing isolated, simultaneous and "
        "actively crosstalk-compensated conditions (cancellation copies at the aggressor's frequency)."
    )
    Parameters: ClassVar[type] = CrosstalkCompensatedSQRBParameters
    Result: ClassVar[type] = CrosstalkCompensatedSQRBResult

    Contract: ClassVar[DatasetContract] = CrosstalkCompensatedSQRBContract(
        sweeps=("condition", "sequence_idx", "depth"),
        sweep_units=("", "", ""),
        variables=("I", "Q"),
        alt_variables=(*POPULATION_ALT, ("I",)),
    )

    params: CrosstalkCompensatedSQRBParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            "condition": np.array(self.params.get_conditions()),
            "sequence_idx": np.arange(self.params.num_random_sequences, dtype=int),
            "depth": self.params.get_depths(),
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        rng = np.random.default_rng(stable_seed("crosstalk_compensated_sqrb", self.params.probe_qubit))

        p_iso = 0.995  # isolated Clifford survival factor
        p_sim = 0.970  # degraded by uncompensated crosstalk

        def _p_comp(amp: float, phi: float, o_amp: float, o_phi: float) -> float:
            # compensation quality falls off with the distance from a nominal optimum
            dist_sq = (amp - o_amp) ** 2 + 0.001 * (phi - o_phi) ** 2
            return p_sim + (p_iso - 0.002 - p_sim) * np.exp(-dist_sq / 0.002)

        p_comp = _p_comp(float(self.params.cancel_amp), float(self.params.init_phase), 0.035, 0.85)
        p_comp_drive = _p_comp(float(self.params.drive_cancel_amp), float(self.params.drive_init_phase), 0.028, -0.6)

        conditions = coords["condition"]
        depths = coords["depth"]
        n_seq = len(coords["sequence_idx"])
        n_targets = max(1, len(self.params.targets))

        def _pop(p_map: dict[str, float]) -> np.ndarray:
            pop = np.empty((len(conditions), n_seq, len(depths)), dtype=float)
            for c_idx, c_name in enumerate(conditions):
                p_val = p_map.get(str(c_name), p_iso)
                for d_idx, d in enumerate(depths):
                    pop[c_idx, :, d_idx] = np.clip(0.5 + 0.48 * (p_val ** d) + rng.normal(0, 0.005, n_seq), 0.0, 1.0)
            return pop

        pop_probe = _pop({"isolated": p_iso, "simultaneous": p_sim, "compensated": p_comp})
        if self.params.bidirectional:
            pop_drive = _pop({"isolated": p_iso, "simultaneous": p_sim, "compensated": p_comp_drive})
            pop_t = np.stack([pop_probe, pop_drive] + [pop_probe] * (n_targets - 2), axis=0)
        else:
            pop_t = np.repeat(pop_probe[None, ...], n_targets, axis=0)
        dims = ("target", "condition", "sequence_idx", "depth")
        if self.params.use_state_discrimination:
            return {"state": (dims, pop_t)}
        return {"I": (dims, pop_t), "Q": (dims, np.zeros_like(pop_t))}

    def estimate(self) -> CrosstalkCompensatedSQRBResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.crosstalk_compensated_sqrb import CrosstalkCompensatedSQRBEstimator

        probe, drive = self.params.probe_qubit, self.params.drive_qubit
        has_target = "target" in self.dataset.coords
        ds_probe = self.dataset.sel(target=probe) if has_target and probe in self.dataset.coords["target"].values else self.dataset

        # (victim, aggressor, victim's dataset): bidirectional fits each qubit's own RB
        fits = [(probe, drive, ds_probe)]
        if self.params.bidirectional:
            if not has_target or drive not in self.dataset.coords["target"].values:
                raise ValueError(
                    f"bidirectional run has no data for drive qubit {drive!r}; the probe must read out both qubits"
                )
            fits.append((drive, probe, self.dataset.sel(target=drive)))

        estimator = CrosstalkCompensatedSQRBEstimator()
        result = CrosstalkCompensatedSQRBResult()
        for victim, aggressor, ds_victim in fits:
            ds_victim = ds_victim.assign_attrs(victim_qubit=victim, aggressor_qubit=aggressor)
            out_dir = None
            if self.artifact_dir is not None:
                out_dir = str(self.artifact_dir / victim) if len(fits) > 1 else str(self.artifact_dir)
            fit_res, _figures = estimator.analyze(ds_victim, output_dir=out_dir)
            result.fit[victim] = fit_res
            result.outcomes[victim] = Outcome.SUCCESSFUL if fit_res.get("success", False) else Outcome.FAILED
        return result

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
