"""Suggested-update review: the table, the prompt, and the selection grammar.

Shared by ``scqo run`` (prompt right after the measurement) and ``scqo accept``
(decide later by run id). Everything human-
facing goes to **stderr** — stdout stays parseable result JSON (``| jq`` safe).

Selection grammar (``parse_selection``): ``a``/``all`` — every pending item;
``n``/``none``/empty — nothing (the default: no update); otherwise a comma/space
list mixing displayed row numbers (1-based), component names (``q0``, ``q0_res``),
field names (``readout_freq``) and ``component.field`` pairs. Pure functions here are unit-tested
without a TTY.
"""

from __future__ import annotations

import sys
from typing import Any

from ..suggestions import pending_count


def _fmt_value(value: Any) -> str:
    if value is None:
        return "(unmeasured)"
    return f"{value:.6g}" if isinstance(value, float) else str(value)


def format_table(suggestions: list[dict]) -> str:
    """Numbered table of a run's suggestions (row numbers are stable: stored order)."""
    header = (
        f"  {'#':>3} {'component':10} {'field':18} {'store':10} "
        f"{'current':>14}    {'suggested':>14}   status"
    )
    lines = [header]
    for i, s in enumerate(suggestions, start=1):
        unit = f" {s['unit']}" if s.get("unit") else ""
        status = s.get("status", "pending")
        if s.get("decided_by"):
            status += f" (by {s['decided_by']})"
        if s.get("origin") == "operator":  # human-proposed (scqo suggest), not a fit
            status += f" [operator: {s['proposed_by']}]" if s.get("proposed_by") else " [operator]"
        note = f"  # {s['comment']}" if s.get("comment") else ""
        lines.append(
            f"  {i:>3} {s['component']:10} {s['field']:18} {s['store']:10} "
            f"{_fmt_value(s.get('before')):>14} -> {_fmt_value(s['after']):>14}{unit}"
            f"   {status}{note}"
        )
    return "\n".join(lines)


def parse_selection(text: str, suggestions: list[dict], *, allow_decided: bool = False) -> list[int]:
    """Selection string -> 0-based indices of PENDING suggestions. Raises ValueError
    on anything unrecognized (the prompt loop re-asks). ``allow_decided`` is the
    ``--reapply`` mode: already-decided rows become selectable too."""
    text = text.strip()
    if text.lower() in ("", "n", "none"):
        return []
    selectable = [i for i, s in enumerate(suggestions)
                  if allow_decided or s.get("status", "pending") == "pending"]
    if text.lower() in ("a", "all", "y", "yes"):
        return selectable
    chosen: list[int] = []
    for token in text.replace(",", " ").split():
        if token.isdigit():
            idx = int(token) - 1  # rows are displayed 1-based
            if not 0 <= idx < len(suggestions):
                raise ValueError(f"no row #{token} (1..{len(suggestions)})")
            if idx not in selectable:
                raise ValueError(f"row #{token} is already decided (re-decide with --reapply)")
            matches = [idx]
        elif "." in token:
            name, _, field = token.partition(".")
            matches = [i for i in selectable
                       if suggestions[i]["component"] == name and suggestions[i]["field"] == field]
        else:
            matches = [i for i in selectable
                       if token in (suggestions[i]["component"], suggestions[i]["field"])]
        if not matches:
            raise ValueError(f"nothing {'selectable' if allow_decided else 'pending'} matches {token!r}")
        chosen += [i for i in matches if i not in chosen]
    return chosen


def format_summary(summary: dict) -> str:
    """Human-readable outcome of a Session.accept call. The from-value shown is the
    live value the apply overwrote (``current``) — on a reapply that differs from
    the suggestion's capture-time ``before``, and the overwrite is the point."""
    lines = []
    for item in summary.get("applied", []):
        was = item.get("current") if item.get("current") is not None else item.get("before")
        lines.append(
            f"  applied  {item['component']}.{item['field']} "
            f"{_fmt_value(was)} -> {_fmt_value(item['after'])}  [{item['store']}]"
        )
    for item in summary.get("stale", []):
        lines.append(
            f"  SKIPPED  {item['component']}.{item['field']}: suggested from "
            f"{_fmt_value(item['before'])} but the current value is "
            f"{_fmt_value(item['current'])} (stale — --force to apply anyway)"
        )
    for err in summary.get("errors", []):
        lines.append(f"  ERROR    {err}")
    lines.append(
        f"  {len(summary.get('applied', []))} applied, "
        f"{summary.get('pending_left', 0)} still pending"
    )
    return "\n".join(lines)


def _ask(prompt: str) -> str:
    """One line from stdin, prompt on stderr (stdout stays clean)."""
    print(prompt, end="", file=sys.stderr, flush=True)
    try:
        return input()
    except EOFError:
        return ""


def _confirm(prompt: str) -> bool:
    """y/yes = yes; anything else — including plain Enter — is No (the suggest
    philosophy: nothing changes unless explicitly confirmed)."""
    return _ask(prompt).strip().lower() in ("y", "yes")


