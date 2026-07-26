"""Qubit-reset capability: how a target returns to |g> between shots.

An experiment HAS this capability exactly when its Parameters subclass
:class:`QubitResetParameters`; the catalog derives the ``"qubit_reset"`` tag
from that subclass relation (never from a declared string). Every experiment
that pulses a qubit and reads it out needs one, because shot-to-shot
independence is the assumption its averaging rests on.

Two methods exist: ``"thermal"`` (wait out the qubit's own relaxation) and
``"active"`` (measure, then play a pi pulse only if it came back excited).
Active reset is ~100x faster on a real chip, and it is why the discriminator
knobs are worth calibrating for their own sake — but it is NOT universally
realizable, so see the BOUNDARY RULE below.

WHERE THE WAIT LIVES (thermal only): the standing value is the neutral knob
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

AND: a backend or experiment that cannot realize the requested method must
REFUSE IT BY NAME — never silently downgrade to thermal. A method the caller
asked for and did not get is the one failure this field cannot survive: the run
completes, the data looks plausible, and only the wall clock disagrees. Today
``"active"`` is realized on the Qblox backend, on the four coherent-drive
carriers whose readout condition is fixed for the whole run; everything else
raises. That asymmetry is deliberate and is documented at each refusal site,
not here — this module owns the vocabulary, not the per-backend policy.
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
    "qubit_relaxation). 'active' measures the target and plays a pi pulse only "
    "if it came back excited — far faster, but it REQUIRES a calibrated readout "
    "discriminator (run single_shot_readout, then accept its "
    "readout_rotation_rad / readout_threshold) and is refused by name wherever "
    "it cannot be realized, never silently downgraded to thermal."
)
THERMALIZATION_TIME_DESC = (
    "Per-run override of the thermal-reset wait, ns. None = use the standing "
    "thermalization_time_s knob on each target's drive channel (the normal "
    "case); set it to shorten or lengthen the wait for THIS run only, without "
    "disturbing device state. reset_method='thermal' only — combining it with "
    "'active' is refused rather than ignored."
)
ACTIVE_RESET_ROUNDS_DESC = (
    "Correction attempts per shot when reset_method='active'; each attempt is "
    "one measurement plus a conditional pi pulse. NOT symmetric across "
    "backends: Qblox plays EXACTLY this many rounds, paying a full readout for "
    "each whether or not the qubit was already in |g>, because its conditional "
    "playback cannot exit a loop early; a repeat-until-success backend would "
    "read the same number as an upper bound. Raise it to 2 if an active run's "
    "contrast is worse than its thermal reference."
)
ACTIVE_RESET_DEPLETION_DESC = (
    "Settle wait AFTER the last conditional pi pulse, ns. It protects the "
    "experiment's NEXT pulse from the readout photons still ringing in the "
    "resonator — not the pi pulse itself, which no backend lets you delay "
    "(the Qblox trigger latency is a fixed 364 ns). 0 disables. The symptom of "
    "too little is a Stark shift on the first pulse: an active run's fitted "
    "frequency drifts from its thermal reference. reset_method='active' only."
)


class QubitResetParameters(Parameters):
    """Mixin: how the qubit is reset between shots (thermal wait, or active)."""

    reset_method: Literal["thermal", "active"] = Field(
        "thermal", description=RESET_METHOD_DESC
    )
    thermalization_time_ns: float | None = Field(
        None, gt=0, description=THERMALIZATION_TIME_DESC
    )
    active_reset_rounds: int = Field(1, ge=1, le=15, description=ACTIVE_RESET_ROUNDS_DESC)
    active_reset_depletion_ns: float = Field(
        1000.0, ge=0, description=ACTIVE_RESET_DEPLETION_DESC
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
