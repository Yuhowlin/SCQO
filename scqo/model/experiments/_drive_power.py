"""Shared drive-power boundary for saturation-drive experiments — greenfield.

Port of :mod:`scqo.experiments._drive_power`. The boundary discipline is
unchanged (recorded set of ``drive_power_dbm`` before acquisition, exact
revert afterwards — the punchout discipline of
``resonator_spectroscopy_power_amp``); what moved is the device surface:
the knob lives on each target's DRIVE CHANNEL, so the boundary reads and
writes through ``experiment.device.channel(t, "drive")`` views instead of
the old component views. ``qubit_spectroscopy`` and
``qubit_spectroscopy_flux_pulse`` share this ONE implementation instead of
copy-pasting the try/finally.
"""

from __future__ import annotations

import math
from contextlib import contextmanager
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from ..experiment import Experiment


@contextmanager
def drive_power_boundary(experiment: "Experiment", target_dbm: float) -> Iterator[None]:
    """Recorded set -> (acquire, in the ``with`` body) -> exact revert of
    ``drive_power_dbm`` for every target's drive channel.

    Each target's standing ``drive_power_dbm`` is read and validated FIRST (a
    single unknown/non-finite chain aborts before any write, so the device is
    never left half-set), then all are written to ``target_dbm``, the body runs
    (acquisition), and the ``finally`` reverts every target to its standing
    value — 2 ChangeRecords + coupled ``drive_amp`` echoes per qubit. While the
    chain is off its standing value the stored pi_amp means a different power; no
    pi pulse is played by these experiments, and the revert is exact (discrete
    chain knob, the amplitude restored verbatim).
    """
    target_dbm = float(target_dbm)
    targets = list(experiment.params.targets)
    views = {q: experiment.device.channel(q, "drive") for q in targets}

    previous: dict[str, float] = {}
    for q, view in views.items():
        try:
            before = view.drive_power_dbm
        except (KeyError, ValueError):
            before = None
        if before is None or not math.isfinite(float(before)):
            raise RuntimeError(
                f"{q}: drive_power_dbm is unknown (unconfigured drive chain / zero "
                f"saturation amplitude) — the revert target would be undefined; set "
                f"drive_power_dbm (or fix drive_amp) first"
            )
        previous[q] = float(before)
    for view in views.values():
        view.drive_power_dbm = target_dbm  # recorded boundary write (+ coupled echo)

    try:
        yield
    finally:
        revert_errors = []
        for q, view in views.items():  # recorded boundary revert (+ coupled echo)
            try:
                view.drive_power_dbm = previous[q]
            except Exception as err:  # noqa: BLE001 - collected and re-raised below
                revert_errors.append(f"{q}: {type(err).__name__}: {err}")
        if revert_errors:
            raise RuntimeError(
                "drive chain revert failed for " + "; ".join(revert_errors)
            )
