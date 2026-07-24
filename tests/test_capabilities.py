"""Derived capability tags + the ``_capabilities`` package contract.

A tag is DERIVED from Parameters-mixin subclassing (``registry._derived_tags``) —
never a declared string — so it cannot lie or rot as the code evolves.
Experiments with ZERO tags are legitimate: a new experiment may not be
classifiable yet, and no test may demand tag completeness.
"""

from __future__ import annotations

import pytest

from scqo import Session, catalog
from scqo.cli._backends import ensure_demo_experiments
from scqo.experiments._capabilities import (
    FLUX_AXIS,
    MAX_FLUX_DESC,
    MIN_FLUX_DESC,
    NUM_FLUX_DESC,
    FluxComponentParameters,
    StateReadoutParameters,
    foreign_flux_source,
)
from scqo.parameters import Parameters
from scqo.registry import get
from scqo.testing import InMemoryDevice, SimulatedBackend, demo_roster


def _catalog_by_name() -> dict[str, dict]:
    ensure_demo_experiments()
    return {entry["name"]: entry for entry in catalog()}


#: derivation order is fixed: state_readout before flux (registry._derived_tags)
EXPECTED_TAGS = {
    "qubit_relaxation": ["state_readout"],
    "qubit_echo": ["state_readout"],
    "qubit_ramsey": ["state_readout"],
    "qubit_power_rabi": ["state_readout"],
    "qubit_sqrb": ["state_readout"],
    "qubit_relaxation_flux": ["state_readout", "flux"],
    "qubit_echo_flux": ["state_readout", "flux"],
    "resonator_spectroscopy_flux": ["flux"],
    "qubit_spectroscopy_flux_pulse": ["flux"],
    # explicitly tag-less — the capability was removed (pi_pulse_error: its QM
    # shell hardcodes discrimination off) or never applied (coupler bias is not
    # the flux capability). Zero tags is a legitimate state, not an error.
    "qubit_pi_pulse_error": [],
    "pair_zz_coupler": [],
    "resonator_spectroscopy": [],
}


def test_tags_derived_from_mixins():
    entries = _catalog_by_name()
    for name, tags in EXPECTED_TAGS.items():
        assert entries[name]["tags"] == tags, f"{name}: {entries[name]['tags']}"
    # every catalog entry carries the key (possibly empty)
    assert all("tags" in entry for entry in entries.values())


def test_tags_survive_session_catalog_overlay():
    """Session.catalog() passes tags through — both the verbatim path (no
    parameter_defaults) and the deepcopy overlay path."""
    ensure_demo_experiments()
    device = InMemoryDevice({"q0": {"readout_freq": 5.95e9, "drive_freq": 3.87e9,
                                    "pi_amp": 0.2, "readout_amp": 0.25}})
    plain = Session(SimulatedBackend(device), demo_roster())
    overlaid = Session(SimulatedBackend(device), demo_roster(),
                       parameter_defaults={"qubit_relaxation": {"num_points": 21}})
    for sess in (plain, overlaid):
        entries = {entry["name"]: entry for entry in sess.catalog()}
        assert entries["qubit_relaxation"]["tags"] == ["state_readout"]
        assert entries["qubit_relaxation_flux"]["tags"] == ["state_readout", "flux"]


def test_canonical_field_text_never_drifts():
    """A carrier inherits (or re-declares with the DESC constants) the mixin's
    field text, so the catalog description can never drift per-experiment."""
    entries = _catalog_by_name()
    state_desc = StateReadoutParameters.model_fields["use_state_discrimination"].description
    for name, entry in entries.items():
        props = entry["parameters_schema"]["properties"]
        if "state_readout" in entry["tags"]:
            assert props["use_state_discrimination"]["description"] == state_desc, name
        if "flux" in entry["tags"]:
            assert props["min_flux_v"]["description"] == MIN_FLUX_DESC, name
            assert props["max_flux_v"]["description"] == MAX_FLUX_DESC, name
            assert props["num_flux_points"]["description"].startswith(NUM_FLUX_DESC), name


def test_flux_axis_is_the_contract_axis():
    """Every flux-tagged experiment sweeps FLUX_AXIS as its first contract axis —
    the probe-boundary name LCHQB/LCHQM emit and read."""
    entries = _catalog_by_name()
    flux_tagged = [n for n, e in entries.items() if "flux" in e["tags"]]
    assert flux_tagged  # the tag exists
    for name in flux_tagged:
        assert get(name).Contract.sweeps[0] == FLUX_AXIS, name


def test_foreign_flux_source_guard():
    class NoField(Parameters):
        pass

    class WithField(FluxComponentParameters):
        pass

    assert foreign_flux_source(NoField()) is False
    assert foreign_flux_source(WithField()) is False
    assert foreign_flux_source(WithField(flux_component="q2")) is True


@pytest.mark.parametrize("name,params", [
    ("qubit_sqrb", {"num_random_sequences": 5, "max_circuit_depth": 16}),
    ("qubit_relaxation_flux", {"num_flux_points": 5, "num_wait_points": 11}),
    ("qubit_echo_flux", {"num_flux_points": 5, "num_wait_points": 11}),
])
def test_state_contract_accepted_for_newly_wired(name, params):
    """The newly wired carriers emit `state` (no I/Q) in discriminated mode and
    I/Q otherwise — and their Contract validates BOTH shapes (the old contracts
    of the flux pair rejected `state`)."""
    ensure_demo_experiments()
    cls = get(name)
    device = InMemoryDevice({"q0": {"readout_freq": 5.95e9, "drive_freq": 3.87e9,
                                    "pi_amp": 0.2, "readout_amp": 0.25}})
    backend = SimulatedBackend(device)
    for use_state in (True, False):
        exp = cls(backend, cls.Parameters(targets=["q0"], num_averages=30,
                                          use_state_discrimination=use_state, **params))
        exp.sweep_axes = exp.define_sweep()
        ds = backend.acquire(exp)
        cls.Contract.validate(ds)
        assert ("state" in ds.data_vars) is use_state
        assert ("I" in ds.data_vars) is not use_state
