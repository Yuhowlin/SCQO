"""Shared campaign rendering + execution behind ``scqo run --repeat`` and ``scqo campaign``.

Both commands end in the same place — one :meth:`scqo.Session.run_campaign` call and
one statistics table — so the printing lives here rather than in either command.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta

from scqo.campaign import stderr_twin
from scqo.report import MEASURED_QUANTITIES

from ._review import announce_suggestions, review_campaign_interactively

#: Widths of the per-step progress line. Wide enough for the longest registered
#: experiment name (resonator_spectroscopy_power_chain) so the value column stays
#: aligned in a log an operator scans for drift.
_EXP_W = 34
_TARGET_W = 8
#: Fitted-values column. Long enough for three `%.6g` key=value pairs; a longer
#: tail pushes the duration right rather than being truncated (never hide a number).
_TAIL_W = 46


def _num(value, width: int = 11) -> str:
    """A number in %g, or '-' when the statistic is undefined for this quantity."""
    if value is None:
        return "-".rjust(width)
    return f"{value:.5g}".rjust(width)


def format_statistics(statistics: dict) -> str:
    """The per-(experiment, target, quantity) table.

    Every quantity any repeat produced is aggregated — no allow-list, so a new
    experiment needs no code here. A quantity that is only some OTHER shown
    quantity's stderr twin is folded into that row's ``mean_stderr`` instead of
    getting a line of its own.
    """
    header = (f"{'experiment':22s} {'target':8s} {'quantity':20s} {'n':>4s} {'miss':>5s} "
              f"{'mean':>11s} {'std':>11s} {'sem':>11s} {'min':>11s} {'max':>11s} {'std/err':>8s}")
    lines = [header, "-" * len(header)]
    any_ratio = False
    missing_twin = False
    nonscalar: list[str] = []
    for experiment in sorted(statistics):
        for target in sorted(statistics[experiment]):
            quantities = statistics[experiment][target]
            twins = {stderr_twin(q) for q in quantities}
            for quantity in sorted(quantities):
                if quantity in twins:
                    continue  # another quantity's stderr twin: it is that row's std/err
                stat = quantities[quantity]
                if not stat["n"] and stat.get("n_nonscalar"):
                    # A record-only diagnostic publishing per-point ARRAYS. The fit
                    # SUCCEEDED; there is just no scalar to average. Showing it as a
                    # row of dashes would read as "the measurement failed".
                    nonscalar.append(f"{experiment}/{target}/{quantity}")
                    continue
                ratio = stat.get("scatter_ratio")
                any_ratio = any_ratio or ratio is not None
                missing_twin = missing_twin or (stat.get("stderr_key") is None)
                lines.append(
                    f"{experiment:22s} {target:8s} {quantity:20s} "
                    f"{stat['n']:>4d} {stat['n_missing']:>5d} "
                    f"{_num(stat['mean'])} {_num(stat['std'])} {_num(stat['sem'])} "
                    f"{_num(stat['min'])} {_num(stat['max'])} "
                    f"{'-'.rjust(8) if ratio is None else f'{ratio:8.2f}'}"
                )
    if len(lines) == 2 and not nonscalar:
        return "no statistics: no repeat produced a successful fit"
    if len(lines) == 2:
        lines = lines[:1]
    if nonscalar:
        # ASCII only: this text reaches consoles in whatever codepage the lab runs.
        lines += ["", "array-valued, nothing to average (record-only; see the run folders): "
                      + ", ".join(nonscalar)]
    if any_ratio or missing_twin:
        lines += [
            "",
            "std/err = across-repeat scatter / mean per-fit standard error:",
            "  >> 1 the qubit really moved between repeats; ~ 1 the spread is just fit noise.",
        ]
    if missing_twin:
        lines.append("  '-' means the fit publishes no standard error for that quantity "
                     "(e.g. t2_star_s), not that the scatter is zero.")
    return "\n".join(lines)


def _fmt_duration(seconds: float) -> str:
    """A wall-clock duration, ASCII, no unit library: 2.4s / 4m12s / 8h07m."""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"
    return f"{int(seconds // 3600)}h{int((seconds % 3600) // 60):02d}m"


def _fmt_scalar(value) -> str | None:
    """One fit value in the house ``%.6g`` style; None for anything not scalar."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return f"{float(value):.6g}"


