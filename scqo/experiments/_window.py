"""A swept window's two edges, ``start_*`` and ``end_*`` — the helpers they share.

The pair is a TRAVERSAL ORDER on every capability window — flux
(``_capabilities/flux.py``), detuning (``_capabilities/detuning.py``) and
amplitude (``_capabilities/amplitude.py``): the probe walks ``start`` -> ``end``,
in either direction, and the dataset keeps the order it walked (decided
2026-09-26). Consecutive points are not always independent — heating, a
long flux tail, hysteresis, a bistable resonator — so the direction is the
caller's choice and must stay visible in the stored data when debugging. It must
NOT change a fitted number, and it cannot: scqat's estimators canonicalize a swept
axis on entry (``scqat.tools.sweep_order``), each pinned by a "the same data
reversed gives the same answer" test. The detuning module docstring carries the
history — why this layer used to normalise ascending, and what it hid.

Two helpers, neither of which builds an axis:

* :func:`refuse_zero_width` — the one refusal every window keeps: two identical
  edges are a typo, not a measurement. It belongs on the Parameters (a pydantic
  ``@model_validator``), so it fires at construction rather than at
  ``define_sweep``.
* :func:`window_bounds` — the pair as ``(low, high)`` BY VALUE, for a RANGE
  question ("did the fitted line land inside the window?"). A chained
  ``start <= x <= end`` is silently always-False on a descending pair.

One family still normalises: the parametric-drive pair
(``qubit_parametric_drive_amp`` / ``_time``) builds its absolute volt, Hz and
nanosecond axes ASCENDING through :func:`window_bounds`, its edges taking either
order but defining only a window. It predates the traversal rule and is tracked
as its own BACKLOG entry; do not copy it into a new window.
"""

from __future__ import annotations


def window_bounds(start: float, end: float) -> tuple[float, float]:
    """The window as ``(low, high)`` — the two edges by value.

    For a range check only ("is the fitted line inside the window?"), never to
    build an axis: the capability windows sweep ``start`` -> ``end`` as given.
    """
    return (start, end) if start <= end else (end, start)


def refuse_zero_width(start: float, end: float, *, start_name: str, end_name: str,
                      points_name: str, quantity: str = "frequency") -> None:
    """Refuse two identical edges, naming both fields and the point count.

    Called from a Parameters ``@model_validator(mode="after")``, so a degenerate
    window is rejected where it is typed rather than deep inside a sweep. Every
    other pair is legal, in either direction — see the module docstring.
    """
    if start == end:
        raise ValueError(
            f"{start_name} and {end_name} are both {start} — a zero-width window "
            f"measures one {quantity} {points_name} times. Give two different "
            f"edges (either direction)."
        )
