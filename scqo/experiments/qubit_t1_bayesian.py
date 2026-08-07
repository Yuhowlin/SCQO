"""Adaptive Bayesian T1 tracking — T1 vs laboratory time, shot by shot.

Reference: Berritta et al., arXiv:2506.09576. Each block runs ``num_probes``
adaptive single shots: the wait is tau = c * T1_est from the current posterior
over the relaxation rate Gamma1 ~ Gamma(shape=k, rate=theta = k*T1), and every
outcome updates (T1, k) through the method-of-moments rule with SPAM (alpha,
beta) folded in — near-optimal information per shot, a T1 estimate in
milliseconds of wall time. The instrument tracks the posterior in the
u = 1/k parametrization so k can grow past the fixed-point range and the
credible interval (~1/sqrt(k)) actually shrinks. Optionally each adaptive
probe is followed by one NON-adaptive shot on a linear wait grid, giving a
classical decay curve as an in-run cross-check.

Record-only: the trace characterizes T1 stability; ``qubit_relaxation``
remains the sole ``t1_s`` authority.

QM backend only today: the per-shot exp/ln/div feedback needs the pulse
processor's real-time math; a Qblox session gets the base ``probe()``'s
NotImplementedError.
"""

from __future__ import annotations

from typing import Any, ClassVar

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

#: the shortest adaptive wait the update law is applied to, seconds — below
#: this the physical wait (and the readout itself) dominates the shot anyway.
TAU_MIN_S = 1e-6


def posterior_step(t1_s: float, u: float, outcome: int, tau_s: float,
                   alpha: float, beta: float, *,
                   t1_min_s: float, t1_max_s: float,
                   u_min: float, u_max: float) -> tuple[float, float]:
    """One float method-of-moments update of the posterior state ``(T1, u)``.

    The float twin of the on-FPGA u = 1/k update (Berritta Eqs. 5-6): the
    simulator runs it directly, and it documents the exact law a driver's
    fixed-point implementation must realize. ``alpha`` = P(read 0 | prep 1),
    ``beta`` = P(read 1 | prep 0).
    """
    k = 1.0 / u
    theta = k * t1_s
    r = theta / (theta + tau_s)
    spam = 1.0 - alpha - beta
    rk = r**k
    rk1 = rk * r
    rk2 = rk1 * r
    if outcome == 0:
        num_k, den_k = 1.0 - beta - spam * rk1, 1.0 - beta - spam * rk
        num_k1, den_k1 = 1.0 - beta - spam * rk2, 1.0 - beta - spam * rk1
    else:
        num_k, den_k = beta + spam * rk1, beta + spam * rk
        num_k1, den_k1 = beta + spam * rk2, beta + spam * rk1
    ratio_k = num_k / den_k
    ratio_k1 = num_k1 / den_k1
    t1_next = float(np.clip(t1_s / ratio_k, t1_min_s, t1_max_s))
    u_next = float(np.clip((1.0 + u) * ratio_k1 / ratio_k - 1.0, u_min, u_max))
    return t1_next, u_next


