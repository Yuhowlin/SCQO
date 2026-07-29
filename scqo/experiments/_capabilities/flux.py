"""Flux-sweep capability: a swept flux-bias window on a z line.

An experiment HAS this capability exactly when its Parameters subclass
:class:`FluxSweepParameters`; the catalog derives the ``"flux"`` tag from that
subclass relation (never from a declared string). The capability owns the window
Parameters (canonical names + the ±0.5 V DAC-rail bounds), the canonical
sweep-axis name (``FLUX_AXIS`` — the probe boundary: LCHQB/LCHQM probes emit and
read exactly this axis), the assignable foreign flux source
(:class:`FluxComponentParameters`), and the record-only ``update()`` guard
(:func:`foreign_flux_source`).

TWO FRAMES, ONE AXIS. The window is volts either way, but the ORIGIN differs and
is decided by the PROBE MECHANISM:

* **absolute** (:class:`FluxSweepParameters`) — the probe SETS the line's DC
  offset to each swept value (QM ``z.set_dc_offset(dc)``), so 0 V is the DAC
  zero and the window is the line voltage itself.
* **relative** (:class:`FluxPulseSweepParameters`) — the probe PLAYS a pulse on
  top of the standing bias (QM ``z.play("const", ...)``), which the DAC adds to
  the idle offset. 0 is then "stay parked" and the window is an excursion
  measured from the flux channel's ``idle_flux`` knob.

A relative carrier MUST end its registered name in ``_pulse`` (pinned by
``tests/test_capabilities.py``), so the frame is legible wherever the name is —
the CLI menu, a run folder, the AI loop's decision surface. The frame is also
recorded per run: every carrier writes ``old_idle_flux`` into its fit, which is
:func:`flux_anchor_v` — 0.0 in the absolute frame, the standing bias in the
relative one. Both keep ``FLUX_AXIS`` as the axis KEY: the frame is an origin,
not a different quantity, and a second key would fork both drivers' probes and
scqat's coords for nothing the name and the recorded origin do not already give.

The FACTS the two frames produce stay absolute in both: a relative carrier
re-references (``absolute = old_idle_flux + fitted``) before writing
``flux_offset``, because that field is a property of the chip's transfer
function, not of where the run happened to be parked.

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

from typing import TYPE_CHECKING

import numpy as np
from pydantic import Field

from ...parameters import Parameters

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...experiment import Experiment

#: the canonical swept-axis name every flux probe emits (volts on the swept line).
FLUX_AXIS = "flux_bias_v"

#: the two origins the window can be measured from — see the module docstring.
#: ``flux_frame(params)`` returns one of these; ``old_idle_flux`` in a fit is the
#: number behind it.
FLUX_FRAME_ABSOLUTE = "absolute"
FLUX_FRAME_RELATIVE = "relative_to_idle_flux"

#: canonical field texts — a subclass overriding a DEFAULT re-declares the Field
#: with these constants, so the catalog text can never drift (test-enforced).
#: The ``_PULSE_`` pair is the RELATIVE frame's wording; the two pairs must stay
#: different (a copy-paste that merged them would erase the frame distinction
#: from the only surface an AI reads).
MIN_FLUX_DESC = "Lowest flux bias (V) on the swept flux line."
MAX_FLUX_DESC = "Highest flux bias (V)."
NUM_FLUX_DESC = "Number of flux points."
MIN_FLUX_PULSE_DESC = (
    "Lowest flux-pulse amplitude (V) RELATIVE to the flux channel's idle_flux "
    "(0 = stay parked at the standing bias)."
)
MAX_FLUX_PULSE_DESC = "Highest flux-pulse amplitude (V) relative to idle_flux."


class FluxSweepParameters(Parameters):
    """Mixin: the swept flux-bias window in ABSOLUTE line volts (canonical
    names, ±0.5 V rail bounds).

    For a probe that SETS the line's DC offset per point. A probe that plays a
    pulse on top of the standing bias takes :class:`FluxPulseSweepParameters`
    instead.
    """

    min_flux_v: float = Field(-0.3, ge=-0.5, description=MIN_FLUX_DESC)
    max_flux_v: float = Field(0.3, le=0.5, description=MAX_FLUX_DESC)
    num_flux_points: int = Field(21, gt=1, description=NUM_FLUX_DESC)


class FluxPulseSweepParameters(FluxSweepParameters):
    """Mixin: the swept flux-PULSE window, RELATIVE to the channel's ``idle_flux``.

    Subclasses the absolute mixin rather than forking it, so the derived
    ``"flux"`` tag and the ``FLUX_AXIS`` contract rule keep covering both frames;
    the extra ``"flux_pulse"`` tag is what distinguishes them in the catalog.
    Only the two window texts are re-declared — ``num_flux_points`` carries no
    frame information and reuses ``NUM_FLUX_DESC``.
    """

    min_flux_v: float = Field(-0.3, ge=-0.5, description=MIN_FLUX_PULSE_DESC)
    max_flux_v: float = Field(0.3, le=0.5, description=MAX_FLUX_PULSE_DESC)


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


def flux_frame(params: FluxSweepParameters) -> str:
    """Which origin this experiment's window is measured from — derived from the
    Parameters mixin, exactly as the catalog tag is, never declared."""
    return (FLUX_FRAME_RELATIVE if isinstance(params, FluxPulseSweepParameters)
            else FLUX_FRAME_ABSOLUTE)


def flux_anchor_v(experiment: "Experiment", target: str) -> float:
    """The origin the swept window is measured from, in the source's native unit.

    ``0.0`` in the absolute frame (the probe sets the offset outright); the
    SWEPT channel's standing ``idle_flux`` in the relative one. Read through
    ``Experiment.anchor``, which already owns "the sweep rides on a standing
    knob": it falls back to ``design.toml`` (tagging the run ``seeded:``) and
    otherwise raises a bring-up instruction rather than fitting around garbage.

    With a foreign ``flux_component`` the bias that matters is the SWEPT
    channel's, not the target's — that run is crosstalk data and its window
    rides on the source line's own idle.
    """
    if flux_frame(experiment.params) == FLUX_FRAME_ABSOLUTE:
        return 0.0
    source = getattr(experiment.params, "flux_component", None) or target
    return experiment.anchor(source, "idle_flux")
