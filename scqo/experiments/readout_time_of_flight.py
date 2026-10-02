"""Readout time of flight — when the readout pulse comes back.

The instrument emits a readout pulse and digitizes its own input. Between the
two sits the round trip: the output chain, the cables into the fridge, the
sample, the cables out, the amplifiers and the input chain. The acquisition
window has to be opened by exactly that much or the integration window and the
pulse do not overlap — which costs readout fidelity with nothing saying so,
because a mis-aligned window still returns a perfectly well-formed number.

Nothing else measures it. It is the one quantity here that is VENDOR-ONLY on
BOTH backends by design — QM's ``resonator.time_of_flight`` (ns) and Qblox's
``measure.acq_delay`` (s) — so this experiment proposes NOTHING and pushes
NOTHING. It measures, and prints where to write the answer (:mod:`._tof_hint`).
Run it after a re-cabling, a fridge cycle that moved a line, or an instrument
swap.

The sequence, identical on both backends: play the readout pulse, open the
acquisition window at a DECLARED EARLY ORIGIN, and record the RAW digitizer
samples averaged over shots. The trace reads noise, then a step, then a
plateau; the step is the delay.

Why the origin is declared, and not the value already configured
----------------------------------------------------------------
Opening the window where the channel currently points is the intuitive choice
and it is wrong: if the current value happens to be RIGHT, the pulse is already
present at sample 0, there is no pre-arrival baseline, and the threshold has
nothing to sit between. The measurement would work only while it was not
needed. So the window opens as early as the instrument allows
(``window_start_ns = 0``) and the answer is ``window_start_ns + arrival`` —
which also makes it independent of how wrong the current setting is.

``window_start_ns = 0`` means "as early as this instrument allows" and each
driver reports its own floor. A NON-ZERO value is a promise: below the floor it
is REFUSED BY NAME rather than quietly raised, because an operator who asks for
a specific origin is usually reproducing an old run.

Phase, and the one way this fails quietly
-----------------------------------------
The trace is AVERAGED over shots, so each shot must start at the same digital
oscillator phase or the cosine averages toward zero and the step disappears
into the noise. Both probes reset it per shot. If the figure shows a flat |IQ|
with two quadratures that look like noise, that is the thing to check first —
the fit reports ``arrival_unresolved`` rather than a number.

Amplitude
---------
``readout_amp_factor`` scales the readout for this run only. It defaults to 1.0
and 1.0 is usually too small: the stored amplitude is calibrated for a
DEMODULATED readout, whose integration over the whole pulse buys roughly
``sqrt(readout_duration / 1 ns)`` of SNR — about 28x for an 800 ns readout —
that a per-sample raw trace does not get. Measured on 5Q4C q1 (2026-09-26): at
factor 1.0 the trace is flat noise across all 1000 ns and the fit correctly
refuses. The retired node hard-set -12 dBm for the same reason.

The reading (scqat ``readout_time_of_flight``, bound here 1:1): a robust
baseline and plateau, a threshold midway between them, and an interpolated
first crossing, plus the 10-90 % rise time and a saturation check. The absolute
delay is rounded onto the instrument's timing grid (4 ns on both shipped
backends) because that is what the vendor field accepts — a real delay need not
be a multiple of 4, so ``arrival_ns`` keeps the unrounded measurement beside it.
"""

from __future__ import annotations

from typing import Any, ClassVar

import numpy as np
from pydantic import Field

from .._scqat import per_qubit_results
from ..contract import DatasetContract
from ..estimate_inputs import acquisition_note, note_acquisition
from ..experiment import Experiment
from ..parameters import AveragingParameters, TargetSelection
from ..result import Outcome, Result
from . import register
from ._sim import stable_seed
from ._tof_hint import HOOK, print_apply_hint

#: the axis crossing the probe <-> estimator boundary: sample time from the
#: window origin, in ns.
TIME_AXIS = "readout_time_ns"

#: the acquisition-note key carrying the frame estimate() needs.
FRAME_NOTE = "tof_frame"

#: fallbacks for a backend that declares no readout_delay_context. Both shipped
#: digitizers run at 1 GSa/s and both vendor fields take a 4 ns grid.
DEFAULT_SAMPLE_NS = 1.0
DEFAULT_GRID_NS = 4.0