class QubitT1BayesianParameters(TargetSelection, QubitResetParameters):
    """Inputs for adaptive Bayesian T1 tracking (single shots — no averaging)."""

    num_blocks: int = Field(
        100, gt=1, description="T1 estimates in the trace (one per block; the "
        "posterior restarts from the prior each block)."
    )
    num_probes: int = Field(
        100, gt=1, description="Adaptive single-shot probes per block; the "
        "final posterior shape k grows with it (CI ~ 1/sqrt(k))."
    )
    adaptive_c: float = Field(
        0.51, gt=0, lt=1.59, description="Adaptive waiting-time coefficient: "
        "tau = c * T1_est. The information-optimal value is ~0.51."
    )
    k0: float = Field(1.0, gt=0, description="Initial posterior shape (Gamma prior).")
    t1_prior_s: float | None = Field(
        35e-6, gt=0, description="Prior mean T1, seconds. None = anchor to the "
        "device's standing t1_s fact (design fallback) instead. A prior far "
        "from the truth pins the estimate at a rail — the interleaved "
        "validation flags that."
    )
    t1_min_s: float = Field(1e-6, gt=0, description="Lower rail of the adaptive T1 estimate.")
    t1_max_s: float = Field(100e-6, gt=0, description="Upper rail of the adaptive T1 estimate.")
    k_min: float = Field(0.2, gt=0, description="Lower bound of the posterior shape k.")
    k_max: float = Field(
        100.0, gt=0, description="Upper bound of the posterior shape k (only "
        "the u = 1/k storage resolution limits it)."
    )
    interleaved_validation: bool = Field(
        True, description="After each adaptive probe, run one NON-adaptive "
        "shot on a linear wait grid — a classical decay curve fitted as the "
        "in-run cross-check on the adaptive estimate."
    )
    min_wait_ns: float = Field(16, ge=16, description="Shortest validation-grid wait.")
    max_wait_ns: float = Field(200_000, gt=0, description="Longest validation-grid wait.")
    active_reset_per_probe: bool = Field(
        False, description="Also actively reset before each interleaved "
        "validation shot (the adaptive probe's conditional flip-back already "
        "leaves |g> otherwise)."
    )
    ci: float = Field(
        0.90, gt=0, lt=1, description="Credible-interval level drawn on the "
        "T1 trace (posterior over T1 is inverse-gamma(k, k*T1))."
    )

    @model_validator(mode="after")
    def _windows_ordered(self) -> "QubitT1BayesianParameters":
        if self.t1_max_s <= self.t1_min_s:
            raise ValueError(
                f"t1_max_s ({self.t1_max_s}) must exceed t1_min_s ({self.t1_min_s})"
            )
        if self.k_max <= self.k_min:
            raise ValueError(f"k_max ({self.k_max}) must exceed k_min ({self.k_min})")
        if self.max_wait_ns <= self.min_wait_ns:
            raise ValueError(
                f"max_wait_ns ({self.max_wait_ns}) must exceed min_wait_ns ({self.min_wait_ns})"
            )
        return self


class QubitT1BayesianResult(Result):
    """``fit[target]``: trace summary (``t1_median_s``, ``k_final_median``,
    validation cross-check). Record-only — nothing is proposed."""


