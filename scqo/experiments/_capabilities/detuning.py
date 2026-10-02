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
share ``start_flux_v``. Flux can share because :class:`FluxPulseSweepParameters`
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

THE PAIR IS A TRAVERSAL ORDER: the probe walks ``start`` -> ``end``, in either
direction, and the dataset keeps the order it walked (decided 2026-09-26).
Consecutive points are not always independent, so the direction is the caller's
choice and must stay visible when debugging; a resonator driven hard enough to
go bistable (Duffing, a high-power punchout) genuinely answers differently
swept up than swept down. Both drivers play a descending axis as given — QM's
``from_array`` branches on the step sign, Qblox's loop domain emits a ``SUB``
for a negative step.

This used to be the opposite rule: the edges took either order but only
defined a window, and ``_window_sweep`` normalised the axis ASCENDING. That hid
a scqat bug rather than making a physics choice — ``tools/peak_fit.py`` built
its width bound as ``detuning[-1] - detuning[0]``, so on a descending axis a
4 MHz line came back 174 MHz wide with no flag, a value bound for ``f_01_hz``.
scqat now fixes that at the source: its tools take spans by value, and every
estimator that reads this axis canonicalizes it on entry
(``scqat.tools.sweep_order``), pinned by a "the same data reversed gives the
same answer" test. So the order changes what the instrument did, never a fitted
number — and a range check here must still ask ``window_bounds`` rather than
chain ``start <= x <= end``.

Only a ZERO-WIDTH window is refused: two identical edges are a typo, not a
measurement. The mechanism (``refuse_zero_width`` + ``window_bounds``) lives one
level up in ``.._window``, shared with the flux and amplitude windows.
"""

from __future__ import annotations

import numpy as np
from pydantic import Field, model_validator

from ...parameters import Parameters
from .._window import refuse_zero_width

#: the canonical swept-axis name every detuning probe emits, in EITHER frame
#: (Hz, relative to the frequency the run centers on).
DETUNING_AXIS = "detuning_hz"

#: canonical field texts — a subclass overriding a DEFAULT re-declares the Field
#: with these constants, optionally APPENDING experiment-specific text (the
#: catalog check is startswith), so the shared wording can never drift.
START_DRIVE_DETUNING_DESC = (
    "First drive detuning of the sweep, Hz, relative to the target's current "
    "drive_freq_hz. The window may be ASYMMETRIC (e.g. -70e6 to 0): when the "
    "line sits systematically to one side, put the whole window there instead "
    "of wasting half the points on empty detuning. The probe walks "
    "start_drive_detuning_hz -> end_drive_detuning_hz IN THAT ORDER, either "
    "direction, and the data keeps it; the fitted result does not depend on it."
)
END_DRIVE_DETUNING_DESC = (
    "Last drive detuning of the sweep, Hz, relative to the current "
    "drive_freq_hz. May be above or below start_drive_detuning_hz; only a "
    "zero-width window (both edges equal) is refused."
)
START_READOUT_DETUNING_DESC = (
    "First readout detuning of the sweep, Hz, relative to the target's current "
    "readout_freq_hz. The window may be ASYMMETRIC (e.g. -25e6 to 5e6): a "
    "punchout walks the dip DOWN from the dressed resonator toward the bare "
    "one, and a flux map walks it down as the qubit detunes, so putting the "
    "whole window on that side spends every point on signal instead of half "
    "above the dip. The probe walks start_readout_detuning_hz -> "
    "end_readout_detuning_hz IN THAT ORDER, either direction, and the data "
    "keeps it (sweep down vs up to see a bistable resonator); the fitted result "
    "does not depend on it."
)
END_READOUT_DETUNING_DESC = (
    "Last readout detuning of the sweep, Hz, relative to the current "
    "readout_freq_hz. May be above or below start_readout_detuning_hz; only a "
    "zero-width window (both edges equal) is refused."
)
#: shared by both frames — a point count carries no frame information, exactly
#: as NUM_FLUX_DESC is shared by the two flux frames.
NUM_FREQ_POINTS_DESC = "Number of frequency points."


class DriveDetuningSweepParameters(Parameters):
    """Mixin: the swept DRIVE-detuning window (canonical names).

    Hz relative to the current drive frequency; the two edges are a traversal
    order, either direction (see the module docstring). Defaults are the coarse two-tone
    search window; a carrier with a different natural scale re-declares the
    Fields with the canonical texts.
    """

    start_drive_detuning_hz: float = Field(-30.0e6, description=START_DRIVE_DETUNING_DESC)
    end_drive_detuning_hz: float = Field(30.0e6, description=END_DRIVE_DETUNING_DESC)
    num_drive_freq_points: int = Field(201, gt=1, description=NUM_FREQ_POINTS_DESC)

    @model_validator(mode="after")
    def _drive_window_spans(self) -> "DriveDetuningSweepParameters":
        refuse_zero_width(
            self.start_drive_detuning_hz, self.end_drive_detuning_hz,
            start_name="start_drive_detuning_hz", end_name="end_drive_detuning_hz",
            points_name="num_drive_freq_points")
        return self


class ReadoutDetuningSweepParameters(Parameters):
    """Mixin: the swept READOUT-detuning window (canonical names).

    Hz relative to the current readout frequency; the two edges are a
    traversal order, either direction (see the module docstring). Defaults are the
    resonator-spectroscopy window every punchout and flux map inherited;
    ``readout_frequency`` re-declares them at its own chi scale.

    A SIBLING of :class:`DriveDetuningSweepParameters`, never a subclass — see
    the module docstring on why the field names carry the frame.
    """

    start_readout_detuning_hz: float = Field(
        -10.0e6, description=START_READOUT_DETUNING_DESC)
    end_readout_detuning_hz: float = Field(
        10.0e6, description=END_READOUT_DETUNING_DESC)
    num_readout_freq_points: int = Field(101, gt=1, description=NUM_FREQ_POINTS_DESC)

    @model_validator(mode="after")
    def _readout_window_spans(self) -> "ReadoutDetuningSweepParameters":
        refuse_zero_width(
            self.start_readout_detuning_hz, self.end_readout_detuning_hz,
            start_name="start_readout_detuning_hz", end_name="end_readout_detuning_hz",
            points_name="num_readout_freq_points")
        return self


def _window_sweep(start: float, end: float, num: int) -> dict[str, np.ndarray]:
    """The one axis both frames emit: ``start`` -> ``end`` in THAT order.

    An origin is not a different quantity, so both frames share it. The
    direction is the caller's and is never normalised here (module docstring).
    """
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
