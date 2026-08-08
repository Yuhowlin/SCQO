"""Which stored gate a ``target_gate`` name belongs to — the ONE deciding place.

The pi/2 is calibrated in its own right, never derived as half the pi, so two
storage pairs exist per drive channel: the pi pair (``pi_amp``/``drag_beta``)
and the x90 pair (``pi_amp_x90``/``drag_beta_x90``). Every experiment that
takes a ``target_gate`` resolves the knob it reads/writes through these
helpers. Normalization must be identical at every layer: three call sites once
spelled it three different ways, and ``target_gate="X90"`` played the x90
sequence while ``update()`` wrote the fitted beta into the x180 knob.
"""

from __future__ import annotations

#: the pi/2 gates. Their amplitude lives on the x90 storage node (y90/-y90 are
#: reference aliases that follow it), so they calibrate the x90 knob pair;
#: everything else is a pi gate and calibrates the pi pair.
X90_GATES = frozenset({"x90", "-x90", "y90", "-y90"})


def normalize_gate(target_gate: str) -> str:
    """The canonical spelling every layer compares against."""
    return str(target_gate).strip().lower()


def is_x90(target_gate: str) -> bool:
    return normalize_gate(target_gate) in X90_GATES


def amp_knob(target_gate: str) -> str:
    """Which neutral drive-channel amplitude knob this target gate calibrates."""
    return "pi_amp_x90" if is_x90(target_gate) else "pi_amp"


def drag_knob(target_gate: str) -> str:
    """Which neutral drive-channel DRAG knob this target gate calibrates."""
    return "drag_beta_x90" if is_x90(target_gate) else "drag_beta"
