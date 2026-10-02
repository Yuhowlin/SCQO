"""The time-of-flight operator hint: WHERE the measured delay still has to go.

The readout delay is VENDOR-ONLY on both shipped backends and both fieldmaps
already say so in as many words — QM's ``time_of_flight`` (ns) and Qblox's
``readout_acq_delay`` (s) each carry *"The TOF measurement's product is written
HERE ... offline - never a neutral field"*. So ``readout_time_of_flight``
proposes NOTHING: there is no neutral knob to propose, and inventing one would
give the loop a field it cannot push.

That leaves the same gap the cryoscopes have between a green fit and a vendor
config that has not moved, and the same answer: print the command. The shape is
:mod:`._distortion_hint`, with one difference. A cryoscope asks the backend for
a whole command string because applying taps is a real operation with its own
CLI. A delay is one number in one field, so the backend only has to say WHICH
field (``readout_delay_context``) and the rest is read from the inventory
``scqo state --fields`` already renders — :meth:`scqo.backend.Backend.vendor_only`.
The fieldmap therefore stays the single source of truth for the path, the unit
and the edit instruction, and this module never learns either vendor's spelling.

Unit conversion is the one thing this module does own: the measurement is in
nanoseconds and the field may be in seconds, so the printed value is converted
to the ``VendorOnly.unit`` the target field declares and refuses to guess at
anything else.

Lines take the CLI meta shape — ASCII, ``#``-prefixed, on STDERR — so a
``scqo run ... | jq`` pipeline keeps parsing.
"""

from __future__ import annotations

import sys
from typing import Any, Sequence, TextIO

#: the backend hook naming the vendor field and its instrument facts.
HOOK = "readout_delay_context"

#: ns -> the unit a VendorOnly entry declares. Anything else is refused rather
#: than guessed: printing a number in the wrong unit is worse than printing none.
_PER_NS = {"ns": 1.0, "s": 1e-9}


def delay_in_unit(delay_ns: float, unit: str) -> float | None:
    """``delay_ns`` expressed in ``unit``, or None when the unit is unknown."""
    factor = _PER_NS.get((unit or "").strip())
    return None if factor is None else float(delay_ns) * factor


def _format(value: float, unit: str) -> str:
    return f"{value:.0f} {unit}" if unit == "ns" else f"{value:.9g} {unit}"


def apply_hint_lines(experiment: str, backend: Any,
                     measured: dict[str, float]) -> list[str]:
    """The hint as LINES — pure, so tests read these and not a stream.

    ``measured`` maps target -> the absolute delay in ns, for the targets whose
    fit succeeded. An empty mapping prints nothing: a run that measured nothing
    has nothing to tell the operator to write.
    """
    if not measured:
        return []
    lines = [
        f"# {experiment}: the measured delay is VENDOR-ONLY - nothing was",
        "#   proposed and nothing was pushed. Write it into the vendor config:",
    ]
    context_of = getattr(backend, HOOK, None)
    inventory = {}
    vendor_only = getattr(backend, "vendor_only", None)
    if callable(vendor_only):
        try:
            inventory = vendor_only() or {}
        except Exception:
            inventory = {}

    for target, delay_ns in measured.items():
        context = None
        if callable(context_of):
            try:
                context = context_of(target)
            except Exception as err:  # a hint must never break a measurement
                lines.append(f"#     {target}: backend {HOOK} failed - "
                             f"{type(err).__name__}: {err}")
                continue
        field = (context or {}).get("field")
        spec = inventory.get(field) if field else None
        if spec is None:
            lines.append(f"#     {target}: this backend declares no {HOOK} - set")
            lines.append(f"#         its readout delay to {delay_ns:.0f} ns by hand")
            continue

        unit = getattr(spec, "unit", "") or ""
        value = delay_in_unit(delay_ns, unit)
        if value is None:
            lines.append(f"#     {target}: {getattr(spec, 'path', field)} - "
                         f"{delay_ns:.0f} ns (unit {unit!r} not convertible here)")
            continue
        lines.append(f"#     {target}: {getattr(spec, 'path', field)} "
                     f"= {_format(value, unit)}")
        edit = getattr(spec, "edit", None)
        if edit:
            lines.append(f"#         {edit}")
    return lines


def print_apply_hint(experiment: str, backend: Any,
                     measured: dict[str, float], *,
                     stream: TextIO | None = None) -> None:
    """Print :func:`apply_hint_lines` on stderr (stdout stays ``| jq``-parseable)."""
    lines = apply_hint_lines(experiment, backend, measured)
    if lines:
        print("\n".join(lines), file=stream or sys.stderr)
