"""Mapped-readout capability: read a mode THROUGH a pair member.

A mode with no readout resonator of its own - a tunable coupler - is read by
copying its state onto a member of its pair and reading that member:

1. a SELECTIVE pi on the member: a square pulse ``selective_pi_len_ns`` long with
   the member's calibrated x180 rotation area, narrower than the member-coupler
   ZZ, so it flips the member only while the coupler sits in |0>;
2. the member's x180, so the member ends EXCITED exactly when the coupler is;
3. the member's discriminated readout.

The member's population then RISES with the coupler's excitation - offset by the
map's misses, scaled by its contrast - so an estimator fitting a free amplitude
and offset reads the coupler through it unchanged. The trailing x180 also keeps
the member out of |0> whenever the coupler is excited, which is the only state in
which the member's readout sees the coupler directly (5Q4C q1 sees q1_q2_c). The
selective pi is ``pair_coupler_spectroscopy_zz``'s, hardware-proven on 5Q4C on
2026-09-27; design and numbers: ``docs/coupler-transmon-plan.md`` section 1.2.

An experiment HAS this capability exactly when its Parameters subclass
:class:`MappedReadoutParameters`; the catalog derives ``"mapped_readout"``.
``readout_member=None`` is the target's own readout - the carrier's behaviour
before, byte for byte.

v1 limits, each refused by name before any instrument time: ONE target per run
(the map occupies the member's drive and readout) and the member is not a target
itself; discriminated acquisition only (an I/Q axial reduction would need the
MEMBER's blob centres); thermal reset only (an active reset would reset the
member, not the coupler); no per-run ``thermalization_time_ns`` (the wait is the
member's own ``thermalization_time_s``). The Session's roster gate checks the
pair: the target must be the coupler of a composite the member belongs to, and
the member must carry ``rx`` and ``readout``.
"""

from __future__ import annotations

import numpy as np
from pydantic import Field, model_validator

from ...parameters import Parameters

READOUT_MEMBER_DESC = (
    "Read the target THROUGH this pair member: the target must be the coupler of a "
    "pair the member belongs to. A selective pi plus the member's x180 copy the "
    "coupler's state onto the member, which is then read (discriminated), so the "
    "member's population rises with the coupler's excitation. None = the target's "
    "own readout. One target per run; needs use_state_discrimination; thermal "
    "reset only."
)

#: shared with pair_coupler_spectroscopy_zz, whose selective pi this is
SELECTIVE_PI_LEN_DESC = (
    "Length of the pi member's SELECTIVE pi (ns, multiple of 4): a square "
    "pulse with the calibrated x180's area, so its bandwidth is ~1/length. It has to "
    "be narrower than the qubit-coupler ZZ (5Q4C: 0.3-0.9 MHz) yet short against the "
    "coupler's T1 (5Q4C: ~5 us), which decays during it; 2 us leaves 0.67-0.83 of "
    "the coupler occupation as dip. Longer for a smaller ZZ."
)

#: the offline model of the map: the member's population for a coupler population
#: p is MAP_OFFSET + MAP_SWING * p (5Q4C q1: misses + readout error ~0.15; the
#: selective pi's contrast 0.80 times its ZZ selectivity ~0.75)
MAP_OFFSET = 0.15
MAP_SWING = 0.6


class MappedReadoutParameters(Parameters):
    """Mixin: read the target through a pair member (None = its own readout)."""

    readout_member: str | None = Field(None, description=READOUT_MEMBER_DESC)
    selective_pi_len_ns: int = Field(
        2000, ge=16, multiple_of=4, description=SELECTIVE_PI_LEN_DESC)

    @model_validator(mode="after")
    def _mapped_readout_limits(self) -> "MappedReadoutParameters":
        member = self.readout_member
        if member is None:
            return self
        needs: list[str] = []
        targets = list(getattr(self, "targets", None) or [])
        if len(targets) != 1:
            needs.append(f"ONE target per run (got {targets})")
        elif member in targets:
            needs.append(f"a target other than the member {member!r} itself")
        if getattr(self, "use_state_discrimination", True) is not True:
            needs.append("use_state_discrimination=true (the member is read "
                         "discriminated; an I/Q reduction would need the member's "
                         "blob centres)")
        if getattr(self, "reset_method", "thermal") != "thermal":
            needs.append("reset_method='thermal' (an active reset resets the "
                         "member, not the coupler)")
        if getattr(self, "thermalization_time_ns", None) is not None:
            needs.append("no thermalization_time_ns override (the wait is the "
                         "member's own thermalization_time_s)")
        if needs:
            raise ValueError(
                f"readout_member={member!r} needs: " + "; ".join(needs))
        return self


def mapped_population(population: np.ndarray) -> np.ndarray:
    """The member population the offline map reports for a coupler population."""
    return MAP_OFFSET + MAP_SWING * np.asarray(population, dtype=float)
