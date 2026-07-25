"""Greenfield ``flux_component`` mixin — kind-agnostic foreign flux source.

The old shared mixin's field text described pair-name coupler routing and
the deleted ``flux_component_categories`` ClassVar; the greenfield gate is
channel-existence (any entity with a default flux channel — a qubit, a
tracked coupler mode). The old mixin's text is test-enforced against the
old stack, so the greenfield ports carry their own until the final cutover
relocates the capability modules.
"""

from __future__ import annotations

from pydantic import Field

from ..parameters import Parameters


class FluxComponentParameters(Parameters):
    """Mixin: an assignable foreign flux source (crosstalk / coupler maps)."""

    flux_component: str | None = Field(
        None,
        description="Roster entity whose flux channel is swept INSTEAD of "
        "each target's own z-line — any entity with a default flux channel "
        "(a qubit, a tracked coupler mode like q1_q2_c). None = each target "
        "fluxes itself. With a foreign source the run is RECORD-ONLY (fits "
        "saved, zero suggestions): the fitted quantities then describe "
        "crosstalk or a coupler-induced shift, not the target's own flux "
        "response.",
    )