def _headline(fit: dict, limit: int = 3) -> str:
    """The fitted quantities worth putting on a progress line, ``key=value``.

    Chosen from :data:`scqo.report.MEASURED_QUANTITIES` — catalogued facts and
    monitors plus the fit-only quantities — so an experiment writing catalogued
    fields gets a good line for free and this never becomes a per-experiment
    allow-list. That picks t1_s / t2_star_s / t2_echo_s / p_e_given_g, drops the
    uncatalogued amplitude / offset / mean_e_i, and drops KNOBS like
    drive_freq_hz, which are the run's standing settings and cannot drift.

    A brand-new experiment writing nothing catalogued still gets a line: fall
    back to its first couple of non-stderr-twin scalars.
    """
    picked = [q for q in MEASURED_QUANTITIES if q in fit]
    if not picked:
        twins = {stderr_twin(q) for q in fit}
        picked = [q for q in sorted(fit) if q not in twins][:2]
    parts = []
    for quantity in picked[:limit]:
        rendered = _fmt_scalar(fit[quantity])
        parts.append(f"{quantity}={rendered}" if rendered else f"{quantity}=<array>")
    return "  ".join(parts)


def _eta(event: dict, plan) -> str:
    """``eta 04:22 (+8h07m)`` for the repeat about to start, or "" when unknown.

    Precedence: an open-ended plan is bounded by ``max_duration_s``; a cadenced
    plan that is keeping up finishes on its cadence; otherwise extrapolate from
    the median of the last few repeats. Repeat 0 has no history and gets nothing
    rather than a guess.
    """
    durations = list(event.get("durations_s") or [])
    planned = event.get("repeat_planned")
    done = event["repeat_idx"]

    if planned is None:
        if not plan.max_duration_s:
            return ""
        remaining = plan.max_duration_s - event.get("elapsed_s", 0.0)
    else:
        if not durations:
            return ""
        recent = sorted(durations[-5:])
        median = recent[len(recent) // 2]
        left = planned - done  # includes the one about to start
        if plan.period_s and median <= plan.period_s:
            remaining = max(0, left - 1) * plan.period_s + median
        else:
            remaining = left * median
    if remaining <= 0:
        return ""
    now = datetime.now()
    at = now + timedelta(seconds=remaining)
    clock = at.strftime("%H:%M") if at.date() == now.date() else at.strftime("%m-%d %H:%M")
    return f"eta {clock} (+{_fmt_duration(remaining)})"


def progress_lines(event: dict, plan) -> list[str]:
    """Render one campaign progress event as console lines (pure: no I/O).

    stderr-bound, so ASCII only and plain newlines — never ``\\r``: on QM the
    vendor's ``progress_counter`` is already rewriting a bar on stdout, and a
    second rewriter would fight it.
    """
    kind = event.get("kind")
    if kind == "cadence_wait":
        return [f"# waiting {_fmt_duration(event['wait_s'])} for the next repeat "
                f"(period_s={plan.period_s:g})"]

    if kind == "repeat_start":
        planned = event.get("repeat_planned")
        counter = f"{event['repeat_idx'] + 1:>4}/{planned}" if planned else \
                  f"{event['repeat_idx'] + 1:>4}"
        started = str(event.get("started_at", ""))[11:19]
        eta = _eta(event, plan)
        late = (f"   late by {_fmt_duration(event['overran_by_s'])}"
                if event.get("overran_by_s") else "")
        return [f"# repeat {counter}  started {started}"
                + (f"   {eta}" if eta else "") + late]

    if kind != "step_done":
        return []  # repeat_done adds nothing the next repeat_start does not carry

    name = event["experiment"]
    took = _fmt_duration(event["duration_s"])
    outcomes = event.get("outcomes") or {}
    fit = event.get("fit") or {}
    error = (event.get("error") or "").splitlines()[0][:80] if event.get("error") else ""

    if not outcomes:  # a hard failure before any target reported
        return [f"#   {name:{_EXP_W}s} {'-':{_TARGET_W}s} FAILED  {error or 'no outcome'}"]

    lines = []
    for target in sorted(outcomes):
        if str(outcomes[target]) == "successful":
            tail = _headline(fit.get(target) or {})
        else:
            tail = error or str(outcomes[target])
        status = "ok    " if str(outcomes[target]) == "successful" else "FAILED"
        lines.append(f"#   {name:{_EXP_W}s} {target:{_TARGET_W}s} {status} "
                     f"{tail:{_TAIL_W}s} {took:>7s}".rstrip())
    return lines


def stderr_progress(plan):
    """The callback ``execute`` hands to ``run_campaign``: print, never raise."""
    def emit(event: dict) -> None:
        for line in progress_lines(event, plan):
            print(line, file=sys.stderr, flush=True)

    return emit


def parameter_default_lines(sess, plan) -> list[str]:
    """Which standing defaults each step picks up from the lab's parameters file.

    The campaign twin of the note ``scqo run`` prints, one row per step: a plan
    deliberately restates no physics, so without this the operator cannot tell
    what a step will actually run without reaching for ``--dry-run``. A key the
    plan supplies itself is NOT listed — the file no longer governs it.
    """
    source = getattr(sess, "parameter_defaults_source", None) or "the parameters file"
    rows: list[tuple[str, list[str]]] = []
    for step in plan.steps:
        supplied = set(plan.step_params(step))
        applied = sorted(k for k in (sess.parameter_defaults.get(step.experiment) or {})
                         if k not in supplied)
        if applied:
            rows.append((step.experiment, applied))
    if not rows:
        return []
    width = max(len(name) for name, _ in rows)
    return [f"# parameter defaults from {source}:"] + [
        f"#   {name:{width}s}  {', '.join(applied)}" for name, applied in rows
    ]


def writeback_note_lines(manifest: dict) -> list[str]:
    """Why the aggregate writeback proposed nothing — ``[]`` when it did.

    Pure and list-returning like :func:`parameter_default_lines`, so the
    campaign's own tail and ``scqo campaign --show`` render the same text from
    the same place. The notes are stored on the manifest (``suggestion_notes``)
    rather than printed once, because the operator who needs them is reading
    ``--show`` the morning after, when the scrollback is gone. The remedy is a
    constant here, not repeated into every stored note.
    """
    notes = manifest.get("suggestion_notes") or []
    if not notes:
        return []
    return ["writeback notes:"] + [f"  {note}" for note in notes] + [
        "  the aggregate needs min_n successful repeats: run more, or lower "
        "[writeback] min_n in the plan"]


def format_summary(manifest: dict) -> str:
    """The one-block status header printed above the table."""
    planned = manifest.get("repeat_planned")
    lines = [
        f"campaign  {manifest.get('campaign_id') or '(not saved: no data_root)'}",
        f"label     {manifest.get('label', '')}   status: {manifest.get('status')}"
        f"   ({manifest.get('stop_reason') or 'n/a'})",
        f"repeats   {manifest.get('repeat_done', 0)}"
        f"{'/' + str(planned) if planned is not None else ' (open-ended)'}"
        f"   failed: {manifest.get('repeats_failed', 0)}"
        f"   steps: {', '.join(manifest.get('experiments', []))}",
        f"window    {manifest.get('started_at')} -> {manifest.get('ended_at')}",
    ]
    if manifest.get("data_path"):
        lines.append(f"saved     {manifest['data_path']}")
    # The final manifest write failed. The child runs are on disk and already
    # campaign-stamped, so the campaign is recoverable — say how, rather than
    # leave the operator reading a summary that quietly was not saved.
    if manifest.get("persist_error"):
        lines.append(
            f"SAVE FAILED {manifest['persist_error']} - the child runs are on disk; "
            f"rebuild the index with: python -m scqo <data_root>")
    return "\n".join(lines)


def render(manifest: dict, *, as_json: bool = False) -> None:
    """Print a finished campaign: JSON for machines, summary + table for humans."""
    if as_json:
        print(json.dumps(manifest, indent=2))
        return
    print(format_summary(manifest))
    print()
    print(format_statistics(manifest.get("statistics") or {}))


def execute(sess, plan, *, update: str, tags: list[str], note: str, as_json: bool) -> int:
    """Run a campaign and print it. Returns the process exit code.

    ``update="apply"`` gets a loud warning: a re-tuning campaign moves the device
    under its own measurement, so its statistics describe the feedback loop, not
    the qubit.
    """
    for line in parameter_default_lines(sess, plan):  # stderr: stdout stays parseable
        print(line, file=sys.stderr)
    if update == "apply":
        print("# WARNING: --accept applies each repeat's updates, so the device MOVES\n"
              "#          between repeats. The resulting spread then describes the\n"
              "#          re-tuning loop, not the qubit's own drift.", file=sys.stderr)
    manifest = sess.run_campaign(plan, update=update, tags=tags, note=note,
                                 on_progress=stderr_progress(plan))
    if manifest.get("problems"):
        print("the campaign plan was refused before any hardware:", file=sys.stderr)
        for problem in manifest["problems"]:
            print(f"  {problem}", file=sys.stderr)
        return 2
    render(manifest, as_json=as_json)
    plot_statistics(sess.datastore, manifest)
    offer_suggestions(sess, manifest)  # last on screen: it is the one action left
    return 0 if manifest.get("status") == "complete" else 1


def offer_suggestions(sess, manifest: dict) -> None:
    """The campaign's accept step: what it proposes, and how to decide it.

    Without this a campaign ended at the statistics table and never mentioned
    the pending updates it had just written to ``campaign.json`` — so adding
    ``--repeat N`` to a ``scqo run`` silently removed the accept prompt that
    command gives. Everything here is stderr; stdout stays ``| jq``-parseable.

    An explanation of an ABSENCE (nothing proposed) prints whether or not there
    are rows. A campaign run with ``--accept`` has no aggregate suggestions by
    construction, and says nothing further.
    """
    for line in writeback_note_lines(manifest):
        print(line, file=sys.stderr)
    rows = manifest.get("suggestions") or []
    if not rows:
        return
    campaign_id = manifest.get("campaign_id")
    if manifest.get("persist_error"):
        # Generated in memory but the final write failed: there is no id to
        # come back to, so say so rather than print a command that cannot work.
        announce_suggestions(rows, f"saving the campaign FAILED "
                                   f"({manifest['persist_error']}) - these are NOT stored, "
                                   f"nothing to accept later")
    elif not campaign_id:
        announce_suggestions(rows, "nothing was saved (no data_root configured), so there "
                                   "is nothing to accept later")
    else:
        review_campaign_interactively(sess, campaign_id, rows)


def plot_statistics(store, manifest: dict, **kwargs) -> None:
    """Write statistics.png into the campaign folder; never fail the campaign for it.

    Losing the picture must never lose the measurement — the same doctrine as
    `scqo._scqat`'s artifact fallback and `_safe_progress`. Also fires for a
    stopped or partial campaign: you still want to see what you got.
    """
    if store is None or not manifest.get("campaign_id") or not manifest.get("statistics"):
        return  # no data_root to write into, or nothing measured to draw
    try:
        from ._campaign_plot import render_statistics

        path = render_statistics(store, manifest["campaign_id"], manifest, **kwargs)
    except Exception as err:
        print(f"# statistics figure skipped ({type(err).__name__}: {err}); "
              f"the campaign itself is unaffected", file=sys.stderr)
        return
    if path is not None:
        # stderr: stdout is the manifest under --json and must stay | jq safe.
        print(f"# figure: {path}", file=sys.stderr)
