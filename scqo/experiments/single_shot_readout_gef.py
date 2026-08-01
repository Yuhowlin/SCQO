"""Three-state single-shot readout — |g>, |e> and |f> IQ blobs.

The qutrit sibling of :mod:`scqo.experiments.single_shot_readout`: same
per-shot ``prepared_state`` x ``shot_idx`` contract, one more prepared state.
The reduction is unchanged (scqat's ``discriminate_states`` sizes its mixture
from the prepared states, so three rows in gives three centers out); what grows
is the bookkeeping — a 3x3 confusion matrix and a three-way label mapping.

Preparing |f> needs a calibrated 1->2 (EF) pi pulse, which today only the QM
backend has; a Qblox session gets the base ``probe()``'s NotImplementedError.

No discriminator knob is proposed here. ``readout_rotation_rad`` /
``readout_threshold`` describe a scalar cut on one rotated quadrature, which
separates two blobs and has no three-state analogue — |g>, |e> and |f> sit on a
TRIANGLE in the IQ plane and are classified by nearest center in 2-D. The
two-state ``single_shot_readout`` stays the authority for those knobs.
"""

from __future__ import annotations

from itertools import permutations
from typing import ClassVar

import numpy as np
from pydantic import Field

from .._scqat import per_qubit_results
from ..contract import DatasetContract
from ._capabilities.qubit_reset import QubitResetParameters
from ._sim import stable_seed
from ..parameters import TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register

#: prepared-state index -> the letter used in every field and fit key.
_LETTERS = ("g", "e", "f")


class SingleShotReadoutGEFParameters(TargetSelection, QubitResetParameters):
    """Inputs for a three-state single-shot readout measurement."""

    num_shots: int = Field(2000, gt=99, description="Shots per prepared state (each recorded individually).")
    readout_freq_shift_hz: float = Field(
        0.0,
        description="Detune the readout tone by this much for THIS RUN only "
                    "(0 = use the channel's readout_freq_hz). |f> shifts the "
                    "resonator further than |e> does, so the frequency that "
                    "best separates g/e is rarely the one that best separates "
                    "all three; sweep it by hand to find the three-state "
                    "point. Not stored — the standing knob is unchanged.")


class SingleShotReadoutGEFResult(Result):
    """``fit[qubit]``: ``readout_fidelity`` (mean of the 3x3 confusion diagonal,
    run-record-only — the stored monitors are the per-state ``fidelity_g`` /
    ``fidelity_e`` / ``fidelity_f``), the six COUNTED off-diagonals
    ``p_<assigned>_given_<prepared>``, their six FITTED twins
    ``pop_<assigned>_prep_<prepared>``, ``outlier_probability``, and the measured
    blob centers ``mean_<letter>_i`` / ``mean_<letter>_q``."""


