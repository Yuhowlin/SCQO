"""High-level Campaign query and filtering engine.

Provides multi-dimensional querying, quality gates, and robust statistic extraction
over campaign histories for devices, cooldowns, and setups.
"""

from __future__ import annotations

import math
from typing import Any, Literal, Sequence


def normalize_target_name(target: str) -> str:
    """Normalize target names (e.g., 'q1_xy', 'q1_ro', 'q1_z', 'q1_res' -> 'q1')."""
    if not target:
        return ""
    base = target.split("_")[0].lower()
    return base


def query_campaign_statistics(
    store: Any,
    device: str,
    *,
    cooldown: str | None = None,
    setup: str | None = None,
    tags: Sequence[str] | None = None,
    min_repeats: int = 2,
    status: Sequence[str] | None = ("complete", "running"),
    estimator: Literal["mean", "median", "mad_sigma"] = "mean",
    limit: int = 50,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Query recent campaigns for a device and return independent latest statistics per (target, quantity).

    Resolves metrics independently per qubit and quantity across multiple campaigns:
    if Q1 T1 is in Campaign A and Q3 T1 is in Campaign B, each qubit receives its latest
    qualifying campaign statistics.

    Returns:
        target (normalized, lowercase) -> quantity_name -> {
            "value": float,             # mean or median based on estimator
            "error": float | None,      # std or mad_sigma based on estimator
            "mean": float | None,
            "std": float | None,
            "sem": float | None,
            "median": float | None,
            "mad": float | None,
            "mad_sigma": float | None,
            "min": float | None,
            "max": float | None,
            "n": int,
            "n_missing": int,
            "slope_per_s": float | None,
            "scatter_ratio": float | None,
            "campaign_id": str,
            "experiment": str,
            "cooldown": str,
            "setup": str,
            "started_at": str,
            "tags": list[str],
        }
    """
    if store is None or not hasattr(store, "find_campaigns"):
        return {}

    query_kwargs: dict[str, Any] = {"device": device or None, "limit": limit}
    if cooldown is not None:
        query_kwargs["cooldown"] = cooldown
    if setup is not None:
        query_kwargs["setup"] = setup

    try:
        camps = store.find_campaigns(**query_kwargs)
    except Exception:
        return {}

    tag_set = set(tags) if tags else None
    allowed_statuses = set(status) if status else None

    stats_by_target: dict[str, dict[str, dict[str, Any]]] = {}

    for camp_row in camps:
        # Status filter
        c_status = camp_row.get("status", "")
        if allowed_statuses and c_status not in allowed_statuses:
            continue

        # Tag filter
        c_tags = camp_row.get("tags") or []
        if tag_set and not tag_set.intersection(c_tags):
            continue

        cid = camp_row.get("campaign_id", "")
        c_cooldown = camp_row.get("cooldown", "")
        c_setup = camp_row.get("setup", "")
        c_started = camp_row.get("started_at", "")

        # Statistics dictionary is cached in sqlite row dict
        statistics = camp_row.get("statistics")
        if not isinstance(statistics, dict):
            try:
                loaded = store.load_campaign(cid)
                manifest = loaded.get("manifest") or {}
                statistics = manifest.get("statistics") or {}
            except Exception:
                continue

        if not isinstance(statistics, dict):
            continue

        for exp_name, target_dict in statistics.items():
            if not isinstance(target_dict, dict):
                continue
            for target_name, qty_dict in target_dict.items():
                if not isinstance(qty_dict, dict):
                    continue
                base_target = normalize_target_name(target_name)
                target_stats = stats_by_target.setdefault(base_target, {})

                for qty_name, stat_obj in qty_dict.items():
                    if not isinstance(stat_obj, dict):
                        continue
                    n = stat_obj.get("n", 0)
                    if n < min_repeats:
                        continue

                    mean_val = stat_obj.get("mean")
                    median_val = stat_obj.get("median")
                    if mean_val is None and median_val is None:
                        continue

                    # Choose primary value and error according to estimator
                    if estimator == "median":
                        val = median_val if median_val is not None else mean_val
                        err = stat_obj.get("mad_sigma") or stat_obj.get("std")
                    else:  # "mean" or default
                        val = mean_val if mean_val is not None else median_val
                        err = stat_obj.get("std") or stat_obj.get("mad_sigma")

                    # Keep newest qualifying occurrence for each quantity
                    if qty_name not in target_stats:
                        target_stats[qty_name] = {
                            "value": val,
                            "error": err,
                            "mean": mean_val,
                            "std": stat_obj.get("std"),
                            "sem": stat_obj.get("sem"),
                            "median": median_val,
                            "mad": stat_obj.get("mad"),
                            "mad_sigma": stat_obj.get("mad_sigma"),
                            "min": stat_obj.get("min"),
                            "max": stat_obj.get("max"),
                            "n": n,
                            "n_missing": stat_obj.get("n_missing", 0),
                            "slope_per_s": stat_obj.get("slope_per_s"),
                            "scatter_ratio": stat_obj.get("scatter_ratio"),
                            "campaign_id": cid,
                            "experiment": exp_name,
                            "cooldown": c_cooldown,
                            "setup": c_setup,
                            "started_at": c_started,
                            "tags": c_tags,
                        }

    return stats_by_target


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
    estimator: Literal["mean", "median", "mad_sigma"] = "mean",
) -> dict[str, Any] | None:
    """Retrieve the latest qualifying campaign statistic for a single (target, quantity)."""
    stats = query_campaign_statistics(
        store,
        device,
        cooldown=cooldown,
        setup=setup,
        tags=tags,
        min_repeats=min_repeats,
        estimator=estimator,
    )
    base_target = normalize_target_name(target)
    target_stats = stats.get(base_target, {})
    stat = target_stats.get(quantity)
    if stat is not None:
        if experiment is None or stat.get("experiment") == experiment:
            return stat
    return None