class ReadoutTimeOfFlightParameters(TargetSelection, AveragingParameters):
    """Inputs for a raw-trace time-of-flight measurement."""

    num_averages: int = Field(
        4000, gt=0,
        description="Shots averaged into the trace. The mixin default of 100 is "
                    "raised here because a RAW trace has no integration gain: "
                    "measured on 5Q4C q1 (2026-09-26), 100 shots gave a plateau "
                    "SNR of 1.2 and the edge finder tripped on noise, while 4000 "
                    "gave 27 and a clean 4 ns edge. The run costs seconds either "
                    "way - 4000 shots of a 1 us trace is a few seconds - so the "
                    "cheap default is the wrong one for this measurement.")
    readout_len_ns: float = Field(
        1000.0, gt=0,
        description="Length of the digitized trace, ns. It must comfortably "
                    "exceed the delay being measured, because an edge past the "
                    "end of the trace cannot be seen at all and one near the "
                    "end is reported as a bound (arrival_at_edge). 1 us covers "
                    "any normal fridge wiring; shorten it only to save time on "
                    "a setup whose delay is already known to be small.")
    readout_amp_factor: float = Field(
        1.0, gt=0, lt=4.0,
        description="Scale the readout amplitude for THIS RUN only, as a factor "
                    "of the channel's stored readout_amp. The default 1.0 is "
                    "usually NOT enough: the stored amplitude is calibrated for "
                    "a DEMODULATED readout, which integrates the whole pulse and "
                    "so buys roughly sqrt(readout_duration / 1 ns) of SNR that a "
                    "per-sample raw trace does not get. On 5Q4C at factor 1.0 the "
                    "pulse is invisible in the noise. The retired node ran this "
                    "measurement at -12 dBm for exactly this reason. Nothing is "
                    "stored and no vendor state is touched — the factor is applied "
                    "to the generated config. factor x readout_amp above the DAC "
                    "rail is refused BY NAME by the driver.")
    window_start_ns: float = Field(
        0.0, ge=0,
        description="Where the acquisition window opens, ns, measured from the "
                    "readout pulse. 0 = as early as this instrument allows "
                    "(the usual choice: it maximizes the visible baseline). A "
                    "non-zero value below the instrument's floor is REFUSED by "
                    "name rather than raised, so an origin you state is the "
                    "origin you get.")


class ReadoutTimeOfFlightResult(Result):
    """``fit[target]``: ``time_of_flight_ns`` — the ABSOLUTE delay to write into
    the vendor field, rounded onto the instrument's timing grid — plus the
    unrounded ``arrival_ns`` it was measured as, the ``window_start_ns`` frame
    it was measured in, ``old_delay_ns`` (what the channel carried) and
    ``delta_ns`` (the change), the quality pair ``rise_time_ns`` /
    ``plateau_snr``, and the refusal flags ``arrival_unresolved`` /
    ``arrival_at_edge`` / ``adc_saturated``. Nothing is proposed: the field is
    vendor-only on both backends."""


