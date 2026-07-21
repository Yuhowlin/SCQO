"""Resonator spectroscopy vs ABSOLUTE readout power — the CHAIN-STEPPED punchout.

The sibling punchout (:mod:`.resonator_spectroscopy_power_amp`) solves the chain
for the window top once and sweeps the digital amplitude down from it in one fast
hardware program — quick, but its low end runs at a tiny DAC amplitude (poor SNR). This
experiment instead steps the OUTPUT CHAIN per power point with a **Python loop**
(neither backend can change QM ``full_scale_power_dbm`` / Qblox ``output_att``
inside the FPGA loop), so the digital amplitude stays ~0.5 (best SNR) at every
point and the absolute range is wide:

* per point, the chain is re-solved so the digital amplitude stays at the
  canonical ~0.5 of full scale wherever the chain can reach (the
  ``readout_power_dbm`` setter policy: chain as attenuated as possible, the
  amplitude absorbing the exact residual — every point hits its requested power
  exactly, so the uniform-dB axis is truthful on both backends);
* per point, ONE 1D detuning acquisition runs at CONSTANT power (drivers reuse
  their plain resonator-spectroscopy probes, including the depletion waits), and
  the points ascend low->high — a high->low power jump inside a fast loop, where
  resonator ring-down could contaminate, never happens;
* the run costs N_power separate compile+run cycles (QM: config+open per point,
  seconds each — the default 21 points adds minutes; the price of per-point SNR).

Audit trail (user-decided): only the BOUNDARY writes are recorded — set to
``max_power_dbm`` at the start, revert at the end (2 ChangeRecords + coupled
echoes per qubit, through the recorded device view). The per-point chain steps go
through the RAW vendor views: unrecorded acquisition detail (like sweeping an IF
inside a program), tracked instead in the dataset coords and the figure's
amp/chain subplot. The knee found is proposed as ``readout_power_dbm`` (+ the dip
position as ``readout_freq``) through the normal suggestion flow — nothing
survives the run unless accepted.

Absolute-scale honesty: QM's dBm axis is exact at the port; Qblox derives from
the nominal +5 dBm full scale (±a few dB). A per-setup photon-number anchor
(AC-Stark) is the Phase-3 refinement.
"""

from __future__ import annotations

import math
from typing import Any, ClassVar

import numpy as np
import xarray as xr
from pydantic import Field, model_validator

from .._scqat import per_qubit_results
from ._sim import stable_seed
from ..contract import DatasetContract
from ..experiment import Experiment
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result


class ResonatorSpectroscopyPowerChainParameters(TargetSelection, AveragingParameters):
    """Inputs for the chain-stepped absolute-power punchout scan."""

    frequency_span_hz: float = Field(20e6, gt=0, description="Total detuning span around the current readout_freq.")
    num_freq_points: int = Field(101, gt=1, description="Number of frequency points.")
    max_power_dbm: float = Field(
        -20.0,
        le=10.0,
        description="Highest absolute readout power (dBm at the instrument port). QM caps at "
        "+10 dBm; Qblox above ~-1 dBm needs amplitude > 0.5.",
    )
    min_power_dbm: float = Field(-50.0, description="Lowest absolute readout power (dBm).")
    num_power_points: int = Field(
        21, gt=1, description="Number of power points — each is a SEPARATE compile+run cycle "
        "(the chain is re-solved per point); the optimal-power derivative uses a ~10-point "
        "smoothing window, so keep this comfortably above 10."
    )

    @model_validator(mode="after")
    def _window_ordered(self) -> "ResonatorSpectroscopyPowerChainParameters":
        if not self.min_power_dbm < self.max_power_dbm:
            raise ValueError(
                f"min_power_dbm ({self.min_power_dbm}) must be below max_power_dbm ({self.max_power_dbm})"
            )
        return self


class ResonatorSpectroscopyPowerChainResult(Result):
    """``fit[qubit]``: ``readout_power_dbm`` (new), ``readout_freq`` (new),
    ``optimal_power_dbm``, ``frequency_shift_hz``, plus the old values."""


