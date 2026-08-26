"""A swept window is an UNORDERED PAIR — and this is where it gets ordered.

An experiment's swept window arrives as two edges, ``start_*`` and ``end_*``.
The pair DEFINES the window; it does not choose a traversal direction. Writing
``start=300e6, end=50e6`` is the same measurement as ``50e6 -> 300e6``:
``np.linspace`` over either ordering visits the identical point set, and which
end an instrument retunes to first changes nothing physical. So both orders are
accepted, and the emitted axis is ALWAYS ASCENDING.

The normalisation is a choice at THIS layer, not a hardware limit — both drivers
can realize a descending sweep. It is here because scqat cannot yet READ one:
``tools/peak_fit.py`` builds its Lorentzian width bound as ``x[-1] - x[0]`` with
no ``abs()``, so a descending axis inverts the bound and lmfit seeds onto a
zero-gradient corner, silently. The full argument — including the measured
damage (a 4 MHz line returned as a 174 MHz one, no failure flag) and where the
door out would be — lives in the module docstring of
``_capabilities/detuning.py``, which is the capability this rule was written
for. Read it there rather than re-deriving it; this module is only the
mechanism, shared by carriers that are NOT detuning frames (the parametric-drive
family sweeps absolute volts, absolute Hz and nanoseconds).

Only a ZERO-WIDTH window is refused: two identical edges are a typo, not a
measurement. That refusal belongs on the Parameters (a pydantic
``@model_validator``), so it fires at construction rather than at
``define_sweep`` — :func:`refuse_zero_width` is the shared body.
"""

from __future__ import annotations


def window_bounds(start: float, end: float) -> tuple[float, float]:
    """The window as ``(low, high)`` — the edges in either order, normalised.

    THE one place the pair is ordered. An estimator asking "did the fitted line
    land inside the window?" must use this, never a chained ``start <= x <=
    end``, which is silently always-False on a reversed pair.
    """
    return (start, end) if start <= end else (end, start)


def refuse_zero_width(start: float, end: float, *, start_name: str, end_name: str,
                      points_name: str, quantity: str = "frequency") -> None:
    """Refuse two identical edges, naming both fields and the point count.

    Called from a Parameters ``@model_validator(mode="after")``, so a degenerate
    window is rejected where it is typed rather than deep inside a sweep. Every
    other ordering is legal — see the module docstring.
    """
    if start == end:
        raise ValueError(
            f"{start_name} and {end_name} are both {start} — a zero-width window "
            f"measures one {quantity} {points_name} times. Give two different "
            f"edges (either order)."
        )
