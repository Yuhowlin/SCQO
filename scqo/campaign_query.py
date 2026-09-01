"""Latest campaign statistics per (target, quantity), across campaigns.

A campaign records aggregate ``statistics`` per experiment, target and quantity.
This resolves them INDEPENDENTLY per (target, quantity): if q1's T1 is in
campaign A and q3's T1 is in campaign B, each takes its own latest qualifying
campaign rather than forcing one campaign to answer for the whole chip.

``DataStore.find_campaigns`` returns newest-first, so the first qualifying
occurrence of a quantity IS the latest one.
"""

from __future__ import annotations

from typing import Any, Literal, Sequence

from .catalog import CHANNELS

#: Entity-name suffixes that belong to a MODE's riders, so stripping one lands
#: back on the mode: the channel kinds' frozen ``rider_suffix`` values plus the
#: ``_res`` resonator a readout rider mints (``roster.py``, ``_expand``).
#:
#: Derived from the catalog rather than written out, because the set is frozen
#: THERE — store keys depend on it — and a hand-copied list would drift.
RIDER_SUFFIXES: tuple[str, ...] = tuple(
    sorted({c.rider_suffix for c in CHANNELS.values() if c.rider_suffix} | {"_res"},
           key=len, reverse=True)
)

#: The estimators this module actually implements. "mad_sigma" is deliberately
#: absent: it was offered here once while the body branched only on "median",
#: so asking for robust statistics quietly returned the mean.
Estimator = Literal["mean", "median"]


def normalize_target_name(target: str) -> str:
    """The entity a statistic belongs to, with a rider suffix stripped.

    ``q1_xy``, ``q1_ro``, ``q1_z`` and ``q1_res`` all normalise to ``q1`` — they
    are that mode's channels and its minted resonator.

    A COMPOSITE does not. ``q1_q2`` is a qubit PAIR, a different entity with its
    own quantities, and it normalises to itself. Splitting on the first ``_``
    instead — which is what this did originally — filed every two-qubit result
    (``qc_n_swap_amp``, ``qc_n_stark_amp``, ``pair_zz_coupler`` and the rest)
    under ``q1``, where it could also shadow that qubit's own value.
    """
    if not target:
        return ""
    name = target.strip().lower()
    for suffix in RIDER_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def query_campaign_statistics(
    store: Any,
    device: str,
    *,
    cooldown: str | None = None,
    setup: str | None = None,
    experiment: str | None = None,
    tags: Sequence[str] | None = None,
    min_repeats: int = 2,
    status: Sequence[str] | None = ("complete", "running"),
    estimator: Estimator = "mean",
    limit: int = 50,
) -> dict[str, dict[str, dict[str, Any]]]:
    """``{target: {quantity: stat}}`` for a device's recent campaigns.

    ``min_repeats`` is the quality gate: a quantity aggregated over fewer than
    this many repeats is skipped. ``estimator`` picks which pair of numbers the
    ``value``/``error`` keys carry — mean/std, or median/mad_sigma.

    Store failures are NOT swallowed. A missing index or an unreadable campaign
    raises, because returning ``{}`` makes a broken datastore look exactly like
    an unmeasured chip, and the caller renders blanks either way with nothing to
    say why.
    """
    if store is None:
        return {}

    campaigns = store.find_campaigns(
        device=device or None, limit=limit,
        **{k: v for k, v in (("cooldown", cooldown), ("setup", setup),
                             ("experiment", experiment)) if v is not None},
    )

    wanted_tags = set(tags) if tags else None
    allowed = set(status) if status else None
    out: dict[str, dict[str, dict[str, Any]]] = {}

    for row in campaigns:
        if allowed is not None and row.get("status", "") not in allowed:
            continue
        row_tags = row.get("tags") or []
        if wanted_tags is not None and not wanted_tags.intersection(row_tags):
            continue

        statistics = row.get("statistics")
        if not isinstance(statistics, dict):
            # The index caches it; fall back to the manifest for a row written
            # before that column existed.
            statistics = (store.load_campaign(row.get("campaign_id", ""))
                          .get("manifest") or {}).get("statistics") or {}
        if not isinstance(statistics, dict):
            continue

        for exp_name, targets in statistics.items():
            if not isinstance(targets, dict):
                continue
            for raw_target, quantities in targets.items():
                if not isinstance(quantities, dict):
                    continue
                target = normalize_target_name(raw_target)
                per_target = out.setdefault(target, {})

                for quantity, stat in quantities.items():
                    if not isinstance(stat, dict) or quantity in per_target:
                        continue  # newest-first: the first one seen wins
                    if (stat.get("n") or 0) < min_repeats:
                        continue

                    mean, median = stat.get("mean"), stat.get("median")
                    if mean is None and median is None:
                        continue
                    if estimator == "median":
                        value = median if median is not None else mean
                        error = stat.get("mad_sigma", stat.get("std"))
                    else:
                        value = mean if mean is not None else median
                        error = stat.get("std", stat.get("mad_sigma"))

                    per_target[quantity] = {
                        "value": value, "error": error,
                        "mean": mean, "std": stat.get("std"), "sem": stat.get("sem"),
                        "median": median, "mad": stat.get("mad"),
                        "mad_sigma": stat.get("mad_sigma"),
                        "min": stat.get("min"), "max": stat.get("max"),
                        "n": stat.get("n"), "n_missing": stat.get("n_missing", 0),
                        "slope_per_s": stat.get("slope_per_s"),
                        "scatter_ratio": stat.get("scatter_ratio"),
                        "campaign_id": row.get("campaign_id", ""),
                        "experiment": exp_name,
                        "cooldown": row.get("cooldown", ""),
                        "setup": row.get("setup", ""),
                        "started_at": row.get("started_at", ""),
                        "tags": row_tags,
                    }

    return out


def get_latest_metric_stat(
    store: Any,
    device: str,
    target: str,
    quantity: str,
    *,
    experiment: str | None = None,
    cooldown: str | None = None,
    setup: str | None = None,
    tags: Sequence[str] | None = None,
    min_repeats: int = 2,
    estimator: Estimator = "mean",
) -> dict[str, Any] | None:
    """The latest qualifying statistic for one (target, quantity), or None.

    ``experiment`` is pushed down into the query, where ``find_campaigns``
    filters it in SQL. Comparing it afterwards instead — as this did — returns
    None whenever the newest campaign carrying the quantity happens to be a
    different experiment, hiding a perfectly good older one.
    """
    stats = query_campaign_statistics(
        store, device, cooldown=cooldown, setup=setup, experiment=experiment,
        tags=tags, min_repeats=min_repeats, estimator=estimator,
    )
    return stats.get(normalize_target_name(target), {}).get(quantity)
