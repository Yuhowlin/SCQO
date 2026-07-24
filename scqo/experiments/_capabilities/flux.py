"""Flux-sweep capability: a swept flux-bias window on a z line.

An experiment HAS this capability exactly when its Parameters subclass
:class:`FluxSweepParameters`; the catalog derives the ``"flux"`` tag from that
subclass relation (never from a declared string). The capability owns the window
Parameters (canonical names + the ±0.5 V DAC-rail bounds), the canonical
sweep-axis name (``FLUX_AXIS`` — the probe boundary: LCHQB/LCHQM probes emit and
read exactly this axis), the assignable foreign flux source
(:class:`FluxComponentParameters`), and the record-only ``update()`` guard
(:func:`foreign_flux_source`).

``pair_zz_coupler`` is deliberately NOT on this capability: it sweeps a pair's
tunable coupler through the ``coupler_bias`` operation and keeps its coupler
naming.

Session._validate_targets stays in ``session.py`` — session must never import
this package (importing any ``scqo.experiments`` submodule triggers the package
``__init__``'s eager import of every experiment module; session stays lazy). The
per-class narrowing of what a foreign source may be remains the
``flux_component_categories`` Experiment ClassVar, which session already reads.
"""

from __future__ import annotations

import numpy as np
from pydantic import Field

from ...parameters import Parameters

#: the canonical swept-axis name every flux probe emits (volts on the swept line).
FLUX_AXIS = "flux_bias_v"

#: canonical field texts — a subclass overriding a DEFAULT re-declares the Field
#: with these constants, so the catalog text can never drift (test-enforced).
MIN_FLUX_DESC = "Lowest flux bias (V) on the swept flux line."
MAX_FLUX_DESC = "Highest flux bias (V)."
NUM_FLUX_DESC = "Number of flux points."


class FluxSweepParameters(Parameters):
    """Mixin: the swept flux-bias window (canonical names, ±0.5 V rail bounds)."""

    min_flux_v: float = Field(-0.3, ge=-0.5, description=MIN_FLUX_DESC)
    max_flux_v: float = Field(0.3, le=0.5, description=MAX_FLUX_DESC)
    num_flux_points: int = Field(21, gt=1, description=NUM_FLUX_DESC)


class FluxComponentParameters(Parameters):
    """Mixin: an assignable foreign flux source (crosstalk / coupler maps)."""

    flux_component: str | None = Field(
        None,
        description="Roster component whose flux line is swept INSTEAD of each "
        "target's own z-line: a qubit name (its z) or a pair name (its tunable "
        "coupler); the experiment's flux_component_categories ClassVar narrows "
        "what is sweepable (validated pre-probe by the Session). None = each "
        "target fluxes itself. With an assigned source the run is RECORD-ONLY "
        "(fits saved, zero suggestions): the fitted quantities then describe "
        "crosstalk or a coupler-induced shift, not the target's own flux "
        "response.",
    )


def flux_sweep(params: FluxSweepParameters) -> dict[str, np.ndarray]:
    """The define_sweep fragment: ``{FLUX_AXIS: linspace(min_flux_v, max_flux_v, n)}``."""
    return {
        FLUX_AXIS: np.linspace(
            params.min_flux_v, params.max_flux_v, params.num_flux_points
        )
    }


def foreign_flux_source(params) -> bool:
    """True when a foreign flux source is assigned — ``update()`` must record-only
    (the fit is crosstalk/coupler data, not the target's own flux response).
    Safe on Parameters without the field (returns False)."""
    return getattr(params, "flux_component", None) is not None
