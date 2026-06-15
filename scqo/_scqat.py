"""Bridge to the shared analysis engine (scqat).

SCQO does not fit anything itself: every experiment's ``estimate()`` delegates to a
single scqat estimator (one estimator per probing method). This module holds the one
piece both share — the per-qubit split + ``analyze`` loop — and is the only place
scqat is imported.

scqat is imported **lazily** (inside the function) so ``import scqo`` stays light and
free of the analysis stack; the import only happens when analysis actually runs.
"""

from __future__ import annotations

from typing import Any

import xarray as xr


def per_qubit_results(prepared: xr.Dataset, estimator: Any) -> dict[str, dict]:
    """Run a scqat estimator on each qubit of an already-prepared dataset.

    ``prepared`` must already carry the variable and coordinate names the estimator
    expects (e.g. ``signal`` + ``idle_time``); the per-experiment ``estimate()`` does
    that renaming/scaling. This helper only splits along ``qubit`` and calls
    ``analyze`` once per qubit, returning ``{qubit_name: results_dict}``.
    """
    from scqat.parsers import repetition_data

    out: dict[str, dict] = {}
    for sq in repetition_data(prepared, repetition_dim="qubit"):
        qubit_name = sq["qubit"].values.item()
        out[qubit_name] = estimator.analyze(sq, output_dir=None, skip_figures=True)[0]
    return out
