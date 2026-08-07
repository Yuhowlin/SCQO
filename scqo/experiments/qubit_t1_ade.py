"""T1 tracking via Analytical Decay Estimation — T1 vs laboratory time.

Reference: arXiv:2602.11912. Each block interleaves shots at the three delays
``t0``, ``t0 + dt``, ``t0 + 3*dt`` (so all three sample the same lab-time
window while T1 drifts) and the instrument computes the closed-form decay rate

    c = (P3 - P0) / (P1 - P0),   r = sqrt(c - 3/4) - 1/2,   gamma = -ln(r)/dt

plus the analytic shot-noise sigma on the fly — SPAM offset and amplitude
cancel in the ratio, so no confusion matrix is needed. The streams are one
(gamma, sigma) pair per block: a T1 time trace with per-point error bars,
`num_blocks` points long. Record-only: the trace characterizes T1 stability;
``qubit_relaxation`` remains the sole ``t1_s`` authority.

QM backend only today: the on-FPGA closed form needs the pulse processor's
real-time fixed-point math; a Qblox session gets the base ``probe()``'s
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
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register

#: the three delays of one block are ``t0 + DELAY_MULTS * dt`` — the 1:3
#: spacing is what makes the decay quadratic solvable in closed form. The
#: ``delay_idx`` coordinate carries these multipliers as its values.
DELAY_MULTS = (0, 1, 3)


def _snap_ns(value_ns: float) -> int:
    """Snap a duration to the 4 ns instrument grid (floor at one cycle)."""
    return max(4, int(round(value_ns / 4.0)) * 4)


class QubitT1AdeParameters(TargetSelection, AveragingParameters, QubitResetParameters):
    """Inputs for ADE T1 tracking. ``num_averages`` = shots per delay per block."""

    num_blocks: int = Field(
        100, gt=1, description="T1 estimates in the trace (one per block)."
    )
    t0_ns: float = Field(
        16, ge=16, description="Base delay of the three-point block, ns "
        "(snapped to the 4 ns grid)."
    )
    t1_guess_s: float = Field(
        50e-6, gt=0, description="Expected T1; sets the initial delay spacing "
        "dt = dt_factor * t1_guess_s."
    )
    dt_factor: float = Field(
        1.0, gt=0, description="Delay spacing as a fraction of t1_guess_s. "
        "Near 1 the three points straddle the decay knee, where the closed "
        "form is most sensitive."
    )
    adaptive_dt: bool = Field(
        False, description="Retune dt for the next block from the running "
        "gamma estimate (clipped to [min_dt_ns, max_dt_ns]). Off = fixed dt."
    )
    min_dt_ns: float = Field(16, ge=16, description="Lower dt clip, ns (adaptive mode).")
    max_dt_ns: float = Field(200_000, gt=0, description="Upper dt clip, ns (adaptive mode).")
    n_bootstrap: int = Field(
        300, ge=0, description="Host bootstrap resamples per block for an "
        "independent sigma from the streamed per-shot outcomes (0 = off)."
    )

    @model_validator(mode="after")
    def _dt_window_ordered(self) -> "QubitT1AdeParameters":
        if self.max_dt_ns <= self.min_dt_ns:
            raise ValueError(
                f"max_dt_ns ({self.max_dt_ns}) must exceed min_dt_ns ({self.min_dt_ns})"
            )
        return self


class QubitT1AdeResult(Result):
    """``fit[target]``: trace summary (``t1_median_s``, sigma medians, block
    counts). Record-only — nothing is proposed to device state."""


@register
class QubitT1Ade(Experiment):
    """Backend-agnostic ADE T1 tracking; the QM driver supplies probe()."""

    name: ClassVar[str] = "qubit_t1_ade"
    description: ClassVar[str] = (
        "Track T1 vs laboratory time: each block measures P(|1>) at three "
        "interleaved delays t0/t0+dt/t0+3dt and the instrument computes the "
        "closed-form decay rate + analytic sigma in real time (ADE, "
        "arXiv:2602.11912 — SPAM cancels, no confusion matrix). Streams a "
        "T1 trace with per-point error bars plus the raw shots for a host "
        "bootstrap cross-check. Record-only: characterizes T1 stability "
        "(drift, spread); qubit_relaxation stays the t1_s authority. "
        "reset_method='active' recommended (the shot rate IS the tracking "
        "bandwidth). Runs on the QM backend only (needs on-FPGA real-time "
        "math); a Qblox session refuses with NotImplementedError."
    )
    Parameters: ClassVar[type] = QubitT1AdeParameters
    Result: ClassVar[type] = QubitT1AdeResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("block_idx",), sweep_units=("",),
        variables=("estimated_gamma", "sigma_gamma", "dt_ns"),
    )
    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout")

    params: QubitT1AdeParameters

    # ------------------------------------------------------------------ helpers
    def initial_dt_ns(self) -> int:
        """The first (or fixed) delay spacing, snapped to the 4 ns grid and
        clipped to the adaptive window — ONE point of truth shared by the
        simulator and the driver probe."""
        dt = _snap_ns(self.params.dt_factor * self.params.t1_guess_s * 1e9)
        return int(np.clip(dt, self.params.min_dt_ns, self.params.max_dt_ns))

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {"block_idx": np.arange(self.params.num_blocks)}

    def readout_coords(self) -> dict[str, Any]:
        # delay_idx VALUES are the dt multipliers (t = t0 + value * dt);
        # shot_idx labels the per-delay shots the probe streams alongside.
        return {
            "delay_idx": np.array(DELAY_MULTS),
            "shot_idx": np.arange(self.params.num_averages),
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        n_blocks = coords["block_idx"].size
        targets = self.params.targets
        n_avg = self.params.num_averages
        rng = np.random.default_rng(stable_seed("qubit_t1_ade", *targets))
        dt_ns = float(self.initial_dt_ns())
        t0 = self.params.t0_ns * 1e-9

        gamma = np.empty((len(targets), n_blocks))
        sigma = np.empty_like(gamma)
        dts = np.full_like(gamma, dt_ns)
        shots = np.empty((len(targets), n_blocks, 3, n_avg), dtype=np.int8)
        times = np.empty_like(gamma)
        for k in range(len(targets)):
            t1 = rng.uniform(20e-6, 60e-6)  # hidden truth the trace must recover
            dt = dt_ns * 1e-9
            delays = t0 + np.array(DELAY_MULTS) * dt
            p = 0.93 * np.exp(-delays / t1) + 0.05  # SPAM cancels in the ratio
            shots[k] = rng.binomial(1, p[:, None], size=(n_blocks, 3, n_avg))
            pops = shots[k].mean(axis=2)
            # the closed form the FPGA evaluates, in float (mirrors
            # scqat.tools.ade_decay — simulate() must not import scqat)
            with np.errstate(divide="ignore", invalid="ignore"):
                denom = pops[:, 1] - pops[:, 0]
                c = np.where(denom != 0.0, (pops[:, 2] - pops[:, 0]) / denom, np.nan)
                valid = np.isfinite(c) & (c > 1.0) & (c < 3.0)
                x = np.sqrt(np.where(valid, c - 0.75, np.nan)) - 0.5
                gamma[k] = np.where(valid, -np.log(x) / dt, np.nan)
                dgamma_dc = -1.0 / (2.0 * x * (x + 0.5) * dt)
                var = sum(
                    (dgamma_dc * dc) ** 2 * pv * (1.0 - pv) / n_avg
                    for dc, pv in (
                        ((pops[:, 2] - pops[:, 1]) / denom**2, pops[:, 0]),
                        (-(pops[:, 2] - pops[:, 0]) / denom**2, pops[:, 1]),
                        (1.0 / denom, pops[:, 2]),
                    )
                )
                sigma[k] = np.where(valid, np.sqrt(var), np.nan)
            # one block = 3 * n_avg shots of (reset + delay + readout)
            block_period = 3 * n_avg * (t0 + 2 * dt + 5e-6)
            times[k] = np.arange(n_blocks) * block_period
        return {
            "estimated_gamma": gamma,
            "sigma_gamma": sigma,
            "dt_ns": dts,
            "state": (("target", "block_idx", "delay_idx", "shot_idx"), shots),
            "block_time_s": times,
        }

    def estimate(self) -> QubitT1AdeResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.qubit_t1_ade import QubitT1AdeEstimator

        # scqat's contract wants SI: dt in seconds as `dt_s`.
        prepared = self.dataset.rename({"dt_ns": "dt_s"})
        prepared = prepared.assign(dt_s=prepared["dt_s"] * 1e-9)

        results = per_qubit_results(
            prepared, QubitT1AdeEstimator(), artifact_dir=self.artifact_dir,
            n_bootstrap=self.params.n_bootstrap,
        )

        result = QubitT1AdeResult()
        for qubit in self.params.targets:
            r = results[qubit]
            result.fit[qubit] = {
                "t1_median_s": float(r["t1_median_s"]),
                "t1_sigma_median_s": float(r["t1_sigma_median_s"]),
                "t1_boot_sigma_median_s": float(r["t1_boot_sigma_median_s"]),
                "n_blocks": float(r["n_blocks"]),
                "n_valid": float(r["n_valid"]),
                "n_clipped": float(r["n_clipped"]),
            }
            result.outcomes[qubit] = (
                Outcome.SUCCESSFUL if bool(r["success"]) else Outcome.FAILED
            )
        return result

    # record-only: no update() — the base no-op stands; RECORD_ONLY pins it.

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
