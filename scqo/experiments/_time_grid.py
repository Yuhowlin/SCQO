"""Swept TIME axes in whole nanoseconds — the neutral instrument-grid promise.

Every ``*_ns`` sweep axis an experiment declares is a time an instrument has to
realize, and no instrument resolves finer than a nanosecond. ``np.linspace`` over
floats does not know that: ``linspace(16, 100000, 51)`` steps by 1999.68 ns and
48 of its 51 points are fractional. QM never noticed — its shells re-round every
axis to clock cycles (``np.round(wait_ns / 4)``, ``/ 8`` for echo's two arms) —
but ``qblox_scheduler`` enforces the grid and REFUSES the schedule
(``GRID_TIME = 1`` ns, ``GRID_TIME_TOLERANCE_TIME = 0.0011`` ns), so a fractional
axis crashes at compile time with a raw vendor error.

WHY UNIFORM, NOT JUST ROUNDED: the Qblox probes rebuild the axis from its
endpoints — ``linspace(axis[0] * 1e-9, axis[-1] * 1e-9, axis.size)``. A
per-point-rounded axis is no longer evenly spaced, so that rebuild re-expands it
straight back off the grid. The axis has to be uniform AND integral: snap the
start, snap the STEP, and generate ``start + step * arange(n)``.

CONTRACT: whole nanoseconds is the NEUTRAL promise, and it is the coarsest thing
every backend can honour exactly. Drivers still quantize further to their own
hardware grid (QM 4 ns clock cycles, 8 ns for the echo arms) — this helper does
not try to be that grid, because there is no single one.
"""

from __future__ import annotations

import math

import numpy as np


def time_axis_ns(start_ns: float, stop_ns: float, num_points: int,
                 *, grid_ns: int = 1) -> np.ndarray:
    """A swept time axis of ``num_points`` times, every one a whole multiple of
    ``grid_ns`` nanoseconds, starting at or after ``start_ns``.

    The window is treated as a bound, not a target: the start rounds UP and the
    step rounds DOWN, so every point lies inside ``[start_ns, stop_ns]``. The
    last point therefore lands up to one step short of ``stop_ns`` — losing
    < 1 step of range beats silently waiting longer than the maximum the caller
    asked for.

    ``grid_ns=2`` is for the ECHO family: those probes split the total idle time
    into two equal arms (Qblox literally schedules ``rel_time=tau / 2``), and an
    odd total would hand the instrument a half-nanosecond. The arm time, not the
    total, is what has to be realizable.

    Raises ``ValueError`` when the window cannot hold ``num_points`` distinct
    points at this grid. That request is already degenerate today (QM would
    collapse it into duplicate clock counts and the fit would see repeated
    x-values), so saying so is better than returning a broken axis.
    """
    num_points = int(num_points)
    grid_ns = int(grid_ns)
    if num_points < 1:
        raise ValueError(f"num_points={num_points} must be >= 1")
    if grid_ns < 1:
        raise ValueError(f"grid_ns={grid_ns} must be >= 1")

    start = math.ceil(float(start_ns) / grid_ns) * grid_ns
    if num_points == 1:
        return np.array([start], dtype=float)

    span = float(stop_ns) - start
    step = math.floor(span / (num_points - 1) / grid_ns) * grid_ns
    if step < grid_ns:
        widest = start + (num_points - 1) * grid_ns
        raise ValueError(
            f"a {span:.0f} ns window cannot hold {num_points} points on the "
            f"{grid_ns} ns instrument time grid: widen the window to at least "
            f"{widest:.0f} ns or reduce num_points to "
            f"{max(1, int(span // grid_ns) + 1)}"
        )
    return start + step * np.arange(num_points, dtype=float)
