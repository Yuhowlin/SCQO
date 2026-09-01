"""The lab report stays fenced off — enforced, in both directions.

``scqo/viewer/lab_report/`` is ONE lab's reporting convention living inside a
vendor-neutral core that other labs install. That is a deliberate decision, and
it only stays tenable while the boundary is real: exactly one line in ``scqo/``
may import the package, and no lab-specific string may leak back out of the
data file into the renderers.

Both are checked here rather than described in a docstring, because a boundary
nobody checks is a boundary that has already moved. Written the same way as
``test_one_estimator_per_experiment.py``: AST over the tree, failing whether a
new violation appears OR a listed one is cleaned up and its entry left behind.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCQO = REPO_ROOT / "scqo"
PACKAGE = SCQO / "viewer" / "lab_report"

#: the ONE module allowed to import the package, and the one call it may make.
THE_ONE_IMPORTER = SCQO / "viewer" / "app.py"
THE_ONE_SYMBOL = "register_lab_report_routes"


def _imports_lab_report(tree: ast.AST) -> list[str]:
    """Every name this module pulls out of the lab_report package."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if "lab_report" in module:
                found += [a.name for a in node.names]
        elif isinstance(node, ast.Import):
            found += [a.name for a in node.names if "lab_report" in a.name]
    return found


def _python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def test_only_app_imports_the_lab_report_package():
    """No core module may reach into the lab report. The day it moves lab-side
    this must be a directory move, not a refactor — which is only true while
    this list has exactly one entry."""
    offenders: dict[str, list[str]] = {}
    for path in _python_files(SCQO):
        if PACKAGE in path.parents or path == PACKAGE:
            continue  # the package may import itself
        names = _imports_lab_report(ast.parse(path.read_text(encoding="utf-8")))
        if names:
            offenders[str(path.relative_to(REPO_ROOT))] = names

    expected = {str(THE_ONE_IMPORTER.relative_to(REPO_ROOT)): [THE_ONE_SYMBOL]}
    assert offenders == expected, (
        "scqo/viewer/lab_report/ is fenced off: exactly one import, "
        f"{THE_ONE_SYMBOL} in {THE_ONE_IMPORTER.name}. Found: {offenders}"
    )


def test_the_package_never_imports_fastapi_outside_its_routes():
    """The builders are pure data -> bytes, the same contract _export.py states.
    Only routes.py — which exists to be mounted — may touch fastapi."""
    offenders = []
    for path in _python_files(PACKAGE):
        if path.name == "routes.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom)
                     else [])
            if any(n.split(".")[0] == "fastapi" for n in names):
                offenders.append(path.name)
    assert not offenders, f"builders must stay renderer-free: {sorted(set(offenders))}"


def test_lab_specific_strings_live_only_in_the_data_file():
    """No CJK anywhere in the package's Python — sheet names, column headers and
    the field dictionary belong to field_dictionary.toml, so another lab swaps
    that file and needs no code change."""
    def has_cjk(text: str) -> bool:
        return any("一" <= ch <= "鿿" for ch in text)

    offenders = [p.name for p in _python_files(PACKAGE)
                 if has_cjk(p.read_text(encoding="utf-8"))]
    assert not offenders, (
        "lab-specific text belongs in field_dictionary.toml, not in "
        f"{sorted(offenders)}"
    )


def test_the_data_file_ships_and_parses():
    """Loaded through importlib.resources, so this passes from an installed
    wheel and not only from a source checkout."""
    from scqo.viewer.lab_report.template import load_template

    tpl = load_template()
    assert tpl.dictionary, "the template declares no dictionary rows"
    assert len(tpl.dictionary_header) == 7
    assert tpl.sheet("data") and tpl.sheet("dashboard") and tpl.sheet("dictionary")
    row = tpl.dictionary[0]
    assert row.name and row.doc, "a dictionary row lost its name or description"


def test_the_renderers_read_their_sheet_names_from_the_template():
    """A rename in the data file must reach the workbook — otherwise the
    'swap the TOML' promise is false."""
    openpyxl = pytest.importorskip("openpyxl")
    from scqo.viewer.lab_report import lab_template_xlsx_bytes
    from scqo.viewer.lab_report.template import Template, load_template

    import scqo.viewer.lab_report.xlsx as xlsx_mod

    real = load_template()
    renamed = Template(
        sheets={**real.sheets, "data": "DATA_X", "dashboard": "DASH_X",
                "dictionary": "DICT_X"},
        dictionary_header=real.dictionary_header, dictionary=real.dictionary,
    )
    xlsx_mod.load_template.cache_clear()
    try:
        xlsx_mod.load_template = lambda: renamed  # type: ignore[assignment]
        ctx = {"device": "devX", "cooldown": "cd1", "setup_name": "s1",
               "cycle": {}, "state_rows": [], "physical_rows": []}
        book = openpyxl.load_workbook(
            __import__("io").BytesIO(lab_template_xlsx_bytes(ctx)))
        assert book.sheetnames == ["DATA_X", "DASH_X", "DICT_X"]
    finally:
        xlsx_mod.load_template = load_template  # type: ignore[assignment]
        load_template.cache_clear()
