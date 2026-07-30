"""Residual excited-state population at idle — prepare |g>, count the |e> shots.

``single_shot_readout`` already reports this number (``pop_e_prep_g``) as a
by-product of preparing BOTH states. This experiment prepares only |g> and
spends every shot on the ground-state cloud, which is what a 1-5% population
needs: the number is a small minority of a large sample, so the statistics —
not the fit — set the error bar.

Because only one state is prepared there is nothing in the data that says where
|e> is. The blob centers come from the readout channel's stored ``pos_*``
monitors (an accepted ``single_shot_readout`` run), pinned into the mixture fit;
the experiment REFUSES to run without them rather than inventing a second
center from a cloud that has only one. That reference is snapshotted into
``dataset.nc`` at acquisition time, so the analysis replays offline unchanged.

Both readings are reported, and they are not one quantity by two methods:
``p_e_given_g`` is COUNTED (nearest-center hard assignment — population PLUS the
readout overlap error) and ``pop_e_prep_g`` is the FITTED mixture weight
(population with the overlap removed). The fitted one is the chip fact ``n_th``.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from pydantic import Field, model_validator

from .._scqat import per_qubit_results
from ..contract import DatasetContract
from ._capabilities.qubit_reset import QubitResetParameters
from ._sim import stable_seed
from ..parameters import TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register

#: (dataset variable, readout-channel monitor) for the pinned reference, in the
#: order the fit wants its centers: |g> first, |e> second.
_REFERENCE = (("ref_pos_g_i", "pos_g_i"), ("ref_pos_g_q", "pos_g_q"),
              ("ref_pos_e_i", "pos_e_i"), ("ref_pos_e_q", "pos_e_q"))

_MISSING_REFERENCE = (
    "no stored readout reference for {targets}: this experiment prepares only "
    "|g>, so the |e> blob center cannot come from its own data. Run "
    "single_shot_readout on {targets} and accept the pos_* monitor proposals "
    "(scqo accept), then re-run this."
)


class QubitThermalPopulationParameters(TargetSelection, QubitResetParameters):
    """Inputs for a residual-population measurement."""

    num_shots: int = Field(
        10000, gt=99,
        description="Ground-state shots (each recorded individually). Higher than a "
                    "two-state single-shot run on purpose: the measured quantity is a "
                    "few percent of the sample, so its error bar is counting noise.")

    @model_validator(mode="after")
    def _thermal_reset_only(self) -> "QubitThermalPopulationParameters":
        if self.reset_method != "thermal":
            raise ValueError(
                f"reset_method={self.reset_method!r} is refused here: this experiment "
                f"measures the population that a passive wait leaves behind, and an "
                f"active reset removes exactly that. Use reset_method='thermal' "
                f"(raise thermalization_time_ns if you want a longer wait).")
        return self


class QubitThermalPopulationResult(Result):
    """``fit[qubit]``: ``pop_e_prep_g`` (the FITTED |e> mixture weight — the
    population with the readout overlap removed; this is what lands as the mode
    fact ``n_th``), ``p_e_given_g`` (the COUNTED twin, population + overlap
    error, run-record-only), ``blob_std`` (the fitted blob width, in the same
    acquisition-frame units as the pinned centers) and
    ``outlier_probability``."""


@register
class QubitThermalPopulation(Experiment):
    """Backend-agnostic |g>-only shot cloud. ``probe()`` must record every shot."""

    name: ClassVar[str] = "qubit_thermal_population"
    description: ClassVar[str] = (
        "Prepare |g> only and record every readout shot's I/Q point, then split the "
        "cloud against the readout channel's STORED |g>/|e> blob centers to get the "
        "residual excited-state population at idle — the chip's thermal population, "
        "written back as the mode fact n_th. Reported twice: pop_e_prep_g is the "
        "fitted mixture weight (readout overlap removed, the value stored) and "
        "p_e_given_g is the counted fraction (population + overlap error, "
        "run-record-only); their difference is the discrimination error. REQUIRES an "
        "accepted single_shot_readout on the same targets — with one prepared state "
        "the data cannot locate |e> on its own — and refuses active reset, which "
        "would remove the population being measured. Defaults to 10000 shots because "
        "a 1-5% population is counting-noise limited."
    )
    Parameters: ClassVar[type] = QubitThermalPopulationParameters
    Result: ClassVar[type] = QubitThermalPopulationResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("prepared_state", "shot_idx"), sweep_units=("state", "shot"), variables=("I", "Q")
    )
    required_operations: ClassVar[tuple[str, ...]] = ("readout",)
    #: snapshot the stored blob centers into dataset.nc at acquisition time
    attach_readout_positions: ClassVar[bool] = True

    params: QubitThermalPopulationParameters

    def _reference_positions(self) -> dict[str, tuple[float, float, float, float]]:
        """The stored ``pos_*`` monitors per target, or raise naming the targets
        that have none.

        This gate cannot live in ``validate_targets``: that hook is a classmethod
        over the ROSTER (structure only) and cannot see stored values. Calling it
        from ``define_sweep`` keeps it pre-hardware all the same — ``run()``
        sweeps before it acquires — and the Session turns the raise into a
        structured failed result.
        """
        out: dict[str, tuple[float, float, float, float]] = {}
        missing: list[str] = []
        for target in self.params.targets:
            try:
                view = self.device.channel(target, "readout")
            except Exception:
                missing.append(target)
                continue
            values = []
            for _var, field in _REFERENCE:
                try:
                    value = getattr(view, field)
                except (KeyError, AttributeError):
                    value = None
                values.append(float("nan") if value is None else float(value))
            if all(np.isfinite(v) for v in values):
                out[target] = tuple(values)  # type: ignore[assignment]
            else:
                missing.append(target)
        if missing:
            raise ValueError(_MISSING_REFERENCE.format(targets=", ".join(missing)))
        return out

    def define_sweep(self) -> dict[str, np.ndarray]:
        self._reference_positions()  # refuse before any hardware runs
        # prepared_state is a length-1 axis, not a dropped one: it keeps the
        # dataset contract, the probes and the estimator identical to the
        # two-state family, so only the state COUNT differs.
        return {
            "prepared_state": np.array([0]),
            "shot_idx": np.arange(self.params.num_shots),
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        n_shots = coords["shot_idx"].size
        targets = self.params.targets
        rng = np.random.default_rng(stable_seed("qubit_thermal_population", *targets))
        reference = self._reference_positions()
        i_data = np.empty((len(targets), 1, n_shots))
        q_data = np.empty_like(i_data)
        for k, target in enumerate(targets):
            g_i, g_q, e_i, e_q = reference[target]
            p_thermal = rng.uniform(0.01, 0.05)
            # blobs sit at the STORED centers — the same reference the fit pins,
            # which is what the real experiment measures against
            excited = rng.random(n_shots) < p_thermal
            i_data[k, 0] = np.where(excited, e_i, g_i) + rng.normal(0, 1.0, n_shots)
            q_data[k, 0] = np.where(excited, e_q, g_q) + rng.normal(0, 1.0, n_shots)
        return {"I": i_data, "Q": q_data}

    def estimate(self) -> QubitThermalPopulationResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.state_discrimination import StateDiscriminationEstimator

        # The reference read from the DATASET, not the device: it was snapshotted at
        # acquisition time, so a re-analysis of a saved run uses the frame the data
        # was taken in even after the monitors have moved on.
        missing = [var for var, _field in _REFERENCE if var not in self.dataset.data_vars]
        if missing:
            raise ValueError(_MISSING_REFERENCE.format(targets=", ".join(self.params.targets)))
        pinned: dict[str, dict] = {}
        for target in self.params.targets:
            centers = [float(self.dataset[var].sel(target=target).values) for var, _f in _REFERENCE]
            if not all(np.isfinite(centers)):
                raise ValueError(_MISSING_REFERENCE.format(targets=target))
            # |g> first, |e> second — pinned centers keep their order through the
            # fit, so center 1 IS |e> and no label mapping is needed (contrast the
            # two-state run, which discovers its own centers and must permute).
            pinned[target] = {"user_mean": [[centers[0], centers[1]], [centers[2], centers[3]]]}

        prepared = self.dataset.transpose("target", "prepared_state", "shot_idx")
        results = per_qubit_results(
            prepared, StateDiscriminationEstimator(), artifact_dir=self.artifact_dir,
            per_target_kwargs=pinned,
        )
        # user_std is deliberately NOT pinned: the blob width is re-derived from
        # this run's own shots, so a stale stored width cannot bias the weight.

        result = QubitThermalPopulationResult()
        nan = float("nan")
        for qubit in self.params.targets:
            r = results[qubit]
            counts = np.asarray(r["direct_counts"], dtype=float)  # (1, 2), row sums to 1
            norms = np.asarray(r.get("gaussian_norms", []), dtype=float)  # (1, 2) fitted weights
            counted = float(counts[0, 1]) if counts.shape == (1, 2) else nan
            fitted = float(norms[0, 1]) if norms.shape == (1, 2) else nan
            std = r.get("trained_paras", {}).get("std", nan)
            result.fit[qubit] = {
                # FITTED mixture weight: the population alone (all Gaussians share
                # one width, so amplitude ratio = area ratio = mixture weight).
                "pop_e_prep_g": fitted,
                # COUNTED: every shot hard-assigned to its nearest center, so this
                # folds the population together with the readout overlap error and
                # reads high. Their difference is roughly that error.
                "p_e_given_g": counted,
                "blob_std": float(np.mean(np.asarray(std, dtype=float))),
                "outlier_probability": float(
                    np.mean(np.asarray(r["outlier_probability"], dtype=float))),
            }
            ok = np.isfinite(fitted) and 0.0 <= fitted < 0.5
            result.outcomes[qubit] = Outcome.SUCCESSFUL if ok else Outcome.FAILED
        return result

    def update(self) -> None:
        # n_th is a sample FACT (physical.json): the population the chip sits at in
        # the dark, no instrument setting realizes it. The FITTED weight is what
        # qualifies — the counted twin carries this readout's overlap error, which
        # is a property of the current knobs, not of the chip, and stays
        # run-record-only.
        if self.result is None:
            return
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is Outcome.SUCCESSFUL:
                self.device.component(qubit).n_th = fit["pop_e_prep_g"]

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