@register
class ReadoutTimeOfFlight(Experiment):
    """Backend-agnostic readout time of flight from the raw digitizer trace."""

    name: ClassVar[str] = "readout_time_of_flight"
    description: ClassVar[str] = (
        "Measure the readout round-trip delay — the time between emitting a "
        "readout pulse and seeing it arrive at the digitizer — by recording the "
        "RAW ADC trace with the acquisition window opened as early as the "
        "instrument allows. The pulse edge in that trace IS the delay. Run it "
        "after any re-cabling, fridge cycle or instrument swap: the delay sets "
        "where the integration window sits, and a wrong one costs readout "
        "fidelity silently, because a mis-aligned window still returns a "
        "well-formed number. This is the only experiment whose result is "
        "VENDOR-ONLY on both backends (QM resonator.time_of_flight in ns, "
        "Qblox measure.acq_delay in s), so it proposes nothing and pushes "
        "nothing — it prints the field and the value to write."
    )
    Parameters: ClassVar[type] = ReadoutTimeOfFlightParameters
    Result: ClassVar[type] = ReadoutTimeOfFlightResult
    Contract: ClassVar[DatasetContract] = DatasetContract(
        sweeps=(TIME_AXIS,), sweep_units=("ns",), variables=("I", "Q"))
    required_operations: ClassVar[tuple[str, ...]] = ("readout",)

    params: ReadoutTimeOfFlightParameters

    # ------------------------------------------------------------------ frame

    def delay_context(self, target: str) -> dict[str, Any]:
        """The backend's instrument facts for this target's readout delay.

        ``{field, floor_ns, grid_ns, sample_ns, full_scale_v, current_ns}`` —
        the ``power_context`` shape: one duck-typed hook, one dict, vendor keys
        inside. A backend that declares none gets the neutral defaults and the
        hint degrades to naming the manual step; this is NOT swallowed like the
        hint's own call, because the floor decides what the instrument actually
        plays.
        """
        hook = getattr(self.backend, HOOK, None)
        if not callable(hook):
            return {}
        return dict(hook(target) or {})

    def resolved_frame(self) -> dict[str, float]:
        """The window origin every target in this run shares, plus its facts.

        One axis serves the whole run, so the origin is the LOOSEST floor among
        the targets — a window that is legal for every one of them.
        """
        contexts = [self.delay_context(t) for t in self.params.targets]
        floor = max([float(c.get("floor_ns", 0.0)) for c in contexts] or [0.0])
        grid = max([float(c.get("grid_ns", DEFAULT_GRID_NS))
                    for c in contexts] or [DEFAULT_GRID_NS])
        sample = min([float(c.get("sample_ns", DEFAULT_SAMPLE_NS))
                      for c in contexts] or [DEFAULT_SAMPLE_NS])

        requested = float(self.params.window_start_ns)
        if requested and requested < floor:
            raise ValueError(
                f"window_start_ns={requested:g} ns is below this backend's "
                f"acquisition floor of {floor:g} ns for "
                f"{', '.join(self.params.targets)}. Pass 0 to open as early as "
                f"the instrument allows, or a value at or above the floor — it "
                f"is not raised silently, because a stated origin is a promise.")
        return {"window_start_ns": requested or floor,
                "grid_ns": grid or DEFAULT_GRID_NS,
                "sample_ns": sample or DEFAULT_SAMPLE_NS}

    # ------------------------------------------------------------- lifecycle

    def define_sweep(self) -> dict[str, np.ndarray]:
        frame = self.resolved_frame()  # refuse before any instrument time
        sample = frame["sample_ns"]
        n = max(2, int(round(float(self.params.readout_len_ns) / sample)))
        return {TIME_AXIS: np.arange(n, dtype=float) * sample}

    def run(self) -> Result:
        """Acquire, then record the FRAME the trace was taken in.

        The arrival is relative to the window origin, so the origin travels with
        the data rather than being re-derived at analysis time — a re-fit of a
        saved run must not consult a device whose delay has since been changed
        (which is, after all, what this experiment asks the operator to do).
        """
        self.sweep_axes = self.define_sweep()
        self.dataset = self.backend.acquire(self)
        self.Contract.validate(self.dataset)
        frame = self.resolved_frame()
        note_acquisition(self.dataset, FRAME_NOTE, {
            **frame,
            "per_target": {t: self.delay_context(t)
                           for t in self.params.targets},
        })
        return self.run_estimate()

    def simulate(self, coords: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """A step at a plausible arrival, with the plateau riding a fixed phase.

        The delay is drawn per target from the first third of the trace, so the
        edge is always visible and never at a rim — the simulated backend
        exercises the reduction, not the refusals (those have their own tests
        with hand-built traces)."""
        times = np.asarray(coords[TIME_AXIS], dtype=float)
        targets = self.params.targets
        span = float(times[-1] - times[0]) if times.size > 1 else 1.0
        i_data = np.empty((len(targets), times.size))
        q_data = np.empty_like(i_data)
        for k, target in enumerate(targets):
            rng = np.random.default_rng(stable_seed(self.name, target))
            arrival = times[0] + span * rng.uniform(0.12, 0.33)
            rise = max(1.0, span * 0.004)
            amp, phase = rng.uniform(0.08, 0.25), rng.uniform(0.0, 2 * np.pi)
            step = amp / (1.0 + np.exp(-(times - arrival) / rise))
            noise = amp * 0.012
            i_data[k] = step * np.cos(phase) + rng.normal(0, noise, times.size)
            q_data[k] = step * np.sin(phase) + rng.normal(0, noise, times.size)
        return {"I": i_data, "Q": q_data}

    def estimate(self) -> ReadoutTimeOfFlightResult:
        assert self.dataset is not None, "run() populates self.dataset"
        from scqat.estimators.readout_time_of_flight import (
            ReadoutTimeOfFlightEstimator,
        )

        frame = acquisition_note(self.dataset, FRAME_NOTE, {}) or {}
        per_target_context = frame.get("per_target", {}) or {}
        shared = {k: frame.get(k) for k in ("window_start_ns", "grid_ns")}

        per_target_kwargs = {}
        for target in self.params.targets:
            context = per_target_context.get(target, {}) or {}
            per_target_kwargs[target] = {
                **{k: v for k, v in shared.items() if v is not None},
                "full_scale_v": context.get("full_scale_v"),
                "old_delay_ns": context.get("current_ns"),
            }

        results = per_qubit_results(
            self.dataset.transpose("target", TIME_AXIS),
            ReadoutTimeOfFlightEstimator(), artifact_dir=self.artifact_dir,
            per_target_kwargs=per_target_kwargs)

        nan = float("nan")
        result = ReadoutTimeOfFlightResult()
        for target in self.params.targets:
            r = results[target]
            result.fit[target] = {
                key: float(r.get(key, nan)) for key in (
                    "time_of_flight_ns", "arrival_ns", "delta_ns",
                    "window_start_ns", "old_delay_ns", "grid_ns",
                    "rise_time_ns", "plateau_snr",
                    "arrival_unresolved", "arrival_at_edge", "adc_saturated",
                )
            }
            result.outcomes[target] = (Outcome.SUCCESSFUL if r.get("success")
                                       else Outcome.FAILED)
        return result

    def probe(self):  # pragma: no cover - driver half
        raise NotImplementedError("a driver backend supplies probe()")

    def update(self) -> None:
        """Propose nothing; print where the measured delay has to be written.

        There is no neutral knob for this quantity on either backend — both
        fieldmaps declare it vendor-only and say the measurement's product is
        written there offline. Proposing something would hand the calibration
        loop a field it cannot push.
        """
        if self.result is None:
            return
        measured = {
            target: float(fit["time_of_flight_ns"])
            for target, fit in self.result.fit.items()
            if self.result.outcomes.get(target) == Outcome.SUCCESSFUL
            and np.isfinite(fit.get("time_of_flight_ns", float("nan")))
        }
        print_apply_hint(self.name, self.backend, measured)
