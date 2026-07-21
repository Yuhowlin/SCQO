"""Bridge to the shared analysis engine (scqat).

SCQO does not fit anything itself: every experiment's ``estimate()`` delegates to a
single scqat estimator (one estimator per probing method). This module holds the one
piece both share — the per-qubit split + ``analyze`` loop — and is the only place
scqat is imported.

scqat is imported **lazily** (inside the function) so ``import scqo`` stays light and
free of the analysis stack; the import only happens when analysis actually runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import xarray as xr


def per_qubit_results(
    prepared: xr.Dataset, estimator: Any, artifact_dir: Path | None = None, **estimator_kwargs
) -> dict[str, dict]:
    """Run a scqat estimator on each qubit of an already-prepared dataset.

    ``prepared`` must already carry the variable and coordinate names the estimator
    expects (e.g. ``signal`` + ``idle_time``); the per-experiment ``estimate()`` does
    that renaming/scaling. This helper only splits along ``qubit`` and calls
    ``analyze`` once per qubit, returning ``{qubit_name: results_dict}``. Extra
    keyword arguments are forwarded to ``analyze`` (estimator knobs, e.g. ``ec_ghz``).

    With ``artifact_dir`` set (the Session does this when a datastore is configured),
    each qubit's scqat artifacts — ``<estimator>_metadata.json``, ``_plotdata.nc`` and
    figure PNGs — are written to ``artifact_dir/<qubit>/``; without it, analysis stays
    in-memory and figures are skipped (standalone use, tests without persistence).
    """
    import sys

    from scqat.parsers import repetition_data

    out: dict[str, dict] = {}
    for sq in repetition_data(prepared, repetition_dim="target"):
        qubit_name = sq["target"].values.item()
        out_dir = str(artifact_dir / str(qubit_name)) if artifact_dir is not None else None
        try:
            results, figures = estimator.analyze(
                sq, output_dir=out_dir, skip_figures=artifact_dir is None, **estimator_kwargs
            )
        except Exception as err:
            if out_dir is None:
                raise  # genuine analysis failure, nothing to fall back to
            # Artifact I/O (netCDF/PNG writes) must never kill a measurement result:
            # redo the analysis in-memory only and keep the run alive.
            print(
                f"scqo: artifact write failed for {qubit_name} ({type(err).__name__}: {err}); "
                "retrying analysis without saving artifacts",
                file=sys.stderr,
            )
            results, figures = estimator.analyze(sq, output_dir=None, skip_figures=True, **estimator_kwargs)
        if figures:  # already saved to out_dir by analyze(); free them (long sessions)
            import matplotlib.pyplot as plt

            for fig in figures.values():
                plt.close(fig)
        out[qubit_name] = results
    return out