def _fmt_era(era) -> str:
    cd, setup = (list(era or []) + ["", ""])[:2]
    return f"({cd}, {setup})" if (cd or setup) else "(none declared)"


def review_interactively(
    sess, run_id: str, suggestions: list[dict], *,
    force: bool = False, comment: str = "", reapply: bool = False,
) -> dict | None:
    """Print the suggestion table; on a real terminal, prompt and apply the choice.

    Default (plain Enter) applies NOTHING — the device stays unchanged. Non-TTY
    (scripts, pipes) prints the table + a decide-later hint and returns None.
    Returns the accept summary when something was applied.

    No flags needed at a terminal: guard trips become warnings + [y/N]
    confirmations (Enter = No) — an era mismatch asks once for the whole run,
    an already-decided row asks "re-apply (rollback)?", a stale row shows the
    before/current diff and asks per item. ``force``/``reapply`` (from the
    caller's command line) pre-answer those confirmations with yes; a comment
    typed at the prompt overrides the ``comment`` flag. The happy path — all
    pending, era matches, nothing stale — asks nothing beyond the selection.
    """
    if not suggestions:
        return None
    print(f"\nsuggested updates ({pending_count(suggestions)} pending):", file=sys.stderr)
    print(format_table(suggestions), file=sys.stderr)
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        # ASCII only: this line reaches consoles in whatever codepage the lab runs
        print(f"not a terminal - the device is unchanged; decide later with: scqo accept {run_id}",
              file=sys.stderr)
        return None
    while True:
        answer = _ask(
            "apply which updates? [a]ll / [n]one (default) / rows, component, field or component.field: "
        )
        try:
            # Decided rows are selectable here: the confirmation below replaces
            # the old refusal, so a rollback needs no --reapply knowledge.
            selected = parse_selection(answer, suggestions, allow_decided=True)
            break
        except ValueError as err:
            print(f"  {err}", file=sys.stderr)
    if not selected:
        print(f"nothing applied - the device is unchanged; decide later with: scqo accept {run_id}",
              file=sys.stderr)
        return None

    plan = sess.accept(run_id, indices=selected, dry_run=True)
    by_index = {item["index"]: item for item in plan["items"]}

    if not plan["era"]["match"] and not force:
        print(f"WARNING: this run was measured under cooldown/setup {_fmt_era(plan['era']['run'])} "
              f"but the device is now on {_fmt_era(plan['era']['current'])} - "
              f"its values may not transfer.", file=sys.stderr)
        if not _confirm("apply anyway? [y/N]: "):
            print(f"nothing applied - the device is unchanged; decide later with: scqo accept {run_id}",
                  file=sys.stderr)
            return None

    kept: list[int] = []
    for i in selected:
        item = by_index[i]
        row = i + 1  # rows are displayed 1-based
        if item["status"] != "pending" and not reapply:
            when = (item["decided_at"] or "")[:10] or "?"
            who = item["decided_by"] or "?"
            if item["status"] == "accepted":
                question = (f"row {row} {item['component']}.{item['field']} was accepted {when} by {who} - "
                            f"re-apply (rollback, overwriting the current "
                            f"{_fmt_value(item['current'])})? [y/N]: ")
            else:
                question = (f"row {row} {item['component']}.{item['field']} was rejected {when} by {who} - "
                            f"accept it after all? [y/N]: ")
            if not _confirm(question):
                print(f"  skipped row {row} (unchanged)", file=sys.stderr)
                continue
        elif item["status"] == "pending" and item["stale"] and not force:
            print(f"row {row} {item['component']}.{item['field']}: suggested from "
                  f"{_fmt_value(item['before'])} but the current value is "
                  f"{_fmt_value(item['current'])} (changed since this run).", file=sys.stderr)
            if not _confirm(f"  overwrite {_fmt_value(item['current'])} -> "
                            f"{_fmt_value(item['after'])}? [y/N]: "):
                print(f"  skipped {item['component']}.{item['field']} (stays pending)", file=sys.stderr)
                continue
        kept.append(i)

    if not kept:
        print(f"nothing applied - the device is unchanged; decide later with: scqo accept {run_id}",
              file=sys.stderr)
        return None

    typed = _ask("comment (optional): ").strip()
    # Explicit indices make the coarse flags safe: force/reapply widen ONLY the
    # guards the user just confirmed, for exactly the rows they kept.
    reapply_flag = reapply or any(by_index[i]["status"] != "pending" for i in kept)
    force_flag = force or not plan["era"]["match"] or any(
        by_index[i]["stale"] and by_index[i]["status"] == "pending" for i in kept
    )
    summary = sess.accept(run_id, indices=kept, comment=typed or comment,
                          force=force_flag, reapply=reapply_flag)
    print(format_summary(summary), file=sys.stderr)
    return summary
