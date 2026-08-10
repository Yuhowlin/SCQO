"""The campaign figure: a histogram + a drift trace per measured quantity.

Rendered into the campaign folder as ``statistics.png`` when a campaign finalizes,
so an overnight run has its picture waiting rather than only if someone remembers
to ask. Re-renderable at any time with ``scqo campaign --show <id> --plot``.

Nothing here recomputes physics. Values come from ``DataStore.campaign_runs``
(execution order, unlimited — never ``find_runs``, which is newest-first and capped
at 50) and every statistic comes from :mod:`scqo.campaign`, so the annotations can
never disagree with what ``scqo campaign --show`` prints.

matplotlib is imported LAZILY and forced to Agg: it is not a direct scqo dependency
(it arrives via scqat) and must not become an import-time one — the same discipline
as ``scqo._scqat``.
"""

from __future__ import annotations

import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from ..campaign import robust_summary, stderr_twin, summarize
from ..catalog import CHANNELS, COMPOSITES, MODES
from ..report import MEASURED_QUANTITIES

#: The figure's file name inside the campaign folder.
STATISTICS_PNG = "statistics.png"

#: SI prefixes for the value axis. The tables stay in raw SI because they are
#: machine-read; a tick label is not.
_PREFIXES = [(1e-12, "p"), (1e-9, "n"), (1e-6, "u"), (1e-3, "m"),
             (1.0, ""), (1e3, "k"), (1e6, "M"), (1e9, "G")]

#: Below this relative spread a quantity is plotted as a DEVIATION from its mean:
#: f_01_hz moves ~1e-5 of its value, so in absolute terms every tick is the same
#: number and matplotlib's own offset notation makes it worse.
_DEVIATION_BELOW = 1e-3

_BLUE, _RED = "#4C78A8", "#E45756"


def unit_of(quantity: str) -> str:
    """The catalogued unit of a fit quantity ("" when it is not a catalogued field)."""
    for kinds in (MODES, COMPOSITES, CHANNELS):
        for spec in kinds.values():
            field = spec.fields.get(quantity)
            if field is not None:
                return field.unit or ""
    return ""


