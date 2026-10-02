"""Qubit Ramsey vs flux PULSE — the local arch around the parking point, at kHz.

Each flux point is one Ramsey fringe: y90 at the idle point -> a square z pulse of
relative amplitude ``a`` for the whole idle ``tau`` -> x90 at the idle point ->
readout at the idle point. So the pi/2 pulses and the readout never leave the point
they were calibrated at; only the free evolution is detuned. The fringe frequency
per amplitude is the qubit frequency there, to a few kHz, and a local quadratic
gives either the apex or the amplitude that puts the qubit at ``park_frequency_hz``.

FRAME (``_pulse`` in the name, ``FluxPulseSweepParameters`` in the schema): the
window is an excursion from the swept flux line's ``idle_flux``. Every written flux
value is re-referenced to ABSOLUTE (``old_idle_flux + fitted``). A pulse moves the
qubit by ``g`` times the same DC step (5Q4C q1 2026-09-26: g ~ 0.96, no offset), so
a park found from far away lands within ~4 % of the move; re-run to converge.

ONE OF THREE ways to locate the arch, complementary rather than ranked:
``resonator_spectroscopy_flux`` (offset and period through the resonator, works
before the qubit is visible), ``qubit_spectroscopy_flux_pulse`` (the whole arch,
works with a short T2*) and this one (local, kHz-level, needs a fringe that
survives several cycles). The first two supply the arch facts this one uses to
choose the direction of its virtual detuning.

FOLDING: the fringe sits at ``|D + (f_q - f_drive)|``. ``define_sweep`` predicts
the excursion over the window from the arch facts and picks the SIGN of ``D`` so
the excursion pushes the fringe away from zero; a window that would still fold,
or that the idle grid cannot sample, is refused before any instrument time.
Without arch facts the sign defaults to the apex case (the qubit can only drop
below its drive) and the run says so (``ramp_sign_from``); the fit's own
``fold_suspected`` flag is then the guard.
"""

from __future__ import annotations

from typing import ClassVar, Literal

import numpy as np
from pydantic import Field, model_validator

from ..contract import DatasetContract
from ..estimate_inputs import acquisition_note, note_acquisition
from ..experiment import Experiment
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from . import register
from ._capabilities.flux import (
    END_FLUX_PULSE_DESC,
    NUM_FLUX_DESC,
    START_FLUX_PULSE_DESC,
    FluxPulseSweepParameters,
    flux_anchor_v,
    flux_sweep,
    foreign_flux_source,
)
from ._capabilities.qubit_reset import QubitResetParameters
from ._capabilities.state_readout import (
    POPULATION_ALT,
    StateReadoutParameters,
    population_row,
    readout_vars,
    signal_rename,
)
from ._flux_component import FluxComponentParameters
from ._sim import iq_from_population, stable_seed
from ._time_grid import time_axis_ns

#: EC used for the fold prediction when the target carries no ``ec_hz`` fact
_DEFAULT_EC_HZ = 0.2e9
#: a detuning must exceed the smaller-side excursion by this factor
FOLD_MARGIN = 1.5
#: the largest fringe may use this fraction of the idle grid's Nyquist frequency
NYQUIST_FRACTION = 0.8
#: a park target may sit this far above the stored apex before it is unreachable
APEX_TOLERANCE_HZ = 0.5e6


