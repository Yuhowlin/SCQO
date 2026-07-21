"""Residual ZZ vs coupler bias — the pair's decouple-point calibration (backend-free half).

2D map per PAIR: sweep the tunable coupler's standing bias x a Hahn-echo evolution
time on one member qubit while the OTHER member sits in |e> half the time — the
echo fringe (under a virtual detuning) oscillates at ``detuning + zz(bias)``, so
the signed residual ZZ is read per bias point. ``update()`` proposes the
zero-crossing bias as the pair's ``coupler_decouple_v`` (instrument knob — this
automates what the lab used to read off the figure and hand-edit into the vendor
config) and records the interpolated residual ``zz_hz`` at that point on the same
name's PHYSICAL Coupling slot (the trend shows how well each calibration nulled ZZ).
"""

from __future__ import annotations

from typing import ClassVar, Literal

import numpy as np
from pydantic import Field

from .._scqat import per_qubit_results
from ._sim import stable_seed
from ..contract import DatasetContract
from ..experiment import Experiment
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result


class PairZZCouplerParameters(TargetSelection, AveragingParameters):
    """Inputs for the ZZ-vs-coupler-bias map. ``targets`` are PAIR components."""

    min_coupler_v: float = Field(-0.3, ge=-0.5, description="Lowest coupler standing bias (V; OPX DAC rail is +/-0.5 V).")
    max_coupler_v: float = Field(0.3, le=0.5, description="Highest coupler standing bias (V).")
    num_coupler_points: int = Field(31, gt=4, description="Number of coupler bias points.")
    max_idle_time_ns: float = Field(4000, gt=0, description="Longest echo evolution time (ns; quantized to the instrument grid by the driver).")
    num_time_points: int = Field(41, gt=4, description="Number of evolution-time points.")
    detuning_hz: float = Field(
        1e6, gt=0,
        description="Virtual echo detuning (frame rotation, Hz): the fringe oscillates "
                    "at detuning + zz(bias), so zz is the signed offset from this.")
    measure: Literal["high", "low"] = Field(
        "low", description="Which pair member is echoed and measured (roster roles; "
                           "the driver maps high/low onto its vendor control/target).")


class PairZZCouplerResult(Result):
    """``fit[pair]``: ``coupler_zero_v`` (interpolated ZZ zero crossing),
    ``zz_hz`` (residual at that point), ``zz_min_hz``/``zz_max_hz`` (map range),
    ``old_coupler_decouple_v``. ``update()`` proposes coupler_decouple_v
    (TransmonPair knob) + zz_hz (Coupling physical fact)."""


