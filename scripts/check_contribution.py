"""Preflight for a contribution to the scqo stack. Read-only; paste the output in your PR.

Documentation is best-effort - every coding agent loads instruction files differently,
and some not at all. This script is the part that does not depend on anyone having read
anything. It checks the four things that actually go wrong:

  1. the SIBLING LAYOUT, because `pyproject.toml` resolves scqat as `../scqat` and each
     driver resolves scqo as `../SCQO`, so a renamed or nested folder breaks the install;
  2. EDITABLE-INSTALL FRESHNESS - the recorded version freezes at install time, so after
     a branch switch the metadata describes a tree that is no longer there;
  3. BRANCH PAIRING - CI pairs your repos by branch NAME, so a mismatch means CI tested
     your change against upstream and its green tick proved nothing;
  4. whether you are about to commit a release-ledger fragment, which contributors
     should draft in the PR body instead.

    python scripts/check_contribution.py

Exit code is 0 when nothing is wrong, 1 when something needs attention.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # py3.10
    tomllib = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parent.parent
PARENT = REPO_ROOT.parent

# folder name -> distribution name. The folder names are the dependency graph.
REPOS = {"SCQO": "scqo", "scqat": "scqat", "scqo-qblox": "scqo-qblox", "scqo-qm": "scqo-qm"}

TEST_COMMANDS = {
    "SCQO": "python -m pytest tests/test_model_experiments.py -k <stem> -q   # targeted; full suite only for shared-core",
    "scqat": "uv run --extra dev pytest tests/test_<name>_estimator.py -q    # --extra dev is required",
    "scqo-qblox": "uv run pytest tests/ -q                                   # full suite IS the targeted run",
    "scqo-qm": "<venv>/bin/python -m pytest tests/ -q                        # avoid bare `uv run`; no CI here",
}

problems: list[str] = []
notes: list[str] = []


def say(line: str = "") -> None:
    print(line)


def git(repo: Path, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def pyproject_version(repo: Path) -> str | None:
    p = repo / "pyproject.toml"
    if not p.is_file():
        return None
    if tomllib is not None:
        try:
            return tomllib.loads(p.read_text(encoding="utf-8"))["project"]["version"]
        except Exception:
            return None
    m = re.search(r'^version\s*=\s*"([^"]+)"', p.read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else None


def installed_version(dist: str) -> str | None:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(dist)
    except PackageNotFoundError:
        return None


def main() -> int:
    present = {name: PARENT / name for name in REPOS if (PARENT / name).is_dir()}

    say("scqo stack - contribution preflight")
    say("=" * 60)
    say(f"parent directory: {PARENT}")
    say()

    # --- 1. sibling layout ---------------------------------------------------
    say("1. Sibling layout")
    if "SCQO" not in present or "scqat" not in present:
        problems.append(
            "SCQO and scqat must both be present as siblings - scqo depends on scqat "
            "via the relative path ../scqat."
        )
    for name in REPOS:
        mark = "ok  " if name in present else "--  "
        say(f"   {mark}{name}{'' if name in present else '   (not cloned)'}")
    drivers = [n for n in ("scqo-qblox", "scqo-qm") if n in present]
    if not drivers:
        notes.append("No driver cloned - fine for a core or analysis change.")
    say()

    # --- 2. editable-install freshness --------------------------------------
    say("2. Installed version vs the tree (editable installs freeze at install time)")
    for name, repo in present.items():
        want, have = pyproject_version(repo), installed_version(REPOS[name])
        if have is None:
            say(f"   --  {name}: not installed in this interpreter")
            continue
        if want and want != have:
            say(f"   !!  {name}: tree says {want}, installed metadata says {have}")
            problems.append(
                f"{name}: re-run the editable install - metadata ({have}) describes a "
                f"different tree than the one you are editing ({want})."
            )
        else:
            say(f"   ok  {name}: {have}")
    say(f"   interpreter: {sys.executable}")
    say()

    # --- 3. branch pairing ---------------------------------------------------
    say("3. Branch pairing (CI pairs your repos by branch NAME)")
    branches: dict[str, str] = {}
    for name, repo in present.items():
        b = git(repo, "branch", "--show-current")
        if b is None:
            say(f"   --  {name}: not a git repo")
            continue
        branches[name] = b or "(detached HEAD)"
        say(f"   {'!!  ' if b == 'main' else 'ok  '}{name}: {branches[name]}")

    feature = {n: b for n, b in branches.items() if b not in ("main", "(detached HEAD)")}
    if not feature:
        problems.append(
            "Every repo is on main. Work on a feature branch: `git checkout -b feature/<slug>`."
        )
    elif len(set(feature.values())) > 1:
        problems.append(
            "Branch names differ across repos: "
            + ", ".join(f"{n}={b}" for n, b in sorted(feature.items()))
            + ". CI pairs by branch name, so it will test against upstream instead of "
            "your change. Use ONE name in every repo you touch."
        )
    say()

    # --- 4. release-ledger fragment ------------------------------------------
    say("4. Release ledger")
    staged = git(REPO_ROOT, "diff", "--cached", "--name-only") or ""
    tracked_new = [l for l in staged.splitlines() if l.startswith("RELEASES.d/") and l.endswith(".toml")]
    if tracked_new:
        problems.append(
            "You have staged a RELEASES.d fragment: "
            + ", ".join(tracked_new)
            + ". Contributors DRAFT the fragment in the PR body instead - the ledger may "
            "only list complete features, so a maintainer commits it after the last "
            "repo's PR lands."
        )
        say("   !!  a fragment is staged (see below)")
    else:
        say("   ok  no fragment staged - draft yours in the PR body")
    say()

    # --- 5. what to run ------------------------------------------------------
    say("5. Test command for each repo you touched")
    for name in present:
        say(f"   {name}:")
        say(f"      {TEST_COMMANDS[name]}")
    say()
    say("   Report the EXACT command you ran. Never describe a targeted run as though")
    say("   it proved the whole suite.")
    say()

    say("=" * 60)
    for n in notes:
        say(f"note: {n}")
    if problems:
        say(f"{len(problems)} thing(s) need attention:")
        for i, p in enumerate(problems, 1):
            say(f"  {i}. {p}")
        return 1
    say("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
