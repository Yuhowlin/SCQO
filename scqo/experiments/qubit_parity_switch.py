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

STRETCHING THE IDLE (``idle_multiple``). The idle may instead be taken as
``N / (2 x parity_delta_f_hz)`` for ODD N: the parity phase is then
``+/- N pi/2`` and the readout still reads sin of it at full contrast. EVEN N
gives exactly zero (both parities land on the same pole) — allowed, but warned
about, and the fit then refuses it for lack of spectral contrast.

Why bother: a run is usually limited by ACQUISITION BINS rather than by wall
clock, so a longer shot buys more recorded time out of the same bin budget and
therefore a lower spectral edge. On chipA (30.4 us shots) the Qblox 3e6-bin
ceiling caps a run at 91 s (f_min 0.088 Hz); N=3 stretches the shot to ~48.5 us,
so the same bins buy 145 s (f_min 0.055 Hz). It also suppresses the
readout-error bias, which scales as ``1 + 2 eps / (rate x shot_period)``. The
ceiling is T2* — contrast decays as ``exp(-idle / T2*)`` — and that is NOT
checked automatically, because ``t2_star_s`` is a mode FACT and mode facts are
unreachable at ``define_sweep`` time.

Between shots only the RESONATOR is reset (the ``readout_depletion_s`` wait).
The Parameters deliberately carry NO ``reset_method``, and the absence is
load-bearing rather than an optimization — see below.

THE READOUT IS NOT THE PARITY; ITS CONSECUTIVE DIFFERENCE IS.
The sequence is a unitary, so it maps antipodal Bloch vectors to antipodal
ones: |0> and |1> can never produce the same outcome. Each shot therefore
starts wherever the previous measurement projected it (no reset, QND readout)
and its outcome INVERTS with that pole:

    s[i] = s[i-1] XOR parity[i]

so the readout trace is the RUNNING XOR of the parity, and the consecutive-pair
difference ``s[i] XOR s[i+1]`` IS the parity telegraph. scqat fits that pair
series, never the raw readout — fitting the readout means fitting an integrated
telegraph and the rate is meaningless (that was the v0.19.0 defect). The raw
readout's spectrum is still plotted, unfitted, as a diagnostic.

This is also why a qubit reset would BREAK the experiment rather than merely
cost time: resetting to |0> every shot severs the XOR chain that carries the
parity. And the shot cadence IS the telegraph timebase, so the loop must
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

import math
import warnings
from typing import ClassVar, Literal

import numpy as np
from pydantic import Field

from .._scqat import per_qubit_results
from ..contract import DatasetContract
from ._capabilities.state_readout import SHOT_STATE_ALT, StateReadoutParameters, shot_state_vars
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

_MULTIDLE_WARNING = (
    "idle_multiple={n} is EVEN, so this run carries no parity signal: the "
    "parity phase is +/- {n} x pi/2 and the readout goes as sin of it, which is "
    "exactly zero for even multiples — both parities land on the same pole. Use "
    "{lower} or {higher} instead. Running anyway as requested; the fit will "
    "refuse it for lack of spectral contrast."
)

_MULTIPLIED_IDLE_TOO_LONG = (
    "idle_multiple={n} stretches {target}'s idle to {idle_ns:.0f} ns (base "
    "{base_ns:.0f} ns from parity_delta_f_hz = {delta_f:.4g} Hz), over "
    "max_derived_idle_ns = {ceiling:.0f} ns. The base itself is fine, so this "
    "is the multiple: raise max_derived_idle_ns to match, or lower "
    "idle_multiple. Keep the idle well under T2* either way — the fringe "
    "contrast decays as exp(-idle / T2*)."
)