class ResonatorSpectroscopyPowerChain(Experiment):
    """Backend-agnostic chain-stepped punchout. ``probe()`` is supplied by a driver
    and builds the 1D DETUNING program at the CURRENT device state (the core run()
    already solved the chain for the point being acquired)."""

    name: ClassVar[str] = "resonator_spectroscopy_power_chain"
    description: ClassVar[str] = (
        "Careful punchout that STEPS THE OUTPUT CHAIN (QM full_scale_power_dbm / Qblox "
        "output_att) per power point, holding the digital amplitude ~0.5 for best SNR (slow: "
        "one compile+run cycle per point; wide absolute range; cross-backend comparable). "
        "Absolute dBm axis; proposes readout_power_dbm + readout_freq. Use for a calibrated "
        "wide sweep. (The sibling resonator_spectroscopy_power_amp is the fast amplitude-sweep "
        "version.)"
    )
    Parameters: ClassVar[type] = ResonatorSpectroscopyPowerChainParameters
    Result: ClassVar[type] = ResonatorSpectroscopyPowerChainResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("detuning_hz", "power_dbm"), sweep_units=("Hz", "dBm"), variables=("I", "Q")
    )
    required_operations: ClassVar[tuple[str, ...]] = ("readout",)

    params: ResonatorSpectroscopyPowerChainParameters

    def define_sweep(self) -> dict[str, np.ndarray]:
        span = self.params.frequency_span_hz
        return {
            "detuning_hz": np.linspace(-span / 2, span / 2, self.params.num_freq_points),
            "power_dbm": np.linspace(
                self.params.min_power_dbm, self.params.max_power_dbm, self.params.num_power_points
            ),
        }

    def run(self) -> Result:
        """Boundary-recorded set/revert around a python loop of per-point
        (chain solve -> 1D acquisition) cycles.

        The boundary writes go through ``self.device`` (the Session's
        RecordingDevice): 2 ChangeRecords + coupled echoes per qubit. The
        per-point steps use the backend's RAW vendor views — unrecorded
        acquisition detail, captured per point into ``self._chain_steps`` for
        the dataset/figure. Ascending order: each acquisition runs at constant
        power and every jump is upward, so ring-down from a previous (lower)
        point cannot contaminate.
        """
        axes_2d = self.define_sweep()
        self.sweep_axes = axes_2d
        detuning = axes_2d["detuning_hz"]
        power_grid = axes_2d["power_dbm"]
        top = float(self.params.max_power_dbm)
        targets = list(self.params.targets)
        views = {q: self.device.component(q) for q in targets}
        raw_views = {q: self.backend.device.component(q) for q in targets}

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

        self._chain_steps: list[dict] = []
        try:
            slices = []
            for p in power_grid:  # ascending — constant power within each acquisition
                p = float(p)
                for raw in raw_views.values():
                    raw.readout_power_dbm = p  # raw vendor write: unrecorded sweep step
                try:
                    ctx = self.backend.power_context(targets) or {}
                except Exception:  # provenance must never fail a measurement
                    ctx = {}
                self._chain_steps.append(ctx)
                self._current_power_dbm = p
                self.sweep_axes = {"detuning_hz": detuning}  # the per-point 1D contract
                try:
                    slices.append(self.backend.acquire(self))
                finally:
                    self.sweep_axes = axes_2d
            dataset = xr.concat(slices, dim="power_dbm")
            dataset = dataset.assign_coords(power_dbm=power_grid)
            self.dataset = dataset.transpose("target", "detuning_hz", "power_dbm")
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
        """Per-POINT simulator: called once per power point with only the detuning
        axis; the current power comes from the run loop. The dip truth (dressed
        position, knee) is drawn from a power-independent seed so every point sees
        the same resonator; noise is seeded per point."""
        detuning = coords["detuning_hz"]
        targets = self.params.targets
        p = float(getattr(self, "_current_power_dbm", self.params.max_power_dbm))
        top = float(self.params.max_power_dbm)
        span = float(detuning[-1] - detuning[0])
        kappa = span / 15
        truth_rng = np.random.default_rng(
            stable_seed("resonator_spectroscopy_power_chain", *targets)
        )
        noise_rng = np.random.default_rng(
            stable_seed("resonator_spectroscopy_power_chain", *targets, f"{p:.9f}")
        )
        i_data = np.empty((len(targets), detuning.size))
        q_data = np.empty_like(i_data)
        for k in range(len(targets)):
            dressed = truth_rng.uniform(-0.1, 0.1) * span  # dispersive dip position
            knee_dbm = top - truth_rng.uniform(8.0, 12.0)  # onset, relative to the top
            # above the knee the dip walks DOWN toward the bare cavity and washes out
            walk = max(0.0, p - knee_dbm)
            center = dressed - walk * 0.8e6  # ~-0.8 MHz per dB past the knee
            depth = 0.8 / (1.0 + walk / 4.0)
            magnitude = 1.0 - depth / (1.0 + ((detuning - center) / kappa) ** 2)
            # the return signal still scales with the DELIVERED power (the chain
            # attenuates the drive; the receive path is fixed)
            amp = 10.0 ** ((p - top) / 20.0)
            noise = 0.01
            i_data[k] = amp * (magnitude + noise_rng.normal(0, noise, detuning.size))
            q_data[k] = amp * noise_rng.normal(0, noise, detuning.size)
        return {"I": i_data, "Q": q_data}

    def estimate(self) -> ResonatorSpectroscopyPowerChainResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.resonator_spectroscopy_power import ResonatorSpectroscopyPowerEstimator

        # scqat's contract: coords `power` + `detuning`, vars I/Q; the estimator's
        # optimal-power logic is derivative-based (Hz per dB step) and its output
        # `optimal_power` is already an absolute dBm value on this axis.
        targets = list(self.dataset["target"].values)
        old_freqs = {q: float(self.device.component(q).readout_freq) for q in targets}
        # estimate() runs after the revert, so this reads the standing (pre-run) chain.
        old_power = {q: float(self.device.component(q).readout_power_dbm) for q in targets}
        prepared = self.dataset.rename({"detuning_hz": "detuning", "power_dbm": "power"})
        prepared = prepared.transpose("target", "power", "detuning")
        detuning = prepared["detuning"].values
        full_freq = np.array([detuning + old_freqs[q] for q in targets])
        prepared = prepared.assign_coords(full_freq=(("target", "detuning"), full_freq))
        prepared = self._attach_chain_steps(prepared, targets)

        results = per_qubit_results(
            prepared, ResonatorSpectroscopyPowerEstimator(), artifact_dir=self.artifact_dir
        )

        result = ResonatorSpectroscopyPowerChainResult()
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

    def _attach_chain_steps(self, prepared: xr.Dataset, targets: list) -> xr.Dataset:
        """Attach the per-point chain provenance (digital amplitude + the used
        full_scale/att) as (qubit, power) coords for the scqat figure's subplot —
        the SAME form the _amp punchout emits (one shared figure format). The
        axis-kind/mode labels are ALWAYS attached (the figure is self-identifying);
        the subplot data only when the backend reported the chain (simulated ->
        plain map)."""
        n_q = len(targets)
        prepared = prepared.assign_coords(
            power_axis_kind=("target", ["absolute dBm"] * n_q),
            mode_label=("target", ["chain-stepped (slow)"] * n_q),
        )
        steps = getattr(self, "_chain_steps", [])
        n_power = prepared.sizes.get("power", 0)
        if len(steps) != n_power or not any(
            step.get(q) for step in steps for q in targets
        ):
            return prepared
        amp = np.full((n_q, n_power), np.nan)
        setting = np.full((n_q, n_power), np.nan)
        names = []
        for k, q in enumerate(targets):
            name = ""
            for j, step in enumerate(steps):
                ctx = step.get(q) or {}
                if "pulse_amp" in ctx:
                    amp[k, j] = float(ctx["pulse_amp"])
                elif "readout_amplitude" in ctx:
                    amp[k, j] = float(ctx["readout_amplitude"])
                if "output_att_db" in ctx:
                    setting[k, j] = float(ctx["output_att_db"])
                    name = "output_att (dB)"
                elif "full_scale_power_dbm" in ctx:
                    setting[k, j] = float(ctx["full_scale_power_dbm"])
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
