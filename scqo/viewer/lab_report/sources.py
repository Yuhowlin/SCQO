"""Optional side inputs the lab report reads off the data drive.

Both are OPTIONAL and both degrade to empty: a report without a datasheet or
without a resistance table is a report with two blank rows, never an error. The
viewer is read-only and must render for any context, so a malformed file here
must not take a page down with it.

What it must NOT do is fail silently in a way that looks like data. That is why
:func:`load_design_for` reports the reason it gave up through ``warnings``
rather than swallowing it: the first draft of this feature parsed design.toml
with a bare ``tomllib`` (no ``utf-8-sig``) inside a blanket ``except``, and on
any machine where the hand-edited file had been saved from PowerShell — which
writes a BOM — the whole Design column silently emptied with nothing to show
for it. ``scqo.design.load_design`` already handles the BOM, and is the one
loader for this file.
"""

from __future__ import annotations

import csv
import warnings
from pathlib import Path
from typing import Any

#: filenames the resistance->frequency prediction may be supplied under, in
#: the device folder or beside the setup's own data.
R_TO_FQ_FILE = "R_to_fq.csv"


def load_design_for(data_root: Path | None, device: str, roster: Any = None) -> Any:
    """The device's datasheet, or None when there is not one to read.

    ``roster`` is required by :func:`scqo.design.load_design` for validation.
    The viewer runs roster-free (it reads only the datastore), so callers that
    have no roster get None and the design columns stay empty — correct, and
    visibly so, rather than a half-validated parse.
    """
    if data_root is None or not device or roster is None:
        return None
    from scqo.design import load_design

    try:
        return load_design(Path(data_root) / device, roster)
    except (OSError, ValueError) as err:
        warnings.warn(f"lab report: ignoring {device}'s design.toml — {err}",
                      RuntimeWarning, stacklevel=2)
        return None


def load_predicted_f_q(data_root: Path | None, device: str, cooldown: str = "",
                       setup: str = "") -> dict[str, float]:
    """``{qubit: GHz}`` predicted from junction resistance, or ``{}``.

    Two rows per line — qubit name, frequency in GHz. Looked for in the device
    folder first, then beside the setup's data. ``utf-8-sig`` because this is a
    hand-made spreadsheet export and Excel writes a BOM.
    """
    if data_root is None or not device:
        return {}
    root = Path(data_root)
    candidates = [root / device / R_TO_FQ_FILE]
    if cooldown and setup:
        candidates.append(root / device / cooldown / setup / R_TO_FQ_FILE)

    for path in candidates:
        if not path.is_file():
            continue
        out: dict[str, float] = {}
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as fh:
                for row in csv.reader(fh):
                    if len(row) < 2:
                        continue
                    try:
                        out[row[0].strip().lower()] = float(row[1].strip())
                    except ValueError:
                        continue  # a header row, or a blank — skip the cell, not the file
        except OSError as err:
            warnings.warn(f"lab report: ignoring {path.name} — {err}",
                          RuntimeWarning, stacklevel=2)
            return {}
        return out
    return {}
