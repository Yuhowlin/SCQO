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

THE EDGES MAY BE GIVEN IN EITHER ORDER; the emitted axis is always ASCENDING.
The pair DEFINES the window, it does not choose a traversal direction: writing
``start=20e6, end=-80e6`` is the same measurement as ``-80e6 -> 20e6``, since
``np.linspace`` over either ordering visits the identical point set, and which
end an NCO retunes to first changes nothing physical.

The normalisation is a deliberate choice at THIS layer, not a hardware limit.
Both drivers can realize a descending sweep — QM's ``from_array`` branches on
the step sign and Qblox's loop domain emits a ``SUB`` opcode for a negative one
(both verified) — but scqat cannot yet READ one: ``tools/peak_fit.py`` builds
its Lorentzian width bound as ``detuning[-1] - detuning[0]`` with no ``abs()``
(``dip_fit.py`` does take it, which is why only the qubit side was exposed), so
a descending axis inverts the bound and lmfit seeds onto a zero-gradient corner.
It raises NOTHING: measured on identical data, a 4 MHz line came back as a
174 MHz one, with centres wrong by up to 17 MHz and no failure flag — a value
that would be written straight to ``f_01_hz``. The sibling
``fit_notch_circle.py`` inverts a ``minimize_scalar`` bound the same way, but
raises. Normalising at the source means no estimator, plotter or driver can
receive a descending axis at all, so neither defect is reachable from here.

If a real need for direction ever appears — Duffing bistability in a high-power
punchout, where up- and down-sweeps genuinely differ — the door is those two
``abs()`` calls in scqat plus a scqat floor, not a change here.

Only a ZERO-WIDTH window is refused: two identical edges are a typo, not a
measurement.
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
    "One edge of the swept drive-detuning window, Hz, relative to the target's "
    "current drive_freq_hz. The window may be ASYMMETRIC (e.g. -70e6 to 0): "
    "when the line sits systematically to one side, put the whole window there "
    "instead of wasting half the points on empty detuning. The two edges may "
    "be given in EITHER order — they define the window, not a sweep direction, "
    "and the axis is always swept ascending."
)
END_DRIVE_DETUNING_DESC = (
    "The other edge of the drive-detuning window, Hz, relative to the current "
    "drive_freq_hz. May be above or below start_drive_detuning_hz; only a "
    "zero-width window (both edges equal) is refused."
)
START_READOUT_DETUNING_DESC = (
    "One edge of the swept readout-detuning window, Hz, relative to the "
    "target's current readout_freq_hz. The window may be ASYMMETRIC (e.g. "
    "-25e6 to 5e6): a punchout walks the dip DOWN from the dressed resonator "
    "toward the bare one, and a flux map walks it down as the qubit detunes, "
    "so putting the whole window on that side spends every point on signal "
    "instead of half above the dip. The two edges may be given in EITHER "
    "order — they define the window, not a sweep direction, and the axis is "
    "always swept ascending."
)
END_READOUT_DETUNING_DESC = (
    "The other edge of the readout-detuning window, Hz, relative to the "
    "current readout_freq_hz. May be above or below "
    "start_readout_detuning_hz; only a zero-width window (both edges equal) "
    "is refused."
)
#: shared by both frames — a point count carries no frame information, exactly
#: as NUM_FLUX_DESC is shared by the two flux frames.
NUM_FREQ_POINTS_DESC = "Number of frequency points."


class DriveDetuningSweepParameters(Parameters):
    """Mixin: the swept DRIVE-detuning window (canonical names).

    Hz relative to the current drive frequency; the two edges may be given in
    either order (see the module docstring). Defaults are the coarse two-tone
    search window; a carrier with a different natural scale re-declares the
    Fields with the canonical texts.
    """

    start_drive_detuning_hz: float = Field(-30.0e6, description=START_DRIVE_DETUNING_DESC)
    end_drive_detuning_hz: float = Field(30.0e6, description=END_DRIVE_DETUNING_DESC)
    num_drive_freq_points: int = Field(201, gt=1, description=NUM_FREQ_POINTS_DESC)

    @model_validator(mode="after")
    def _drive_window_spans(self) -> "DriveDetuningSweepParameters":
        if self.end_drive_detuning_hz == self.start_drive_detuning_hz:
            raise ValueError(
                f"start_drive_detuning_hz and end_drive_detuning_hz are both "
                f"{self.start_drive_detuning_hz} — a zero-width window measures "
                f"one frequency num_drive_freq_points times. Give two different "
                f"edges (either order)."
            )
        return self


class ReadoutDetuningSweepParameters(Parameters):
    """Mixin: the swept READOUT-detuning window (canonical names).

    Hz relative to the current readout frequency; the two edges may be given in
    either order (see the module docstring). Defaults are the
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
        if self.end_readout_detuning_hz == self.start_readout_detuning_hz:
            raise ValueError(
                f"start_readout_detuning_hz and end_readout_detuning_hz are both "
                f"{self.start_readout_detuning_hz} — a zero-width window measures "
                f"one frequency num_readout_freq_points times. Give two different "
                f"edges (either order)."
            )
        return self


def window_bounds(start: float, end: float) -> tuple[float, float]:
    """The window as ``(low, high)`` — the edges in either order, normalised.

    THE one place the pair is ordered. An estimator asking "did the fitted line
    land inside the window?" must use this, never a chained ``start <= x <=
    end``, which is silently always-False on a reversed pair.
    """
    return (start, end) if start <= end else (end, start)


def _window_sweep(start: float, end: float, num: int) -> dict[str, np.ndarray]:
    """The one axis both frames emit — ALWAYS ASCENDING, edges in either order.

    An origin is not a different quantity, and neither is a traversal
    direction: the normalisation here is what guarantees no estimator, plotter
    or driver ever sees a descending axis (module docstring).
    """
    low, high = window_bounds(start, end)
    return {DETUNING_AXIS: np.linspace(low, high, num)}


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