@register
class QubitT1Bayesian(Experiment):
    """Backend-agnostic adaptive Bayesian T1; the QM driver supplies probe()."""

    name: ClassVar[str] = "qubit_t1_bayesian"
    description: ClassVar[str] = (
        "Track T1 vs laboratory time with per-shot adaptive Bayesian "
        "estimation (Berritta et al., arXiv:2506.09576): each single shot "
        "waits tau = c * T1_est from the current posterior and updates it in "
        "real time (u = 1/k parametrization), reaching a T1 estimate with a "
        "shrinking credible interval in ~num_probes shots per block. "
        "Interleaved non-adaptive shots give a classical decay cross-check. "
        "Record-only: characterizes T1 stability; qubit_relaxation stays the "
        "t1_s authority. REQUIRES a calibrated readout discriminator AND a "
        "measured confusion matrix (the SPAM-aware likelihood reads "
        "alpha/beta from it). Runs on the QM backend only (per-shot exp/ln "
        "feedback on the pulse processor); a Qblox session refuses with "
        "NotImplementedError."
    )
    Parameters: ClassVar[type] = QubitT1BayesianParameters
    Result: ClassVar[type] = QubitT1BayesianResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("block_idx",), sweep_units=("",),
        variables=("estimated_t1_s", "u_final"),
    )
    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout")

    params: QubitT1BayesianParameters

    # ------------------------------------------------------------------ helpers
    def resolve_t1_prior_s(self, target: str) -> float:
        """The prior mean T1 for one target — the explicit parameter when set,
        else the device's standing ``t1_s`` fact (design fallback) via
        ``anchor()``. ONE point of truth shared by the simulator and the
        driver probe."""
        if self.params.t1_prior_s is not None:
            return float(self.params.t1_prior_s)
        return self.anchor(target, "t1_s")

    def lin_wait_ns(self) -> np.ndarray:
        """The interleaved validation wait grid, snapped to the 4 ns
        instrument grid — shared by the simulator and the driver probe."""
        grid = np.linspace(
            self.params.min_wait_ns, self.params.max_wait_ns, self.params.num_probes
        )
        return np.maximum(16, np.round(grid / 4.0).astype(int) * 4)

    def define_sweep(self) -> dict[str, np.ndarray]:
        # resolve the priors NOW so a missing t1_s fact (t1_prior_s=None on an
        # unmeasured device) refuses before any hardware runs
        self._t1_prior_s = {t: self.resolve_t1_prior_s(t) for t in self.params.targets}
        return {"block_idx": np.arange(self.params.num_blocks)}

    def readout_coords(self) -> dict[str, Any]:
        return {"probe_idx": np.arange(self.params.num_probes)}

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        p = self.params
        n_blocks = coords["block_idx"].size
        n_probes = p.num_probes
        targets = p.targets
        rng = np.random.default_rng(stable_seed("qubit_t1_bayesian", *targets))
        alpha, beta = 0.03, 0.05  # hidden SPAM of the simulated readout
        u_min, u_max = 1.0 / p.k_max, 1.0 / p.k_min
        lin_wait_s = self.lin_wait_ns() * 1e-9

        t1_final = np.empty((len(targets), n_blocks))
        u_final = np.empty_like(t1_final)
        tau_all = np.empty((len(targets), n_blocks, n_probes))
        state_all = np.empty((len(targets), n_blocks, n_probes), dtype=np.int8)
        state_lin = np.empty_like(state_all)
        u_evol = np.empty((len(targets), n_probes))
        t1_evol = np.empty_like(u_evol)
        times = np.empty_like(t1_final)
        for kk, target in enumerate(targets):
            t1_true = rng.uniform(20e-6, 60e-6)  # hidden truth the trace must recover
            prior = self._t1_prior_s[target]
            for b in range(n_blocks):
                t1_est = float(np.clip(prior, p.t1_min_s, p.t1_max_s))
                u = float(np.clip(1.0 / p.k0, u_min, u_max))
                for j in range(n_probes):
                    if b == n_blocks - 1:
                        u_evol[kk, j] = u
                        t1_evol[kk, j] = t1_est
                    tau = float(np.clip(p.adaptive_c * t1_est, TAU_MIN_S, p.t1_max_s))
                    m = int(rng.random() < beta + (1 - alpha - beta) * np.exp(-tau / t1_true))
                    tau_all[kk, b, j] = tau
                    state_all[kk, b, j] = m
                    t1_est, u = posterior_step(
                        t1_est, u, m, tau, alpha, beta,
                        t1_min_s=p.t1_min_s, t1_max_s=p.t1_max_s,
                        u_min=u_min, u_max=u_max,
                    )
                    state_lin[kk, b, j] = int(
                        rng.random()
                        < beta + (1 - alpha - beta) * np.exp(-lin_wait_s[j] / t1_true)
                    )
                t1_final[kk, b] = t1_est
                u_final[kk, b] = u
            # one block = num_probes adaptive (+ validation) shots
            block_period = n_probes * (p.adaptive_c * t1_true + 5e-6)
            if p.interleaved_validation:
                block_period += float(np.mean(lin_wait_s)) * n_probes + n_probes * 5e-6
            times[kk] = np.arange(n_blocks) * block_period
        out: dict[str, Any] = {
            "estimated_t1_s": t1_final,
            "u_final": u_final,
            "tau_s": (("target", "block_idx", "probe_idx"), tau_all),
            "state": (("target", "block_idx", "probe_idx"), state_all),
            "u_evol": (("target", "probe_idx"), u_evol),
            "t1_evol_s": (("target", "probe_idx"), t1_evol),
            "block_time_s": times,
        }
        if p.interleaved_validation:
            out["state_lin"] = (("target", "block_idx", "probe_idx"), state_lin)
            out["lin_wait_s"] = (
                ("probe_idx",), lin_wait_s,
            )
        return out

    def estimate(self) -> QubitT1BayesianResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.qubit_t1_bayesian import QubitT1BayesianEstimator

        results = per_qubit_results(
            self.dataset, QubitT1BayesianEstimator(), artifact_dir=self.artifact_dir,
            ci=self.params.ci,
        )

        result = QubitT1BayesianResult()
        for qubit in self.params.targets:
            r = results[qubit]
            fit = {
                "t1_median_s": float(r["t1_median_s"]),
                "k_final_median": float(r["k_final_median"]),
                "t1_prior_s": float(self._t1_prior_s[qubit]),
            }
            if r.get("has_validation"):
                fit["t1_lin_s"] = float(r["t1_lin_s"])
                fit["t1_lin_ratio"] = float(r["t1_lin_ratio"])
                fit["validation_disagrees"] = float(bool(r["validation_disagrees"]))
            result.fit[qubit] = fit
            result.outcomes[qubit] = (
                Outcome.SUCCESSFUL if bool(r["success"]) else Outcome.FAILED
            )
        return result

    # record-only: no update() — the base no-op stands; RECORD_ONLY pins it.

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