class QubitRamseyFluxPulseParameters(
    TargetSelection, AveragingParameters, StateReadoutParameters, QubitResetParameters,
    FluxPulseSweepParameters, FluxComponentParameters,
):
    """Inputs for the Ramsey-vs-flux-pulse map (window relative to idle_flux)."""

    # capability defaults narrowed to a parking window (canonical text constants)
    start_flux_v: float = Field(-0.012, description=START_FLUX_PULSE_DESC)
    end_flux_v: float = Field(0.012, description=END_FLUX_PULSE_DESC)
    num_flux_points: int = Field(
        7, gt=4, description=NUM_FLUX_DESC + " (the local quadratic needs >= 5 "
        "resolved points).")
    num_averages: int = Field(200, gt=0, description="Number of shots to average per sweep point.")
    park_frequency_hz: float | None = Field(
        None, gt=0,
        description="The f01 (Hz) to park the target at; None = its flux apex. A "
        "target frequency needs ONE target and the target's own flux line, and is "
        "found only inside the window (never extrapolated).")
    flux_side: Literal["nearest", "lower", "upper"] = Field(
        "nearest",
        description="Which root of f(x) = park_frequency_hz when the window holds one "
        "on each side of the apex: 'lower'/'upper' = the one at the lower/higher "
        "flux, 'nearest' = the one closer to the current idle_flux. Only consulted "
        "when both roots are inside the window.")
    frequency_detuning_hz: float = Field(
        4.0e6, gt=0,
        description="MAGNITUDE of the virtual detuning; the experiment picks its sign "
        "so the qubit's excursion pushes the fringe away from zero (see ramp_sign_from "
        "in the fit). Must exceed the smaller-side excursion over the window by 1.5x.")
    min_idle_time_ns: float = Field(
        16, ge=16, description="Shortest idle = z-pulse length (ns); 16 ns is the "
        "shortest pulse every backend plays.")
    max_idle_time_ns: float = Field(4000, gt=0, description="Longest idle = z-pulse length (ns).")
    num_idle_points: int = Field(201, gt=4, description="Number of idle-time points (4 ns grid).")
    flux_buffer_ns: int = Field(
        20, ge=0,
        description="Wait at the idle point on BOTH sides of the z pulse, so neither "
        "pi/2 pulse overlaps its edges: 0, or a multiple of 4 ns from 16 ns up.")

    @model_validator(mode="after")
    def _buffer_on_grid(self) -> "QubitRamseyFluxPulseParameters":
        b = self.flux_buffer_ns
        if b != 0 and (b < 16 or b % 4):
            raise ValueError(
                f"flux_buffer_ns={b}: use 0, or a multiple of 4 ns from 16 ns up "
                f"(a shorter nonzero wait is not playable on every backend)")
        return self


class QubitRamseyFluxPulseResult(Result):
    """``fit[target]``: the reading in the swept frame (``flux_offset_from_idle``,
    ``park_excursion_v``) and re-referenced ABSOLUTE values under the catalog names
    they propose (``idle_flux``, ``flux_offset``, ``f_q_max_hz``, ``f_01_hz``,
    ``drive_freq_hz``), with stderrs, ``curvature_hz_per_v2``, ``df_dflux_hz_per_v``,
    provenance (``old_idle_flux``, ``old_drive_freq_hz``, ``ramp_detuning_hz``,
    ``ramp_sign_from``) and 0/1 flags (``apex_not_bracketed``,
    ``park_out_of_window``, ``fold_suspected``, ``multi_target_context``)."""


