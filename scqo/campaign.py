"""Campaign — an ordered list of experiment steps, walked N times.

A campaign is **outer repetition**: each (repeat, step) is a full, separate
:meth:`scqo.Session.run` with its own run folder, dataset, fit, artifacts and
**timestamp**. One experiment repeated N times is the degenerate 1-step
campaign. The campaign owns the plan, the cadence, the stop conditions and the
per-(experiment, target, quantity) statistics; it owns no measurement code and
no analysis code.

Why outer and not an inner ``repeat_idx`` dataset dimension: a drift study asks
"did T1 move over six hours", and a dataset axis buries N repeats under one
``started_at``. On top of that ``DatasetContract`` validates by exact dim-set
equality, so a declared repeat dim would become mandatory for every probe of
that experiment on both drivers forever — and it would not generalize to a
bundle at all, since a bundle has no shared dataset.

This module is **pure**: a pydantic plan plus pure aggregation over fit dicts.
All I/O lives in :mod:`scqo.datastore`; all orchestration in
``Session.run_campaign``.

.. note::
   The simulated backend is deterministic in (experiment, targets) — see
   ``scqo.experiments._sim.stable_seed`` — so an **offline** campaign reports
   ``std == 0.0`` by construction. That is the right answer for a noiseless
   virtual chip, not a bug in the aggregator.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

try:  # the repo-wide pattern: stdlib tomllib on 3.11+, tomli backport below
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

#: A campaign label becomes a folder segment and part of the campaign_id, and
#: travels as a CLI argument and an index value — keep it plain.
LABEL_RE = re.compile(r"^[A-Za-z0-9_-]+$")

#: Statistic keys in render order; the CLI table and any future viewer page
#: share this ordering so the same numbers always appear in the same place.
STAT_KEYS: tuple[str, ...] = (
    "n", "n_missing", "n_nonscalar", "mean", "std", "sem", "median", "mad", "mad_sigma",
    "min", "max", "first", "last", "slope_per_s",
    "stderr_key", "mean_stderr", "scatter_ratio",
)

#: 1.4826 * MAD is a consistent estimator of the standard deviation for normal
#: data — the same constant scqat's robust helpers use.
_MAD_TO_SIGMA = 1.4826


class CampaignStep(BaseModel):
    """One experiment invocation inside a repeat."""

    model_config = ConfigDict(extra="forbid")

    experiment: str = Field(..., description="Registered experiment name, e.g. 'qubit_relaxation'.")
    #: This step's Parameters overlay. Merged OVER CampaignPlan.defaults, and
    #: over the session's parameter_defaults[experiment] inside Session.run.
    params: dict[str, Any] = Field(default_factory=dict,
                                   description="Parameters for this step (override the plan defaults).")
    tags: list[str] = Field(default_factory=list,
                            description="Extra run tags for this step's children.")
    note: str = Field("", description="Free-text note stored with this step's runs.")


class CampaignPlan(BaseModel):
    """What to run, how many times, how fast, and when to give up."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(..., description="Slug for the campaign_id and its folder.")
    steps: list[CampaignStep] = Field(..., min_length=1,
                                      description="Ordered steps; one walk of them is one repeat.")
    repeat: int | None = Field(None, gt=0,
                               description="Number of walks; unset means run until max_duration_s.")
    #: MINIMUM wall-clock seconds between consecutive repeat STARTS. A repeat
    #: that overruns is recorded (``overran_by_s``), never padded and never
    #: skipped to catch up — the cadence is a floor, not a schedule to hit.
    period_s: float | None = Field(None, gt=0,
                                   description="Minimum seconds between repeat starts.")
    #: Checked BEFORE a repeat starts, so a repeat is never cut in half — and never
    #: before repeat 0, so a campaign never returns zero measurements.
    max_duration_s: float | None = Field(None, gt=0,
                                         description="Stop starting new repeats after this much wall clock.")
    on_error: Literal["continue", "stop"] = Field(
        "continue", description="What a failed repeat does.")
    #: Circuit breaker. An overnight campaign against a disconnected instrument
    #: would otherwise mint failed run folders as fast as the disk allows.
    max_consecutive_failures: int = Field(
        5, gt=0, description="Stop after this many consecutive failed repeats.")
    #: Write no analysis/ folder for the children — no figures, and no scqat
    #: metadata or plotdata either. dataset.nc and result.json still land.
    skip_artifacts: bool = Field(
        False, description="Skip the per-run scqat artifacts (figures, metadata, plotdata).")
    #: Params shared by every step, merged UNDER each step's own params —
    #: typically just {"targets": [...]}.
    defaults: dict[str, Any] = Field(default_factory=dict,
                                     description="Parameters shared by every step.")
    tags: list[str] = Field(default_factory=list,
                            description="Searchable tags for every child run.")
    note: str = Field("", description="Free-text note stored with the campaign.")

    @model_validator(mode="after")
    def _check(self) -> "CampaignPlan":
        if not LABEL_RE.match(self.label):
            raise ValueError(
                f"label {self.label!r} must be letters, digits, '_' or '-' only "
                "(it becomes a folder name and an index value)")
        if self.repeat is None and self.max_duration_s is None:
            raise ValueError(  # ASCII: this reaches the lab console verbatim
                "an open-ended campaign (no `repeat`) needs `max_duration_s`, "
                "otherwise it never stops on its own")
        return self

    @property
    def experiments(self) -> list[str]:
        """Step experiment names in order (duplicates kept — order is the plan)."""
        return [s.experiment for s in self.steps]

    def step_params(self, step: CampaignStep) -> dict[str, Any]:
        """One step's parameters with the plan defaults merged underneath."""
        return {**self.defaults, **step.params}

    def targets(self) -> list[str]:
        """Union of every step's declared targets, first-seen order."""
        seen: dict[str, None] = {}
        for step in self.steps:
            for t in self.step_params(step).get("targets", []) or []:
                seen.setdefault(str(t), None)
        return list(seen)


