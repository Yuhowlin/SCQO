"""Coupler-flux capability: a swept flux-PULSE window on a pair's tunable coupler.

An experiment HAS this capability exactly when its Parameters subclass
:class:`CouplerFluxPulseSweepParameters`; the catalog derives ``"coupler_flux"``
from that subclass relation (never from a declared string). The capability owns the
window Parameters (canonical names and texts), the swept-axis name
(``COUPLER_FLUX_AXIS``) and the window's origin (:func:`coupler_anchor_v`).

ONE FRAME ONLY: RELATIVE. The probe PLAYS a pulse on the coupler's flux line on top
of the standing bias, so 0 is "stay at the coupler's ``idle_flux``" and the window
is an excursion from it - the same mechanism as
:class:`~scqo.experiments._capabilities.flux.FluxPulseSweepParameters`, on a
different line. There is no absolute sibling, so every carrier ends its registered
name in ``_pulse`` (pinned by ``tests/test_capabilities.py``) and records the origin
as ``old_coupler_idle_flux``. A fact written from the window is re-referenced
first (``absolute = old_coupler_idle_flux + fitted``): ``flux_offset`` on the
coupler's flux channel is a property of the chip, not of where the coupler was
parked.

Why not the ``flux`` capability: that one is the TARGET's own z-line window
(``flux_bias_v``, a qubit or a foreign ``flux_component``), and its axis name is the
probe boundary both drivers read. A pair experiment sweeping its coupler would need
a pair-to-coupler hop behind that name; a separate capability keeps the two lines
apart in the catalog, the contract and the probe.

THE WINDOW IS A TRAVERSAL ORDER, as on every capability window: the probe walks
``start_coupler_flux_v`` -> ``end_coupler_flux_v`` either way and the dataset keeps
that order; scqat is direction-blind. Only a zero-width window is refused
(``.._window``). No rail bound here - the coupler port's rail, and the sum of the
standing bias and the excursion, are the backend's to refuse by name.

``pair_swap_flux_map`` and ``pair_swap_chevron`` also pulse the coupler, but predate
this capability and keep their own ``min/max_coupler_flux_v`` fields.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
from pydantic import Field, model_validator

from ...parameters import Parameters
from .._window import refuse_zero_width

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ...experiment import Experiment

#: the swept-axis name: volts of the coupler pulse, relative to the coupler idle.
COUPLER_FLUX_AXIS = "coupler_flux_v"

#: canonical field texts - a carrier overriding a DEFAULT re-declares the Field with
#: these constants, so the catalog text can never drift (test-enforced).
START_COUPLER_FLUX_DESC = (
    "First coupler flux-pulse amplitude (V) of the sweep, RELATIVE to the "
    "coupler's idle_flux (0 = the coupler stays at its standing bias). The probe "
    "walks start_coupler_flux_v -> end_coupler_flux_v IN THAT ORDER, either "
    "direction, and the data keeps it; the fitted result does not depend on it."
)
END_COUPLER_FLUX_DESC = (
    "Last coupler flux-pulse amplitude (V) of the sweep, relative to the coupler's "
    "idle_flux. May be above or below start_coupler_flux_v; only a zero-width "
    "window is refused. The backend refuses a window its coupler port cannot emit "
    "on top of the standing bias, naming the reachable range."
)
NUM_COUPLER_FLUX_DESC = "Number of coupler flux points."


class CouplerFluxPulseSweepParameters(Parameters):
    """Mixin: the swept coupler flux-PULSE window, RELATIVE to the coupler's
    ``idle_flux`` (canonical names). A traversal order; see the module docstring."""

    start_coupler_flux_v: float = Field(-0.1, description=START_COUPLER_FLUX_DESC)
    end_coupler_flux_v: float = Field(0.1, description=END_COUPLER_FLUX_DESC)
    num_coupler_flux_points: int = Field(21, gt=1, description=NUM_COUPLER_FLUX_DESC)

    @model_validator(mode="after")
    def _coupler_window_spans(self) -> "CouplerFluxPulseSweepParameters":
        refuse_zero_width(
            self.start_coupler_flux_v, self.end_coupler_flux_v,
            start_name="start_coupler_flux_v", end_name="end_coupler_flux_v",
            points_name="num_coupler_flux_points", quantity="coupler flux")
        return self


def coupler_flux_sweep(params: CouplerFluxPulseSweepParameters) -> dict[str, np.ndarray]:
    """The define_sweep fragment: ``{COUPLER_FLUX_AXIS: linspace(start, end, n)}``
    - in THAT order, descending when ``start_coupler_flux_v > end_coupler_flux_v``."""
    return {
        COUPLER_FLUX_AXIS: np.linspace(
            params.start_coupler_flux_v, params.end_coupler_flux_v,
            params.num_coupler_flux_points)
    }


def pair_coupler(roster, pair: str) -> str:
    """The coupler MODE a pair declares (its ``coupler`` role), or raise by name."""
    couplers = (getattr(roster.entities.get(pair), "roles", {}) or {}).get("coupler", ())
    if not couplers:
        raise ValueError(f"{pair}: declares no coupler role - there is no coupler "
                         f"flux line to pulse")
    return couplers[0]


def coupler_anchor_v(experiment: "Experiment", pair: str) -> float:
    """The origin the window is measured from: the pair's coupler ``idle_flux``.

    Read through ``Experiment.anchor`` (pair -> coupler role -> the coupler's flux
    channel), which falls back to ``design.toml`` (tagging the run ``seeded:``) and
    otherwise raises a bring-up instruction rather than fitting around garbage.
    """
    return experiment.anchor(pair_coupler(experiment.device.roster, pair), "idle_flux")
