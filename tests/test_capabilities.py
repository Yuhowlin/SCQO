"""Derived capability tags + the ``_capabilities`` package contract.

A tag is DERIVED from Parameters-mixin subclassing
(``scqo.experiments._derived_tags``) —
never a declared string — so it cannot lie or rot as the code evolves.
Experiments with ZERO tags are legitimate: a new experiment may not be
classifiable yet, and no test may demand tag completeness.
"""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from scqo import Session, catalog
from scqo.catalog import CHANNELS
from scqo.experiments._depletion import depletion_time_s
from scqo.cli._backends import ensure_demo_experiments
from scqo.experiments._capabilities import (
    ACTIVE_RESET_ROUNDS_DESC,
    FLUX_AXIS,
    MAX_FLUX_DESC,
    MAX_FLUX_PULSE_DESC,
    MIN_FLUX_DESC,
    MIN_FLUX_PULSE_DESC,
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


#: derivation order is fixed: state_readout, then flux, then qubit_reset, then
#: flux_pulse (``scqo.experiments._derived_tags``). ``flux_pulse`` REFINES
#: ``flux`` — a relative window measured from idle_flux — so it never appears
#: without it, and its carriers' names all end in ``_pulse``.
EXPECTED_TAGS = {
    "qubit_relaxation": ["state_readout", "qubit_reset"],
    "qubit_echo": ["state_readout", "qubit_reset"],
    "qubit_ramsey": ["state_readout", "qubit_reset"],
    "qubit_power_rabi": ["state_readout", "qubit_reset"],
    "qubit_sqrb": ["state_readout", "qubit_reset"],
    "qubit_relaxation_flux_pulse": ["state_readout", "flux", "qubit_reset", "flux_pulse"],
    "qubit_echo_flux_pulse": ["state_readout", "flux", "qubit_reset", "flux_pulse"],
    "resonator_spectroscopy_flux": ["flux"],
    "qubit_spectroscopy_flux_pulse": ["flux", "flux_pulse"],
    # reset without discrimination: these pulse the qubit and read it out, so
    # shot independence needs a reset, but their probes do not return `state`
    # (pi_pulse_error's QM shell hardcodes discrimination off; the readout
    # trio works on raw per-shot IQ by construction).
    "qubit_pi_pulse_error": ["qubit_reset"],
    "pair_zz_coupler": ["qubit_reset"],
    # the swap maps sweep FLUX but are not "flux"-tagged: that capability is
    # the single-qubit z-bias sweep (FluxSweepParameters, contract axis
    # flux_bias_v), and these sweep a pair's pulse amplitudes instead. Their
    # probes hardcode discrimination, so no state_readout tag either.
    "pair_swap_chevron": ["qubit_reset"],
    "pair_swap_flux_map": ["qubit_reset"],
    "single_shot_readout": ["qubit_reset"],
    "readout_power": ["qubit_reset"],
    "readout_frequency": ["qubit_reset"],
    "qubit_spectroscopy": ["qubit_reset"],
    "qubit_spectroscopy_overlap": ["qubit_reset"],
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
        assert entries["qubit_relaxation_flux_pulse"]["tags"] == [
            "state_readout", "flux", "qubit_reset", "flux_pulse"]


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
            # the window text is per-FRAME; num_flux_points carries no frame
            # information and reuses the one constant in both
            pulse = "flux_pulse" in entry["tags"]
            assert props["min_flux_v"]["description"] == (
                MIN_FLUX_PULSE_DESC if pulse else MIN_FLUX_DESC), name
            assert props["max_flux_v"]["description"] == (
                MAX_FLUX_PULSE_DESC if pulse else MAX_FLUX_DESC), name
            assert props["num_flux_points"]["description"].startswith(NUM_FLUX_DESC), name
        if "qubit_reset" in entry["tags"]:
            assert props["reset_method"]["description"] == RESET_METHOD_DESC, name
            assert (props["thermalization_time_ns"]["description"]
                    == THERMALIZATION_TIME_DESC), name
            assert (props["active_reset_rounds"]["description"]
                    == ACTIVE_RESET_ROUNDS_DESC), name


def test_flux_axis_is_the_contract_axis():
    """Every flux-tagged experiment sweeps FLUX_AXIS as its first contract axis —
    the probe-boundary name LCHQB/LCHQM emit and read.

    Note this is now true of BOTH frames: the frame is an origin, not a
    different quantity, so it is carried by the name and the recorded
    ``old_idle_flux``, not by a second axis key. That makes
    ``test_flux_pulse_names_carry_the_suffix`` below load-bearing rather than
    decorative — it is the only check that a relative carrier announced itself.
    """
    entries = _catalog_by_name()
    flux_tagged = [n for n, e in entries.items() if "flux" in e["tags"]]
    assert flux_tagged  # the tag exists
    for name in flux_tagged:
        assert get(name).Contract.sweeps[0] == FLUX_AXIS, name


def test_the_two_flux_frames_say_different_things():
    """The absolute and relative window texts must not converge.

    They are the ONLY place the catalog states which origin a window is measured
    from, and the catalog is what an AI loop reads to choose parameters. A
    copy-paste that made them identical would erase the distinction while every
    other test still passed.
    """
    assert MIN_FLUX_DESC != MIN_FLUX_PULSE_DESC
    assert MAX_FLUX_DESC != MAX_FLUX_PULSE_DESC
    assert "idle_flux" in MIN_FLUX_PULSE_DESC
    assert "idle_flux" not in MIN_FLUX_DESC


def test_flux_pulse_names_carry_the_suffix():
    """The naming rule, as a checked property: a window measured from
    ``idle_flux`` announces itself in the registered NAME.

    Both frames share one axis key and one contract, so the name is what tells a
    human (and an AI reading the catalog) that ``flux_bias_v = 0`` means "stay
    parked" rather than "0 V on the line". Enforced in both directions, because
    a plain-frame experiment wearing ``_pulse`` misleads exactly as badly as a
    relative one without it.
    """
    entries = _catalog_by_name()
    flux_tagged = {n: e for n, e in entries.items() if "flux" in e["tags"]}
    assert flux_tagged
    for name, entry in flux_tagged.items():
        assert name.endswith("_pulse") == ("flux_pulse" in entry["tags"]), name
    # and the refinement never floats free of the capability it refines
    for name, entry in entries.items():
        if "flux_pulse" in entry["tags"]:
            assert "flux" in entry["tags"], name


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


def test_reset_method_admits_exactly_the_realized_methods():
    """Both methods validate, and the selector stays a Literal so a NEAR MISS is
    caught here rather than silently thermalizing on the instrument. 'activ'
    (not some absurd string) is the realistic typo and the reason the field was
    never a plain str.

    Widening this Literal is a cross-repo commitment: every backend that carries
    the mixin must either realize the new method or refuse it BY NAME. Adding a
    member here without a refusal path on the other backend is the bug this test
    cannot catch — see the module docstring's BOUNDARY RULE."""
    assert QubitResetParameters().reset_method == "thermal"  # default unchanged
    assert QubitResetParameters(reset_method="active").reset_method == "active"
    for typo in ("activ", "Active", "active_gef", "none"):
        with pytest.raises(ValidationError):
            QubitResetParameters(reset_method=typo)
    with pytest.raises(ValidationError):
        QubitResetParameters(thermalization_time_ns=0)


def test_active_reset_rounds_are_bounded():
    """Rounds is a per-run choice, so the schema is its only guard, and it is
    capped because each round costs a FULL readout on a fixed-round backend."""
    assert QubitResetParameters().active_reset_rounds == 1
    for bad in (0, 16, -1):
        with pytest.raises(ValidationError):
            QubitResetParameters(active_reset_rounds=bad)


def test_the_depletion_settle_is_device_state_not_a_parameter():
    """It briefly lived on this mixin as active_reset_depletion_ns and that was
    wrong: the photon-depletion time is a property of the resonator and the
    readout condition, identical for every experiment that measures it, and it
    has a real vendor field on both backends. So it is the readout channel's
    readout_depletion_s KNOB (placement rule step 4), proposed from the measured
    linewidth by resonator_spectroscopy — the same shape as t1_s ->
    thermalization_time_s one level over."""
    assert "active_reset_depletion_ns" not in QubitResetParameters.model_fields
    assert "readout_depletion_s" in CHANNELS["readout"].fields
    assert CHANNELS["readout"].fields["readout_depletion_s"].role == "knob"


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


def test_resonator_spectroscopy_proposes_the_depletion_wait():
    """The readout twin of the test above, and the reason both exist: ONE fit,
    TWO roles, TWO homes. The linewidth is sample physics and stays a resonator
    FACT; factor / (2 pi x kappa) is an operating choice realized by a vendor
    field, so it becomes a KNOB on the readout CHANNEL. Getting that split wrong
    is how a value ends up in physical.json where nothing pushes it."""
    ensure_demo_experiments()
    roster, design, vendor = demo_device()
    sess = Session(SimulatedBackend(vendor), roster, design=design)
    out = sess.run("resonator_spectroscopy",
                   {"targets": ["q0"], "num_averages": 30, "num_points": 51,
                    "depletion_factor": 4.0})
    proposed = {(s["entity"], s["field"]): s["after"] for s in out["suggestions"]}
    kappa = out["fit"]["q0"]["kappa_tot_hz"]

    assert proposed[("q0_res", "kappa_tot_hz")] == pytest.approx(kappa)
    assert proposed[("q0_ro", "readout_depletion_s")] == pytest.approx(
        depletion_time_s(kappa, 4.0))
    # the factor is a choice, the linewidth is a fact: the knob must MOVE with it
    assert proposed[("q0_ro", "readout_depletion_s")] == pytest.approx(
        4.0 / (2 * math.pi * kappa))


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
    ("qubit_relaxation_flux_pulse", {"num_flux_points": 5, "num_wait_points": 11}),
    ("qubit_echo_flux_pulse", {"num_flux_points": 5, "num_wait_points": 11}),
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