def load_campaign_plan(path: str | Path) -> CampaignPlan:
    """Read a campaign plan from a TOML file.

    utf-8-sig: tolerate a UTF-8 BOM — Windows PowerShell 5.1's ``-Encoding
    utf8`` writes one, and a plan file is exactly the kind of thing a student
    writes from a PowerShell prompt.
    """
    path = Path(path).expanduser()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as err:
        raise ValueError(f"cannot read campaign plan {path}: {err}") from None
    except tomllib.TOMLDecodeError as err:
        raise ValueError(f"campaign plan {path} is not valid TOML: {err}") from None
    return CampaignPlan(**raw)


def stderr_twin(quantity: str) -> str:
    """The conventional standard-error key for a fitted quantity.

    ``t1_s`` -> ``t1_stderr_s``, ``t2_echo_s`` -> ``t2_echo_stderr_s``,
    ``fidelity_g`` -> ``fidelity_g_stderr``: the unit suffix (if any) stays
    last. This is a HEURISTIC over an unenforced naming convention, so the
    resolved name is reported back as ``stderr_key`` and is used only when that
    key is actually present in the fit dict. Not every quantity has a twin —
    ``qubit_ramsey`` publishes no ``t2_star_stderr_s``, so ``scatter_ratio`` is
    ``None`` there.
    """
    for unit in ("_s", "_hz", "_ns", "_v", "_rad", "_dbm"):
        if quantity.endswith(unit):
            return f"{quantity[: -len(unit)]}_stderr{unit}"
    return f"{quantity}_stderr"


def _classify(value: Any) -> tuple[float | None, bool]:
    """``(usable float or None, is_nonscalar)``.

    Two different kinds of "no number here", and conflating them lies to the
    operator:

    * MISSING — ``None`` or a non-finite float. Both roads reach here: an
      in-memory fit dict carries NaN (pydantic's ``model_dump`` does not scrub
      it) while a fit read back from JSON carries ``null``
      (``datastore._scrub``). This means the fit did not resolve.
    * NON-SCALAR — a list or dict. The record-only diagnostics
      (``qubit_relaxation_flux``, ``qubit_echo_flux``, ``qubit_tomography``)
      legitimately publish per-point ARRAYS in ``fit``. Those fits SUCCEEDED;
      there is simply no scalar to take a mean of.
    """
    if value is None:
        return None, False
    if isinstance(value, (list, tuple, dict, str)):
        return None, True
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None, True
    return (out, False) if math.isfinite(out) else (None, False)


