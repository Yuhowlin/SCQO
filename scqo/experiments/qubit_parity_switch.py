"""Charge-parity monitor — a fixed y90-idle-x90 sequence sampled as a shot trace.

After a beat-model ``qubit_ramsey`` retunes the drive to the MEAN of the two
charge-parity branches, the branches sit at +/- parity_delta_f_hz / 2 around
the drive. A fixed free evolution of ``t = 1 / (2 x parity_delta_f_hz)``
accumulates a parity-dependent phase of exactly +/- pi/2, and the 90-degree
SHIFTED second pulse (y90 ... x90) measures sin of it — odd in parity, so the
two parities land on opposite readout poles (a same-axis pair would measure
the even cos and see nothing). Repeating the sequence back-to-back turns
parity switches into a random telegraph signal; the Welch-PSD Lorentzian
corner gives the per-direction switching rate (rate = pi x corner, scqat's
``fit_telegraph_psd``), written back as the mode fact ``parity_rate_hz``.

Between shots only the RESONATOR is reset (the ``readout_depletion_s`` wait).
The Parameters deliberately carry NO ``reset_method``: the sequence re-prepares
the equator each shot regardless of the starting pole, so a qubit reset adds
nothing — and the shot cadence IS the telegraph timebase, so the loop must
contain exactly the scheduled operations and a governed depletion wait.

Driver contract (``probe()``):

- per shot play ``y90`` — idle (:meth:`QubitParitySwitch.resolved_idle_ns`,
  per target, ns) — ``x90`` — measure, and record EVERY shot (``shot_idx`` is
  a labeled sweep dim; no averaging exists here by design);
- between shots insert ONLY the depletion wait
  (``scqo.experiments._depletion.depletion_wait_ns``; never ``None`` — this
  experiment refused already) and NO qubit reset of any kind;
- SHOULD set ``self.probe_shot_period_s[target] = <seconds>`` — the exact
  scheduled shot-to-shot period. :meth:`attach_acquisition_coords` attaches it
  to the dataset (falling back to a knob-based estimate) and the estimator
  converts the per-shot switching rate into Hz with it; a period error scales
  the rate linearly.
"""

from __future__ import annotations

from typing import ClassVar

import numpy as np
from pydantic import Field

from .._scqat import per_qubit_results
from ..contract import DatasetContract
from ._capabilities.state_readout import STATE_ALT, StateReadoutParameters, readout_vars
from ._depletion import READOUT_DEPLETION_NS_DESC, depletion_wait_ns
from ._sim import stable_seed
from ..parameters import TargetSelection
from ..result import Outcome, Result
from ..experiment import Experiment
from . import register

#: (dataset variable, readout-channel monitor) for the pinned discrimination
#: reference, |g> first, |e> second — the qubit_thermal_population shape.
_REFERENCE = (("ref_pos_g_i", "pos_g_i"), ("ref_pos_g_q", "pos_g_q"),
              ("ref_pos_e_i", "pos_e_i"), ("ref_pos_e_q", "pos_e_q"))

_MISSING_SPLITTING = (
    "no stored parity splitting for {targets}: the fixed idle time is "
    "1 / (2 x parity_delta_f_hz) and the drive channel has no accepted value. "
    "Run qubit_ramsey with ramsey_model='beat' on {targets} and accept the "
    "parity_delta_f_hz proposal (scqo accept), then re-run this — or pass "
    "idle_time_ns= explicitly."
)

_IDLE_TOO_LONG = (
    "derived idle time {idle_ns:.0f} ns (from parity_delta_f_hz = "
    "{delta_f:.4g} Hz) exceeds max_derived_idle_ns = {ceiling:.0f} ns on "
    "{target}: the stored splitting looks stale or unresolved. Re-run "
    "qubit_ramsey with ramsey_model='beat', or pass idle_time_ns= explicitly."
)

_MISSING_DEPLETION = (
    "no governed depletion wait for {targets}: between shots this experiment "
    "waits ONLY for resonator depletion, and that wait sets the shot cadence "
    "— the telegraph timebase — so it must be a governed value, not a guess. "
    "Run resonator_spectroscopy on {targets} and accept its "
    "readout_depletion_s proposal, or pass readout_depletion_ns= (0 is legal "
    "and means no wait)."
)

_MISSING_REFERENCE = (
    "no stored readout reference for {targets}: discriminating the shot trace "
    "into a 0/1 telegraph needs the |g>/|e> blob centers. Run "
    "single_shot_readout on {targets} and accept the pos_* monitor proposals "
    "(scqo accept), then re-run this — or set use_state_discrimination=True "
    "with a calibrated discriminator."
)