_TOO_MANY_SHOTS = (
    "record_time_s = {want:g} s needs {n} shots on {target} (its estimated shot "
    "period is {period_us:.3g} us), over max_num_shots = {ceiling}. Shorten "
    "record_time_s, raise max_num_shots consciously (instruments have their own "
    "limits — Qblox refuses past 3e6 acquisition bins), or pass num_shots "
    "explicitly to bypass this ceiling."
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

    record_time_s: float = Field(
        30.0, gt=0,
        description="How long to record the telegraph, seconds. This — not the shot count — "
                    "is what sets the measurement's reach: the parity spectrum's lowest "
                    "frequency bin is 8 / record_time_s, so a rate whose PSD corner "
                    "(rate / pi) sits near that bin is fitted from only a handful of "
                    "points. Aim for record_time_s > 40 / corner, i.e. > 130 / rate. "
                    "Converted to shots with the target's ESTIMATED shot period (idle + "
                    "readout + depletion + the two pi/2 pulses), so the achieved duration "
                    "differs slightly from the request; both are recorded.")
    num_shots: int | None = Field(
        None, gt=99,
        description="Explicit shot count, overriding record_time_s. None (the normal case) "
                    "derives it. Given, it wins outright and BYPASSES max_num_shots — the "
                    "ceiling guards the derived value, exactly as max_derived_idle_ns "
                    "guards the derived idle. Use it to reproduce an earlier run's exact "
                    "length, or for a quick short test.")
    max_num_shots: int = Field(
        2_000_000, gt=99,
        description="Refusal ceiling for the DERIVED shot count (never a clamp). A long "
                    "record_time_s on a slow-cadence qubit can ask for far more shots than "
                    "an instrument will hold, so this refuses early and by name rather "
                    "than deep in a vendor error. Deliberately under the Qblox 3e6 "
                    "acquisition-bin limit. Raise it consciously, or pass num_shots.")
    idle_time_ns: float | None = Field(
        None, gt=0,
        description="Fixed free-evolution time between the two pi/2 pulses, ns. None (the "
                    "normal case) derives idle_multiple / (2 x parity_delta_f_hz) from the "
                    "drive channel's stored monitor (an accepted beat qubit_ramsey). Given "
                    "explicitly it wins outright and is NOT multiplied by idle_multiple. "
                    "Given or derived, the value is snapped to the 4 ns cross-backend grid "
                    "with a 16 ns floor.")
    idle_multiple: int = Field(
        1, gt=0,
        description="Stretch the DERIVED idle to N / (2 x parity_delta_f_hz). ONLY ODD N "
                    "CARRIES SIGNAL: the parity phase is +/- N x pi/2 and the readout goes "
                    "as sin of it, so odd N gives full contrast and EVEN N gives exactly "
                    "zero (both parities land on the same pole) — even values are allowed "
                    "but warn. Raise it when runs are acquisition-BIN limited rather than "
                    "time limited: a longer shot means the same bin budget covers more "
                    "wall-clock, which lowers the spectrum's reach, and it also suppresses "
                    "the readout-error bias (~1 + 2 x eps / (rate x shot_period)). The "
                    "limit is T2*: contrast decays as exp(-idle / T2*), which is NOT "
                    "checked here because t2_star_s is a mode fact and unreachable "
                    "pre-run. Ignored when idle_time_ns is given.")
    max_derived_idle_ns: float = Field(
        20000, gt=0,
        description="Refusal ceiling for the DERIVED idle time: a splitting so small that "
                    "1 / (2 x parity_delta_f_hz) exceeds this is treated as a stale or "
                    "unresolved monitor and refused (never clamped). Pass idle_time_ns= "
                    "explicitly or re-run qubit_ramsey with ramsey_model='beat'.")
    readout_depletion_ns: float | None = Field(
        None, ge=0, description=READOUT_DEPLETION_NS_DESC)
    psd_model: Literal["constrained", "independent"] = Field(
        "constrained",
        description="Which spectrum model the estimator fits the parity PSD with. "
                    "'constrained' (default) = the reference RTS model "
                    "F^2 x 4G/((2G)^2+(2 pi f)^2) + (1-F^2) x dt with a SINGLE "
                    "shared mapping fidelity F and dt FIXED to the scheduled shot "
                    "period (a 2-parameter fit {F, G}); F is read out directly. "
                    "'independent' = Lorentzian + FREE constant floor (3 params), "
                    "which yields the fidelity twice — from the plateau and the "
                    "floor — plus their ratio as a model-consistency check. The "
                    "selected model's rate is what writes back to parity_rate_hz.")


class QubitParitySwitchResult(Result):
    """``fit[qubit]``: ``parity_rate_hz`` (per-direction rate, pi x the PSD
    corner of the PARITY series — the value stored as the mode fact), the PSD
    fit scalars (``psd_corner_hz`` / ``psd_amplitude`` / ``psd_white_floor``),
    the diagnostics ``n_parity_switches``, ``p_switch`` (fraction of
    consecutive PARITY samples that differ — 0.5 means they are independent and
    NO rate is recoverable) and ``p_parity_odd`` (how often the chip sits in the
    odd parity; ~0.5 is HEALTHY and carries no rate information), the spectrum
    fit residual ``psd_fit_residual``, the sequence mapping fidelity
    ``mapping_fidelity`` (the single fitted F under the 'constrained' default;
    the plateau estimate under 'independent', where ``mapping_fidelity_floor``
    and ``mapping_fidelity_ratio`` add the floor estimate and the
    model-consistency check — both NaN under 'constrained'), and the timing
    provenance ``shot_period_s`` / ``idle_time_ns`` / ``idle_multiple`` /
    ``parity_delta_f_hz`` (NaN when the idle was overridden)."""


@register
class QubitParitySwitch(Experiment):
    """Backend-agnostic parity monitor. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "qubit_parity_switch"
    description: ClassVar[str] = (
        "Fixed-sequence charge-parity monitor: y90 - idle - x90 - measure repeated as "
        "back-to-back single shots for record_time_s (the shot count is derived from it, "
        "since the spectrum's lowest frequency is 8 / record_time_s and THAT is what sets "
        "how slow a rate is measurable), idle = 1 / (2 x parity_delta_f_hz) (the "
        "drive channel's stored beat splitting from a ramsey_model='beat' qubit_ramsey; "
        "+/- pi/2 parity phase), with ONLY the resonator depletion wait between shots — "
        "deliberately NO qubit reset, which is what lets the parity accumulate. Because "
        "the sequence inverts with the pole the previous shot left behind, the readout is "
        "the RUNNING XOR of the parity and the consecutive-pair difference IS the parity "
        "telegraph; the Lorentzian corner of THAT series' Welch PSD gives the "
        "per-direction switching rate (rate = pi x corner), written back as the mode fact "
        "parity_rate_hz. NOTE the readout must be good: one bad shot corrupts two parity "
        "samples, so the rate inflates as 1 + 2 x readout_error / (rate x shot_period). "
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
        alt_variables=SHOT_STATE_ALT,
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
                base_ns = 1e9 / (2.0 * delta_f)
                idle_ns = base_ns * self.params.idle_multiple
                if idle_ns > self.params.max_derived_idle_ns:
                    # Two different faults reach here and they have different
                    # remedies, so say which one this is: a base already over
                    # the ceiling means the stored splitting is stale, whereas
                    # only the MULTIPLIED value being over is a deliberate
                    # choice that just needs headroom.
                    template = (_IDLE_TOO_LONG
                                if base_ns > self.params.max_derived_idle_ns
                                else _MULTIPLIED_IDLE_TOO_LONG)
                    raise ValueError(template.format(
                        idle_ns=idle_ns, base_ns=base_ns, delta_f=delta_f,
                        n=self.params.idle_multiple,
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

    def _resolve_num_shots(self) -> int:
        """The shot count, from ``record_time_s`` unless overridden.

        Uses the ESTIMATED shot period: ``shot_idx`` has to be sized here, and a
        probe's exactly-scheduled period is not known until ``probe()`` runs.
        The achieved duration is recorded alongside the request so the
        difference is visible rather than silent.

        ``shot_idx`` is ONE axis shared by every target, so the count is the MAX
        over targets: each target then records at LEAST ``record_time_s``, and a
        target whose shot period is shorter simply records for longer. Rounding
        down instead would leave some target short of the reach that was asked
        for, which is the failure that matters.
        """
        if self.params.num_shots is not None:
            return int(self.params.num_shots)

        want = float(self.params.record_time_s)
        per_target = {t: math.ceil(want / self._estimated_shot_period_s(t))
                      for t in self.params.targets}
        n = max(per_target.values())
        if n > self.params.max_num_shots:
            slowest = min(per_target, key=lambda t: per_target[t])
            raise ValueError(_TOO_MANY_SHOTS.format(
                n=n, want=want, ceiling=self.params.max_num_shots,
                period_us=self._estimated_shot_period_s(slowest) * 1e6,
                target=max(per_target, key=lambda t: per_target[t])))
        return int(n)

    def define_sweep(self) -> dict[str, np.ndarray]:
        n = self.params.idle_multiple
        # only meaningful on the derived path — an explicit idle_time_ns is not
        # multiplied, so an even multiple there is simply unused, not a defect.
        if self.params.idle_time_ns is None and n % 2 == 0:
            warnings.warn(_MULTIDLE_WARNING.format(n=n, lower=n - 1, higher=n + 1),
                          stacklevel=2)
        self._resolved = self._resolve_timing()  # refuse before any hardware runs
        if not self.params.use_state_discrimination:
            self._reference_positions()
        self._num_shots = self._resolve_num_shots()
        return {"shot_idx": np.arange(self._num_shots)}

    # ------------------------------------------------------- probe interface
    def resolved_num_shots(self) -> int:
        """The shot count a probe must record (``define_sweep`` resolved it
        from ``record_time_s``, or took the explicit override)."""
        return self._num_shots

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
        # what was ACHIEVED, from the period the probe really scheduled — the
        # count came from an ESTIMATE, so this is not the same as the request.
        n = float(self.dataset.sizes["shot_idx"])
        self.dataset["record_time_s"] = (
            "target", np.asarray([n * p for p in periods], dtype=float))

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
            # The PARITY is the telegraph: per-shot switch probability =
            # rate x shot period.
            p_flip = rng.uniform(0.002, 0.01)
            parity = np.cumsum(rng.random(n_shots) < p_flip) % 2
            # ... and the READOUT is its running XOR, because the sequence
            # inverts with the pole the previous shot left behind (class
            # docstring). Planting the telegraph straight into the readout —
            # as this simulator did before — models the wrong physics and
            # would let a wrong estimator pass its own offline test.
            trace = np.cumsum(parity) % 2
            if use_state:
                # Readout error is deliberately TINY here. It is not benign on
                # this experiment: one bad shot flips two adjacent parity
                # samples, and the rate inflates as 1 + 2*eps/(p_flip). At the
                # old 2% it swamped the planted rate entirely.
                errors = rng.random(n_shots) < 1e-4
                state[k] = (trace ^ errors).astype(float)
            else:
                g_i, g_q, e_i, e_q = reference[target]
                # blob sigma 0.4 against a separation of 4 -> ~1e-7 error, for
                # the same reason
                i_data[k] = np.where(trace, e_i, g_i) + rng.normal(0, 0.4, n_shots)
                q_data[k] = np.where(trace, e_q, g_q) + rng.normal(0, 0.4, n_shots)
        return shot_state_vars(use_state, state, i_data, q_data)

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
                                    artifact_dir=self.artifact_dir,
                                    model=self.params.psd_model)

        nan = float("nan")
        result = QubitParitySwitchResult()
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
                # the PARITY's own switch count and fraction (not the readout's)
                "n_parity_switches": int(r.get("n_transitions", 0)),
                # p_switch saturates at 0.5, where consecutive parity samples
                # are independent and no rate exists. REPORTED ONLY — it does
                # not gate anything: any shot-to-shot noise drives it toward 0.5
                # whether or not a knee exists, and a clean chipA fit sat at
                # 0.294. The gate is psd_contrast below.
                "p_switch": float(r.get("p_switch", nan)),
                # how often the chip sits in the odd parity. ~0.5 is HEALTHY and
                # carries no rate information — do not confuse it with p_switch.
                "p_parity_odd": p_parity_odd,
                # (state_source stays in the scqat metadata artifact — Result.fit
                # is a float-only surface)
                # what the fit could see: the spectrum's low edge and how much
                # headroom the corner had above it. A small margin is the
                # readable symptom of "record for longer".
                "psd_freq_min_hz": float(r.get("psd_freq_min_hz", nan)),
                "psd_contrast": float(r.get("psd_contrast", nan)),
                "corner_margin_low": float(r.get("corner_margin_low", nan)),
                # log-space RMS residual of the spectrum fit — the constrained
                # model's own fit-quality number, since it has no plateau/floor
                # ratio. (Which model was fitted, psd_model, is a STRING and so
                # stays in the scqat metadata artifact + the run parameters, not
                # here — Result.fit is a float-only surface, like state_source.)
                "psd_fit_residual": float(r.get("psd_fit_residual", nan)),
                # the sequence mapping fidelity F. Under psd_model='constrained'
                # (default) this is the single fitted F and the floor/ratio below
                # are NaN (the model couples plateau and floor by construction).
                # Under psd_model='independent' F is read TWICE — plateau
                # (mapping_fidelity) and floor (mapping_fidelity_floor) — and
                # their RATIO is the diagnostic: near 1 the Lorentzian+white-floor
                # model fits, well below 1 it does not (the readable symptom of
                # the correlated readout error that also inflates the rate, which
                # psd_contrast is blind to). Reported, never gated.
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
            }
            if "outlier_probability" in r:
                fit["outlier_probability"] = float(r["outlier_probability"])
            result.fit[qubit] = fit
            # A trustworthy rate: the knee fit converged inside the spectral
            # window AND there was a real knee rather than a curve through noise
            # (scqat gates on the plateau-to-floor contrast — see its
            # telegraph_psd docstring), and the parity toggled (a pinned
            # occupancy means no telegraph — wrong idle time, wrong centers, or
            # no switching resolved at this cadence).
            ok = (bool(r.get("success")) and np.isfinite(rate)
                  and 0.0 < rate < 0.5 / dt and 0.02 < p_parity_odd < 0.98)
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
