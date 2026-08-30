"""The estimator binding is 1:1 in BOTH directions — enforced, with the backlog visible.

An estimator is keyed by a READING (a dataset shape AND the model fitted to it), so a
binding is a claim that this model describes this signal. Two experiments binding one
estimator assert the same physics; if they really do, they are one experiment. The rule and
its decision procedure live in `CLAUDE.md` → *The estimator binding*.

The tree does not conform yet, and pretending otherwise would make the rule folklore. So
the exceptions are listed here with their migrations, and the test fails **in both
directions**: a new violation that is not listed fails, and a listed violation that has
been fixed fails until its entry is deleted. The lists can only shrink — never add to them.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "update_docs.py"
EXPERIMENTS = REPO_ROOT / "scqo" / "experiments"

#: estimator -> (the experiments bound to it, the migration that removes the entry).
KNOWN_SHARED_BINDINGS: dict[str, tuple[tuple[str, ...], str]] = {
    "qubit_spectroscopy": (
        ("qubit_spectroscopy", "qubit_spectroscopy_overlap"),
        "The overlap variant's line is AC-Stark shifted by the live readout tone — a "
        "different MODEL that shares a Lorentzian only by coincidence. Migration: its own "
        "estimator package over the tools/peak_fit reduction, and decide there whether to "
        "FIT the shift instead of folding it into f_01_hz.",
    ),
    "readout_fidelity": (
        ("readout_frequency", "readout_power"),
        "Different dataset SHAPES bridged by a `sweep_coord` class attribute, which is why "
        "ReadoutFreqFidelityEstimator is 13 lines of one coordinate name. Migration: "
        "tools/fidelity_sweep.py + one estimator per experiment (state_iq_arrays moves to "
        "tools/discriminate.py first — tools must not import estimators).",
    ),
    "resonator_spectroscopy_power": (
        ("resonator_spectroscopy_power_amp", "resonator_spectroscopy_power_chain"),
        "One physical measurement split by a converter limitation — finite DAC resolution "
        "and a log-scale power input that degrades over a wide span. An instrument concern "
        "in the physics catalog. Migration: merge the two experiments, mechanism as an "
        "advanced-user knob over a backend-chosen default.",
    ),
    "state_discrimination": (
        ("qubit_thermal_population", "single_shot_readout", "single_shot_readout_gef"),
        "Identical shape and identical GMM model; only the label mapping differs (2x2 "
        "majority diagonal / permutations(range(3)) / pinned centers) and it lives in "
        "SCQO's estimate(), i.e. fitting logic in the orchestration layer. Migration: three "
        "estimators over tools/discriminate.py + estimators/_iq_plane.py.",
    ),
}

#: experiment -> the migration that gives it an estimator.
KNOWN_UNBOUND: dict[str, str] = {
    "qubit_pi_pulse_error": (
        "Fits inline with np.polyfit (parabola vertex), draws its own PNG, and reaches into "
        "scqat's PRIVATE estimators._twin_axis. Migration: move the fit and the figure into "
        "a scqat estimator package."
    ),
}


@pytest.fixture(scope="module")
def update_docs():
    """The generator owns the derivation; this test only judges it."""
    assert SCRIPT.is_file(), f"missing {SCRIPT}"
    spec = importlib.util.spec_from_file_location("update_docs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["update_docs"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bindings(update_docs):
    return update_docs.estimator_map()


def test_no_unlisted_shared_binding(bindings):
    """One experiment per estimator — every exception must be declared here."""
    bound, _ = bindings
    shared = {e for e, xs in bound.items() if len(xs) > 1}
    unlisted = sorted(shared - set(KNOWN_SHARED_BINDINGS))
    assert not unlisted, (
        f"estimator(s) bound by more than one experiment and not listed in "
        f"KNOWN_SHARED_BINDINGS: {unlisted}. Work the decision procedure in CLAUDE.md -> "
        f"The estimator binding: the answer is a merge or a tools/ reduction, never a "
        f"second binding."
    )


def test_listed_shared_bindings_still_exist(bindings):
    """The allowlist may only shrink: a fixed entry must be deleted, not left behind."""
    bound, _ = bindings
    shared = {e: tuple(xs) for e, xs in bound.items() if len(xs) > 1}
    for estimator, (experiments, migration) in KNOWN_SHARED_BINDINGS.items():
        assert estimator in shared, (
            f"{estimator!r} is listed in KNOWN_SHARED_BINDINGS but is no longer shared — "
            f"the migration landed. Delete its entry.\n  was: {migration}"
        )
        assert shared[estimator] == tuple(sorted(experiments)), (
            f"{estimator!r} is bound by {shared[estimator]}, but KNOWN_SHARED_BINDINGS "
            f"declares {tuple(sorted(experiments))}. A binding was added or removed; update "
            f"the entry deliberately rather than widening it by accident."
        )


def test_no_unlisted_unbound_experiment(bindings):
    """One estimator per experiment — an experiment that fits inline must be declared."""
    _, unbound = bindings
    unlisted = sorted(set(unbound) - set(KNOWN_UNBOUND))
    assert not unlisted, (
        f"experiment(s) binding no scqat estimator and not listed in KNOWN_UNBOUND: "
        f"{unlisted}. Analysis belongs in scqat, not in estimate()."
    )


def test_listed_unbound_experiments_still_exist(bindings):
    _, unbound = bindings
    for name, migration in KNOWN_UNBOUND.items():
        assert name in unbound, (
            f"{name!r} is listed in KNOWN_UNBOUND but now binds an estimator — the "
            f"migration landed. Delete its entry.\n  was: {migration}"
        )


def test_no_experiment_module_imports_two_estimators():
    """The structural half, checked against the SOURCE rather than the derived map.

    `estimator_map` resolves one estimator per experiment by construction (first MRO hit
    wins), so it cannot see a module that imports two. This can, and it is the shape a
    "just run both readings" change would take.
    """
    offenders: dict[str, list[str]] = {}
    for path in sorted(EXPERIMENTS.glob("*.py")):
        if path.stem.startswith("_"):
            continue
        found: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if not node.module.startswith("scqat.estimators"):
                continue
            if not any(a.name.endswith("Estimator") for a in node.names):
                continue
            parts = node.module.split(".")
            if len(parts) > 2 and not parts[2].startswith("_"):
                found.add(parts[2])
        if len(found) > 1:
            offenders[path.stem] = sorted(found)
    assert not offenders, (
        f"experiment module(s) importing more than one scqat estimator: {offenders}. An "
        f"experiment binds exactly one estimator; two readings of one protocol are two "
        f"registered experiments (re-estimate the saved dataset.nc offline to avoid "
        f"re-acquiring)."
    )