class PairZZCoupler(Experiment):
    """Backend-agnostic pair ZZ map. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "pair_zz_coupler"
    description: ClassVar[str] = (
        "Residual-ZZ vs coupler standing bias (echo fringe under a virtual detuning, "
        "one pair member measured): finds the signed ZZ zero crossing and proposes it "
        "as the pair's coupler_decouple_v (the interaction-OFF operating point); the "
        "residual zz_hz at the new point lands on the pair's Coupling physical slot."
    )
    Parameters: ClassVar[type] = PairZZCouplerParameters
    Result: ClassVar[type] = PairZZCouplerResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("coupler_bias_v", "idle_time_ns"), sweep_units=("V", "ns"),
        variables=("signal",),
    )
    target_category: ClassVar[str] = "TransmonPair"
    required_operations: ClassVar[tuple[str, ...]] = ("coupler_bias",)

    params: PairZZCouplerParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        return {
            "coupler_bias_v": np.linspace(self.params.min_coupler_v,
                                          self.params.max_coupler_v,
                                          self.params.num_coupler_points),
            # 16 ns floor: each echo arm is one coupler pulse of >= 4 clock cycles
            "idle_time_ns": np.linspace(16, self.params.max_idle_time_ns,
                                        self.params.num_time_points),
        }

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """Synthetic map from the model the estimator fits: a signed zz(bias)
        crossing zero inside the sweep, fringe at detuning + zz, echo decay."""
        bias = coords["coupler_bias_v"]
        t_ns = coords["idle_time_ns"]
        pairs = self.params.targets
        rng = np.random.default_rng(stable_seed("pair_zz_coupler", *pairs))
        det = self.params.detuning_hz
        signal = np.empty((len(pairs), bias.size, t_ns.size))
        for k in range(len(pairs)):
            zero_v = rng.uniform(0.4 * bias.min(), 0.4 * bias.max())
            slope = rng.uniform(1.5, 3.0) * 1e6 / (bias.max() - bias.min())  # Hz/V
            t2_ns = rng.uniform(2e3, 5e3)
            zz = slope * (bias - zero_v)
            for j in range(bias.size):
                f_hz = det + zz[j]
                fringe = 0.5 - 0.5 * np.exp(-t_ns / t2_ns) * np.cos(2 * np.pi * f_hz * t_ns * 1e-9)
                signal[k, j] = fringe + rng.normal(0, 0.02, t_ns.size)
        return {"signal": signal}

    def estimate(self) -> PairZZCouplerResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.zz_interaction import ZZInteractionEchoEstimator

        det = self.params.detuning_hz
        # scqat's contract: var `signal`, dims (flux, time). Time in SECONDS so the
        # fitted per-flux frequency `f` comes out directly in Hz.
        prepared = self.dataset.rename({"coupler_bias_v": "flux", "idle_time_ns": "time"})
        prepared = prepared.transpose("target", "flux", "time")
        prepared = prepared.assign_coords(time=prepared["time"].values * 1e-9)
        results = per_qubit_results(prepared, ZZInteractionEchoEstimator(),
                                    artifact_dir=self.artifact_dir)

        result = PairZZCouplerResult()
        for pair in self.params.targets:
            bias = np.asarray(results[pair]["flux"], dtype=float)
            f_hz = np.asarray(results[pair]["f"], dtype=float)
            zz = f_hz - det  # signed residual ZZ per bias point
            try:
                old = self.device.component(pair).coupler_decouple_v
                old = float(old) if old is not None else None
            except Exception:
                old = None
            fit: dict = {
                "zz_min_hz": float(np.nanmin(zz)),
                "zz_max_hz": float(np.nanmax(zz)),
                "n_flux": int(bias.size),
                "old_coupler_decouple_v": old,
            }
            crossing = _zero_crossing(bias, zz)
            if crossing is not None:
                zero_v, zz_at_zero = crossing
                fit["coupler_zero_v"] = float(zero_v)
                fit["zz_hz"] = float(zz_at_zero)
                result.outcomes[pair] = Outcome.SUCCESSFUL
            else:
                # no sign change inside the sweep: report where |zz| is smallest
                i = int(np.nanargmin(np.abs(zz)))
                fit["closest_bias_v"] = float(bias[i])
                fit["zz_hz"] = float(zz[i])
                result.outcomes[pair] = Outcome.FAILED
            result.fit[pair] = fit
        return result

    def update(self) -> None:
        """Propose the decouple point (instrument knob on the pair) + the residual
        ZZ there (physical fact on the pair's Coupling slot). One component view
        carries both — routing by declaring side."""
        if self.result is None:
            return
        for pair, fit in self.result.fit.items():
            if self.result.outcomes[pair] is not Outcome.SUCCESSFUL:
                continue
            view = self.device.component(pair)
            view.coupler_decouple_v = fit["coupler_zero_v"]
            view.zz_hz = fit["zz_hz"]


def _zero_crossing(bias: np.ndarray, zz: np.ndarray) -> tuple[float, float] | None:
    """First sign change of zz(bias), linearly interpolated -> (bias, residual).

    The residual at the interpolated point is the linear-model error there —
    ~0 by construction, but kept honest (finite fit noise) for the zz_hz trend."""
    ok = np.isfinite(zz)
    b, z = bias[ok], zz[ok]
    if b.size < 2:
        return None
    sign_change = np.nonzero(np.diff(np.signbit(z)))[0]
    if sign_change.size == 0:
        return None
    i = int(sign_change[0])
    frac = z[i] / (z[i] - z[i + 1])
    zero_v = b[i] + frac * (b[i + 1] - b[i])
    zz_at = z[i] + frac * (z[i + 1] - z[i])  # ~0: interpolation residual
    return float(zero_v), float(zz_at)
