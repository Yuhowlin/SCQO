"""Drive-line capability: play the drive through a NAMED line's channel.

An experiment HAS this capability exactly when its Parameters subclass
:class:`DriveLineParameters`; the catalog derives the ``"drive_line"`` capability
from that subclass relation (never from a declared string).

The default, ``drive_line=None``, is the target's own DESIGNED drive channel -
the behaviour every carrier had before, byte for byte. Naming a line drives the
target through the channel ``<drive_line>.<target>`` instead, and that channel
may be BORROWED: every drive line reaches every drivable mode it does not carry
by design (``xy2.q1_q2_c`` - the coupler q1_q2_c through q2's xy wire), realized
only once the vendor config ADOPTS it (QM: ``scqo-qm adopt-channel``). The
channel's knobs - ``pi_amp``, ``drive_freq_hz``, ``pi_duration_s`` - are read
and written ON IT, so a coupler's pi pulse is calibrated on the route that
plays it, and a second route to the same mode keeps its own numbers.

Two helpers resolve the channel for every carrier: :func:`drive_owner` for a
READ through :meth:`Experiment.anchor` (a name the roster resolves) and
:func:`drive_view` for a WRITE in ``update()``. Without the field both fall back
to the qubit shorthand / the designed default channel, exactly as before.

The Session's roster gate reads the field BY NAME (``session.py`` never imports
the experiments package), the way it reads ``flux_component``: a named line
drops ``rx`` from the required operations, the channel ``<line>.<target>`` must
exist in the roster, and the backend must realize it - all before any run folder
or instrument time. Design: ``docs/coupler-transmon-plan.md``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import Field, model_validator

from ...parameters import Parameters

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...experiment import Experiment

DRIVE_LINE_DESC = (
    "Roster line whose channel plays the drive, when that is not the target's own "
    "designed drive channel: the channel <drive_line>.<target>, which may be "
    "BORROWED (xy2 drives the coupler q1_q2_c through q2's xy wire as xy2.q1_q2_c; "
    "the vendor config must have adopted it - on QM, scqo-qm adopt-channel). Its "
    "knobs (pi_amp, drive_freq_hz, pi_duration_s ...) are read and written on that "
    "channel. None = the target's designed drive channel. One target per run."
)


class DriveLineParameters(Parameters):
    """Mixin: the line whose channel drives the target (None = its designed one)."""

    drive_line: str | None = Field(None, description=DRIVE_LINE_DESC)

    @model_validator(mode="after")
    def _one_named_line(self) -> "DriveLineParameters":
        line = self.drive_line
        if line is None:
            return self
        if not line or "." in line:
            raise ValueError(
                f"drive_line={line!r}: name the LINE only (e.g. 'xy2'); the "
                f"channel is <drive_line>.<target> and the target comes from "
                f"targets")
        targets = list(getattr(self, "targets", None) or [])
        if len(targets) != 1:
            raise ValueError(
                f"drive_line={line!r} drives ONE target per run; got {targets}")
        return self


def drive_owner(experiment: "Experiment", target: str) -> str:
    """The roster name a drive-knob READ resolves through: ``<line>.<target>``
    when a drive line is named, else ``target`` itself (the qubit shorthand,
    which lands on the designed drive channel - today's read, unchanged)."""
    line = getattr(experiment.params, "drive_line", None)
    return target if line is None else f"{line}.{target}"


def drive_view(experiment: "Experiment", target: str):
    """The channel view a drive-knob WRITE lands on: the named line's channel,
    designed or borrowed, else the target's designed default drive channel."""
    line = getattr(experiment.params, "drive_line", None)
    if line is None:
        return experiment.device.channel(target, "drive")
    return experiment.device.channel_on(line, target)
