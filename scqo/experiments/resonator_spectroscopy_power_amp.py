"""Resonator spectroscopy vs ABSOLUTE readout power via the AMPLITUDE sweep — the
FAST punchout (backend-free half; named ``_power_amp`` because the power is swept by
changing the digital amplitude inside the FPGA loop — one hardware program, unlike
the chain-stepped ``_power_chain``).

Same inputs and outputs as the sibling: an absolute-dBm window
(``min_power_dbm``/``max_power_dbm``) in, ``readout_power_dbm`` + ``readout_freq``
proposals out. Only the mechanism differs. Realization (the lab's proven qualibrate
pattern): ``run()`` solves the output chain for the WINDOW TOP once
(``readout_power_dbm = max_power_dbm`` — the setter parks the digital amplitude at
~0.5 of full scale), sweeps the amplitude PREFACTOR down from 1 in one program, and
reverts the chain afterwards. Every qubit therefore hits the same absolute window
exactly, whatever its standing power. Cost of the speed: the low end of the window
runs at a tiny DAC amplitude (SNR degrades toward the bottom), where ``_chain``
re-solves the chain per point and keeps amp ~0.5 everywhere.

Audit trail (same discipline as ``_chain``): only the BOUNDARY writes are recorded —
set to ``max_power_dbm`` at the start, revert at the end (2 ChangeRecords + coupled
echoes per qubit, through the recorded device view). The swept prefactors are
acquisition detail, tracked in the figure's amp/chain subplot. Nothing survives the
run unless a suggestion is accepted.

Acquisition loop order (both backends): amplitude (outer) -> averages (middle) ->
frequency (INNER = the fastest loop) — each power point repeats a fast frequency
sweep ``num_averages`` times, so the resonator only jumps power between (slow) outer
steps. The dataset axis order therefore is ``(power_dbm, detuning_hz)`` (power outer,
detuning fastest); the scqat estimator transposes by name, so this is invisible to
analysis. The axis is UNIFORM in dBm on both backends (QM: geometric prefactors via
``for_each_``; Qblox: the amplitude axis is Python-unrolled — one block per power
point — since scheduler loop domains are linear-only).
``resonator_relaxation_time_ns`` controls the between-readout ring-down wait
(None = the backend's configured value).

Absolute-scale honesty: QM's dBm axis is exact at the port; Qblox derives from the
nominal +5 dBm full scale (±a few dB). A per-setup photon-number anchor (AC-Stark)
is the Phase-3 refinement.
"""

from __future__ import annotations

import math
from typing import ClassVar

import numpy as np
from pydantic import Field, model_validator

from .._scqat import per_qubit_results
from ._sim import stable_seed
from ..contract import DatasetContract
from ..experiment import Experiment
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result


class ResonatorSpectroscopyPowerAmpParameters(TargetSelection, AveragingParameters):
    """Inputs for the fast amplitude-sweep absolute-power punchout scan."""

    frequency_span_hz: float = Field(20e6, gt=0, description="Total detuning span around the current readout_freq.")
    num_freq_points: int = Field(101, gt=1, description="Number of frequency points.")
    max_power_dbm: float = Field(
        -20.0,
        le=10.0,
        description="Highest absolute readout power (dBm at the instrument port); the chain is "
        "solved for THIS power once (recorded, reverted after) and the amplitude sweep descends "
        "from it. QM caps at +10 dBm; Qblox above ~-1 dBm needs amplitude > 0.5.",
    )
    min_power_dbm: float = Field(-50.0, description="Lowest absolute readout power (dBm).")
    num_power_points: int = Field(
        21, gt=1, description="Number of power points (one hardware program sweeps them all; "
        "the optimal-power derivative uses a ~10-point smoothing window, so keep this "
        "comfortably above 10)."
    )
    resonator_relaxation_time_ns: float | None = Field(
        None, gt=0,
        description="Resonator relaxation (depletion) wait between readouts, ns. None = the "
        "backend's configured value (QM: each resonator's depletion_time; Qblox: the probe's "
        "built-in idle).",
    )

    @model_validator(mode="after")
    def _window_ordered(self) -> "ResonatorSpectroscopyPowerAmpParameters":
        if not self.min_power_dbm < self.max_power_dbm:
            raise ValueError(
                f"min_power_dbm ({self.min_power_dbm}) must be below max_power_dbm ({self.max_power_dbm})"
            )
        return self


class ResonatorSpectroscopyPowerAmpResult(Result):
    """``fit[qubit]``: ``readout_power_dbm`` (new), ``readout_freq`` (new),
    ``optimal_power_dbm``, ``frequency_shift_hz``, plus the old values."""


