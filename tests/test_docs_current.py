"""CLAUDE.md's derived blocks must match the code they describe.

The experiment census in CLAUDE.md had drifted to 31 of 41 registered
experiments by the v3.1.0 cut - it was missing both cryoscopes, both broadband
scans, both qc_n_* calibrations and `qubit_ramsey_phasor`, the flagship feature
of that same release. The block is generated now; this test is what keeps it
that way, so a new @register cannot land with the docs left behind.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "update_docs.py"


def _load_update_docs():
    spec = importlib.util.spec_from_file_location("update_docs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["update_docs"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def update_docs():
    assert SCRIPT.is_file(), f"missing {SCRIPT}"
    return _load_update_docs()


def test_experiment_census_is_current(update_docs):
    """Every registered experiment is listed, and nothing retired lingers."""
    text = update_docs.CLAUDE_MD.read_text(encoding="utf-8")
    wanted = update_docs.render_block(update_docs.experiment_names())
    assert update_docs.current_block(text) == wanted, (
        "CLAUDE.md's experiment census is stale.\n"
        "Run `python scripts/update_docs.py` and commit the result."
    )


def test_every_registered_experiment_appears_in_claude_md(update_docs):
    """The invariant behind the census, checked independently of formatting."""
    text = update_docs.CLAUDE_MD.read_text(encoding="utf-8")
    missing = [name for name in update_docs.experiment_names() if name not in text]
    assert not missing, f"registered but undocumented in CLAUDE.md: {missing}"
