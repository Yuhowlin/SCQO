"""Detuning-sweep capabilities: a swept frequency window, in two FRAMES.

TWO FRAMES, ONE AXIS. The window is Hz either way and both frames emit
``DETUNING_AXIS`` — what differs is the ORIGIN the numbers are measured from,
and the origin is decided by which LINE the sweep retunes:

* **drive** (:class:`DriveDetuningSweepParameters`) — relative to the target's
  current ``drive_freq_hz``, the xy line. Derives the ``"drive_detuning"``
  capability.
* **readout** (:class:`ReadoutDetuningSweepParameters`) — relative to the
  target's current ``readout_freq_hz``, the readout line. Derives the
  ``"readout_detuning"`` capability.

An experiment HAS a capability exactly when its Parameters subclass the
corresponding mixin; the catalog derives both from that subclass relation (never
from a declared string). Each frame owns its window Parameters (canonical names
+ texts) and its ``define_sweep`` fragment (:func:`drive_detuning_sweep` /
:func:`readout_detuning_sweep`); the axis name is shared, because a frame is an
origin and not a different quantity — the probe boundary is that both drivers'
probes read exactly ``DETUNING_AXIS``.

THE FRAME IS IN THE FIELD NAME (``start_drive_detuning_hz`` vs
``start_readout_detuning_hz``), unlike the flux capability's two frames, which
share ``min_flux_v``. Flux can share because :class:`FluxPulseSweepParameters`
SUBCLASSES the absolute mixin — one window, refined. These two are independent
siblings that a single experiment could legitimately carry at once (a
drive x readout frequency map), and shared names would then MERGE by MRO into
one number silently driving both sweeps. Such a carrier still has to define its
own two axes and its own ``define_sweep``, since ``DETUNING_AXIS`` is one key —
but its Parameters are safe by construction, and
``tests/test_capabilities.py`` pins the two field sets DISJOINT.

The window is an explicit ``[start, end]`` pair rather than a symmetric span
because a line that sits systematically to ONE side wastes half a centred
window on empty detuning. On the drive side that is an imperfectly centred qubit
line; on the readout side it is the physics of both power and flux sweeps, which
walk the resonator dip DOWN from ``f_dress0`` toward ``f_bare``.

ASCENDING on purpose, enforced per frame: the end must exceed the start. scqat's
peak fitters assume an ascending detuning axis (the gamma bound in ``peak_fit``),
the readout estimators build ``full_freq = detuning + <center>`` on top of it,
and both drivers realize the axis exactly as given — a descending window would
not be a different measurement, only a broken fit.
"""

from __future__ import annotations

import numpy as np
from pydantic import Field, model_validator

from ...parameters import Parameters

#: the canonical swept-axis name every detuning probe emits, in EITHER frame
#: (Hz, relative to the frequency the run centers on).
DETUNING_AXIS = "detuning_hz"

#: canonical field texts — a subclass overriding a DEFAULT re-declares the Field
#: with these constants, optionally APPENDING experiment-specific text (the
#: catalog check is startswith), so the shared wording can never drift.
START_DRIVE_DETUNING_DESC = (
    "LOW edge of the swept drive-detuning window, Hz, relative to the target's "
    "current drive_freq_hz. The window may be ASYMMETRIC (e.g. start=-70e6, "
    "end=0): when the line sits systematically to one side, put the whole "
    "window there instead of wasting half the points on empty detuning."
)
END_DRIVE_DETUNING_DESC = (
    "HIGH edge of the drive-detuning window, Hz, relative to the current "
    "drive_freq_hz (must exceed start_drive_detuning_hz)."
)
START_READOUT_DETUNING_DESC = (
    "LOW edge of the swept readout-detuning window, Hz, relative to the "
    "target's current readout_freq_hz. The window may be ASYMMETRIC (e.g. "
    "start=-25e6, end=5e6): a punchout walks the dip DOWN from the dressed "
    "resonator toward the bare one, and a flux map walks it down as the qubit "
    "detunes, so putting the whole window on that side spends every point on "
    "signal instead of half above the dip."
)
END_READOUT_DETUNING_DESC = (
    "HIGH edge of the readout-detuning window, Hz, relative to the current "
    "readout_freq_hz (must exceed start_readout_detuning_hz)."
)
#: shared by both frames — a point count carries no frame information, exactly
#: as NUM_FLUX_DESC is shared by the two flux frames.
NUM_FREQ_POINTS_DESC = "Number of frequency points."


class DriveDetuningSweepParameters(Parameters):
    """Mixin: the swept DRIVE-detuning window (canonical names).

    Hz relative to the current drive frequency, ascending (see the module
    docstring). Defaults are the coarse two-tone search window; a carrier with
    a different natural scale re-declares the Fields with the canonical texts.
    """

    start_drive_detuning_hz: float = Field(-30.0e6, description=START_DRIVE_DETUNING_DESC)
    end_drive_detuning_hz: float = Field(30.0e6, description=END_DRIVE_DETUNING_DESC)
    num_drive_freq_points: int = Field(201, gt=1, description=NUM_FREQ_POINTS_DESC)

    @model_validator(mode="after")
    def _drive_window_ordered(self) -> "DriveDetuningSweepParameters":
        if self.end_drive_detuning_hz <= self.start_drive_detuning_hz:
            raise ValueError(
                f"end_drive_detuning_hz ({self.end_drive_detuning_hz}) must exceed "
                f"start_drive_detuning_hz ({self.start_drive_detuning_hz})"
            )
        return self


class ReadoutDetuningSweepParameters(Parameters):
    """Mixin: the swept READOUT-detuning window (canonical names).

    Hz relative to the current readout frequency, ascending (see the module
    docstring). Defaults are the resonator-spectroscopy window every punchout
    and flux map inherited; ``readout_frequency`` re-declares them at its own
    chi scale.

    A SIBLING of :class:`DriveDetuningSweepParameters`, never a subclass — see
    the module docstring on why the field names carry the frame.
    """

    start_readout_detuning_hz: float = Field(
        -10.0e6, description=START_READOUT_DETUNING_DESC)
    end_readout_detuning_hz: float = Field(
        10.0e6, description=END_READOUT_DETUNING_DESC)
    num_readout_freq_points: int = Field(101, gt=1, description=NUM_FREQ_POINTS_DESC)

    @model_validator(mode="after")
    def _readout_window_ordered(self) -> "ReadoutDetuningSweepParameters":
        if self.end_readout_detuning_hz <= self.start_readout_detuning_hz:
            raise ValueError(
                f"end_readout_detuning_hz ({self.end_readout_detuning_hz}) must exceed "
                f"start_readout_detuning_hz ({self.start_readout_detuning_hz})"
            )
        return self


def _window_sweep(start: float, end: float, num: int) -> dict[str, np.ndarray]:
    """The one axis both frames emit — an origin is not a different quantity."""
    return {DETUNING_AXIS: np.linspace(start, end, num)}


def drive_detuning_sweep(params: DriveDetuningSweepParameters) -> dict[str, np.ndarray]:
    """The drive frame's define_sweep fragment."""
    return _window_sweep(params.start_drive_detuning_hz,
                         params.end_drive_detuning_hz,
                         params.num_drive_freq_points)


def readout_detuning_sweep(params: ReadoutDetuningSweepParameters) -> dict[str, np.ndarray]:
    """The readout frame's define_sweep fragment."""
    return _window_sweep(params.start_readout_detuning_hz,
                         params.end_readout_detuning_hz,
                         params.num_readout_freq_points)
