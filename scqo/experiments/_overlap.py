"""The saturation/readout anchor arithmetic: THE one point, both backends.

``qubit_spectroscopy`` plays a finite saturation drive and then measures. Where
the drive sits relative to the readout is one boolean, ``readout_overlap``, and
the rule is the same sentence in both states:

    THE DRIVE **ENDS** AT AN ANCHOR; IT STARTS ``drive_len_ns`` EARLIER.

    readout_overlap = False              readout_overlap = True
    [==== drive ====]                       [======== drive ========]
                    [## readout ##]   [## readout tone ############]
                    ^ anchor                                anchor ^
                                            [acq_start_ns][== ADC ==]

``False`` is the QM ``align()`` written out: the drive is over before the
readout tone starts, so the line is measured with no readout photons present —
which is what the experiment assumes when it relies on T1 outlasting the
readout. ``True`` ends the drive with the tone instead, so the ADC window (which
sits at the tone's tail, after ``acq_start_ns``) is guaranteed to be covered by
the drive, and what comes back is the line under measurement conditions.

NOTHING BOUNDS ``drive_len_ns`` AGAINST THE TONE. A 20 us drive against a 2 us
tone simply starts 18 us before the tone and runs through it. That is why this
module hands out LEADS rather than a refusal: exactly one of them is non-zero,
and each backend spends it on whichever element starts second — a ``wait()`` on
QM, a non-negative ``rel_time`` on Qblox (Qblox subschedules never get
``_normalize_absolute_timing``, so a negative one is not an option).

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
        "How long the readout tone runs BEFORE the ADC starts integrating, ns "
        "(multiple of 4). The readout pulse is lengthened by this much for the "
        "run so the standing readout_integration_s window still fits inside it; "
        "the readout_duration_s knob is never written. Only meaningful with "
        "readout_overlap=true (a non-zero value is refused otherwise, because "
        "with the drive already over it would just push the ADC into the tone). "
        "Raise it past the resonator's filling time and the qubit's driven "
        "settling time to integrate a steady state."
    ),
    "drive_len_ns": (
        "Saturation-drive length in ns (multiple of 4). The drive ENDS at the "
        "readout tone's START when readout_overlap=false, and at its END when "
        "readout_overlap=true; either way it begins this long before that "
        "anchor. It is not bounded by the tone — a drive longer than the tone "
        "simply starts before it."
    ),
}


@dataclass(frozen=True)
class OverlapWindows:
    """One target's resolved timing, all in ns. Probes emit these numbers
    directly and derive nothing themselves."""

    #: total readout tone length = acq_start_ns + the readout_duration_s knob
    tone_len_ns: float
    #: ADC integration onset, from the tone onset (backend TOF is on top)
    acq_start_ns: float
    #: saturation-drive length, straight from the params
    drive_len_ns: float
    #: the standing readout_integration_s knob, for the probes that need it
    integration_ns: float
    #: how much LATER the readout tone starts than the drive (drive is longer)
    drive_lead_ns: float
    #: how much LATER the drive starts than the readout tone (tone is longer)
    readout_lead_ns: float


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
            f"{target}: {field} has never been set, so the readout window is "
            f"undefined. Set it (`scqo set {target}.{field}=...`) or run the "
            f"readout calibration that proposes it, then re-run."
        )
    return float(value) * 1e9


def overlap_windows(experiment, target: str) -> OverlapWindows:
    """Resolve one target's drive/readout timing.

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

    drive_len_ns = _on_grid("drive_len_ns", experiment.params.drive_len_ns, target)

    # exactly one of these is non-zero (both are 0 when the two are equal); the
    # element that starts SECOND spends it, which keeps every backend offset
    # non-negative.
    return OverlapWindows(
        tone_len_ns=tone_len_ns,
        acq_start_ns=acq_start_ns,
        drive_len_ns=drive_len_ns,
        integration_ns=integration_ns,
        drive_lead_ns=max(0.0, drive_len_ns - tone_len_ns),
        readout_lead_ns=max(0.0, tone_len_ns - drive_len_ns),
    )
