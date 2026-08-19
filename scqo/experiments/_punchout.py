"""The punchout's resonator-mode physics, shared by both punchout carriers.

`resonator_spectroscopy_power_amp` (fast amplitude sweep) and
`..._chain` (careful chain-stepped sweep) are two MECHANISMS for one measurement,
so the standing readout knobs they propose and the physics they extract are
identical — only how the power axis is realized differs. This module owns the
half that is the same, so the two carriers cannot drift apart.

WHAT A PUNCHOUT MEASURES BEYOND THE READOUT SETTINGS: at low readout power the
qubit stays in |0> and dresses its resonator, so the dip sits at ``f_dress0``.
Driven hard enough the qubit saturates and stops dressing it, and the dip walks
to the BARE resonator ``f_bare``. That makes the punchout the one experiment that
measures the bare frequency DIRECTLY — a dispersive flux fit can only trade
``f_r0`` off against ``g`` along a degenerate valley, which is exactly why
`resonator_spectroscopy_flux` pins a stored ``f_bare_hz`` when it has one.

The loop this closes runs both ways:
``power_amp -> flux -> power_amp``. The first punchout hands the flux map a
measured ``f_bare_hz``; the flux map re-parks ``idle_flux`` at the sweet spot; a
second punchout then measures ``f_dress0_hz`` at that NEW idle point.
``old_idle_flux`` recorded on each run is what tells the two apart — the dressed
frequency is only meaningful together with the flux it was measured at, while
``f_bare_hz`` is flux-independent and durable.
"""

from __future__ import annotations

from typing import Any, Dict

import numpy as np

from ._capabilities.flux import standing_flux_v
from ._transmon_estimate import g_coeff_from_g, g_hz_from_pull

#: where a punchout's coupling came from, recorded per target in ``fit``.
G_PUNCHOUT = "punchout"   # the run's own Lamb shift + the standing drive freq
G_NONE = "none"           # no calibrated drive frequency (or an unphysical pull)


def coupling_fit(experiment, results: Dict[str, Any], target: str) -> Dict[str, Any]:
    """The coupling a punchout measures, when the qubit frequency is known.

    ``g = sqrt(lamb_shift * (f_bare - f_q))`` with ``f_q`` the target's standing
    ``drive_freq_hz``. Everything but that one number comes from THIS run's two
    branches, which makes this route independent of the flux map's arch fit —
    and specifically of the ``f_r0``-pin systematic that moves the flux fit's g
    by ~13% (see ``RELEASES.d/flux-design-g-seed-rescale.toml``). On 5Q4C the
    two routes agree to 0.2-2.4% in ``g_coeff``, inside their own run-to-run
    scatter.

    THE ASSUMPTION, stated because nothing can check it: ``drive_freq_hz`` must
    be the qubit frequency at the flux this punchout ran at. Both are current
    device state, so they agree unless the flux moved between calibrating the
    drive and running this sweep — ``old_idle_flux`` in the same fit is the
    audit trail.

    Withheld (NaN, ``g_source = "none"``, nothing proposed) when there is no
    finite standing drive frequency — the bring-up case — or when the pull and
    the detuning disagree in sign, which means the data is not a dispersive
    pull at all.
    """
    nan = float("nan")
    withheld = {"g_hz": nan, "g_coeff": nan, "g_source": G_NONE}
    lamb = results.get("lamb_shift")
    f_bare = results.get("f_bare")
    if lamb is None or f_bare is None:
        return withheld
    if not (np.isfinite(lamb) and np.isfinite(f_bare)):
        return withheld
    try:
        f_q = float(experiment.anchor(target, "drive_freq_hz"))
    except (ValueError, KeyError, AttributeError):
        return withheld
    if not (np.isfinite(f_q) and f_q > 0.0):
        return withheld
    try:
        g = g_hz_from_pull(float(lamb), float(f_bare), f_q)
        coeff = g_coeff_from_g(g, f_q, float(f_bare))
    except ValueError:
        return withheld  # not a dispersive pull, or a nonsensical frequency
    return {"g_hz": g, "g_coeff": coeff, "g_source": G_PUNCHOUT}


def branch_fit(experiment, results: Dict[str, Any], target: str) -> Dict[str, Any]:
    """The punchout's physical extras for ``result.fit[target]``.

    ``f_bare_hz`` / ``f_dress0_hz`` are catalog facts on the resonator mode;
    everything else is record-only provenance. ``lamb_shift_hz`` because it is
    derivable from the two facts; ``old_idle_flux`` because it is the condition
    ``f_dress0_hz`` was measured under, not a measurement of its own; and the
    plateau boundary powers ``dress_max_power_dbm`` / ``bare_min_power_dbm``
    (the highest power that is still dispersive and the lowest that is fully
    punched out; NaN when that branch did not resolve) because a port power is
    a property of THIS setup's output chain, never of the chip.
    """
    return {
        "f_bare_hz": float(results["f_bare"]),
        "f_dress0_hz": float(results["f_dress0"]),
        "lamb_shift_hz": float(results["lamb_shift"]),
        "dress_max_power_dbm": float(results["dress_max_power"]),
        "bare_min_power_dbm": float(results["bare_min_power"]),
        "branch_success": bool(results["branch_success"]),
        "old_idle_flux": standing_flux_v(experiment, target),
        # ...and the coupling the same two branches imply, when the qubit
        # frequency is known (see coupling_fit).
        **coupling_fit(experiment, results, target),
    }


def propose_branches(experiment, target: str, fit: Dict[str, Any]) -> None:
    """Write the resolved branch frequencies + coupling onto the RESONATOR mode.

    Per-field rather than all-or-nothing: a punchout whose window only reached the
    dispersive regime still measured ``f_dress0_hz`` honestly, and withholding it
    because the bare branch was out of range would discard good physics. A branch
    that did not resolve is NaN and is simply not proposed — same for the
    coupling pair, which needs both branches AND a known drive frequency.
    """
    res_view = experiment.device.component(experiment.device.resonator_of(target))
    for field in ("f_bare_hz", "f_dress0_hz", "g_hz", "g_coeff"):
        value = fit.get(field)
        if value is not None and np.isfinite(value):
            setattr(res_view, field, float(value))