@register
class QubitRamseyFluxPulse(Experiment):
    """Backend-agnostic Ramsey-vs-flux-pulse map. ``probe()`` is supplied by a driver."""

    name: ClassVar[str] = "qubit_ramsey_flux_pulse"
    description: ClassVar[str] = (
        "Ramsey fringe vs a z PULSE played during the idle (relative to idle_flux, "
        "0 = stay parked): the pi/2 pulses and the readout stay at the idle point, so "
        "the fringe frequency gives f01 at each flux to a few kHz. A local quadratic "
        "gives the flux APEX (park_frequency_hz None) or the flux that puts f01 at "
        "park_frequency_hz; proposes idle_flux (absolute) with drive_freq_hz and the "
        "f_01_hz fact, plus flux_offset and f_q_max_hz when the apex is inside the "
        "window. One of three complementary ways to place a qubit on its arch: "
        "resonator_spectroscopy_flux (offset and period via the resonator, before the "
        "qubit is visible) and qubit_spectroscopy_flux_pulse (the whole arch, short "
        "T2* is fine) supply the arch facts this one uses to orient its virtual "
        "detuning; this one needs a fringe that survives several cycles. A pulse moves "
        "the qubit ~4 % less than the same DC step, so a far move converges on a "
        "re-run. On a coupler chip the apex it reports holds at the CURRENT coupler "
        "biases: re-park couplers first. Record-only with several targets or a foreign "
        "flux_component."
    )
    Parameters: ClassVar[type] = QubitRamseyFluxPulseParameters
    Result: ClassVar[type] = QubitRamseyFluxPulseResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=("flux_bias_v", "idle_time_ns"), sweep_units=("V", "ns"),
        variables=("I", "Q"), alt_variables=POPULATION_ALT,
    )
    required_operations: ClassVar[tuple[str, ...]] = ("rx", "readout", "flux_bias")
    #: stored blob centers ride the dataset -> the axial axis is the measured g->e vector
    attach_readout_positions: ClassVar[bool] = True

    params: QubitRamseyFluxPulseParameters

    # ------------------------------------------------------------------ sweep
    def define_sweep(self) -> dict[str, np.ndarray]:
        p = self.params
        if p.park_frequency_hz is not None:
            if len(p.targets) != 1:
                raise ValueError(
                    "park_frequency_hz needs exactly ONE target: parking several qubits "
                    "at once sweeps every line together, and crosstalk then correlates "
                    "with the sweep index (run one qubit at a time)")
            if foreign_flux_source(p):
                raise ValueError(
                    "park_frequency_hz with a foreign flux_component is a record-only "
                    "crosstalk run and cannot park anything")
            f_max = self.fact(p.targets[0], "f_q_max_hz", None)
            if f_max is not None and p.park_frequency_hz > f_max + APEX_TOLERANCE_HZ:
                raise ValueError(
                    f"park_frequency_hz={p.park_frequency_hz:.6g} Hz is above the stored "
                    f"apex f_q_max_hz={f_max:.6g} Hz: unreachable on this arch")
        axes = {
            **flux_sweep(p),
            "idle_time_ns": time_axis_ns(p.min_idle_time_ns, p.max_idle_time_ns,
                                         p.num_idle_points, grid_ns=4),
        }
        self._ramps = self._resolve_ramps(axes)
        return axes

    def _resolve_ramps(self, axes: dict[str, np.ndarray]) -> dict[str, dict]:
        """Per target: the SIGNED detuning and where its sign came from.

        The ONE place the direction is decided; both probes spend the number via
        :meth:`ramp_detuning_hz`."""
        p = self.params
        amp = p.frequency_detuning_hz
        idle = axes["idle_time_ns"]
        step_s = float(np.min(np.diff(np.sort(idle)))) * 1e-9 if idle.size > 1 else np.nan
        nyquist = 0.5 / step_s if step_s > 0 else np.inf
        window = np.linspace(float(np.min(axes["flux_bias_v"])),
                             float(np.max(axes["flux_bias_v"])), 101)
        ramps: dict[str, dict] = {}
        for q in p.targets:
            excursion = None if foreign_flux_source(p) else self._predicted_excursion(q, window)
            if excursion is None:
                below = (p.park_frequency_hz is None
                         or p.park_frequency_hz <= self.anchor(q, "drive_freq_hz"))
                ramps[q] = {"ramp_detuning_hz": -amp if below else amp,
                            "ramp_sign_from": "default"}
                continue
            e_min, e_max = excursion
            # the side the qubit swings further to pushes the fringe; the other one limits A
            sign = -1.0 if -e_min >= e_max else 1.0
            limit = max(0.0, e_max) if sign < 0 else max(0.0, -e_min)
            if amp < FOLD_MARGIN * limit:
                raise ValueError(
                    f"{q}: folding_risk - the window moves the qubit {limit / 1e6:.3f} MHz "
                    f"to the side the detuning cannot cover; raise frequency_detuning_hz "
                    f"to >= {FOLD_MARGIN * limit / 1e6:.3f} MHz or narrow the window")
            top = amp + max(abs(e_min), abs(e_max))
            if top > NYQUIST_FRACTION * nyquist:
                raise ValueError(
                    f"{q}: undersampled - the fringe reaches {top / 1e6:.3f} MHz but the "
                    f"idle grid resolves {NYQUIST_FRACTION * nyquist / 1e6:.3f} MHz; shorten "
                    f"the idle step (more num_idle_points or a smaller max_idle_time_ns) "
                    f"or narrow the window")
            ramps[q] = {"ramp_detuning_hz": sign * amp, "ramp_sign_from": "facts"}
        return ramps

    def _predicted_excursion(self, q: str, window: np.ndarray) -> tuple[float, float] | None:
        """(min, max) of f_q - f_drive over the window, from the arch facts; None
        when a fact is missing (the default sign is used instead)."""
        try:
            f_max = self.fact(q, "f_q_max_hz", None)
            offset = self.fact(q, "flux_offset", None)
            period = self.fact(q, "flux_per_phi0", None)
            if f_max is None or offset is None or not period:
                return None
            ec = self.fact(q, "ec_hz", _DEFAULT_EC_HZ)
            idle = self.anchor(q, "idle_flux")
            drive = self.anchor(q, "drive_freq_hz")
        except (AttributeError, KeyError, ValueError):
            # no device model to read facts from (a bare backend): same as missing
            return None
        x = idle + window
        f = (f_max + ec) * np.sqrt(np.abs(np.cos(np.pi * (x - offset) / period))) - ec
        err = f - drive
        return float(np.min(err)), float(np.max(err))

    def ramp_detuning_hz(self, target: str) -> float:
        """The signed virtual detuning a probe applies for ``target`` (the
        ``qubit_ramsey`` convention: the fringe sits at ``|D + (f_q - f_drive)|``)."""
        ramps = getattr(self, "_ramps", None)
        if ramps is None:  # a caller that skipped define_sweep (define_sweep caches it)
            ramps = self._resolve_ramps(self.sweep_axes or self.define_sweep())
        return float(ramps[target]["ramp_detuning_hz"])

    # ------------------------------------------------------------------ run
    def run(self) -> Result:
        """define_sweep -> acquire -> the resolved ramps ride the dataset -> estimate."""
        self.sweep_axes = self.define_sweep()
        self.dataset = self.backend.acquire(self)
        self.Contract.validate(self.dataset)
        self._attach_reference_positions()
        self.attach_acquisition_coords()
        note_acquisition(self.dataset, "ramps", self._ramps)
        return self.run_estimate()

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """A hidden local arch per target, seen through the fringe the probe records.

        The apex sits inside the window and the edge swing is ~40 % of the detuning,
        so the synthetic run neither folds nor leaves the window."""
        flux = coords["flux_bias_v"]
        t = coords["idle_time_ns"] * 1e-9
        targets = self.params.targets
        rng = np.random.default_rng(stable_seed("qubit_ramsey_flux_pulse", *targets))
        amp = self.params.frequency_detuning_hz
        half = max(float(np.max(np.abs(flux))), 1e-6)
        use_state = self.params.use_state_discrimination
        shape = (len(targets), flux.size, t.size)
        i_data, q_data, state = np.empty(shape), np.empty(shape), np.empty(shape)
        for k, q in enumerate(targets):
            ramp = self.ramp_detuning_hz(q)
            x0 = rng.uniform(-0.3, 0.3) * half
            curv = -0.4 * amp / (1.7 * half) ** 2
            height = rng.uniform(-0.05, 0.05) * amp
            t2 = rng.uniform(8e-6, 15e-6)
            for j, x in enumerate(flux):
                fringe = abs(ramp + curv * (x - x0) ** 2 + height)
                pop = 0.5 - 0.45 * np.exp(-t / t2) * np.cos(2 * np.pi * fringe * t)
                if use_state:
                    state[k, j] = population_row(pop, rng)
                else:
                    i_data[k, j], q_data[k, j] = iq_from_population(pop, rng)
        return readout_vars(use_state, state, i_data, q_data)

    # ------------------------------------------------------------------ estimate
    def estimate(self) -> QubitRamseyFluxPulseResult:
        assert self.dataset is not None, "run() populates self.dataset before estimate()"
        from scqat.estimators.qubit_ramsey_flux_pulse import QubitRamseyFluxPulseEstimator
        from .._scqat import per_qubit_results

        p = self.params
        ramps = (acquisition_note(self.dataset, "ramps", None)
                 or getattr(self, "_ramps", None) or {})
        rename = signal_rename(self.dataset, {"flux_bias_v": "flux_bias",
                                              "idle_time_ns": "idle_time"})
        prepared = self.dataset.rename(rename)
        prepared = prepared.assign_coords(idle_time=prepared["idle_time"] * 1e-9)
        drives = {q: float(self.anchor(q, "drive_freq_hz")) for q in p.targets}
        per_target = {q: {"ramp_detuning_hz": float(ramps[q]["ramp_detuning_hz"]),
                          "drive_freq_hz": drives[q]} for q in p.targets}
        kwargs = {"flux_side": p.flux_side, "nearest_to": 0.0}
        if p.park_frequency_hz is not None:
            kwargs["park_frequency_hz"] = p.park_frequency_hz
        results = per_qubit_results(prepared, QubitRamseyFluxPulseEstimator(),
                                    artifact_dir=self.artifact_dir,
                                    per_target_kwargs=per_target, **kwargs)

        multi = int(len(p.targets) > 1)
        result = QubitRamseyFluxPulseResult()
        for q in p.targets:
            r = results[q]
            old_idle = flux_anchor_v(self, q)
            fit: dict = {
                "question": r["question"],
                "old_idle_flux": old_idle,
                "old_drive_freq_hz": drives[q],
                "ramp_detuning_hz": float(ramps[q]["ramp_detuning_hz"]),
                "ramp_sign_from": str(ramps[q]["ramp_sign_from"]),
                "curvature_hz_per_v2": r["curvature_hz_per_v2"],
                "n_valid_points": r["n_valid_points"],
                "apex_not_bracketed": r["apex_not_bracketed"],
                "park_out_of_window": r["park_out_of_window"],
                "fold_suspected": r["fold_suspected"],
                "multi_target_context": multi,
            }
            if not r["apex_not_bracketed"]:
                fit.update(
                    flux_offset_from_idle=r["apex_flux"],
                    flux_offset=old_idle + r["apex_flux"],
                    flux_offset_stderr=r["apex_flux_stderr"],
                    f_q_max_hz=r["apex_f01_hz"],
                    f_q_max_stderr_hz=r["apex_delta_f_stderr_hz"],
                )
            if r["question"] == "apex":
                if not r["apex_not_bracketed"]:
                    fit.update(idle_flux=fit["flux_offset"], idle_flux_stderr=r["apex_flux_stderr"],
                               f_01_hz=r["apex_f01_hz"], drive_freq_hz=r["apex_f01_hz"],
                               df_dflux_hz_per_v=0.0)
            elif not r["park_out_of_window"]:
                fit.update(park_excursion_v=r["park_flux"], idle_flux=old_idle + r["park_flux"],
                           idle_flux_stderr=r["park_flux_stderr"],
                           f_01_hz=r["park_f01_hz"], drive_freq_hz=r["park_f01_hz"],
                           df_dflux_hz_per_v=r["park_slope_hz_per_v"])
            result.fit[q] = fit
            ok = bool(r["success"]) and not r["fold_suspected"]
            result.outcomes[q] = Outcome.SUCCESSFUL if ok else Outcome.FAILED
        return result

    def update(self) -> None:
        """One target on its own line, SUCCESSFUL: re-park it (``idle_flux``, absolute),
        retune its drive to the new f01 (``drive_freq_hz`` with its ``f_01_hz`` twin),
        and record the apex facts when the window held the apex."""
        if self.result is None or foreign_flux_source(self.params) or len(self.result.fit) != 1:
            return
        for q, fit in self.result.fit.items():
            if self.result.outcomes[q] is not Outcome.SUCCESSFUL or "idle_flux" not in fit:
                continue
            flux = self.device.channel(q, "flux")
            self.device.flux_line(q).idle_flux = fit["idle_flux"]
            self.device.channel(q, "drive").drive_freq_hz = fit["drive_freq_hz"]
            mode = self.device.component(q)
            mode.f_01_hz = fit["f_01_hz"]
            if "flux_offset" in fit:
                flux.flux_offset = fit["flux_offset"]
                mode.f_q_max_hz = fit["f_q_max_hz"]

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")
