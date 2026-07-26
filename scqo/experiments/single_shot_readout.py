"""Single-shot readout fidelity — IQ blobs, greenfield.

Port of :mod:`scqo.experiments.single_shot_readout`. The physics half
(per-shot ``prepared_state`` x ``shot_idx`` contract, two-Gaussian fit) is
byte-for-byte; what moved is the device surface in ``update()``: the
aggregate ``readout_fidelity`` is DELETED — the per-state ``fidelity_g`` /
``fidelity_e`` monitors and the ``pos_*`` blob centers land on the target's
READOUT CHANNEL, where the discriminator knobs (``readout_rotation_rad`` /
``readout_threshold`` / ``readout_rus_threshold``) a discriminating driver
proposes now live too.
"""

from __future__ import annotations

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


class SingleShotReadoutParameters(TargetSelection, QubitResetParameters):
    """Inputs for a single-shot readout-fidelity measurement."""

    num_shots: int = Field(2000, gt=99, description="Shots per prepared state (each recorded individually).")


class SingleShotReadoutResult(Result):
    """``fit[qubit]``: ``readout_fidelity`` (aggregate, run-record-only — the
    stored monitors are the per-state ``fidelity_g``/``fidelity_e`` on the
    readout channel), ``p_e_given_g`` (thermal + error proxy), ``p_g_given_e``
    (relaxation during readout + error), ``outlier_probability``, and the
    measured blob centers ``mean_g_i``/``mean_g_q``/``mean_e_i``/``mean_e_q``
    (acquisition-frame units; instrument-dependent run-record facts — the input a
    driver's discriminator calibration consumes)."""


