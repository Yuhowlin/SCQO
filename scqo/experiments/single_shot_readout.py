"""Single-shot readout fidelity — IQ blobs (backend-free half).

The stack's first PER-SHOT experiment: prepare |g> and |e|, record every shot's
I/Q point (no averaging), fit a two-Gaussian mixture and report the assignment
fidelity and the confusion probabilities. ``p_e_given_g`` doubles as the
thermal-population + assignment-error proxy — the quantity to compare across
instruments for the same sample (filter runs by backend).

Contract note: unlike every other experiment, the "sweep" axes are the prepared
state {0, 1} and the shot index — probes must return one I/Q pair PER SHOT
(non-averaged acquisition; Qblox: append bin mode, QM: no ``.average()`` on the
stream). ``update()`` is a no-op (reported quantities).
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


class SingleShotReadoutParameters(TargetSelection, Parameters):
    """Inputs for a single-shot readout-fidelity measurement."""

    num_shots: int = Field(2000, gt=99, description="Shots per prepared state (each recorded individually).")


class SingleShotReadoutResult(Result):
    """``fit[qubit]``: ``readout_fidelity``, ``p_e_given_g`` (thermal + error proxy),
    ``p_g_given_e`` (relaxation during readout + error), ``outlier_probability``."""


class SingleShotReadout(Experiment):
    """Backend-agnostic IQ blobs. ``probe()`` must record every shot (no averaging)."""

    name: ClassVar[str] = "single_shot_readout"
    description: ClassVar[str] = (
        "Prepare |g> and |e> and record every readout shot's I/Q point; two-Gaussian "
        "mixture gives the assignment fidelity (recorded into the device state, "
        "record-only) and confusion probabilities (run-record only)."
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
        for qubit in self.params.targets:
            r = results[qubit]
            counts = np.asarray(r["direct_counts"], dtype=float)  # (prepared_state, label), rows sum to 1
            # The GMM's center order is not guaranteed to match the prepared-state
            # order; pick the label mapping that makes the diagonal the majority.
            if counts.shape == (2, 2):
                direct = 0.5 * (counts[0, 0] + counts[1, 1])
                swapped = 0.5 * (counts[0, 1] + counts[1, 0])
                if direct >= swapped:
                    fidelity, p_e_g, p_g_e = direct, counts[0, 1], counts[1, 0]
                    g_center = r["trained_paras"]["mean"][0]
                    e_center = r["trained_paras"]["mean"][1]
                else:
                    fidelity, p_e_g, p_g_e = swapped, counts[0, 0], counts[1, 1]
                    g_center = r["trained_paras"]["mean"][1]
                    e_center = r["trained_paras"]["mean"][0]
                gef_centers = [
                    [float(g_center[0]), float(g_center[1])],
                    [float(e_center[0]), float(e_center[1])],
                ]
            else:  # degenerate fit (blobs merged into one component)
                fidelity, p_e_g, p_g_e = float("nan"), float("nan"), float("nan")
                gef_centers = None

            outlier_p = float(np.mean(np.asarray(r["outlier_probability"], dtype=float)))
            fit_dict = {
                "readout_fidelity": float(fidelity),
                "p_e_given_g": float(p_e_g),
                "p_g_given_e": float(p_g_e),
                "outlier_probability": outlier_p,
            }
            if gef_centers is not None:
                fit_dict["gef_centers"] = gef_centers
            result.fit[qubit] = fit_dict

            ok = np.isfinite(fidelity) and 0.5 < fidelity <= 1.0
            result.outcomes[qubit] = Outcome.SUCCESSFUL if ok else Outcome.FAILED

            # Directly update & persist QUAM machine gef_centers, integration_weights_angle & threshold
            if ok and gef_centers is not None and self.backend is not None and hasattr(self.backend, "machine"):
                machine = getattr(self.backend, "machine", None)
                if machine is not None and hasattr(machine, "qubits") and qubit in machine.qubits:
                    q_obj = machine.qubits[qubit]
                    if hasattr(q_obj, "resonator"):
                        q_obj.resonator.gef_centers = gef_centers
                        
                        # Method 2: Calculate residual 2D rotation angle delta_theta & update total integration_weights_angle
                        Ig, Qg = g_center[0], g_center[1]
                        Ie, Qe = e_center[0], e_center[1]
                        dI = float(Ie - Ig)
                        dQ = float(Qe - Qg)
                        delta_angle = float(np.arctan2(dQ, dI))

                        rot_g = Ig * np.cos(delta_angle) + Qg * np.sin(delta_angle)
                        rot_e = Ie * np.cos(delta_angle) + Qe * np.sin(delta_angle)
                        v_th = float(0.5 * (rot_g + rot_e))

                        if "readout" in q_obj.resonator.operations:
                            readout_op = q_obj.resonator.operations["readout"]
                            curr_angle = float(getattr(readout_op, "integration_weights_angle", 0.0) or 0.0)
                            total_angle = (curr_angle + delta_angle + np.pi) % (2 * np.pi) - np.pi
                            readout_op.integration_weights_angle = total_angle
                            readout_op.threshold = v_th

                        if hasattr(machine, "save"):
                            try:
                                machine.save()
                            except Exception:
                                pass
        return result

    def update(self) -> None:
        # Record the assignment fidelity as device state (record-only field).
        if self.result is None:
            return
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is Outcome.SUCCESSFUL:
                comp = self.device.component(qubit)
                comp.readout_fidelity = fit["readout_fidelity"]