#: nominal per-shot contributions used ONLY by the neutral shot-period
#: fallback estimate when the corresponding knob was never calibrated.
_NOMINAL_READOUT_S = 2e-6
_NOMINAL_PI2_S = 40e-9


class QubitParitySwitchParameters(TargetSelection, StateReadoutParameters):
    """Inputs for a charge-parity switching-rate measurement."""

    num_shots: int = Field(
        100000, gt=99,
        description="Back-to-back shots (each recorded individually). The trace must span "
                    "many switching events: at ~100 Hz parity rate and ~10 us per shot, "
                    "100000 shots is ~1 s of telegraph, ~100 switches per direction.")
    idle_time_ns: float | None = Field(
        None, gt=0,
        description="Fixed free-evolution time between the two pi/2 pulses, ns. None (the "
                    "normal case) derives 1 / (2 x parity_delta_f_hz) from the drive "
                    "channel's stored monitor (an accepted beat qubit_ramsey). Given or "
                    "derived, the value is snapped to the 4 ns cross-backend grid with a "
                    "16 ns floor.")
    max_derived_idle_ns: float = Field(
        20000, gt=0,
        description="Refusal ceiling for the DERIVED idle time: a splitting so small that "
                    "1 / (2 x parity_delta_f_hz) exceeds this is treated as a stale or "
                    "unresolved monitor and refused (never clamped). Pass idle_time_ns= "
                    "explicitly or re-run qubit_ramsey with ramsey_model='beat'.")
    readout_depletion_ns: float | None = Field(
        None, ge=0, description=READOUT_DEPLETION_NS_DESC)


class QubitParitySwitchResult(Result):
    """``fit[qubit]``: ``parity_rate_hz`` (per-direction rate, pi x the PSD
    corner — the value stored as the mode fact), the PSD fit scalars
    (``psd_corner_hz`` / ``psd_amplitude`` / ``psd_white_floor``), the
    diagnostics ``n_transitions`` (readout-error-inflated flip count),
    ``p_odd`` (fraction of consecutive pairs that disagree — 0.5 means the
    shots are independent and NO rate is recoverable) and ``p_excited``, and
    the timing provenance ``shot_period_s`` / ``idle_time_ns`` /
    ``parity_delta_f_hz`` (NaN when the idle was overridden)."""