def si_scale(values, unit: str) -> tuple[float, str]:
    """One (divisor, label) for the whole series, from its median magnitude.

    A single scale for every point keeps the histogram and the trace comparable and
    puts the prefix in the axis label instead of on every tick."""
    finite = [abs(v) for v in values if v and math.isfinite(v)]
    if not finite or not unit:
        return 1.0, unit
    median = sorted(finite)[len(finite) // 2]
    scale, prefix = 1.0, ""
    for factor, name in _PREFIXES:
        if median >= factor:
            scale, prefix = factor, name
    return scale, f"{prefix}{unit}"


def refit_readout(store, run: dict) -> dict[str, dict[str, float]]:
    """Recover a single_shot run's fitted populations from its saved dataset.

    Campaigns run with ``skip_artifacts`` wrote no ``analysis/`` folder, so the
    Gaussian blob weights were computed and discarded — but ``dataset.nc`` holds
    every shot, so they recompute offline with no instrument.

    Re-runs the EXPERIMENT's own ``estimate()``, never a private copy: the g/e label
    mapping (which GMM centre is which prepared state) has to agree with what SCQO
    recorded, and a second implementation of it would eventually drift. ``estimate()``
    touches only ``.dataset`` / ``.params`` / ``.artifact_dir``, so a stub backend is
    enough to construct the experiment.
    """
    from types import SimpleNamespace

    from ..experiments import get

    cls = get("single_shot_readout")
    fields = cls.Parameters.model_fields
    params = cls.Parameters(
        targets=list(run.get("targets") or []),
        **{k: v for k, v in (run.get("parameters") or {}).items()
           if k in fields and k != "targets"})
    exp = cls(SimpleNamespace(device=None), params)
    exp.dataset = store.open_dataset(run["run_id"])
    return {t: dict(f) for t, f in exp.estimate().fit.items()}


def collect(store, campaign_id: str, *, refit: bool = False):
    """``{(experiment, target, quantity): [value per slot]}`` + the slot clock.

    Slots follow :func:`scqo.campaign.aggregate`'s walk exactly: a plan that lists
    the SAME experiment at several step positions of one repeat gives it one slot
    PER OCCURRENCE, in step order — never the last occurrence overwriting the
    repeat's slot. The flat index is ``repeat_idx * n_occurrences + occurrence``,
    where ``occurrence`` is the rank of the run's ``step_idx`` among every step
    position that experiment occupies in the campaign — so a run missing from a
    failed or partial repeat leaves an aligned ``None`` gap, never a shifted
    series, and ``n_missing`` stays truthful against the repeat clock. A repeat
    whose fit did not resolve likewise contributes ``None``, never a dropped slot.
    """
    runs = store.campaign_runs(campaign_id)
    if not runs:
        return {}, {}, []

    # The occurrence geometry, recovered from the stamped (repeat_idx, step_idx):
    # ranking against the positions seen ANYWHERE in the campaign means one
    # repeat's missing run cannot shift its neighbours into the wrong slot.
    positions: dict[str, set[int]] = defaultdict(set)
    n_repeats = 0
    for run in runs:
        if run["repeat_idx"] is not None:
            n_repeats = max(n_repeats, run["repeat_idx"] + 1)
            if run["step_idx"] is not None:
                positions[run["experiment"]].add(run["step_idx"])
    rank = {exp: {step: occ for occ, step in enumerate(sorted(steps))}
            for exp, steps in positions.items()}

    slot: dict[str, int] = defaultdict(int)  # fallback for unstamped rows only
    series: dict[tuple[str, str, str], dict[int, float]] = defaultdict(dict)
    stamps: dict[str, dict[int, datetime]] = defaultdict(dict)
    for run in runs:
        experiment = run["experiment"]
        ranks = rank.get(experiment) or {}
        if run["repeat_idx"] is not None and run["step_idx"] in ranks:
            index = run["repeat_idx"] * len(ranks) + ranks[run["step_idx"]]
        else:
            index = slot[experiment]
        slot[experiment] = max(slot[experiment], index + 1)
        stamps[experiment][index] = datetime.fromisoformat(run["started_at"])
        fits = run["fit"] or {}
        if refit and experiment == "single_shot_readout":
            try:
                fits = {**fits, **refit_readout(store, run)}
            except Exception as err:  # a corrupt dataset must not lose the figure
                print(f"# refit failed for {run['run_id']}: "
                      f"{type(err).__name__}: {err}", file=sys.stderr)
        for target, fit in fits.items():
            for quantity, value in (fit or {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    series[(experiment, target, quantity)][index] = float(value)

    def width(experiment: str) -> int:
        # Every experiment spans the campaign's full repeat count (a repeat whose
        # run never persisted stays a None gap); per experiment, because
        # occurrence counts differ. The max() covers the unstamped fallback path.
        occurrences = len(rank.get(experiment) or ()) or 1
        return max(n_repeats * occurrences, max(stamps[experiment], default=-1) + 1)

    dense = {key: [vals.get(i) for i in range(width(key[0]))]
             for key, vals in series.items()}
    clock = {exp: [s.get(i) for i in range(width(exp))] for exp, s in stamps.items()}
    return dense, clock, runs


def choose(series: dict, only=None, show_all: bool = False) -> list[tuple]:
    """Which (experiment, target, quantity) rows to draw, in a stable order.

    Default: :data:`scqo.report.MEASURED_QUANTITIES` — the same catalog-derived rule
    the progress line uses, so curve-fit bookkeeping (amplitude/offset) and settings
    that cannot drift (drive_freq_hz) stay out."""
    if only:
        wanted = [k for k in series if k[2] in only]
    elif show_all:
        twins = {stderr_twin(q) for _, _, q in series}
        wanted = [k for k in series if k[2] not in twins]
    else:
        wanted = [k for k in series if k[2] in MEASURED_QUANTITIES]
    order = {q: i for i, q in enumerate(MEASURED_QUANTITIES)}
    return sorted(wanted, key=lambda k: (order.get(k[2], 999), k[0], k[1], k[2]))


def _title(stat: dict, scale: float, label: str) -> str:
    """The per-row annotation: counts, spread, drift rate, and the drift verdict."""
    bits = [f"n={stat['n']}"]
    for key, text in (("n_missing", "miss"), ("n_nonscalar", "array")):
        if stat.get(key):
            bits.append(f"{text}={stat[key]}")
    if stat["mean"] is not None:
        bits.append(f"mean={stat['mean'] / scale:.4g}")
    if stat["std"] is not None:
        bits.append(f"std={stat['std'] / scale:.3g}")
        if stat["mean"]:
            bits.append(f"({100 * stat['std'] / abs(stat['mean']):.1f}%)")
    if stat.get("slope_per_s"):
        # Per HOUR: a campaign is hours long and per-second slopes are unreadable.
        bits.append(f"slope={stat['slope_per_s'] * 3600 / scale:+.3g} {label or ''}/h".rstrip())
    if stat["scatter_ratio"] is not None:
        bits.append(f"std/err={stat['scatter_ratio']:.1f}")
    return "   ".join(bits)


def draw(series: dict, clock: dict, keys: list, manifest: dict, out: Path) -> Path:
    """Render the figure to ``out``. Lazy, headless matplotlib."""
    import matplotlib

    matplotlib.use("Agg")  # file only: never try to open a window on a lab PC
    import matplotlib.pyplot as plt

    rows = len(keys)
    fig, axes = plt.subplots(rows, 2, figsize=(12, 2.6 * rows), squeeze=False,
                             gridspec_kw={"width_ratios": [1, 1.6]})
    try:
        for row, key in enumerate(keys):
            experiment, target, quantity = key
            values = series[key]
            twin = stderr_twin(quantity)
            # The repeat clock, in seconds since the campaign's first run. Passing it
            # is what makes summarize() compute slope_per_s at all - the drift rate is
            # the whole point of a stability campaign, so it must not be left None.
            times = clock.get(experiment) or []
            base = next((t for t in times if t is not None), None)
            elapsed = None if base is None else [
                None if i >= len(times) or times[i] is None
                else (times[i] - base).total_seconds() for i in range(len(values))]
            stat = summarize(
                values, elapsed_s=elapsed,
                stderrs=series.get((experiment, target, twin)),
                stderr_key=twin if (experiment, target, twin) in series else None)
            robust = robust_summary(values)
            unit = unit_of(quantity)

            offset, base_text = 0.0, ""
            if (stat["mean"] and stat["std"]
                    and abs(stat["std"] / stat["mean"]) < _DEVIATION_BELOW):
                offset = stat["mean"]
                base_scale, base_label = si_scale([offset], unit)
                base_text = f" - {offset / base_scale:.9g} {base_label}".rstrip()

            scale, label = si_scale([v - offset for v in values if v is not None], unit)
            axis_label = f"{quantity}{base_text}" + (f" [{label}]" if label else "")
            good = [(i, (v - offset) / scale)
                    for i, v in enumerate(values) if v is not None]
            flagged = set(robust["outlier_indices"])
            ax_h, ax_t = axes[row]
            ys = [v for _, v in good]
            mean_at = None if stat["mean"] is None else (stat["mean"] - offset) / scale

            ax_h.hist(ys, bins="auto", color=_BLUE, edgecolor="white")
            if mean_at is not None:
                ax_h.axvline(mean_at, color=_RED, lw=1.6)
                if stat["std"]:
                    for sign in (-1, 1):
                        ax_h.axvline(mean_at + sign * stat["std"] / scale,
                                     color=_RED, lw=0.9, ls="--")
            ax_h.set_xlabel(axis_label)
            ax_h.set_ylabel("repeats")
            ax_h.set_title(f"{experiment}  {target}", fontsize=9, loc="left")

            xs = [(elapsed[i] / 3600 if elapsed and elapsed[i] is not None else i)
                  for i, _ in good]
            ax_t.plot(xs, ys, "-o", ms=3.5, lw=1.0, color=_BLUE, zorder=2)
            out_x = [x for x, (i, _) in zip(xs, good) if i in flagged]
            out_y = [v for (i, v) in good if i in flagged]
            if out_x:  # median/MAD flagged, via scqat's mad_outliers
                ax_t.plot(out_x, out_y, "o", ms=7, mfc="none", mec=_RED, mew=1.6,
                          zorder=3, label=f"{len(out_x)} outlier(s)")
                ax_t.legend(fontsize=7, loc="best")
            if mean_at is not None:
                ax_t.axhline(mean_at, color=_RED, lw=1.0, alpha=0.7)
            ax_t.set_xlabel("hours since campaign start" if base else "repeat")
            ax_t.set_ylabel(axis_label)
            ax_t.set_title(_title(stat, scale, label), fontsize=9, loc="left")
            for ax in (ax_h, ax_t):  # never let matplotlib re-add an offset of its own
                ax.ticklabel_format(axis="both", useOffset=False, style="plain")

        partial = manifest.get("repeats_partial") or 0
        fig.suptitle(
            f"{manifest.get('campaign_id')}   {manifest.get('label')}   "
            f"{manifest.get('repeat_done')} repeats"
            + (f" (+{partial} partial)" if partial else "")
            + f"   {manifest.get('status')}", fontsize=10)
        fig.tight_layout(rect=(0, 0, 1, 0.99 - 0.004 * rows))
        out.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out, dpi=140)
    finally:
        plt.close(fig)  # long sessions must not accumulate figures
    return out


def render_statistics(store, campaign_id: str, manifest: dict, *, out: Path | None = None,
                      only=None, show_all: bool = False, refit: bool = False) -> Path | None:
    """Draw a campaign's figure; ``None`` when there is nothing to draw."""
    series, clock, runs = collect(store, campaign_id, refit=refit)
    keys = choose(series, only, show_all)
    if not keys:
        return None
    if out is None:
        out = Path(store.data_root) / manifest["path"] / STATISTICS_PNG
    return draw(series, clock, keys, manifest, Path(out))