@register
class SingleShotReadout(Experiment):
    """Backend-agnostic IQ blobs. ``probe()`` must record every shot (no averaging)."""

    name: ClassVar[str] = "single_shot_readout"
    description: ClassVar[str] = (
        "Prepare |g> and |e> and record every readout shot's I/Q point; a two-Gaussian "
        "mixture gives the per-state assignment fidelities (stored as the readout "
        "channel's fidelity_g/fidelity_e monitors), the confusion probabilities "
        "(run-record only) and the measured |g>/|e> blob centers (stored as the "
        "channel's pos_* reference). A driver that can discriminate additionally "
        "PROPOSES the readout channel's discriminator knobs (readout_rotation_rad / "
        "readout_threshold / readout_rus_threshold) as governed suggestions — review "
        "with scqo accept, then re-run to confirm; the run itself never mutates the "
        "readout frame, so its figure always shows the data as measured."
    )
    Parameters: ClassVar[type] = SingleShotReadoutParameters
    Result: ClassVar[type] = SingleShotReadoutResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("prepared_state", "shot_idx"), sweep_units=("state", "shot"), variables=("I", "Q")
    )
    required_operations: ClassVar[tuple[str, ...]] = ("readout",)

    params: SingleShotReadoutParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            "prepared_state": np.array([0, 1]),
            "shot_idx": np.arange(self.params.num_shots),
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        n_shots = coords["shot_idx"].size
        targets = self.params.targets
        rng = np.random.default_rng(stable_seed("single_shot_readout", *targets))
        i_data = np.empty((len(targets), 2, n_shots))
        q_data = np.empty_like(i_data)
        for k in range(len(targets)):
            sep = rng.uniform(3.5, 5.0)  # blob separation in units of sigma
            p_thermal = rng.uniform(0.01, 0.05)  # |e> population in the "ground" prep
            p_decay = rng.uniform(0.03, 0.08)  # relaxation during readout
            centers = {0: (0.0, 0.0), 1: (sep, 0.0)}
            for state in (0, 1):
                flip = p_thermal if state == 0 else p_decay
                actual = np.where(rng.random(n_shots) < flip, 1 - state, state)
                cx = np.array([centers[s][0] for s in actual])
                cy = np.array([centers[s][1] for s in actual])
                i_data[k, state] = cx + rng.normal(0, 1.0, n_shots)
                q_data[k, state] = cy + rng.normal(0, 1.0, n_shots)
        return {"I": i_data, "Q": q_data}

    def estimate(self) -> SingleShotReadoutResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.state_discrimination import StateDiscriminationEstimator

        # scqat's contract: I/Q over (prepared_state, shot_idx) — names already match.
        prepared = self.dataset.transpose("target", "prepared_state", "shot_idx")

        results = per_qubit_results(
            prepared, StateDiscriminationEstimator(), artifact_dir=self.artifact_dir
        )

        result = SingleShotReadoutResult()
        nan = float("nan")
        for qubit in self.params.targets:
            r = results[qubit]
            counts = np.asarray(r["direct_counts"], dtype=float)  # (prepared_state, label), rows sum to 1
            mean = np.asarray(r.get("trained_paras", {}).get("mean", []), dtype=float)  # (n_center, 2) IQ
            # The GMM's center order is not guaranteed to match the prepared-state
            # order; pick the label mapping that makes the diagonal the majority, and
            # map the same labels onto the g/e blob centers.
            (g_i, g_q), (e_i, e_q) = (nan, nan), (nan, nan)
            if counts.shape == (2, 2):
                direct = 0.5 * (counts[0, 0] + counts[1, 1])
                swapped = 0.5 * (counts[0, 1] + counts[1, 0])
                if direct >= swapped:
                    fidelity, p_e_g, p_g_e = direct, counts[0, 1], counts[1, 0]
                    g_label, e_label = 0, 1
                else:
                    fidelity, p_e_g, p_g_e = swapped, counts[0, 0], counts[1, 1]
                    g_label, e_label = 1, 0
                if mean.shape == (2, 2):
                    g_i, g_q = float(mean[g_label, 0]), float(mean[g_label, 1])
                    e_i, e_q = float(mean[e_label, 0]), float(mean[e_label, 1])
            else:  # degenerate fit (blobs merged into one component)
                fidelity, p_e_g, p_g_e = nan, nan, nan
            outlier_p = float(np.mean(np.asarray(r["outlier_probability"], dtype=float)))
            result.fit[qubit] = {
                "readout_fidelity": float(fidelity),
                "p_e_given_g": float(p_e_g),
                "p_g_given_e": float(p_g_e),
                "outlier_probability": outlier_p,
                # measured blob centers (acquisition-frame units) — the input a
                # driver's discriminator calibration consumes
                "mean_g_i": g_i, "mean_g_q": g_q, "mean_e_i": e_i, "mean_e_q": e_q,
            }
            ok = np.isfinite(fidelity) and 0.5 < fidelity <= 1.0
            result.outcomes[qubit] = Outcome.SUCCESSFUL if ok else Outcome.FAILED
        return result

    def update(self) -> None:
        # Record the per-state assignment fidelities + the measured |g>/|e> blob
        # centers on the target's READOUT CHANNEL (monitor fields, never pushed).
        # fidelity_g/fidelity_e come from the confusion entries (rows sum to 1, so
        # F_g = 1 - p_e_given_g and F_e = 1 - p_g_given_e); the aggregate
        # (F_g+F_e)/2 is derivable and never stored (run-record-only in the fit).
        # The centers are the stored REFERENCE the IQ->1D reductions consume
        # (radial ref / axial positions) and the input of the volts->population
        # conversion; consumers must staleness-gate them (they drift with the
        # readout condition). The confusion entries (p_e_given_g = thermal
        # population etc.) deliberately stay run-record-only: they are
        # instrument-dependent — compare across instruments by query, never as
        # device state. The run never mutates the readout frame, so the centers
        # are always in the frame the figure shows and are always safe to store.
        #
        # The DISCRIMINATOR knobs (the readout channel's readout_rotation_rad /
        # readout_threshold / readout_rus_threshold) are a driver concern (the
        # vendor-convention math needs the current rotation): a backend that can
        # discriminate overrides update() to PROPOSE them from these centers
        # through self.device (governed suggestions).
        if self.result is None:
            return
        pos_fields = (("pos_g_i", "mean_g_i"), ("pos_g_q", "mean_g_q"),
                      ("pos_e_i", "mean_e_i"), ("pos_e_q", "mean_e_q"))
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is Outcome.SUCCESSFUL:
                view = self.device.channel(qubit, "readout")
                view.fidelity_g = 1.0 - fit["p_e_given_g"]
                view.fidelity_e = 1.0 - fit["p_g_given_e"]
                if np.all(np.isfinite([fit[key] for _, key in pos_fields])):
                    for field, key in pos_fields:
                        setattr(view, field, fit[key])

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
