"""Regenerate the DERIVED blocks in CLAUDE.md from the code they describe.

Hand-kept lists rot. `report.py` already applies that rule to the viewer's field
orders (catalog-derived, never hand-kept); this script applies it to the docs.
Today it owns one block: the registered-experiment census, which had drifted to
31 of 41 by the v3.1.0 cut - missing both cryoscopes, both broadband scans and
`qubit_ramsey_phasor`, the flagship feature of that very release.

    python scripts/update_docs.py            # rewrite the block in place
    python scripts/update_docs.py --check    # exit 1 if the block is stale (CI)

`tests/test_docs_current.py` runs the --check form, so a new @register without a
doc refresh fails the suite.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"

BEGIN = "<!-- BEGIN generated: experiments -->"
END = "<!-- END generated: experiments -->"

COLUMNS = 3


def experiment_names() -> list[str]:
    """Every CORE experiment name, taken from the EXPORTED classes.

    Deliberately not `scqo.catalog()`: several test modules `@register`
    deliberately-broken fixture experiments at import time, so under the full suite
    the live registry is wider than the shipped one (46 vs 41 when this was written,
    which is how CI caught it while an isolated run passed). Selection is by TYPE,
    matching `tests/test_model_experiments.py`'s `CORE` and `test_capabilities` -
    `__all__` also re-exports the registry functions and the driver-facing capability
    surface, and a name-exclusion list would need editing every time one is added.
    """
    sys.path.insert(0, str(REPO_ROOT))
    import scqo.experiments as registry
    from scqo.experiment import Experiment

    return sorted(
        obj.name
        for obj in (getattr(registry, n) for n in registry.__all__)
        if isinstance(obj, type) and issubclass(obj, Experiment)
    )


def render_block(names: list[str]) -> str:
    """The census body that lives between the BEGIN/END markers."""
    rows = -(-len(names) // COLUMNS)  # ceil
    width = max(len(n) for n in names) + 2
    lines = []
    for r in range(rows):
        # column-major, so the alphabetical order reads DOWN each column -
        # the same shape `scqo run` prints.
        cells = [names[r + c * rows] for c in range(COLUMNS) if r + c * rows < len(names)]
        lines.append("".join(cell.ljust(width) for cell in cells).rstrip())

    return "\n".join(
        [
            BEGIN,
            f"**{len(names)} registered experiments.** This list is GENERATED from the registry",
            "(`scqo.catalog()`) - refresh it with `python scripts/update_docs.py`. Descriptions are",
            "catalog-quality and live in the registry, never here: read one with",
            "`scqo run <name> --help`, or browse by capability with `scqo run --capability <name>`.",
            "",
            "```",
            *lines,
            "```",
            END,
        ]
    )


def current_block(text: str) -> str:
    start = text.index(BEGIN)
    end = text.index(END) + len(END)
    return text[start:end]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="exit 1 if stale instead of rewriting")
    args = ap.parse_args()

    text = CLAUDE_MD.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        print(f"{CLAUDE_MD.name}: missing the {BEGIN} / {END} markers", file=sys.stderr)
        return 2

    wanted = render_block(experiment_names())
    have = current_block(text)
    if have == wanted:
        print(f"{CLAUDE_MD.name}: experiment census is current")
        return 0

    if args.check:
        print(
            f"{CLAUDE_MD.name}: experiment census is STALE.\n"
            f"Run `python scripts/update_docs.py` and commit the result.",
            file=sys.stderr,
        )
        return 1

    CLAUDE_MD.write_text(text.replace(have, wanted), encoding="utf-8", newline="\n")
    print(f"{CLAUDE_MD.name}: experiment census rewritten")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
