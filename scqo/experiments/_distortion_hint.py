"""The cryoscopes' operator hint: WHERE the measured taps still have to go.

``distortion_amp`` / ``distortion_tau_s`` are FACTS. Accepting them records the
measured cryo-wiring transient in physical.json and pushes NOTHING to the
instrument — applying a predistortion filter is a deliberate, opt-in vendor step
(characterize on a filter-CLEARED line, then apply once). Between a green fit and
a still-distorted flux line that gap is easy to forget, so both cryoscopes print
the command that closes it.

The command is VENDOR knowledge, so it comes from the backend through one
duck-typed hook — the ``power_context`` / ``preview`` pattern:

    backend.distortion_apply_command(target, run_id) -> str | None

Return the exact shell command, or None when this backend has no one-command
path. Declaring nothing at all is not an error: the taps are recorded either way
and the hint then names the manual step instead. Both shipped backends return
their ``apply_distortion`` operator CLI (``scqo_qm.backend.apply_distortion`` /
``scqo_qblox.backend.apply_distortion``), run-addressed when the run id is known.

Lines take the CLI meta shape — ASCII, ``#``-prefixed, on STDERR — so a
``scqo run ... | jq`` pipeline keeps parsing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Sequence, TextIO

#: the paired fields this hint is about (both cryoscopes propose exactly these).
TAP_FIELDS = ("distortion_amp", "distortion_tau_s")

#: the backend hook the vendor command comes from.
HOOK = "distortion_apply_command"


def run_id_of(artifact_dir: Path | None) -> str | None:
    """The run id an experiment is writing into, or None.

    The Session hands an experiment ``artifact_dir = <run_dir>/analysis`` and the
    run folder is NAMED by the run id (``datastore.new_run_dir``), so the hint can
    address the run without the experiment carrying a second copy of it. None
    without a datastore and under ``--skip-artifacts`` — the hint then names the
    accepted-facts form of the command, which needs no run id.
    """
    if artifact_dir is None:
        return None
    return Path(artifact_dir).parent.name or None


def apply_hint_lines(experiment: str, backend: Any, targets: Sequence[str],
                     run_id: str | None = None) -> list[str]:
    """The hint as LINES — pure, so tests read these and not a stream."""
    if not targets:
        return []
    lines = [
        f"# {experiment}: the fitted taps are FACTS - accepting records the",
        "#   measurement and pushes NOTHING to the instrument. Write them into the",
        "#   vendor config with:",
    ]
    hook = getattr(backend, HOOK, None)
    for target in targets:
        command = None
        if callable(hook):
            try:
                command = hook(target, run_id)
            except Exception as err:  # a hint must never break a measurement
                lines.append(f"#     {target}: backend {HOOK} failed - "
                             f"{type(err).__name__}: {err}")
                continue
        if command:
            lines.append(f"#     {target}: {command}")
        else:
            lines.append(f"#     {target}: this backend declares no {HOOK} - key")
            lines.append(f"#         {' + '.join(TAP_FIELDS)} into its output "
                         f"filter by hand")
    return lines


def print_apply_hint(experiment: str, backend: Any, targets: Sequence[str],
                     run_id: str | None = None, *,
                     stream: TextIO | None = None) -> None:
    """Print :func:`apply_hint_lines` on stderr (stdout stays ``| jq``-parseable)."""
    lines = apply_hint_lines(experiment, backend, targets, run_id)
    if lines:
        print("\n".join(lines), file=stream or sys.stderr)