def _finite(value: Any) -> float | None:
    """The usable float of a fit value, or None (see :func:`_classify`)."""
    return _classify(value)[0]


def _slope(values: list[float], elapsed_s: list[float]) -> float | None:
    """Least-squares slope of value vs elapsed seconds, or None if undefined."""
    n = len(values)
    if n < 3:
        return None
    mean_x = sum(elapsed_s) / n
    mean_y = sum(values) / n
    sxx = sum((x - mean_x) ** 2 for x in elapsed_s)
    if sxx <= 0:  # every repeat stamped the same instant — no time axis to fit
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(elapsed_s, values))
    return sxy / sxx


def summarize(
    values: Sequence[Any],
    elapsed_s: Sequence[float] | None = None,
    stderrs: Sequence[Any] | None = None,
    stderr_key: str | None = None,
) -> dict[str, Any]:
    """Statistics of one (experiment, target, quantity) over the repeats.

    ``values`` is one entry per repeat, in repeat order; ``None`` and
    non-finite floats both count as missing. ``elapsed_s`` (same length) are
    seconds since the campaign start and enable ``slope_per_s``. ``stderrs``
    are the per-fit standard errors of the same quantity, when the fit
    published a twin key.

    ``n`` + ``n_missing`` + ``n_nonscalar`` always equals the number of repeats:
    a fit that did not resolve and a fit that published an ARRAY (the record-only
    flux diagnostics) are counted apart, because only the first is a failure.

    Returns the :data:`STAT_KEYS` dict. ``std`` uses ddof=1 and is ``None`` for
    n < 2; ``mad_sigma = 1.4826 * mad`` is the std-comparable robust spread;
    ``slope_per_s`` needs n >= 3. ``scatter_ratio = std / mean_stderr`` is the
    physics question this feature exists to answer — much greater than 1 means
    real drift, around 1 means the scatter is just fit noise — and is ``None``
    when the quantity has no stderr twin.
    """
    kept: list[tuple[float, float | None]] = []
    nonscalar = 0
    for i, raw in enumerate(values):
        value, is_nonscalar = _classify(raw)
        nonscalar += is_nonscalar
        if value is None:
            continue
        stamp = (float(elapsed_s[i]) if elapsed_s is not None
                 and i < len(elapsed_s) and elapsed_s[i] is not None else None)
        kept.append((value, stamp))

    out: dict[str, Any] = {key: None for key in STAT_KEYS}
    out["n"] = len(kept)
    out["n_missing"] = len(values) - len(kept) - nonscalar
    out["n_nonscalar"] = nonscalar
    out["stderr_key"] = stderr_key

    errs = [e for e in (_finite(e) for e in (stderrs or [])) if e is not None]
    out["mean_stderr"] = (sum(errs) / len(errs)) if errs else None

    if not kept:
        return out

    vals = [v for v, _ in kept]
    n = len(vals)
    mean = sum(vals) / n
    ordered = sorted(vals)
    median = (ordered[n // 2] if n % 2
              else 0.5 * (ordered[n // 2 - 1] + ordered[n // 2]))
    deviations = sorted(abs(v - median) for v in vals)
    mad = (deviations[n // 2] if n % 2
           else 0.5 * (deviations[n // 2 - 1] + deviations[n // 2]))

    out["mean"] = mean
    out["median"] = median
    out["mad"] = mad
    out["mad_sigma"] = _MAD_TO_SIGMA * mad
    out["min"] = ordered[0]
    out["max"] = ordered[-1]
    out["first"] = vals[0]
    out["last"] = vals[-1]

    if n >= 2:
        std = math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1))
        out["std"] = std
        out["sem"] = std / math.sqrt(n)
        if out["mean_stderr"]:  # None or exactly 0.0 -> the ratio says nothing
            out["scatter_ratio"] = std / out["mean_stderr"]

    times = [t for _, t in kept]
    if all(t is not None for t in times):
        out["slope_per_s"] = _slope(vals, [float(t) for t in times])
    return out


def aggregate(repeats: Sequence[dict]) -> dict[str, dict[str, dict[str, dict]]]:
    """``{experiment: {target: {quantity: summarize(...)}}}`` over repeat rows.

    ``repeats`` are the rows ``Session.run_campaign`` accumulates: each is
    ``{"repeat_idx", "elapsed_s", "steps": [{"experiment", "fit", "outcomes",
    ...}]}``. A target that did not report SUCCESSFUL contributes a **missing
    value**, not a skipped slot — ``n_missing`` is the count the operator needs
    to see. Quantity keys are the UNION over all repeats, so a quantity that
    only sometimes resolves still gets a row, and a brand-new experiment needs
    no campaign code at all.

    A plan that runs the same experiment twice per repeat (a legitimate
    before/after step pair) contributes both to that experiment's series, in
    step order.
    """
    # {experiment: {target: {quantity: [value per slot]}}} plus a parallel
    # elapsed list, so every quantity of one experiment shares a time axis.
    series: dict[str, dict[str, dict[str, list[Any]]]] = {}
    elapsed: dict[str, list[float | None]] = {}
    slots: dict[str, int] = {}

    for row in repeats:
        row_elapsed = row.get("elapsed_s")
        for step in row.get("steps", []):
            name = step.get("experiment")
            if not name:
                continue
            fit = step.get("fit") or {}
            outcomes = step.get("outcomes") or {}
            by_target = series.setdefault(name, {})
            slot = slots.get(name, 0)
            elapsed.setdefault(name, [])
            while len(elapsed[name]) <= slot:
                elapsed[name].append(None)
            elapsed[name][slot] = row_elapsed

            targets = set(by_target) | set(fit) | set(outcomes)
            for target in targets:
                quantities = by_target.setdefault(target, {})
                good = str(outcomes.get(target, "")) == "successful"
                target_fit = fit.get(target) or {}
                for quantity in set(quantities) | set(target_fit):
                    values = quantities.setdefault(quantity, [])
                    while len(values) < slot:
                        values.append(None)
                    values.append(target_fit.get(quantity) if good else None)
            slots[name] = slot + 1

    out: dict[str, dict[str, dict[str, dict]]] = {}
    for name, by_target in series.items():
        width = slots[name]
        times = elapsed[name]
        base = times[0] if times and times[0] is not None else None
        rel = ([None] * width if base is None
               else [None if t is None else float(t) - float(base) for t in times])
        out[name] = {}
        for target, quantities in sorted(by_target.items()):
            for values in quantities.values():  # pad the trailing gaps
                while len(values) < width:
                    values.append(None)
            rendered: dict[str, dict] = {}
            for quantity in sorted(quantities):
                twin = stderr_twin(quantity)
                has_twin = twin in quantities and twin != quantity
                rendered[quantity] = summarize(
                    quantities[quantity],
                    elapsed_s=None if any(t is None for t in rel) else rel,
                    stderrs=quantities[twin] if has_twin else None,
                    stderr_key=twin if has_twin else None,
                )
            out[name][target] = rendered
    return out


def robust_summary(values: Sequence[Any], *, n_sigma: float = 3.0) -> dict[str, Any]:
    """Median/MAD outlier view over one quantity's repeats.

    Returns ``{"n_outliers", "outlier_indices", "median", "mad", "mean",
    "std"}`` where ``mean``/``std`` exclude the flagged points. Indices are
    positions in the ORIGINAL ``values`` sequence, so the operator can go find
    the offending run. Nothing is flagged below 4 finite points — that is
    scqat's ``mad_outliers`` contract, not a choice made here.
    """
    from ._scqat import mad_outlier_mask

    finite = [_finite(v) for v in values]
    mask, median, mad = mad_outlier_mask(finite, n_sigma=n_sigma)
    indices = [i for i, flagged in enumerate(mask) if flagged]
    keep = [v for i, v in enumerate(finite) if v is not None and i not in set(indices)]
    stats = summarize(keep)
    return {
        "n_outliers": len(indices),
        "outlier_indices": indices,
        "median": None if median is None or not math.isfinite(median) else median,
        "mad": None if mad is None or not math.isfinite(mad) else mad,
        "mean": stats["mean"],
        "std": stats["std"],
    }
