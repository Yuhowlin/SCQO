"""The `scqo run` catalog listing (names + capability footer) and --capability.

In-process against the pure ``_catalog_listing_lines`` / ``_check_capability_flags``
helpers — the one subprocess end-to-end pass lives in test_cli_run.py (each spawn
costs seconds; the formatting logic does not need one).
"""

from __future__ import annotations

import pytest

from scqo import catalog
from scqo.cli._backends import ensure_demo_experiments
from scqo.cli._engine import (
    _catalog_listing_lines,
    _check_capability_flags,
    run_experiment_cli,
)

#: Synthetic entries: golden-line tests must not depend on the live registry.
ENTRIES = [
    {"name": "aaa_first", "maturity": "core", "capabilities": ["flux"]},
    {"name": "bbb_second", "maturity": "contrib",
     "capabilities": ["flux", "qubit_reset"]},
    {"name": "ccc_third", "maturity": "core", "capabilities": []},
]


def test_bare_listing_names_and_footer():
    lines = _catalog_listing_lines(ENTRIES, width=80)
    body = "\n".join(lines)
    assert "aaa_first" in body
    assert "bbb_second [contrib]" in body  # maturity marker survives
    assert "ccc_third" in body
    # counts computed from the entries, every capability + the none bucket
    assert lines[-2] == ("# capabilities: state_readout(0) flux(2) "
                         "qubit_reset(1) flux_pulse(0) amplitude(0) none(1)")
    assert lines[-1] == ("# filter: scqo run --capability <name>    "
                         "detail: scqo run <name> --help")
    # the NAME columns respect the width (the two meta footer lines may wrap;
    # their length follows the counts, not the terminal)
    assert all(len(line) <= 80 for line in lines[:-2])


def test_narrow_terminal_still_one_name_per_line():
    lines = _catalog_listing_lines(ENTRIES, width=10)  # narrower than any cell
    assert lines[:3] == ["aaa_first", "bbb_second [contrib]", "ccc_third"]


def test_filtered_listing_single_capability():
    lines = _catalog_listing_lines(ENTRIES, capabilities=["flux"])
    assert lines[0].startswith("# capability: flux - ")
    assert lines[0].endswith("[2 experiments]")
    assert lines[0].isascii()  # Windows consoles mangle wider glyphs
    assert lines[1:] == ["aaa_first", "bbb_second [contrib]"]


def test_filtered_listing_and_semantics():
    lines = _catalog_listing_lines(ENTRIES, capabilities=["flux", "qubit_reset"])
    assert " AND " in lines[0] and lines[0].endswith("[1 experiment]")
    assert lines[1:] == ["bbb_second [contrib]"]


def test_filtered_listing_none_bucket():
    lines = _catalog_listing_lines(ENTRIES, capabilities=["none"])
    assert "legitimate" in lines[0]  # zero capabilities is a state, not an error
    assert lines[1:] == ["ccc_third"]


def _core_entries() -> list[dict]:
    """Catalog entries for the EXPORTED experiments only.

    Other test modules ``@register`` deliberately-broken fixtures
    (``broken_contract``, ``partial_success``, ...) into the live registry, and
    those carry no capabilities — so a whole-registry assertion passes alone and
    fails in the full suite, on test ORDER, with every fixture piling into the
    ``none`` bucket. Same selection-by-type guard as
    ``test_capabilities.test_every_experiment_is_pinned_here``.
    """
    from scqo import experiments as registry
    from scqo.experiment import Experiment

    ensure_demo_experiments()
    core = {obj.name for obj in (getattr(registry, n) for n in registry.__all__)
            if isinstance(obj, type) and issubclass(obj, Experiment)}
    return [entry for entry in catalog() if entry["name"] in core]


def test_real_catalog_flux_filter_matches_the_pinned_carriers():
    """Cross-check against the live registry; the authoritative per-experiment
    pins live in test_capabilities.EXPECTED_CAPABILITIES — drift lands there first."""
    lines = _catalog_listing_lines(_core_entries(), capabilities=["flux"])
    assert lines[1:] == [
        "qubit_echo_flux_pulse",
        "qubit_relaxation_flux_pulse",
        "qubit_spectroscopy_flux_pulse",
        "resonator_spectroscopy_flux",
    ]


def test_real_catalog_none_bucket_is_the_resonator_trio():
    lines = _catalog_listing_lines(_core_entries(), capabilities=["none"])
    assert lines[1:] == [
        "resonator_spectroscopy",
        "resonator_spectroscopy_power_amp",
        "resonator_spectroscopy_power_chain",
    ]


def test_guard_refuses_experiment_name():
    with pytest.raises(SystemExit, match="drop the experiment name"):
        _check_capability_flags(["flux"], "qubit_ramsey")


def test_guard_refuses_unknown_capability():
    with pytest.raises(SystemExit, match="pick from: .*flux_pulse.*none"):
        _check_capability_flags(["bogus"], None)


def test_guard_refuses_none_combined():
    with pytest.raises(SystemExit, match="cannot combine"):
        _check_capability_flags(["none", "flux"], None)


def test_guard_accepts_valid_and_mix():
    _check_capability_flags(["flux", "qubit_reset"], None)  # no raise


def test_cli_wiring_reaches_the_guard():
    """--capability with a bad name dies at the guard, before any session or
    config is touched (safe to run in-process: no lab config is read)."""
    with pytest.raises(SystemExit, match="unknown capability"):
        run_experiment_cli(argv=["--capability", "bogus"])