@register
class QubitParitySwitch(Experiment):
    """Backend-agnostic parity monitor. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "qubit_parity_switch"
    description: ClassVar[str] = (
        "Fixed-sequence charge-parity monitor: y90 - idle - x90 - measure repeated as "
        "num_shots back-to-back single shots, idle = 1 / (2 x parity_delta_f_hz) (the "
        "drive channel's stored beat splitting from a ramsey_model='beat' qubit_ramsey; "
        "+/- pi/2 parity phase), with ONLY the resonator depletion wait between shots — "
        "deliberately NO qubit reset. The discriminated 0/1 trace is a random telegraph "
        "signal; the Lorentzian corner of its Welch PSD gives the per-direction switching "
        "rate (rate = pi x corner), written back as the mode fact parity_rate_hz. "
        "use_state_discrimination returns each shot's FPGA-discriminated 0/1 state "
        "instead of I/Q (per-shot here, not averaged; needs a calibrated discriminator); "
        "the I/Q path REQUIRES an accepted single_shot_readout (the stored pos_* centers "
        "pin the trace discrimination). Also REQUIRES an accepted resonator_spectroscopy: "
        "readout_depletion_s governs the shot cadence, which is the telegraph timebase."
    )
    Parameters: ClassVar[type] = QubitParitySwitchParameters
    Result: ClassVar[type] = QubitParitySwitchResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("shot_idx",), sweep_units=("shot",), variables=("I", "Q"),
        alt_variables=STATE_ALT,
    )
    #: charge parity is transmon physics — matches the parity_rate_hz fact's
    #: catalog scope (fluxonium has no charge-Ramsey beat to derive the idle from).
    target_kinds: ClassVar[tuple[str, ...]] = ("transmon", "flux_transmon")
    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout")
    #: snapshot the stored blob centers into dataset.nc at acquisition time —
    #: the estimator discriminates the trace against exactly these.
    attach_readout_positions: ClassVar[bool] = True

    params: QubitParitySwitchParameters

    # ------------------------------------------------------------- pre-flight
    def _resolve_timing(self) -> dict[str, dict[str, float]]:
        """Per-target resolved idle + depletion, refusing when a governed input
        is missing.

        Same placement rationale as ``qubit_thermal_population``: these gates
        cannot live in ``validate_targets`` (roster-only classmethod), and
        ``define_sweep`` runs before any hardware does, so a raise here becomes
        a structured failed result without touching the instrument.
        """
        resolved: dict[str, dict[str, float]] = {}
        missing_split: list[str] = []
        missing_depletion: list[str] = []
        for target in self.params.targets:
            delta_f = float("nan")
            if self.params.idle_time_ns is not None:
                idle_ns = float(self.params.idle_time_ns)
            else:
                try:
                    value = self.device.channel(target, "drive").parity_delta_f_hz
                except (KeyError, AttributeError):
                    value = None
                if value is None or not np.isfinite(value) or value <= 0:
                    missing_split.append(target)
                    continue
                delta_f = float(value)
                idle_ns = 1e9 / (2.0 * delta_f)
                if idle_ns > self.params.max_derived_idle_ns:
                    raise ValueError(_IDLE_TOO_LONG.format(
                        idle_ns=idle_ns, delta_f=delta_f,
                        ceiling=self.params.max_derived_idle_ns, target=target))
            # cross-backend legal fixed delay: QM plays waits on a 4 ns clock
            # with a 16 ns floor, Qblox on a 1 ns grid — snap to the coarser.
            idle_ns = max(16.0, round(idle_ns / 4.0) * 4.0)
            try:
                depletion_ns = depletion_wait_ns(self, target)
            except KeyError:  # knob never seeded — same remedy as NaN
                depletion_ns = None
            if depletion_ns is None:
                missing_depletion.append(target)
                continue
            resolved[target] = {
                "idle_ns": float(idle_ns),
                "depletion_ns": float(depletion_ns),
                "delta_f_hz": delta_f,
            }
        if missing_split:
            raise ValueError(_MISSING_SPLITTING.format(targets=", ".join(missing_split)))
        if missing_depletion:
            raise ValueError(_MISSING_DEPLETION.format(targets=", ".join(missing_depletion)))
        return resolved

    def _reference_positions(self) -> dict[str, tuple[float, float, float, float]]:
        """The stored ``pos_*`` monitors per target, or raise naming the
        targets that have none (the I/Q discrimination reference)."""
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
        self._resolved = self._resolve_timing()  # refuse before any hardware runs
        if not self.params.use_state_discrimination:
            self._reference_positions()
        return {"shot_idx": np.arange(self.params.num_shots)}

    # ------------------------------------------------------- probe interface
    def resolved_idle_ns(self, target: str) -> float:
        """The fixed idle a probe must play for ``target`` (grid-snapped ns;
        ``define_sweep`` resolved it)."""
        return self._resolved[target]["idle_ns"]

    def _estimated_shot_period_s(self, target: str) -> float:
        """Knob-based estimate of the shot-to-shot period: the fallback when a
        probe did not report the exactly-scheduled value, and the simulated
        backend's timebase. Real probes should override via
        ``probe_shot_period_s`` — this misses vendor scheduling overhead."""
        timing = self._resolved[target]
        period = (timing["idle_ns"] + timing["depletion_ns"]) * 1e-9
        try:
            period += float(self.device.channel(target, "readout").readout_duration_s)
        except (KeyError, AttributeError):
            period += _NOMINAL_READOUT_S
        try:
            pulse = float(self.device.channel(target, "drive").pi_duration_s)
        except (KeyError, AttributeError):
            pulse = _NOMINAL_PI2_S
        return period + 2.0 * pulse

    def attach_acquisition_coords(self) -> None:
        """Attach the per-target timing the analysis needs to ``dataset.nc``:
        ``shot_period_s`` (probe-reported, else the knob-based estimate), the
        resolved ``idle_time_ns`` and the ``parity_delta_f_hz`` the idle came
        from (NaN when overridden) — so a saved run replays offline with the
        timebase it was taken at."""
        assert self.dataset is not None
        probe_periods = getattr(self, "probe_shot_period_s", None) or {}
        targets = [str(t) for t in self.dataset["target"].values]
        periods = [float(probe_periods.get(t, self._estimated_shot_period_s(t)))
                   for t in targets]
        idles = [self._resolved[t]["idle_ns"] for t in targets]
        deltas = [self._resolved[t]["delta_f_hz"] for t in targets]
        self.dataset["shot_period_s"] = ("target", np.asarray(periods, dtype=float))
        self.dataset["idle_time_ns"] = ("target", np.asarray(idles, dtype=float))
        self.dataset["parity_delta_f_hz"] = ("target", np.asarray(deltas, dtype=float))

    # -------------------------------------------------------------- offline
    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        n_shots = coords["shot_idx"].size
        targets = self.params.targets
        rng = np.random.default_rng(stable_seed("qubit_parity_switch", *targets))
        use_state = self.params.use_state_discrimination
        reference = None if use_state else self._reference_positions()
        i_data = np.empty((len(targets), n_shots))
        q_data = np.empty_like(i_data)
        state = np.empty_like(i_data)
        for k, target in enumerate(targets):
            # Markov telegraph: per-shot flip probability = rate x shot period.
            p_flip = rng.uniform(0.002, 0.01)
            trace = np.cumsum(rng.random(n_shots) < p_flip) % 2
            if use_state:
                errors = rng.random(n_shots) < 0.02  # readout errors: floor, not knee
                state[k] = (trace ^ errors).astype(float)
            else:
                g_i, g_q, e_i, e_q = reference[target]
                i_data[k] = np.where(trace, e_i, g_i) + rng.normal(0, 1.0, n_shots)
                q_data[k] = np.where(trace, e_q, g_q) + rng.normal(0, 1.0, n_shots)
        return readout_vars(use_state, state, i_data, q_data)

    # ------------------------------------------------------------- analysis
    def estimate(self) -> QubitParitySwitchResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.parity_switch import ParitySwitchEstimator

        # The estimator self-serves everything from the dataset snapshot: the
        # 0/1 trace ('state', or I/Q discriminated against the attached
        # ref_pos_* centers) and the timebase (the attached shot_period_s) —
        # so a saved run re-analyzes offline unchanged.
        prepared = self.dataset.transpose("target", "shot_idx")
        results = per_qubit_results(prepared, ParitySwitchEstimator(),
                                    artifact_dir=self.artifact_dir)

        nan = float("nan")
        result = QubitParitySwitchResult()
        for qubit in self.params.targets:
            r = results[qubit]
            rate = float(r.get("parity_rate_hz", nan))
            dt = float(r.get("dt_s", nan))
            p_excited = float(r.get("p_excited", nan))
            fit = {
                "parity_rate_hz": rate,
                "psd_corner_hz": float(r.get("psd_corner_hz", nan)),
                "psd_amplitude": float(r.get("psd_amplitude", nan)),
                "psd_white_floor": float(r.get("psd_white_floor", nan)),
                "n_transitions": int(r.get("n_transitions", 0)),
                # the fraction of consecutive pairs that DISAGREE. It saturates
                # at 0.5, where the shots are independent and no rate exists;
                # scqat refuses above 0.4, so this is the number that explains a
                # FAILED run whose fit otherwise looked healthy.
                "p_odd": float(r.get("p_odd", nan)),
                "p_excited": p_excited,
                # (state_source stays in the scqat metadata artifact — Result.fit
                # is a float-only surface)
                # timing provenance, from the acquisition-time snapshot
                "shot_period_s": dt,
                "idle_time_ns": self._acquired("idle_time_ns", qubit),
                "parity_delta_f_hz": self._acquired("parity_delta_f_hz", qubit),
            }
            if "outlier_probability" in r:
                fit["outlier_probability"] = float(r["outlier_probability"])
            result.fit[qubit] = fit
            # A trustworthy rate: the knee fit converged inside the spectral
            # window AND the consecutive shots were actually correlated (scqat
            # refuses p_odd > 0.4 — see its telegraph_psd docstring), and the
            # trace toggled (a pinned occupancy means no telegraph — wrong idle
            # time, wrong centers, or no switching resolved at this cadence).
            ok = (bool(r.get("success")) and np.isfinite(rate)
                  and 0.0 < rate < 0.5 / dt and 0.02 < p_excited < 0.98)
            result.outcomes[qubit] = Outcome.SUCCESSFUL if ok else Outcome.FAILED
        return result

    def _acquired(self, name: str, qubit: str) -> float:
        """A per-target value attached to the dataset at acquisition time."""
        assert self.dataset is not None
        if name not in self.dataset:
            return float("nan")
        return float(self.dataset[name].sel(target=qubit).values)

    def update(self) -> None:
        # parity_rate_hz is a sample FACT (physical.json): quasiparticle
        # tunneling is chip + environment physics, no instrument setting
        # realizes it — the same placement as t1_s and n_th.
        if self.result is None:
            return
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is Outcome.SUCCESSFUL:
                self.device.component(qubit).parity_rate_hz = fit["parity_rate_hz"]

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
