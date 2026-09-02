"""Bridge to the shared analysis engine (scqat).

SCQO does not fit anything itself: every experiment's ``estimate()`` delegates to
EXACTLY ONE scqat estimator, and that estimator is bound by exactly one experiment —
the binding is 1:1 in both directions, because an estimator is keyed by a READING (a
dataset shape AND the model fitted to it) and a binding is therefore a claim that this
model describes this signal. Share math through ``scqat.tools`` instead; the rule and
its decision procedure are in ``CLAUDE.md`` -> Terminology -> The estimator binding.
This module holds the pieces every experiment shares — the per-target split and its
whole-dataset sibling, each wrapping the ``analyze`` loop — and is the only place scqat
is imported.

scqat is imported **lazily** (inside the function) so ``import scqo`` stays light and
free of the analysis stack; the import only happens when analysis actually runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import xarray as xr


def per_qubit_results(
    prepared: xr.Dataset,
    estimator: Any,
    artifact_dir: Path | None = None,
    *,
    per_target_kwargs: dict[str, dict] | None = None,
    **estimator_kwargs,
) -> dict[str, dict]:
    """Run a scqat estimator on each qubit of an already-prepared dataset.

    ``prepared`` must already carry the variable and coordinate names the estimator
    expects (e.g. ``signal`` + ``idle_time``); the per-experiment ``estimate()`` does
    that renaming/scaling. This helper only splits along ``qubit`` and calls
    ``analyze`` once per qubit, returning ``{qubit_name: results_dict}``. Extra
    keyword arguments are forwarded to ``analyze`` (estimator knobs, e.g. ``ec_ghz``).

    ``per_target_kwargs`` carries knobs whose VALUE differs per qubit — the stored
    blob centers a pinned-center fit needs, say. They are merged over the shared
    ``estimator_kwargs`` (per-target wins) for that qubit only; a qubit absent from
    the mapping just gets the shared set.

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
        kwargs = {**estimator_kwargs, **(per_target_kwargs or {}).get(str(qubit_name), {})}
        try:
            results, figures = estimator.analyze(
                sq, output_dir=out_dir, skip_figures=artifact_dir is None, **kwargs
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
            results, figures = estimator.analyze(sq, output_dir=None, skip_figures=True, **kwargs)
        if figures:  # already saved to out_dir by analyze(); free them (long sessions)
            import matplotlib.pyplot as plt

            for fig in figures.values():
                plt.close(fig)
        out[qubit_name] = results
    return out


def whole_dataset_results(
    prepared: xr.Dataset,
    estimator: Any,
    artifact_dir: Path | None = None,
    *,
    label: str,
    **estimator_kwargs,
) -> dict:
    """Run a scqat estimator ONCE on the whole multi-target dataset.

    The sibling of :func:`per_qubit_results` for an experiment whose analysis
    is a property of the target SET rather than of each target: a chain's joint
    distribution spans every member at once, so the per-target split would make
    it undrawable. ``label`` names the artifact subfolder
    (``artifact_dir/<label>/``) — a role word like ``"chain"``, not an entity
    name, because the analysis belongs to no single entity.

    Same artifact-failure discipline as :func:`per_qubit_results`: a netCDF/PNG
    write that fails must never kill a measurement, so the analysis is redone
    in-memory and the run stays alive.
    """
    import sys

    out_dir = str(artifact_dir / label) if artifact_dir is not None else None
    try:
        results, figures = estimator.analyze(
            prepared, output_dir=out_dir, skip_figures=artifact_dir is None,
            **estimator_kwargs
        )
    except Exception as err:
        if out_dir is None:
            raise  # genuine analysis failure, nothing to fall back to
        print(
            f"scqo: artifact write failed for {label} ({type(err).__name__}: {err}); "
            "retrying analysis without saving artifacts",
            file=sys.stderr,
        )
        results, figures = estimator.analyze(
            prepared, output_dir=None, skip_figures=True, **estimator_kwargs
        )
    if figures:  # already saved to out_dir by analyze(); free them (long sessions)
        import matplotlib.pyplot as plt

        for fig in figures.values():
            plt.close(fig)
    return results


def mad_outlier_mask(
    values: Sequence[float | None], *, n_sigma: float = 3.0, rel_floor: float = 0.25
) -> tuple[list[bool], float | None, float | None]:
    """Robust (median / MAD) outlier flags over a 1-D value list.

    The campaign aggregator's robust view. ``None`` entries are treated as
    missing (never flagged, never judged); everything else follows scqat's
    ``mad_outliers`` contract exactly — a point is an outlier only when it
    deviates from the median both by more than ``n_sigma`` robust sigmas AND by
    more than ``rel_floor`` times the median, and **nothing is flagged below 4
    finite points**. Returns ``(mask, median, mad)`` with the mask as plain
    bools so no numpy type escapes into JSON.
    """
    import numpy as np
    from scqat.tools.robust import mad_outliers

    array = np.array([np.nan if v is None else float(v) for v in values], dtype=float)
    valid = np.isfinite(array)
    mask, median, mad = mad_outliers(array, valid, n_sigma, rel_floor=rel_floor)
    return (
        [bool(flag) for flag in mask],
        None if not np.isfinite(median) else float(median),
        None if not np.isfinite(mad) else float(mad),
    )
