"""Qubit-reset capability: how a target returns to |g> between shots.

An experiment HAS this capability exactly when its Parameters subclass
:class:`QubitResetParameters`; the catalog derives the ``"qubit_reset"`` tag
from that subclass relation (never from a declared string). Every experiment
that pulses a qubit and reads it out needs one, because shot-to-shot
independence is the assumption its averaging rests on.

Only ``"thermal"`` (passive relaxation) exists today. The lab's other method,
active reset, widens the :attr:`QubitResetParameters.reset_method` Literal and
adds its own fields — nothing here is renamed when it lands.

WHERE THE WAIT LIVES: the standing value is the neutral knob
``thermalization_time_s`` on each target's DRIVE channel (``q1_xy``) — role
``knob``, so it is stored in scqo_state.json and pushed to the vendor
(QM: ``q.thermalization_time_ns``; Qblox: ``element.reset.duration``).
``qubit_relaxation`` proposes ``thermalization_factor x t1_s`` for it, and it is
settable by hand (``scqo set q1.thermalization_time_s=2e-4``) before any T1
exists. The Parameters field is a per-run OVERRIDE only.

BOUNDARY RULE: drivers resolve the wait through :func:`reset_wait_ns` and never
re-implement the precedence — one point of truth, so the per-run override can
never mean different things on QM and Qblox. ``reset_method`` crosses the probe
boundary verbatim (it maps onto QM's own ``reset_type`` vocabulary), so neither
side renames it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ...parameters import Parameters

#: canonical field texts — a subclass overriding a DEFAULT re-declares the Field
#: with these constants, so the catalog text can never drift (test-enforced).
RESET_METHOD_DESC = (
    "How each target is returned to |g> before a shot. 'thermal' waits out the "
    "qubit's own relaxation (the wait is the drive channel's "
    "thermalization_time_s knob, proposed as a multiple of the measured T1 by "
    "qubit_relaxation)."
)
THERMALIZATION_TIME_DESC = (
    "Per-run override of the thermal-reset wait, ns. None = use the standing "
    "thermalization_time_s knob on each target's drive channel (the normal "
    "case); set it to shorten or lengthen the wait for THIS run only, without "
    "disturbing device state."
)


class QubitResetParameters(Parameters):
    """Mixin: how the qubit is reset between shots (thermal wait today)."""

    reset_method: Literal["thermal"] = Field("thermal", description=RESET_METHOD_DESC)
    thermalization_time_ns: float | None = Field(
        None, gt=0, description=THERMALIZATION_TIME_DESC
    )


def reset_wait_ns(experiment, target: str) -> float:
    """The resolved thermal-reset wait for one target, in ns.

    THE one precedence point, called by every driver probe: the per-run
    ``thermalization_time_ns`` override when set, else the standing
    ``thermalization_time_s`` knob on the target's drive channel (seconds ->
    ns). Raises the device's own "no value yet" ``KeyError`` when neither
    exists — a clear bring-up instruction (``scqo set
    <target>.thermalization_time_s=...``) beats silently resetting for zero.
    """
    override = getattr(experiment.params, "thermalization_time_ns", None)
    if override is not None:
        return float(override)
    standing = experiment.device.channel(target, "drive").thermalization_time_s
    return float(standing) * 1e9
