"""Regenerate the DERIVED blocks in CLAUDE.md from the code they describe.

Hand-kept lists rot. `report.py` already applies that rule to the viewer's field
orders (catalog-derived, never hand-kept); this script applies it to the docs.
Two blocks:

* the registered-experiment census, which had drifted to 31 of 41 by the v3.1.0
  cut - missing both cryoscopes, both broadband scans and `qubit_ramsey_phasor`,
  the flagship feature of that very release.
* the experiment -> scqat estimator map. The binding rule is 1:1 in BOTH
  directions (CLAUDE.md -> Terminology), and the tree does not yet conform: this
  block is what makes each exception visible instead of folklore, and
  `tests/test_one_estimator_per_experiment.py` is what keeps the list shrinking.

    python scripts/update_docs.py            # rewrite the blocks in place
    python scripts/update_docs.py --check    # exit 1 if a block is stale (CI)

`tests/test_docs_current.py` runs the --check form, so a new @register without a
doc refresh fails the suite.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
EXPERIMENTS = REPO_ROOT / "scqo" / "experiments"

BLOCKS = {
    "experiments": "<!-- {} generated: experiments -->",
    "estimator-map": "<!-- {} generated: estimator-map -->",
}

COLUMNS = 3


def _core_experiments() -> list[type]:
    """The CORE experiment classes, taken from the EXPORTED names.

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
        (
            obj
            for obj in (getattr(registry, n) for n in registry.__all__)
            if isinstance(obj, type) and issubclass(obj, Experiment)
        ),
        key=lambda c: c.name,
    )


def experiment_names() -> list[str]:
    return [cls.name for cls in _core_experiments()]


# ----------------------------------------------------------------- estimator map
def _resolve_bare(class_name: str) -> str:
    """`from scqat.estimators import X` names no subpackage - ask scqat which one.

    The fallback is the class name itself, so a missing scqat degrades the block
    rather than breaking the build; the row stays unique per experiment either way.
    """
    try:
        import scqat.estimators as agg

        module = getattr(agg, class_name).__module__  # scqat.estimators.<pkg>.estimator
        parts = module.split(".")
        return parts[2] if len(parts) > 2 else class_name
    except Exception:
        return class_name