@register
class SingleShotReadoutGEF(Experiment):
    """Backend-agnostic three-state IQ blobs. ``probe()`` must record every shot."""

    name: ClassVar[str] = "single_shot_readout_gef"
    description: ClassVar[str] = (
        "Prepare |g>, |e> and |f> and record every readout shot's I/Q point; a "
        "three-Gaussian mixture gives the per-state assignment fidelities (stored as "
        "the readout channel's fidelity_g/fidelity_e/fidelity_f monitors), the full "
        "3x3 confusion matrix both counted and fitted (run-record only) and the three "
        "measured blob centers (stored as the channel's pos_* reference). Use it to "
        "quantify LEAKAGE — how much |f> a gate or a reset leaves behind — which a "
        "two-state readout silently folds into |e>. Preparing |f> requires a "
        "calibrated EF pi pulse, so this runs on the QM backend only. It proposes no "
        "discriminator knob: a scalar threshold on one rotated quadrature cannot "
        "separate three blobs, so single_shot_readout remains the authority for "
        "readout_rotation_rad / readout_threshold."
    )
    Parameters: ClassVar[type] = SingleShotReadoutGEFParameters
    Result: ClassVar[type] = SingleShotReadoutGEFResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("prepared_state", "shot_idx"), sweep_units=("state", "shot"), variables=("I", "Q")
    )
    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout")

    params: SingleShotReadoutGEFParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            "prepared_state": np.array([0, 1, 2]),
            "shot_idx": np.arange(self.params.num_shots),
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        n_shots = coords["shot_idx"].size
        targets = self.params.targets
        rng = np.random.default_rng(stable_seed("single_shot_readout_gef", *targets))
        i_data = np.empty((len(targets), 3, n_shots))
        q_data = np.empty_like(i_data)
        for k in range(len(targets)):
            sep = rng.uniform(3.5, 5.0)  # |g>-|e> separation in units of sigma
            # |f> is NOT collinear with |g>-|e>: the dispersive shift is not
            # linear in the state, so the three blobs form a triangle.
            theta = rng.uniform(0.9, 1.2)
            p_thermal = rng.uniform(0.01, 0.05)  # |e> population in the "ground" prep
            p_decay = rng.uniform(0.03, 0.08)  # |e> relaxation during readout
            p_f_decay = rng.uniform(0.05, 0.12)  # |f> -> |e> (faster than |e> -> |g>)
            centers = {0: (0.0, 0.0), 1: (sep, 0.0),
                       2: (sep * np.cos(theta), sep * np.sin(theta))}
            for state in (0, 1, 2):
                # each state decays one rung down (|g> "decays" upward = thermal)
                flip, landing = ((p_thermal, 1) if state == 0 else
                                 (p_decay, 0) if state == 1 else (p_f_decay, 1))
                actual = np.where(rng.random(n_shots) < flip, landing, state)
                cx = np.array([centers[s][0] for s in actual])
                cy = np.array([centers[s][1] for s in actual])
                i_data[k, state] = cx + rng.normal(0, 1.0, n_shots)
                q_data[k, state] = cy + rng.normal(0, 1.0, n_shots)
        return {"I": i_data, "Q": q_data}

    def estimate(self) -> SingleShotReadoutGEFResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.state_discrimination import StateDiscriminationEstimator

        # scqat's contract: I/Q over (prepared_state, shot_idx) — names already match.
        prepared = self.dataset.transpose("target", "prepared_state", "shot_idx")

        results = per_qubit_results(
            prepared, StateDiscriminationEstimator(), artifact_dir=self.artifact_dir
        )

        result = SingleShotReadoutGEFResult()
        nan = float("nan")
        for qubit in self.params.targets:
            r = results[qubit]
            counts = np.asarray(r["direct_counts"], dtype=float)  # (prepared_state, label), rows sum to 1
            mean = np.asarray(r.get("trained_paras", {}).get("mean", []), dtype=float)  # (n_center, 2) IQ
            # (prepared_state, center) fitted blob WEIGHTS. Same indexing as counts,
            # so the one label mapping below serves both — a second mapping would be
            # a second authority that can disagree.
            norms = np.asarray(r.get("gaussian_norms", []), dtype=float)
            fit: dict[str, float] = {}
            fidelity = nan
            if counts.shape == (3, 3):
                # The GMM's center order is not guaranteed to match the prepared-state
                # order. Two states admit two orderings and the parent enumerates both;
                # three admit six, so enumerate those and keep the one that makes the
                # diagonal the majority. The same mapping labels the centers.
                order = max(permutations(range(3)),
                            key=lambda p: sum(counts[s, p[s]] for s in range(3)))
                fidelity = float(np.mean([counts[s, order[s]] for s in range(3)]))
                for prep in range(3):
                    for assigned in range(3):
                        if prep == assigned:
                            continue
                        key = f"{_LETTERS[assigned]}_given_{_LETTERS[prep]}"
                        fit[f"p_{key}"] = float(counts[prep, order[assigned]])
                        twin = f"{_LETTERS[assigned]}_prep_{_LETTERS[prep]}"
                        fit[f"pop_{twin}"] = (float(norms[prep, order[assigned]])
                                              if norms.shape == (3, 3) else nan)
                for state, letter in enumerate(_LETTERS):
                    ok_mean = mean.shape == (3, 2)
                    fit[f"mean_{letter}_i"] = float(mean[order[state], 0]) if ok_mean else nan
                    fit[f"mean_{letter}_q"] = float(mean[order[state], 1]) if ok_mean else nan
            else:  # degenerate fit (blobs merged into fewer components)
                for prep in range(3):
                    for assigned in range(3):
                        if prep == assigned:
                            continue
                        fit[f"p_{_LETTERS[assigned]}_given_{_LETTERS[prep]}"] = nan
                        fit[f"pop_{_LETTERS[assigned]}_prep_{_LETTERS[prep]}"] = nan
                for letter in _LETTERS:
                    fit[f"mean_{letter}_i"] = fit[f"mean_{letter}_q"] = nan
            # COUNTED confusion (p_*_given_*): every shot hard-assigned to its
            # nearest blob center, so these fold the residual population together
            # with the discrimination overlap error. FITTED twins (pop_*_prep_*):
            # the population with the overlap removed (all Gaussians share one
            # width, so amplitude ratio = area ratio = mixture weight). Their
            # difference is roughly the discrimination error, which is why they
            # are two quantities and never share a key.
            fit["readout_fidelity"] = float(fidelity)
            fit["outlier_probability"] = float(
                np.mean(np.asarray(r["outlier_probability"], dtype=float)))
            result.fit[qubit] = fit
            # Chance is 1/3 here, but the gate stays at the parent's 0.5: a
            # three-state readout barely beating a coin is not usable data.
            ok = np.isfinite(fidelity) and 0.5 < fidelity <= 1.0
            result.outcomes[qubit] = Outcome.SUCCESSFUL if ok else Outcome.FAILED
        return result

    def update(self) -> None:
        # Per-state assignment fidelities + the three measured blob centers on the
        # target's READOUT CHANNEL (monitor fields, never pushed). fidelity_<x>
        # is the confusion DIAGONAL, i.e. 1 - (the two off-diagonals of that row);
        # the aggregate is derivable and stays run-record-only, as does the rest of
        # the confusion matrix (instrument-dependent — compare across instruments
        # by query, never as device state).
        #
        # All nine land together or not at all: a half-written reference (|f> from
        # this run, |g>/|e> from a previous one) is a frame nothing measured.
        if self.result is None:
            return
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is not Outcome.SUCCESSFUL:
                continue
            fidelities = {
                f"fidelity_{letter}": 1.0 - sum(
                    fit[f"p_{_LETTERS[a]}_given_{letter}"] for a in range(3) if a != s)
                for s, letter in enumerate(_LETTERS)
            }
            positions = {f"pos_{letter}_{axis}": fit[f"mean_{letter}_{axis}"]
                         for letter in _LETTERS for axis in ("i", "q")}
            if not np.all(np.isfinite(list(fidelities.values()) + list(positions.values()))):
                continue
            view = self.device.channel(qubit, "readout")
            for field, value in {**fidelities, **positions}.items():
                setattr(view, field, value)

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
