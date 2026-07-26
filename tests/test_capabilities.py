"""Derived capability tags + the ``_capabilities`` package contract.

A tag is DERIVED from Parameters-mixin subclassing
(``scqo.experiments._derived_tags``) —
never a declared string — so it cannot lie or rot as the code evolves.
Experiments with ZERO tags are legitimate: a new experiment may not be
classifiable yet, and no test may demand tag completeness.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scqo import Session, catalog
from scqo.cli._backends import ensure_demo_experiments
from scqo.experiments._capabilities import (
    FLUX_AXIS,
    MAX_FLUX_DESC,
    MIN_FLUX_DESC,
    NUM_FLUX_DESC,
    RESET_METHOD_DESC,
    THERMALIZATION_TIME_DESC,
    FluxComponentParameters,
    QubitResetParameters,
    StateReadoutParameters,
    foreign_flux_source,
    reset_wait_ns,
)
from scqo.parameters import Parameters
from scqo.experiments import get
from scqo.testing import SimulatedBackend, demo_device


def _catalog_by_name() -> dict[str, dict]:
    ensure_demo_experiments()
    return {entry["name"]: entry for entry in catalog()}


#: derivation order is fixed: state_readout, then flux, then qubit_reset
#: (``scqo.experiments._derived_tags``)
EXPECTED_TAGS = {
    "qubit_relaxation": ["state_readout", "qubit_reset"],
    "qubit_echo": ["state_readout", "qubit_reset"],
    "qubit_ramsey": ["state_readout", "qubit_reset"],
    "qubit_power_rabi": ["state_readout", "qubit_reset"],
    "qubit_sqrb": ["state_readout", "qubit_reset"],
    "qubit_relaxation_flux": ["state_readout", "flux", "qubit_reset"],
    "qubit_echo_flux": ["state_readout", "flux", "qubit_reset"],
    "resonator_spectroscopy_flux": ["flux"],
    "qubit_spectroscopy_flux_pulse": ["flux"],
    # reset without discrimination: these pulse the qubit and read it out, so
    # shot independence needs a reset, but their probes do not return `state`
    # (pi_pulse_error's QM shell hardcodes discrimination off; the readout
    # trio works on raw per-shot IQ by construction).
    "qubit_pi_pulse_error": ["qubit_reset"],
    "pair_zz_coupler": ["qubit_reset"],
    "single_shot_readout": ["qubit_reset"],
    "readout_power": ["qubit_reset"],
    "readout_frequency": ["qubit_reset"],
    "qubit_spectroscopy": ["qubit_reset"],
    "qubit_tomography": ["qubit_reset"],
    "qubit_drag_equator": ["qubit_reset"],
    "qubit_drag_alternating": ["qubit_reset"],
    # explicitly tag-less: no qubit pulse at all, so nothing to reset and no
    # state to discriminate. Zero tags is a legitimate state, not an error.
    "resonator_spectroscopy": [],
    "resonator_spectroscopy_power_amp": [],
    "resonator_spectroscopy_power_chain": [],
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
    roster, design, vendor = demo_device()
    plain = Session(SimulatedBackend(vendor), roster, design=design)
    overlaid = Session(SimulatedBackend(vendor), roster, design=design,
                       parameter_defaults={"qubit_relaxation": {"num_points": 21}})
    for sess in (plain, overlaid):
        entries = {entry["name"]: entry for entry in sess.catalog()}
        assert entries["qubit_relaxation"]["tags"] == ["state_readout", "qubit_reset"]
        assert entries["qubit_relaxation_flux"]["tags"] == [
            "state_readout", "flux", "qubit_reset"]


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
        if "qubit_reset" in entry["tags"]:
            assert props["reset_method"]["description"] == RESET_METHOD_DESC, name
            assert (props["thermalization_time_ns"]["description"]
                    == THERMALIZATION_TIME_DESC), name


def test_flux_axis_is_the_contract_axis():
    """Every flux-tagged experiment sweeps FLUX_AXIS as its first contract axis —
    the probe-boundary name LCHQB/LCHQM emit and read."""
    entries = _catalog_by_name()
    flux_tagged = [n for n, e in entries.items() if "flux" in e["tags"]]
    assert flux_tagged  # the tag exists
    for name in flux_tagged:
        assert get(name).Contract.sweeps[0] == FLUX_AXIS, name


def test_reset_wait_precedence():
    """``reset_wait_ns`` is THE precedence point both drivers call: the per-run
    override when set, else the standing drive-channel knob (s -> ns). If the
    two backends resolved this themselves the override could come to mean
    different things on each."""
    ensure_demo_experiments()
    cls = get("qubit_relaxation")
    roster, design, vendor = demo_device()
    backend = SimulatedBackend(vendor)
    sess = Session(backend, roster, design=design)

    def experiment(**params):
        exp = cls(backend, cls.Parameters(targets=["q0"], **params))
        exp.device = sess.device  # what Session.run does before probe()
        return exp

    # the demo drive channel is seeded at 200 us
    assert reset_wait_ns(experiment(), "q0") == pytest.approx(200_000.0)
    assert reset_wait_ns(
        experiment(thermalization_time_ns=5_000.0), "q0") == pytest.approx(5_000.0)


def test_reset_method_is_the_extension_point():
    """Only thermal exists today; the selector is what active reset widens, so
    it must be a Literal (a plain str would let a typo through validation)."""
    assert QubitResetParameters().reset_method == "thermal"
    with pytest.raises(ValidationError):
        QubitResetParameters(reset_method="active")
    with pytest.raises(ValidationError):
        QubitResetParameters(thermalization_time_ns=0)


def test_relaxation_proposes_the_reset_wait():
    """The loop the capability exists for: qubit_relaxation fits T1 and proposes
    factor x T1 as the drive channel's knob — one fit, two roles, two homes."""
    ensure_demo_experiments()
    roster, design, vendor = demo_device()
    sess = Session(SimulatedBackend(vendor), roster, design=design)
    out = sess.run("qubit_relaxation",
                   {"targets": ["q0"], "num_averages": 30, "num_points": 21,
                    "thermalization_factor": 8.0})
    proposed = {(s["entity"], s["field"]): s["after"] for s in out["suggestions"]}
    t1 = out["fit"]["q0"]["t1_s"]
    assert proposed[("q0", "t1_s")] == pytest.approx(t1)
    assert proposed[("q0_xy", "thermalization_time_s")] == pytest.approx(8.0 * t1)


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
    _roster, _design, vendor = demo_device(tunable=True)  # flux carriers need z lines
    backend = SimulatedBackend(vendor)
    for use_state in (True, False):
        exp = cls(backend, cls.Parameters(targets=["q0"], num_averages=30,
                                          use_state_discrimination=use_state, **params))
        exp.sweep_axes = exp.define_sweep()
        ds = backend.acquire(exp)
        cls.Contract.validate(ds)
        assert ("state" in ds.data_vars) is use_state
        assert ("I" in ds.data_vars) is not use_state
