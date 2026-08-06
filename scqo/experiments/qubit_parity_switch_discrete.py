"""Charge-parity monitor, DISCRETE variant — two measurements per cycle at a
fixed cycle period.

Per cycle: ``M1`` (measure) — depletion wait — ``x90`` — fixed idle — ``y90``
— ``M2`` (measure) — pad wait, repeated at the total period ``t_p``
(``cycle_period_ns``). M1 PROJECTS the qubit (measurement-based
re-initialization), the mapping block flips the pole iff the charge parity is
odd, and M2 reads the result — so the parity of cycle ``i`` is the
WITHIN-CYCLE difference ``m1[i] XOR m2[i]``, self-contained per cycle. The
idle derivation is identical to the continuous sibling
(``idle_multiple / (2 x parity_delta_f_hz)``, odd multiples only), and the
pulse order (x90 first, y90 second) is physically equivalent to the
continuous y90-first order up to a global sign flip of the telegraph, which
the PSD cannot see.

WHY A SECOND MEASUREMENT: in ``qubit_parity_switch_continuous`` the previous
shot's measurement plays M1's role, so the readout is the running XOR of the
parity and any T1 decay between shots corrupts the XOR chain — the cadence
must stay as tight as depletion allows. Here M1 re-projects every cycle:
decay or readout error during the pad wait only shows up as a mismatch
between ``m1[i+1]`` and ``m2[i]`` (the ``p_intercycle_flip`` diagnostic) and
corrupts NO parity sample. That is what makes a LONG cycle period safe — the
telegraph may be sampled slowly, lowering the spectrum's low edge per
acquisition bin, at the price of two readouts (and two acquisition bins) per
parity sample. A single bad measurement flips exactly ONE parity sample
(continuous: two, since adjacent pairs share the shot).

The cost side: every cycle spends two readout durations, and on Qblox two
acquisition bins, so the bin-limited record halves relative to continuous at
the same cycle content — raise ``cycle_period_ns`` rather than the shot count
when reach is the goal.

Driver contract (``probe()``):

- per cycle measure ``M1`` — depletion wait
  (``scqo.experiments._depletion.depletion_wait_ns``; never ``None``) —
  ``x90`` — idle (:meth:`QubitParitySwitchContinuous.resolved_idle_ns`, per
  target, ns) — ``y90`` — measure ``M2`` — pad wait; record BOTH measurements
  of every cycle individually on the ``(shot_idx, meas_idx)`` grid,
  ``meas_idx`` 0 = M1 (no averaging exists here by design);
- NO qubit reset of any kind — M1's projection is the initialization;
- when ``cycle_period_ns`` is set, pad the cycle to EXACTLY that period and
  refuse by name if the scheduled sequence alone is longer; ``None`` means no
  pad (the minimal cycle);
- SHOULD set ``self.probe_shot_period_s[target] = <seconds>`` — the exact
  scheduled cycle period. :meth:`attach_acquisition_coords` attaches it to
  the dataset and the estimator uses it as the telegraph timebase; a period
  error scales the rate linearly.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from pydantic import Field

from .._scqat import per_qubit_results
from ..contract import DatasetContract
from ._capabilities.state_readout import SHOT_STATE_ALT, shot_state_vars
from ._sim import stable_seed
from ..result import Outcome, Result
from . import register
from .qubit_parity_switch_continuous import (
    _NOMINAL_PI2_S,
    _NOMINAL_READOUT_S,
    QubitParitySwitchContinuous,
    QubitParitySwitchContinuousParameters,
)


class QubitParitySwitchDiscreteParameters(QubitParitySwitchContinuousParameters):
    """Inputs for the discrete (two-measurement-per-cycle) parity monitor.

    A "shot" here is one CYCLE = two measurements; ``record_time_s`` /
    ``num_shots`` / ``max_num_shots`` all count cycles.
    """

    cycle_period_ns: float | None = Field(
        None, gt=0,
        description="Total cycle period t_p, ns — the telegraph timebase (the parity is "
                    "sampled once per cycle at exactly this cadence). None (the default) "
                    "= no pad: the cycle is just the scheduled sequence (M1 + depletion + "
                    "the two pi/2 pulses + idle + M2). Set it to sample SLOWLY: the "
                    "spectrum's window scales as 1/t_p, so a longer period reaches lower "
                    "rates from the same number of acquisition bins — and M1's "
                    "re-projection makes that safe (T1 during the pad corrupts no parity "
                    "sample, unlike the continuous variant). The probe pads the tail "
                    "wait to hit t_p exactly and REFUSES by name when t_p is shorter "
                    "than the scheduled sequence.")
    max_num_shots: int = Field(
        1_400_000, gt=99,
        description="Refusal ceiling for the DERIVED cycle count (never a clamp). Each "
                    "cycle records TWO measurements — two acquisition bins on Qblox — so "
                    "this default sits deliberately under HALF the Qblox 3e6 bin limit. "
                    "Raise it consciously, or pass num_shots to bypass.")


class QubitParitySwitchDiscreteResult(Result):
    """``fit[qubit]``: the continuous variant's key set (``parity_rate_hz``,
    the PSD fit scalars, ``n_parity_switches`` / ``p_switch`` /
    ``p_parity_odd``, the spectral-reach numbers, ``mapping_fidelity*``, the
    timing provenance) plus the discrete-only diagnostics:
    ``p_intercycle_flip`` (how often ``m1[i+1] != m2[i]`` — the QND-chain
    health check; 0 is clean, and it corrupts no parity sample),
    ``p_m1_high`` / ``p_m2_high`` (per-measurement-slot readout balance) and
    ``cycle_period_ns`` (the requested period; NaN when unset)."""


@register
class QubitParitySwitchDiscrete(QubitParitySwitchContinuous):
    """Backend-agnostic discrete parity monitor. ``probe()`` is supplied by a
    driver."""

    name: ClassVar[str] = "qubit_parity_switch_discrete"
    description: ClassVar[str] = (
        "Two-measurement charge-parity monitor: per cycle M1 - depletion wait - x90 - "
        "idle - y90 - M2 - pad, repeated at the fixed period cycle_period_ns (None = "
        "minimal). M1 projects the qubit and M2 reads the parity mapping, so parity[i] "
        "= m1[i] XOR m2[i] WITHIN each cycle — no chain across cycles, which is what "
        "makes a slow cycle period safe: T1 or readout error between cycles shows up "
        "only as the p_intercycle_flip diagnostic and corrupts no parity sample (the "
        "continuous variant cannot slow down without corruption; it costs half the "
        "readouts, this one tolerates slow sampling). idle = idle_multiple / (2 x "
        "parity_delta_f_hz) exactly as in qubit_parity_switch_continuous (odd multiples "
        "only), from the drive channel's stored beat splitting (accepted "
        "ramsey_model='beat' qubit_ramsey). The Lorentzian corner of the parity series' "
        "Welch PSD gives the per-direction switching rate (rate = pi x corner), written "
        "back as the mode fact parity_rate_hz. Records BOTH measurements per cycle "
        "(shot_idx x meas_idx; two acquisition bins per cycle — the Qblox bin-limited "
        "record halves). use_state_discrimination returns FPGA-discriminated 0/1 per "
        "measurement; the I/Q path REQUIRES an accepted single_shot_readout (stored "
        "pos_* centers). Also REQUIRES an accepted resonator_spectroscopy "
        "(readout_depletion_s governs the post-M1 wait)."
    )
    Parameters: ClassVar[type] = QubitParitySwitchDiscreteParameters
    Result: ClassVar[type] = QubitParitySwitchDiscreteResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("shot_idx", "meas_idx"), sweep_units=("shot", ""),
        variables=("I", "Q"), alt_variables=SHOT_STATE_ALT,
    )

    params: QubitParitySwitchDiscreteParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        # the whole continuous pre-flight (idle/depletion resolution, reference
        # centers, cycle count) applies verbatim; only the meas axis is new.
        sweep = super().define_sweep()
        sweep["meas_idx"] = np.arange(2)
        return sweep

    def _estimated_shot_period_s(self, target: str) -> float:
        """Knob-based estimate of the CYCLE period: the minimal scheduled
        sequence (with TWO readouts), floored by ``cycle_period_ns`` when set.
        Never refuses on an estimate — only the probe knows the exact sequence
        length, so the too-short refusal is its call."""
        timing = self._resolved[target]
        period = (timing["idle_ns"] + timing["depletion_ns"]) * 1e-9
        try:
            readout = float(
                self.device.channel(target, "readout").readout_duration_s)
        except (KeyError, AttributeError):
            readout = _NOMINAL_READOUT_S
        try:
            pulse = float(self.device.channel(target, "drive").pi_duration_s)
        except (KeyError, AttributeError):
            pulse = _NOMINAL_PI2_S
        minimal = period + 2.0 * readout + 2.0 * pulse
        if self.params.cycle_period_ns is not None:
            return max(minimal, float(self.params.cycle_period_ns) / 1e9)
        return minimal

    # -------------------------------------------------------------- offline
    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        n_shots = coords["shot_idx"].size
        targets = self.params.targets
        rng = np.random.default_rng(
            stable_seed("qubit_parity_switch_discrete", *targets))
        use_state = self.params.use_state_discrimination
        reference = None if use_state else self._reference_positions()
        i_data = np.empty((len(targets), n_shots, 2))
        q_data = np.empty_like(i_data)
        state = np.empty_like(i_data)
        for k, target in enumerate(targets):
            # the parity telegraph, sampled at the cycle period
            p_flip = rng.uniform(0.002, 0.01)
            parity = np.cumsum(rng.random(n_shots) < p_flip) % 2
            # QND chain: M1 re-measures the pole the previous M2 left behind
            # (m1[0] = 0) and the mapping flips it iff the parity is odd, so
            # m2 = m1 XOR parity — m1 is the running XOR of the parity while
            # the WITHIN-CYCLE difference recovers the telegraph exactly.
            m2 = np.cumsum(parity) % 2
            m1 = np.concatenate([[0], m2[:-1]])
            pair = np.stack([m1, m2], axis=-1)
            if use_state:
                # per-MEASUREMENT readout error: one bad measurement flips
                # exactly ONE parity sample here (continuous: two).
                errors = rng.random(pair.shape) < 1e-4
                state[k] = (pair ^ errors).astype(float)
            else:
                g_i, g_q, e_i, e_q = reference[target]
                i_data[k] = np.where(pair, e_i, g_i) + rng.normal(
                    0, 0.4, pair.shape)
                q_data[k] = np.where(pair, e_q, g_q) + rng.normal(
                    0, 0.4, pair.shape)
        return shot_state_vars(use_state, state, i_data, q_data)

    # ------------------------------------------------------------- analysis
    def estimate(self) -> QubitParitySwitchDiscreteResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.parity_switch_discrete import (
            ParitySwitchDiscreteEstimator,
        )

        prepared = self.dataset.transpose("target", "shot_idx", "meas_idx")
        results = per_qubit_results(prepared, ParitySwitchDiscreteEstimator(),
                                    artifact_dir=self.artifact_dir,
                                    model=self.params.psd_model)

        nan = float("nan")
        result = QubitParitySwitchDiscreteResult()
        for qubit in self.params.targets:
            r = results[qubit]
            rate = float(r.get("parity_rate_hz", nan))
            dt = float(r.get("dt_s", nan))
            p_parity_odd = float(r.get("p_parity_odd", nan))
            fit = {
                "parity_rate_hz": rate,
                "psd_corner_hz": float(r.get("psd_corner_hz", nan)),
                "psd_amplitude": float(r.get("psd_amplitude", nan)),
                "psd_white_floor": float(r.get("psd_white_floor", nan)),
                "n_parity_switches": int(r.get("n_transitions", 0)),
                "p_switch": float(r.get("p_switch", nan)),
                "p_parity_odd": p_parity_odd,
                # the discrete health check: m1[i+1] != m2[i] counts QND-chain
                # breaks (T1 / readout error during the pad wait). Reported,
                # never gated — it corrupts no parity sample.
                "p_intercycle_flip": float(r.get("p_intercycle_flip", nan)),
                "p_m1_high": float(r.get("p_m1_high", nan)),
                "p_m2_high": float(r.get("p_m2_high", nan)),
                "psd_freq_min_hz": float(r.get("psd_freq_min_hz", nan)),
                "psd_contrast": float(r.get("psd_contrast", nan)),
                "corner_margin_low": float(r.get("corner_margin_low", nan)),
                "psd_fit_residual": float(r.get("psd_fit_residual", nan)),
                "mapping_fidelity": float(r.get("mapping_fidelity", nan)),
                "mapping_fidelity_floor": float(
                    r.get("mapping_fidelity_floor", nan)),
                "mapping_fidelity_ratio": float(
                    r.get("mapping_fidelity_ratio", nan)),
                # timing provenance, from the acquisition-time snapshot
                "shot_period_s": dt,
                "record_time_s": self._acquired("record_time_s", qubit),
                "requested_record_time_s": float(self.params.record_time_s),
                "idle_time_ns": self._acquired("idle_time_ns", qubit),
                "idle_multiple": float(self.params.idle_multiple),
                "parity_delta_f_hz": self._acquired("parity_delta_f_hz", qubit),
                "cycle_period_ns": (float(self.params.cycle_period_ns)
                                    if self.params.cycle_period_ns is not None
                                    else nan),
            }
            if "outlier_probability" in r:
                fit["outlier_probability"] = float(r["outlier_probability"])
            result.fit[qubit] = fit
            # same trust gate as continuous: converged knee inside the window,
            # real plateau-to-floor contrast, and a parity that actually
            # toggles.
            ok = (bool(r.get("success")) and np.isfinite(rate)
                  and 0.0 < rate < 0.5 / dt and 0.02 < p_parity_odd < 0.98)
            result.outcomes[qubit] = Outcome.SUCCESSFUL if ok else Outcome.FAILED
        return result

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
