"""The punchout's resonator-mode physics, shared by both punchout carriers.

`resonator_spectroscopy_power_amp` (fast amplitude sweep) and
`..._chain` (careful chain-stepped sweep) are two MECHANISMS for one measurement,
so the operating-point knobs they propose and the physics they extract are
identical — only how the power axis is realized differs. This module owns the
half that is the same, so the two carriers cannot drift apart.

WHAT A PUNCHOUT MEASURES BEYOND THE OPERATING POINT: at low readout power the
qubit stays in |0> and dresses its resonator, so the dip sits at ``f_dress0``.
Driven hard enough the qubit saturates and stops dressing it, and the dip walks
to the BARE resonator ``f_bare``. That makes the punchout the one experiment that
measures the bare frequency DIRECTLY — a dispersive flux fit can only trade
``f_r0`` off against ``g`` along a degenerate valley, which is exactly why
`resonator_spectroscopy_flux` pins a stored ``f_bare_hz`` when it has one.

The loop this closes runs both ways:
``power_amp -> flux -> power_amp``. The first punchout hands the flux map a
measured ``f_bare_hz``; the flux map re-parks ``idle_flux`` at the sweet spot; a
second punchout then measures ``f_dress0_hz`` at that NEW operating point.
``old_idle_flux`` recorded on each run is what tells the two apart — the dressed
frequency is only meaningful together with the flux it was measured at, while
``f_bare_hz`` is flux-independent and durable.
"""

from __future__ import annotations

from typing import Any, Dict

import numpy as np

from ._capabilities.flux import standing_flux_v


def branch_fit(experiment, results: Dict[str, Any], target: str) -> Dict[str, Any]:
    """The punchout's physical extras for ``result.fit[target]``.

    ``f_bare_hz`` / ``f_dress0_hz`` are catalog facts on the resonator mode;
    ``lamb_shift_hz`` and ``old_idle_flux`` are record-only provenance — the first
    because it is derivable from the two facts, the second because it is the
    condition ``f_dress0_hz`` was measured under, not a measurement of its own.
    """
    return {
        "f_bare_hz": float(results["f_bare"]),
        "f_dress0_hz": float(results["f_dress0"]),
        "lamb_shift_hz": float(results["lamb_shift"]),
        "branch_success": bool(results["branch_success"]),
        "old_idle_flux": standing_flux_v(experiment, target),
    }


def propose_branches(experiment, target: str, fit: Dict[str, Any]) -> None:
    """Write the resolved branch frequencies onto the target's RESONATOR mode.

    Per-field rather than all-or-nothing: a punchout whose window only reached the
    dispersive regime still measured ``f_dress0_hz`` honestly, and withholding it
    because the bare branch was out of range would discard good physics. A branch
    that did not resolve is NaN and is simply not proposed.
    """
    res_view = experiment.device.component(experiment.device.resonator_of(target))
    for field in ("f_bare_hz", "f_dress0_hz"):
        value = fit.get(field)
        if value is not None and np.isfinite(value):
            setattr(res_view, field, float(value))
