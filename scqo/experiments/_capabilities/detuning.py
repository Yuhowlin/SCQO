"""Drive-detuning sweep capability: a swept drive-frequency window on an xy line.

An experiment HAS this capability exactly when its Parameters subclass
:class:`DriveDetuningSweepParameters`; the catalog derives the
``"drive_detuning"`` capability from that subclass relation (never from a
declared string). The capability owns the window Parameters (canonical names +
texts), the canonical sweep-axis name (``DETUNING_AXIS`` — the probe boundary:
both drivers' probes read exactly this axis) and the ``define_sweep`` fragment
(:func:`detuning_sweep`).

ONE FRAME. The window is Hz RELATIVE to the drive frequency the run centers on
— the target's current ``drive_freq_hz`` knob. ``qubit_spectroscopy_cryoscope``
alone refines that origin to the arch-predicted PARKED drive
(``drive_freq_hz + center_offset_hz``) and says so on its re-declared fields;
the frame stays drive-relative either way. The window is an explicit
``[start, end]`` pair rather than a symmetric span because an imperfectly
centered line sits systematically to ONE side, and a one-sided window spends
every point on signal instead of half on empty detuning.

ASCENDING on purpose: ``end_detuning_hz`` must exceed ``start_detuning_hz``
(validated on the mixin). scqat's peak fitters assume an ascending detuning
axis (the gamma bound in ``peak_fit``), and both drivers realize the axis
exactly as given — a descending window would not be a different measurement,
only a broken fit.

The READOUT-side frequency sweeps (``resonator_spectroscopy*``,
``readout_frequency``) share the ``detuning_hz`` axis NAME but their window is
relative to ``readout_freq_hz`` — deliberately NOT this capability; "drive" in
the capability name is the frame declaration.
"""

from __future__ import annotations

import numpy as np
from pydantic import Field, model_validator

from ...parameters import Parameters

#: the canonical swept-axis name every drive-detuning probe emits (Hz, relative
#: to the drive frequency the run centers on).
DETUNING_AXIS = "detuning_hz"

#: canonical field texts — a subclass overriding a DEFAULT re-declares the Field
#: with these constants, optionally APPENDING experiment-specific text (the
#: catalog check is startswith), so the shared wording can never drift.
START_DETUNING_DESC = (
    "LOW edge of the swept drive-detuning window, Hz, relative to the target's "
    "current drive_freq_hz. The window may be ASYMMETRIC (e.g. start=-70e6, "
    "end=0): when the line sits systematically to one side, put the whole "
    "window there instead of wasting half the points on empty detuning."
)
END_DETUNING_DESC = (
    "HIGH edge of the drive-detuning window, Hz, relative to the current "
    "drive_freq_hz (must exceed start_detuning_hz)."
)
NUM_FREQ_POINTS_DESC = "Number of frequency points."


class DriveDetuningSweepParameters(Parameters):
    """Mixin: the swept drive-detuning window (canonical names).

    Hz relative to the current drive frequency, ascending (see the module
    docstring). Defaults are the coarse two-tone search window; a carrier with
    a different natural scale re-declares the Fields with the canonical texts.
    """

    start_detuning_hz: float = Field(-30.0e6, description=START_DETUNING_DESC)
    end_detuning_hz: float = Field(30.0e6, description=END_DETUNING_DESC)
    num_freq_points: int = Field(201, gt=1, description=NUM_FREQ_POINTS_DESC)

    @model_validator(mode="after")
    def _window_ordered(self) -> "DriveDetuningSweepParameters":
        if self.end_detuning_hz <= self.start_detuning_hz:
            raise ValueError(
                f"end_detuning_hz ({self.end_detuning_hz}) must exceed "
                f"start_detuning_hz ({self.start_detuning_hz})"
            )
        return self


def detuning_sweep(params: DriveDetuningSweepParameters) -> dict[str, np.ndarray]:
    """The define_sweep fragment: ``{DETUNING_AXIS: linspace(start, end, n)}``."""
    return {
        DETUNING_AXIS: np.linspace(
            params.start_detuning_hz, params.end_detuning_hz, params.num_freq_points
        )
    }
