"""The concurrent drive+readout window: THE one arithmetic point, both backends.

``qubit_spectroscopy_overlap`` runs the saturation drive and the readout tone
TOGETHER and lets the ADC integrate only after both have been on a while. Three
times describe that, and every one of them is derived here rather than in a
probe, so QM and Qblox cannot drift apart on what the same Parameters mean:

    t = 0                                     acq_start_ns
     |                                             |
     v                                             v
     ############## readout tone ###########################  tone_len_ns
     [========= saturation drive =========]                   drive_len_ns
                                           [==== ADC ====]    integration_ns

THE TWO TONES ARE CO-STARTED BY CONSTRUCTION. There is deliberately no
drive-offset field: both go up at ``t = 0`` (right after the reset), which is
what each backend's natural idiom already gives — a zero-duration
``VoltageOffset`` latched immediately before the ``Measure`` on Qblox, a shared
``align()`` on QM. Nothing is ever emitted at a negative time and neither probe
needs an offset wait.

WHY THE TONE IS LONGER THAN THE READOUT KNOB: ``acq_start_ns`` delays the ADC,
so the readout PULSE has to grow by the same amount or the standing
``readout_integration_s`` window would run off its end. That growth is a per-run
STIMULUS realized by a vendor override (Qblox ``Measure(pulse_duration=...)``,
QM a pre-tone played back-to-back into ``measure()``); it never writes the
``readout_duration_s`` knob — the discipline of :mod:`._drive_power`.

THE 4 ns GRID IS NOT ARBITRARY. It is already SCQO's own neutral readout grid
(``readout_duration_s`` in catalog.py: "positive multiple of 4 ns; drivers
refuse off-grid") and it is the QM clock cycle, which is the coarsest grid of
the two backends. Off-grid values are REFUSED, not snapped: snapping would let
the two backends realize different timings from one Parameters object, which is
exactly the class of silent divergence this module exists to prevent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: the instrument grid every time in this module lands on (QM clock cycle;
#: also catalog.py's stated multiple for readout_duration_s).
GRID_NS = 4

#: canonical field text, re-declared by the experiment that offers these, so the
#: catalog descriptions cannot drift per-backend — the shape of
#: ``_depletion.READOUT_DEPLETION_NS_DESC``.
OVERLAP_FIELD_DESCS = {
    "acq_start_ns": (
        "How long the readout tone and the saturation drive both run BEFORE the "
        "ADC starts integrating, ns (multiple of 4). The readout pulse is "
        "lengthened by this much for the run so the standing "
        "readout_integration_s window still fits inside it; the readout_duration_s "
        "knob is never written. 0 = the ADC opens with the readout pulse (cable "
        "delay aside). Raise it past the resonator's filling time and the qubit's "
        "driven settling time to integrate a steady state."
    ),
    "drive_len_ns": (
        "Saturation-drive length in ns (multiple of 4), measured from the shared "
        "tone onset. None = run for the whole readout tone (full overlap), which "
        "is the normal case. It may not outlast the tone."
    ),
}


@dataclass(frozen=True)
class OverlapWindows:
    """One target's resolved concurrent-tone timing, all in ns from the shared
    onset. Probes emit these numbers directly and derive nothing themselves."""

    #: total readout tone length = acq_start_ns + the readout_duration_s knob
    tone_len_ns: float
    #: ADC integration onset, from the shared tone onset (backend TOF is on top)
    acq_start_ns: float
    #: resolved saturation-drive length (params value, or the whole tone)
    drive_len_ns: float
    #: the standing readout_integration_s knob, for the probes that need it
    integration_ns: float


def _on_grid(name: str, value: float, target: str) -> float:
    """``value`` in ns, refused unless it is a whole multiple of ``GRID_NS``."""
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{target}: {name}={value!r} is not a finite number of ns")
    nearest = round(value / GRID_NS) * GRID_NS
    if abs(value - nearest) > 1e-6:
        raise ValueError(
            f"{target}: {name}={value:g} ns is off the {GRID_NS} ns instrument "
            f"time grid (QM clock cycle). Use {nearest:g} ns. Off-grid values are "
            f"refused rather than snapped, because the two backends would round "
            f"them differently and realize different timings."
        )
    return float(nearest)


def _knob_ns(experiment, target: str, field: str) -> float:
    """A readout-channel duration knob in ns, refusing an uncalibrated one."""
    value = getattr(experiment.device.channel(target, "readout"), field)
    if value is None or not math.isfinite(float(value)):
        raise ValueError(
            f"{target}: {field} has never been set, so the concurrent readout "
            f"window is undefined. Set it (`scqo set {target}.{field}=...`) or "
            f"run the readout calibration that proposes it, then re-run."
        )
    return float(value) * 1e9


def overlap_windows(experiment, target: str) -> OverlapWindows:
    """Resolve one target's concurrent drive+readout windows.

    THE one precedence point, called by every driver probe of this family.
    Reads the neutral knobs through the target's READOUT CHANNEL — never the
    vendor tree — and refuses, naming the target, anything a backend could only
    realize by silently changing what the caller asked for.
    """
    acq_start_ns = _on_grid("acq_start_ns", experiment.params.acq_start_ns, target)
    if acq_start_ns < 0:
        raise ValueError(f"{target}: acq_start_ns={acq_start_ns:g} ns must be >= 0")

    readout_ns = _knob_ns(experiment, target, "readout_duration_s")
    integration_ns = _knob_ns(experiment, target, "readout_integration_s")
    tone_len_ns = acq_start_ns + readout_ns

    requested = getattr(experiment.params, "drive_len_ns", None)
    if requested is None:
        drive_len_ns = tone_len_ns  # full overlap: the drive spans the whole tone
    else:
        drive_len_ns = _on_grid("drive_len_ns", requested, target)
        if drive_len_ns > tone_len_ns:
            raise ValueError(
                f"{target}: drive_len_ns={drive_len_ns:g} ns outlasts the "
                f"{tone_len_ns:g} ns readout tone (acq_start_ns={acq_start_ns:g} + "
                f"readout_duration_s={readout_ns:g} ns). The drive has to end "
                f"inside the tone it overlaps — shorten it, or raise "
                f"acq_start_ns / readout_duration_s."
            )

    return OverlapWindows(
        tone_len_ns=tone_len_ns,
        acq_start_ns=acq_start_ns,
        drive_len_ns=drive_len_ns,
        integration_ns=integration_ns,
    )