def module_estimators() -> dict[str, str]:
    """`scqo/experiments/<stem>.py` -> the scqat estimator SUBPACKAGE it imports.

    Keyed on the subpackage, not the class: `readout_fidelity` serves two
    experiments through two thin subclasses, and that shared binding is exactly
    what the 1:1 rule is about, so class-keying would hide it.

    Private helpers (`scqat.estimators._twin_axis`) are not bindings and are
    skipped, as is any imported name that is not an `*Estimator`.
    """
    out: dict[str, str] = {}
    for path in sorted(EXPERIMENTS.glob("*.py")):
        if path.stem.startswith("_"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if not node.module.startswith("scqat.estimators"):
                continue
            classes = [a.name for a in node.names if a.name.endswith("Estimator")]
            if not classes:
                continue
            parts = node.module.split(".")
            if len(parts) > 2:
                if parts[2].startswith("_"):
                    continue  # a shared function module, not a binding
                out[path.stem] = parts[2]
            else:
                out[path.stem] = _resolve_bare(classes[0])
    return out


def estimator_map() -> tuple[dict[str, list[str]], list[str]]:
    """(estimator -> [experiment names], [experiments binding none]).

    Resolved through the MRO, so an experiment that inherits `estimate()` from a
    sibling (`qubit_spectroscopy_overlap`) is attributed to the estimator it
    actually runs rather than counted as binding none.
    """
    by_module = module_estimators()
    bound: dict[str, list[str]] = {}
    unbound: list[str] = []
    for cls in _core_experiments():
        estimator = None
        for ancestor in cls.__mro__:
            mod = getattr(ancestor, "__module__", "")
            if not mod.startswith("scqo.experiments."):
                continue
            hit = by_module.get(mod.rsplit(".", 1)[-1])
            if hit:
                estimator = hit
                break
        if estimator is None:
            unbound.append(cls.name)
        else:
            bound.setdefault(estimator, []).append(cls.name)
    return {k: sorted(v) for k, v in sorted(bound.items())}, sorted(unbound)


# -------------------------------------------------------------------- renderers
def render_experiments() -> str:
    names = experiment_names()
    rows = -(-len(names) // COLUMNS)  # ceil
    width = max(len(n) for n in names) + 2
    lines = []
    for r in range(rows):
        # column-major, so the alphabetical order reads DOWN each column -
        # the same shape `scqo run` prints.
        cells = [names[r + c * rows] for c in range(COLUMNS) if r + c * rows < len(names)]
        lines.append("".join(cell.ljust(width) for cell in cells).rstrip())

    begin = BLOCKS["experiments"].format("BEGIN")
    end = BLOCKS["experiments"].format("END")
    return "\n".join(
        [
            begin,
            f"**{len(names)} registered experiments.** This list is GENERATED from the registry",
            "(`scqo.catalog()`) - refresh it with `python scripts/update_docs.py`. Descriptions are",
            "catalog-quality and live in the registry, never here: read one with",
            "`scqo run <name> --help`, or browse by capability with `scqo run --capability <name>`.",
            "",
            "```",
            *lines,
            "```",
            end,
        ]
    )


def render_estimator_map() -> str:
    bound, unbound = estimator_map()
    shared = [e for e, xs in bound.items() if len(xs) > 1]
    begin = BLOCKS["estimator-map"].format("BEGIN")
    end = BLOCKS["estimator-map"].format("END")
    lines = [
        begin,
        "**GENERATED** - refresh with `python scripts/update_docs.py`. Which scqat estimator",
        "each experiment binds, resolved through the MRO (so an inherited `estimate()` is",
        "attributed to the estimator it actually runs). The rule is ONE estimator per",
        "experiment and ONE experiment per estimator - see **Terminology**. A row naming two",
        "experiments, or a name in the trailing line, is a KNOWN VIOLATION carried in",
        "`tests/test_one_estimator_per_experiment.py`; that list may only shrink.",
        "",
        "| scqat estimator | experiments |",
        "|---|---|",
    ]
    for estimator, experiments in bound.items():
        mark = " **(shared)**" if len(experiments) > 1 else ""
        lines.append(f"| `{estimator}` | {', '.join(experiments)}{mark} |")
    lines.append("")
    if unbound:
        lines.append(
            "Binds no estimator (fits inline - also a violation): "
            + ", ".join(f"`{n}`" for n in unbound)
            + "."
        )
    else:
        lines.append("Every registered experiment binds an estimator.")
    lines.append(
        f"Shared bindings: {len(shared)}"
        + ((" - " + ", ".join(f"`{e}`" for e in shared) + ".") if shared else ".")
    )
    lines.append(end)
    return "\n".join(lines)


RENDERERS = {"experiments": render_experiments, "estimator-map": render_estimator_map}


def current_block(text: str, key: str) -> str:
    begin, end = BLOCKS[key].format("BEGIN"), BLOCKS[key].format("END")
    return text[text.index(begin) : text.index(end) + len(end)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="exit 1 if stale instead of rewriting")
    args = ap.parse_args()

    text = CLAUDE_MD.read_text(encoding="utf-8")
    stale = []
    for key, render in RENDERERS.items():
        begin, end = BLOCKS[key].format("BEGIN"), BLOCKS[key].format("END")
        if begin not in text or end not in text:
            print(f"{CLAUDE_MD.name}: missing the {key} markers", file=sys.stderr)
            return 2
        have, wanted = current_block(text, key), render()
        if have != wanted:
            stale.append(key)
            text = text.replace(have, wanted)

    if not stale:
        print(f"{CLAUDE_MD.name}: generated blocks are current")
        return 0
    if args.check:
        print(
            f"{CLAUDE_MD.name}: STALE generated block(s): {', '.join(stale)}.\n"
            f"Run `python scripts/update_docs.py` and commit the result.",
            file=sys.stderr,
        )
        return 1

    CLAUDE_MD.write_text(text, encoding="utf-8", newline="\n")
    print(f"{CLAUDE_MD.name}: rewrote {', '.join(stale)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