class ResonatorSpectroscopyPowerAmp(Experiment):
    """Backend-agnostic FAST amplitude-sweep punchout. ``probe()`` is supplied by a
    driver and sweeps the amplitude prefactor down from the window top (``run()``
    already solved the chain for ``max_power_dbm``)."""

    name: ClassVar[str] = "resonator_spectroscopy_power_amp"
    description: ClassVar[str] = (
        "Fast punchout: solves the output chain for max_power_dbm once (recorded boundary "
        "write, reverted after), then sweeps the digital readout AMPLITUDE down from it in ONE "
        "hardware program. Same absolute-dBm window and proposals (readout_power_dbm + "
        "readout_freq) as resonator_spectroscopy_power_chain, minutes faster; SNR is best near "
        "the top of the window and degrades toward the bottom (the chain-stepped sibling keeps "
        "amp ~0.5 at every point). Use for quick scans; use _chain for per-point-optimal SNR."
    )
    Parameters: ClassVar[type] = ResonatorSpectroscopyPowerAmpParameters
    Result: ClassVar[type] = ResonatorSpectroscopyPowerAmpResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("power_dbm", "detuning_hz"), sweep_units=("dBm", "Hz"), variables=("I", "Q")
    )
    required_operations: ClassVar[tuple[str, ...]] = ("readout",)

    params: ResonatorSpectroscopyPowerAmpParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        # Order (power_dbm, detuning_hz) = outer -> fastest: the frequency loop is
        # innermost on hardware, so the acquired axis order is (power, detuning).
        span = self.params.frequency_span_hz
        return {
            "power_dbm": np.linspace(
                self.params.min_power_dbm, self.params.max_power_dbm, self.params.num_power_points
            ),
            "detuning_hz": np.linspace(-span / 2, span / 2, self.params.num_freq_points),
        }

    def run(self) -> Result:
        """Boundary-recorded set-top -> ONE 2D acquisition -> revert.

        The boundary writes go through ``self.device`` (the Session's
        RecordingDevice): 2 ChangeRecords + coupled echoes per qubit — the same
        audit discipline as the chain-stepped sibling. The swept prefactors are
        unrecorded acquisition detail, captured once at the top into
        ``self._top_context`` / ``self._top_amps`` for the dataset/figure.
        """
        self.sweep_axes = self.define_sweep()
        top = float(self.params.max_power_dbm)
        targets = list(self.params.targets)
        views = {q: self.device.component(q) for q in targets}

        previous: dict[str, float] = {}
        for q, view in views.items():
            try:
                before = view.readout_power_dbm
            except (KeyError, ValueError):
                before = None
            if before is None or not math.isfinite(float(before)):
                raise RuntimeError(
                    f"{q}: readout_power_dbm is unknown (unconfigured output chain / zero "
                    f"readout amplitude) — the revert target would be undefined; set "
                    f"readout_power_dbm (or fix readout_amp) first"
                )
            previous[q] = float(before)
        for view in views.values():
            view.readout_power_dbm = top  # recorded boundary write (+ coupled echo)

        # Figure provenance, captured once at the top (the chain stays put during
        # the sweep). Never fail a measurement over it.
        try:
            self._top_context = self.backend.power_context(targets) or {}
        except Exception:  # noqa: BLE001 - provenance only
            self._top_context = {}
        self._top_amps: dict[str, float] = {}
        for q in targets:
            try:
                self._top_amps[q] = float(self.backend.device.component(q).readout_amp)
            except Exception:  # noqa: BLE001 - provenance only
                self._top_amps[q] = float("nan")

        try:
            self.dataset = self.backend.acquire(self)
        finally:
            revert_errors = []
            for q, view in views.items():  # recorded boundary revert (+ coupled echo)
                try:
                    view.readout_power_dbm = previous[q]
                except Exception as err:  # noqa: BLE001 - collected and re-raised below
                    revert_errors.append(f"{q}: {type(err).__name__}: {err}")
            if revert_errors:
                raise RuntimeError(
                    "readout chain revert failed for " + "; ".join(revert_errors)
                )
        self.Contract.validate(self.dataset)
        self.result = self.estimate()
        return self.result

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        detuning = coords["detuning_hz"]
        power = coords["power_dbm"]
        targets = self.params.targets
        top = float(self.params.max_power_dbm)
        rng = np.random.default_rng(stable_seed("resonator_spectroscopy_power_amp", *targets))
        span = float(detuning[-1] - detuning[0])
        kappa = span / 15
        i_data = np.empty((len(targets), power.size, detuning.size))
        q_data = np.empty_like(i_data)
        for k in range(len(targets)):
            dressed = rng.uniform(-0.1, 0.1) * span  # dispersive dip position (low power)
            knee_dbm = top - rng.uniform(8.0, 12.0)  # punchout onset
            for j, p in enumerate(power):
                # above the knee the dip walks DOWN toward the bare cavity (the
                # estimator's derivative threshold is negative) and washes out
                walk = max(0.0, p - knee_dbm)
                center = dressed - walk * 0.8e6  # ~-0.8 MHz per dB past the knee
                depth = 0.8 / (1.0 + walk / 4.0)
                magnitude = 1.0 - depth / (1.0 + ((detuning - center) / kappa) ** 2)
                # like the real instrument, the measured |IQ| scales with the
                # delivered amplitude — the prefactor relative to the window top
                amp = 10.0 ** ((p - top) / 20.0)
                noise = 0.01
                i_data[k, j, :] = amp * (magnitude + rng.normal(0, noise, detuning.size))
                q_data[k, j, :] = amp * rng.normal(0, noise, detuning.size)
        return {"I": i_data, "Q": q_data}

    def estimate(self) -> ResonatorSpectroscopyPowerAmpResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.resonator_spectroscopy_power import ResonatorSpectroscopyPowerEstimator

        # scqat's contract: coords `power` + `detuning`, vars I/Q; the estimator's
        # optimal-power logic is derivative-based and its `optimal_power` output is
        # already an absolute dBm value on this axis.
        targets = list(self.dataset["target"].values)
        old_freqs = {q: float(self.device.component(q).readout_freq) for q in targets}
        # estimate() runs after the revert, so this reads the standing (pre-run) chain.
        old_power = {q: float(self.device.component(q).readout_power_dbm) for q in targets}
        prepared = self.dataset.rename({"detuning_hz": "detuning", "power_dbm": "power"})
        prepared = prepared.transpose("target", "power", "detuning")
        detuning = prepared["detuning"].values
        full_freq = np.array([detuning + old_freqs[q] for q in targets])
        prepared = prepared.assign_coords(full_freq=(("target", "detuning"), full_freq))
        prepared = self._attach_sweep_provenance(prepared, targets)

        results = per_qubit_results(
            prepared, ResonatorSpectroscopyPowerEstimator(), artifact_dir=self.artifact_dir
        )

        result = ResonatorSpectroscopyPowerAmpResult()
        for qubit in self.params.targets:
            r = results[qubit]
            optimal_dbm = float(r["optimal_power"])
            shift = float(r["frequency_shift"])
            result.fit[qubit] = {
                "readout_power_dbm": optimal_dbm,
                "optimal_power_dbm": optimal_dbm,
                "readout_freq": old_freqs[qubit] + shift,
                "frequency_shift_hz": shift,
                "old_readout_power_dbm": old_power[qubit],
                "old_readout_freq": old_freqs[qubit],
            }
            ok = bool(r["optimal_success"]) and np.isfinite(optimal_dbm) and np.isfinite(shift)
            result.outcomes[qubit] = Outcome.SUCCESSFUL if ok else Outcome.FAILED
        return result

    def _attach_sweep_provenance(self, prepared, targets: list) -> "xr.Dataset":  # noqa: F821
        """Amp/chain provenance in the SAME (qubit, power) form as the _chain
        punchout — the two figures share one format. Labels are always attached;
        the amp/chain subplot data only when the backend reported the chain
        (real backends; simulated -> plain map, like _chain)."""
        n_q = len(targets)
        prepared = prepared.assign_coords(
            power_axis_kind=("target", ["absolute dBm"] * n_q),
            mode_label=("target", ["amplitude sweep (fast)"] * n_q),
        )
        ctx = getattr(self, "_top_context", {}) or {}
        if not any(ctx.get(q) for q in targets):
            return prepared
        top = float(self.params.max_power_dbm)
        power = prepared.coords["power"].values.astype(float)
        top_amps = getattr(self, "_top_amps", {}) or {}
        n_power = power.size
        amp = np.full((n_q, n_power), np.nan)
        setting = np.full((n_q, n_power), np.nan)
        names = []
        for k, q in enumerate(targets):
            qctx = ctx.get(q) or {}
            a_top = float(top_amps.get(q, float("nan")))
            if not math.isfinite(a_top):  # fall back to the context's own capture
                a_top = float(qctx.get("pulse_amp", qctx.get("readout_amplitude", float("nan"))))
            amp[k, :] = a_top * 10.0 ** ((power - top) / 20.0)
            name = ""
            if "output_att_db" in qctx:
                setting[k, :] = float(qctx["output_att_db"])
                name = "output_att (dB)"
            elif "full_scale_power_dbm" in qctx:
                setting[k, :] = float(qctx["full_scale_power_dbm"])
                name = "full_scale_power_dbm (dBm)"
            names.append(name)
        return prepared.assign_coords(
            digital_amp=(("target", "power"), amp),
            chain_setting=(("target", "power"), setting),
            chain_name=("target", names),
        )

    def update(self) -> None:
        if self.result is None:
            return
        for qubit, fit in self.result.fit.items():
            if self.result.outcomes[qubit] is Outcome.SUCCESSFUL:
                view = self.device.component(qubit)
                view.readout_power_dbm = fit["readout_power_dbm"]
                view.readout_freq = fit["readout_freq"]
