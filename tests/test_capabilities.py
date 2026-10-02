"""Derived capabilities + the ``_capabilities`` package contract.

A capability is DERIVED from Parameters-mixin subclassing
(``scqo.experiments._derived_capabilities``) —
never a declared string — so it cannot lie or rot as the code evolves.
Experiments with ZERO capabilities are legitimate: a new experiment may not be
classifiable yet, and no test may demand capability completeness. (Not "tags":
that word belongs to the datastore's user-attached run tags.)
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from pydantic import ValidationError

from scqo import Session, catalog
from scqo.catalog import CHANNELS
from scqo.experiments._depletion import depletion_time_s
from scqo.cli._backends import ensure_demo_experiments
from scqo.experiments._capabilities import (
    ABS_AMP_COORD,
    ACTIVE_RESET_ROUNDS_DESC,
    AMP_AXIS,
    COUPLER_FLUX_AXIS,
    DETUNING_AXIS,
    DRIVE_LINE_DESC,
    READOUT_MEMBER_DESC,
    SELECTIVE_PI_LEN_DESC,
    END_AMP_FACTOR_DESC,
    END_COUPLER_FLUX_DESC,
    NUM_COUPLER_FLUX_DESC,
    START_COUPLER_FLUX_DESC,
    END_DRIVE_DETUNING_DESC,
    END_FLUX_DESC,
    END_FLUX_PULSE_DESC,
    END_READOUT_DETUNING_DESC,
    FLUX_AXIS,
    NUM_AMP_POINTS_DESC,
    NUM_AMP_POINTS_OPTIONAL_DESC,
    NUM_FLUX_DESC,
    NUM_FREQ_POINTS_DESC,
    RESET_METHOD_DESC,
    START_AMP_FACTOR_DESC,
    START_DRIVE_DETUNING_DESC,
    START_FLUX_DESC,
    START_FLUX_PULSE_DESC,
    START_READOUT_DETUNING_DESC,
    THERMALIZATION_TIME_DESC,
    AmplitudeSweepParameters,
    DriveDetuningSweepParameters,
    ReadoutDetuningSweepParameters,
    FluxComponentParameters,
    FluxPulseSweepParameters,
    FluxSweepParameters,
    QubitResetParameters,
    StateReadoutParameters,
    amp_sweep,
    drive_detuning_sweep,
    flux_sweep,
    foreign_flux_source,
    readout_detuning_sweep,
    reset_wait_ns,
)
from scqo.parameters import Parameters
from scqo.experiments._window import window_bounds
from scqo.experiments import get
from scqo.testing import SimulatedBackend, demo_device


def _catalog_by_name() -> dict[str, dict]:
    ensure_demo_experiments()
    return {entry["name"]: entry for entry in catalog()}


def _core_catalog() -> dict[str, dict]:
    """``_catalog_by_name`` restricted to the EXPORTED core classes.

    Any test that asserts an exact CARRIER SET must use this: other test
    modules ``@register`` deliberately-broken fixtures, and
    ``tests/test_datastore.py`` builds two of them (``broken_resonator_spectroscopy``,
    ``update_explodes``) by SUBCLASSING ResonatorSpectroscopy — so they inherit
    its Parameters mixins and derive its capabilities for real. The live
    registry then holds them or not depending on test ORDER. Same
    selection-by-type as :func:`test_every_experiment_is_pinned_here`."""
    from scqo import experiments as registry
    from scqo.experiment import Experiment

    core = {obj.name for obj in (getattr(registry, n) for n in registry.__all__)
            if isinstance(obj, type) and issubclass(obj, Experiment)}
    return {name: entry for name, entry in _catalog_by_name().items()
            if name in core}


#: derivation order is fixed: state_readout, then flux, then qubit_reset, then
#: flux_pulse (``scqo.experiments._derived_capabilities``). ``flux_pulse``
#: REFINES ``flux`` — a relative window measured from idle_flux — so it never
#: appears without it, and its carriers' names all end in ``_pulse``.
EXPECTED_CAPABILITIES = {
    "qubit_relaxation": ["state_readout", "qubit_reset"],
    "qubit_echo": ["state_readout", "qubit_reset"],
    "qubit_ramsey": ["state_readout", "qubit_reset"],
    # the first carrier of the coupler pair of capabilities: drive_line (a named,
    # possibly borrowed, drive channel) and mapped_readout (read through a member)
    "qubit_power_rabi": ["state_readout", "qubit_reset", "amplitude", "drive_line",
                         "mapped_readout"],
    "qubit_deterministic_benchmarking": ["state_readout", "qubit_reset", "amplitude"],
    "qubit_sqrb": ["state_readout", "qubit_reset"],
    "crosstalk_compensated_sqrb": ["state_readout", "qubit_reset"],
    "crosstalk_cancellation_tomography": ["state_readout", "qubit_reset"],
    # ramsey cryoscope: state_readout + qubit_reset, but NO flux capability — the
    # flux-pulse amplitude is a scalar parameter, not a swept window, so it does
    # not subclass the flux mixins; the swept axis is the pulse duration.
    "qubit_ramsey_cryoscope": ["state_readout", "qubit_reset"],
    "qubit_ramsey_phasor": ["state_readout", "qubit_reset"],
    # spectroscopy cryoscope: same flux reasoning — the flux amplitude is a
    # scalar parked excursion, not a swept window; the swept axes are the drive
    # detuning (the drive_detuning window, origin refined to the parked drive)
    # and the (log-spaced) wait time.
    "qubit_spectroscopy_cryoscope": ["state_readout", "qubit_reset", "drive_detuning"],
    # xyz delay: like cryoscope, NO flux capability — the Z pulse amplitude
    # (z_pulse_amp_v) is a scalar parameter, not a swept flux window, so it does
    # not subclass the flux mixins; the swept axes are prepared_state and the
    # relative XY/Z timing.
    "qubit_xyz_delay": ["state_readout", "qubit_reset"],
    # stark phase echo: state_readout + qubit_reset, but NO amplitude capability — the
    # swept window is a factor of the STARK operation's baked amplitude, not of a
    # target knob (pi_amp/readout_amp), so it owns min/max_stark_amp instead of
    # subclassing AmplitudeSweepParameters (same reasoning as qc_n_stark_amp).
    "qubit_stark_phase_echo": ["state_readout", "qubit_reset"],
    "qubit_relaxation_flux_pulse": ["state_readout", "flux", "qubit_reset", "flux_pulse"],
    "qubit_echo_flux_pulse": ["state_readout", "flux", "qubit_reset", "flux_pulse"],
    "qubit_ramsey_flux_pulse": ["state_readout", "flux", "qubit_reset", "flux_pulse"],
    # parametric drive (both siblings): state_readout + qubit_reset, but NO flux
    # capability — the swept axes are the modulation TONE's own amplitude
    # (absolute volts of a new RF drive, not a z-bias window), its frequency
    # (absolute Hz, not a detuning around a standing knob) and its raw driving
    # duration, so none of the sweep mixins apply.
    "qubit_parametric_drive_amp": ["state_readout", "qubit_reset"],
    "qubit_parametric_drive_time": ["state_readout", "qubit_reset"],
    # parity monitors: state_readout only — deliberately NO qubit_reset. In the
    # continuous variant the readout is the running XOR of the parity (each
    # shot inverts with the pole the last one left), so a reset would sever the
    # chain the rate is fitted from; in the discrete variant M1's projection IS
    # the initialization. Both keep the depletion-only wait as their timebase.
    "qubit_parity_switch_continuous": ["state_readout"],
    "qubit_parity_switch_discrete": ["state_readout"],
    "resonator_spectroscopy_flux": ["flux", "readout_detuning"],
    "qubit_spectroscopy_flux_pulse": ["flux", "flux_pulse", "drive_detuning"],
    # reset without discrimination: these pulse the qubit and read it out, so
    # shot independence needs a reset, but their probes do not return `state`
    # (pi_pulse_error's QM shell hardcodes discrimination off; the readout
    # trio works on raw per-shot IQ by construction).
    "qubit_pi_pulse_error": ["qubit_reset", "amplitude"],
    "pair_zz_coupler": ["qubit_reset"],
    # the coupler-crossing scan: its window is the COUPLER's flux pulse (relative to
    # the coupler's idle_flux), not a target z-line window, so coupler_flux and not
    # flux; discrimination is hardcoded (joint populations), so no state_readout.
    "pair_coupler_crossing_pulse": ["qubit_reset", "coupler_flux"],
    # the coupler swap spectroscopy: its tone window is ABSOLUTE Hz (no drive-
    # detuning origin - the coupler has no drive knob) and its ramp is a fixed
    # waveform, not a swept coupler-flux window; discrimination is hardcoded.
    "pair_coupler_spectroscopy_swap": ["qubit_reset"],
    "pair_coupler_spectroscopy_zz": ["qubit_reset"],
    # the swap maps sweep FLUX but do not carry "flux": that capability is
    # the single-qubit z-bias sweep (FluxSweepParameters, contract axis
    # flux_bias_v), and these sweep a pair's pulse amplitudes instead. Their
    # probes hardcode discrimination, so no state_readout either.
    "pair_swap_chevron": ["qubit_reset"],
    "pair_swap_angle": ["qubit_reset"],
    "pair_swap_flux_map": ["qubit_reset"],
    "qc_n_stark_amp": ["qubit_reset"],
    "qc_n_swap_amp": ["qubit_reset"],
    "qc_swap_flux_stark": ["qubit_reset"],
    # the Trotter chain: qubit_reset only. Its Stark compensation is a fixed
    # per-qubit FACTOR of the stark operation's baked amplitude, not a swept
    # window of a target knob, so it carries no `amplitude` capability (same
    # reasoning as qc_n_stark_amp); the only swept axis is the round count.
    "qc_trotter_compensation": ["qubit_reset"],
    "qc_unidirectional_trotter": ["qubit_reset"],
    "single_shot_readout": ["qubit_reset"],
    "qubit_thermal_population": ["qubit_reset"],
    # T1 trackers: qubit_reset only — their probes ALWAYS discriminate (the
    # on-FPGA math consumes the state bit), so there is no I/Q-vs-state choice
    # and no StateReadoutParameters mixin.
    "qubit_t1_ade": ["qubit_reset"],
    "qubit_t1_bayesian": ["qubit_reset"],
    "readout_power": ["qubit_reset", "amplitude"],
    "readout_frequency": ["qubit_reset", "readout_detuning"],
    # readout_time_of_flight: NONE, and deliberately. It prepares no qubit
    # state (so no qubit_reset), discriminates nothing (so no
    # state_readout - the variables are raw ADC samples), and sweeps no
    # knob at all: its one axis is digitizer TIME, which is not a
    # capability window. The decision is written down, which is what this
    # map demands; zero capabilities stays legitimate.
    "readout_time_of_flight": [],
    "qubit_spectroscopy": ["qubit_reset", "drive_detuning"],
    # the Stark tone's amplitude is a factor of readout_amp; the drive sweeps a
    # drive-detuning window, as in qubit_spectroscopy
    "qubit_resonator_stark": ["qubit_reset", "amplitude", "drive_detuning"],
    "qubit_tomography": ["qubit_reset"],
    "qubit_drag_equator": ["qubit_reset"],
    "qubit_drag_alternating": ["qubit_reset"],
    "broadband_qubit_spectroscopy": ["qubit_reset"],
    # explicitly capability-less: no qubit pulse at all, so nothing to reset and
    # no state to discriminate. Zero capabilities is a legitimate state, not an
    # error.
    "resonator_spectroscopy": ["readout_detuning"],
    "resonator_spectroscopy_power_amp": ["readout_detuning"],
    "resonator_spectroscopy_power_chain": ["readout_detuning"],
    # the wideband SEARCH stays capability-less: its span is absolute Hz, not a
    # detuning window around a known readout_freq_hz.
    "broadband_resonator_spectroscopy": [],
}


def test_capabilities_derived_from_mixins():
    entries = _catalog_by_name()
    for name, caps in EXPECTED_CAPABILITIES.items():
        assert entries[name]["capabilities"] == caps, (
            f"{name}: {entries[name]['capabilities']}")
    # every catalog entry carries the key (possibly empty)
    assert all("capabilities" in entry for entry in entries.values())


def test_every_experiment_is_pinned_here():
    """The map is checked key-by-key, so an experiment MISSING from it has its
    capabilities unpinned entirely — which is how qubit_deterministic_benchmarking
    went unchecked. An entry of ``[]`` is still an entry, so this does not demand
    capability completeness (zero capabilities stays legitimate); it demands that
    the DECISION was written down.

    Enumerated from the EXPORTED classes, not the live registry: other test
    modules ``@register`` deliberately-broken fixtures (``broken_contract``,
    ``update_explodes``, ...) which would otherwise make this fail on test
    ORDER. Same reasoning, and the same selection-by-type, as
    ``test_model_experiments.CORE``.
    """
    from scqo import experiments as registry
    from scqo.experiment import Experiment

    core = {obj.name for obj in (getattr(registry, n) for n in registry.__all__)
            if isinstance(obj, type) and issubclass(obj, Experiment)}
    assert core - set(EXPECTED_CAPABILITIES) == set(), (
        "add these to EXPECTED_CAPABILITIES (use [] if they carry no capability): "
        f"{sorted(core - set(EXPECTED_CAPABILITIES))}"
    )


def test_capability_summaries_track_the_derived_set():
    """CAPABILITY_SUMMARIES feeds the CLI catalog browser (`scqo run
    --capability`); its keys must be exactly the derivable capabilities, in
    derivation order, or the browser lies about what exists. 'none' is NOT a
    key — it is the absence of capabilities, rendered by the CLI itself."""
    from scqo.experiments._capabilities import CAPABILITY_SUMMARIES

    assert list(CAPABILITY_SUMMARIES) == [
        "state_readout", "flux", "qubit_reset", "flux_pulse", "amplitude",
        "drive_detuning", "readout_detuning", "coupler_flux", "drive_line",
        "mapped_readout"]
    assert set(CAPABILITY_SUMMARIES) == {
        cap for caps in EXPECTED_CAPABILITIES.values() for cap in caps}
    # one short plain line each: no reST markup, no scraped "Mixin:" prefix
    for cap, summary in CAPABILITY_SUMMARIES.items():
        assert summary and "`" not in summary and not summary.startswith("Mixin"), cap


def test_capabilities_survive_session_catalog_overlay():
    """Session.catalog() passes capabilities through — both the verbatim path
    (no parameter_defaults) and the deepcopy overlay path."""
    ensure_demo_experiments()
    roster, design, vendor = demo_device()
    plain = Session(SimulatedBackend(vendor), roster, design=design)
    overlaid = Session(SimulatedBackend(vendor), roster, design=design,
                       parameter_defaults={"qubit_relaxation": {"num_points": 21}})
    for sess in (plain, overlaid):
        entries = {entry["name"]: entry for entry in sess.catalog()}
        assert entries["qubit_relaxation"]["capabilities"] == [
            "state_readout", "qubit_reset"]
        assert entries["qubit_relaxation_flux_pulse"]["capabilities"] == [
            "state_readout", "flux", "qubit_reset", "flux_pulse"]


def test_canonical_field_text_never_drifts():
    """A carrier inherits (or re-declares with the DESC constants) the mixin's
    field text, so the catalog description can never drift per-experiment."""
    entries = _catalog_by_name()
    state_desc = StateReadoutParameters.model_fields["use_state_discrimination"].description
    for name, entry in entries.items():
        props = entry["parameters_schema"]["properties"]
        if "state_readout" in entry["capabilities"]:
            assert props["use_state_discrimination"]["description"] == state_desc, name
        if "flux" in entry["capabilities"]:
            # the window text is per-FRAME; num_flux_points carries no frame
            # information and reuses the one constant in both
            pulse = "flux_pulse" in entry["capabilities"]
            assert props["start_flux_v"]["description"] == (
                START_FLUX_PULSE_DESC if pulse else START_FLUX_DESC), name
            assert props["end_flux_v"]["description"] == (
                END_FLUX_PULSE_DESC if pulse else END_FLUX_DESC), name
            assert props["num_flux_points"]["description"].startswith(NUM_FLUX_DESC), name
        if "amplitude" in entry["capabilities"]:
            # every carrier re-declares the window (each has its own defaults), so
            # the TEXT is the only thing stopping four descriptions drifting apart
            assert props["start_amp_factor"]["description"] == START_AMP_FACTOR_DESC, name
            assert props["end_amp_factor"]["description"] == END_AMP_FACTOR_DESC, name
            # deterministic_benchmarking allows a single point and says so
            assert props["num_amp_points"]["description"] in (
                NUM_AMP_POINTS_DESC, NUM_AMP_POINTS_OPTIONAL_DESC), name
        if "qubit_reset" in entry["capabilities"]:
            assert props["reset_method"]["description"] == RESET_METHOD_DESC, name
            assert (props["thermalization_time_ns"]["description"]
                    == THERMALIZATION_TIME_DESC), name
            assert (props["active_reset_rounds"]["description"]
                    == ACTIVE_RESET_ROUNDS_DESC), name
        if "drive_detuning" in entry["capabilities"]:
            # carriers may APPEND an origin refinement (the cryoscope's parked
            # drive), so the check is startswith — the shared wording still pins
            assert props["start_drive_detuning_hz"]["description"].startswith(
                START_DRIVE_DETUNING_DESC), name
            assert props["end_drive_detuning_hz"]["description"].startswith(
                END_DRIVE_DETUNING_DESC), name
            assert props["num_drive_freq_points"]["description"].startswith(
                NUM_FREQ_POINTS_DESC), name
        if "readout_detuning" in entry["capabilities"]:
            # same startswith rule (readout_frequency appends its chi-scale
            # note to the point count)
            assert props["start_readout_detuning_hz"]["description"].startswith(
                START_READOUT_DETUNING_DESC), name
            assert props["end_readout_detuning_hz"]["description"].startswith(
                END_READOUT_DETUNING_DESC), name
            assert props["num_readout_freq_points"]["description"].startswith(
                NUM_FREQ_POINTS_DESC), name
        if "coupler_flux" in entry["capabilities"]:
            assert props["start_coupler_flux_v"]["description"] == START_COUPLER_FLUX_DESC, name
            assert props["end_coupler_flux_v"]["description"] == END_COUPLER_FLUX_DESC, name
            assert props["num_coupler_flux_points"]["description"].startswith(
                NUM_COUPLER_FLUX_DESC), name
        if "drive_line" in entry["capabilities"]:
            assert props["drive_line"]["description"] == DRIVE_LINE_DESC, name
        if "mapped_readout" in entry["capabilities"]:
            assert props["readout_member"]["description"] == READOUT_MEMBER_DESC, name
            assert props["selective_pi_len_ns"]["description"] == SELECTIVE_PI_LEN_DESC, name
        # the zz spectroscopy's selective pi IS the map's, one text for both
        if "selective_pi_len_ns" in props:
            assert props["selective_pi_len_ns"]["description"] == SELECTIVE_PI_LEN_DESC, name


def test_flux_axis_is_the_contract_axis():
    """Every flux-capable experiment sweeps FLUX_AXIS as its first contract axis —
    the probe-boundary name LCHQB/LCHQM emit and read.

    Note this is now true of BOTH frames: the frame is an origin, not a
    different quantity, so it is carried by the name and the recorded
    ``old_idle_flux``, not by a second axis key. That makes
    ``test_flux_pulse_names_carry_the_suffix`` below load-bearing rather than
    decorative — it is the only check that a relative carrier announced itself.
    """
    entries = _catalog_by_name()
    flux_carriers = [n for n, e in entries.items() if "flux" in e["capabilities"]]
    assert flux_carriers  # the capability exists
    for name in flux_carriers:
        assert get(name).Contract.sweeps[0] == FLUX_AXIS, name


def test_the_two_flux_frames_say_different_things():
    """The absolute and relative window texts must not converge.

    They are the ONLY place the catalog states which origin a window is measured
    from, and the catalog is what an AI loop reads to choose parameters. A
    copy-paste that made them identical would erase the distinction while every
    other test still passed.
    """
    assert START_FLUX_DESC != START_FLUX_PULSE_DESC
    assert END_FLUX_DESC != END_FLUX_PULSE_DESC
    assert "idle_flux" in START_FLUX_PULSE_DESC
    assert "idle_flux" not in START_FLUX_DESC


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
    flux_carriers = {n: e for n, e in entries.items() if "flux" in e["capabilities"]}
    assert flux_carriers
    for name, entry in flux_carriers.items():
        assert name.endswith("_pulse") == ("flux_pulse" in entry["capabilities"]), name
    # and the refinement never floats free of the capability it refines
    for name, entry in entries.items():
        if "flux_pulse" in entry["capabilities"]:
            assert "flux" in entry["capabilities"], name


def test_coupler_flux_carriers_carry_the_pulse_suffix():
    """The coupler window has ONE frame - a pulse relative to the coupler's
    idle_flux - so every carrier announces it in its name, sweeps
    COUPLER_FLUX_AXIS, and the window text names its origin."""
    entries = _catalog_by_name()
    carriers = [n for n, e in entries.items() if "coupler_flux" in e["capabilities"]]
    assert carriers  # the capability exists
    for name in carriers:
        assert name.endswith("_pulse"), name
        assert COUPLER_FLUX_AXIS in get(name).Contract.sweeps, name
    assert "idle_flux" in START_COUPLER_FLUX_DESC and "idle_flux" in END_COUPLER_FLUX_DESC


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
    assert proposed[("xy_q0.q0", "thermalization_time_s")] == pytest.approx(8.0 * t1)


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
                   {"targets": ["q0"], "num_averages": 30,
                    "num_readout_freq_points": 51, "depletion_factor": 4.0})
    proposed = {(s["entity"], s["field"]): s["after"] for s in out["suggestions"]}
    kappa = out["fit"]["q0"]["kappa_tot_hz"]

    assert proposed[("q0_res", "kappa_tot_hz")] == pytest.approx(kappa)
    assert proposed[("fl.q0", "readout_depletion_s")] == pytest.approx(
        depletion_time_s(kappa, 4.0))
    # the factor is a choice, the linewidth is a fact: the knob must MOVE with it
    assert proposed[("fl.q0", "readout_depletion_s")] == pytest.approx(
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
    ("qubit_ramsey_flux_pulse", {"num_flux_points": 5, "num_idle_points": 11}),
])
def test_population_contract_accepted_for_newly_wired(name, params):
    """The newly wired carriers emit `population` (no I/Q) in discriminated mode
    and I/Q otherwise — and their Contract validates BOTH shapes (`state` is
    reserved for PER-SHOT outcomes under the readout schema)."""
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
        assert ("population" in ds.data_vars) is use_state
        assert ("I" in ds.data_vars) is not use_state


# --------------------------------------------------------------------------
# amplitude capability: the absolute amplitude behind a swept RATIO
# --------------------------------------------------------------------------
#: (experiment, extra params, the knob each ratio multiplies). Every carrier
#: sweeps the ONE canonical axis, AMP_AXIS — that is the point of the capability.
AMPLITUDE_CARRIERS = [
    ("qubit_power_rabi", {"num_amp_points": 21}, "pi_amp"),
    ("qubit_pi_pulse_error", {"num_amp_points": 11}, "pi_amp"),
    ("readout_power", {"num_amp_points": 5, "num_shots": 200}, "readout_amp"),
    ("qubit_deterministic_benchmarking",
     {"num_amp_points": 5, "target_gate": "x90", "max_repetitions": 20},
     "pi_amp_x90"),
    # a fresh demo device has no readout_depletion_s, which this one refuses
    # without — the per-run override stands in for it
    ("qubit_resonator_stark",
     {"num_amp_points": 4, "num_drive_freq_points": 41, "readout_depletion_ns": 1000.0},
     "readout_amp"),
]


@pytest.mark.parametrize("name,params,knob", AMPLITUDE_CARRIERS)
def test_amplitude_carriers_attach_the_absolute_axis(name, params, knob, tmp_path):
    """Every ratio sweep also carries the ABSOLUTE amplitude it stood for.

    Without this the absolute value exists only in the fit dict as
    ``old_<knob> * factor``, so a saved dataset cannot be read without
    separately recovering the device snapshot from the moment of the run.
    """
    import xarray as xr

    ensure_demo_experiments()
    roster, design, vendor = demo_device()
    channel = "fl.q0" if knob == "readout_amp" else "xy_q0.q0"
    base = float(getattr(vendor.component(channel), knob))
    sess = Session(SimulatedBackend(vendor), roster, design=design,
                   scqo_dir=tmp_path / "scqo", data_root=tmp_path / "data")
    out = sess.run(name, {"targets": ["q0"], **params})

    with xr.open_dataset(f"{out['data_path']}/dataset.nc") as ds:
        coord = ds[ABS_AMP_COORD]
        assert coord.dims == ("target", AMP_AXIS)
        # dimensionless (a fraction of full scale), NOT volts and NOT dBm
        assert coord.attrs["units"] == ""
        assert coord.attrs["reference_field"] == knob
        ratios = ds.coords[AMP_AXIS].values.astype(float)
        assert coord.sel(target="q0").values == pytest.approx(ratios * base)


def test_absolute_axis_is_per_target_which_is_why_the_input_stays_a_ratio(tmp_path):
    """THE property the design turns on: one shared ratio axis, a DIFFERENT
    absolute axis per target. A single shared absolute input window could not
    express this, which is why the parameter stays a ratio."""
    import xarray as xr

    ensure_demo_experiments()
    roster, design, vendor = demo_device()
    sess = Session(SimulatedBackend(vendor), roster, design=design,
                   scqo_dir=tmp_path / "scqo", data_root=tmp_path / "data")
    sess.set_values({"xy_q0.q0.pi_amp": 0.15, "xy_q1.q1.pi_amp": 0.35})
    out = sess.run("qubit_power_rabi", {"targets": ["q0", "q1"], "num_amp_points": 21})

    with xr.open_dataset(f"{out['data_path']}/dataset.nc") as ds:
        coord = ds[ABS_AMP_COORD]
        ratios = ds.coords[AMP_AXIS].values.astype(float)
        assert coord.sel(target="q0").values == pytest.approx(ratios * 0.15)
        assert coord.sel(target="q1").values == pytest.approx(ratios * 0.35)


def test_an_unreadable_reference_never_fails_the_run(tmp_path):
    """The axis is PROVENANCE: when a target's reference knob cannot be read the
    coordinate is simply absent, never an exception. A measurement that already
    reached the instrument must not die over a decoration."""
    import xarray as xr

    from scqo.experiments._capabilities.amplitude import attach_absolute_amp

    ensure_demo_experiments()
    roster, design, vendor = demo_device()
    sess = Session(SimulatedBackend(vendor), roster, design=design,
                   scqo_dir=tmp_path / "scqo", data_root=tmp_path / "data")
    out = sess.run("qubit_power_rabi", {"targets": ["q0"], "num_amp_points": 11})

    class Stub:
        """An experiment whose reference knob does not resolve."""
        dataset = xr.open_dataset(f"{out['data_path']}/dataset.nc").drop_vars(
            ABS_AMP_COORD)

        def amp_reference_field(self):
            return "not_a_catalogued_field"

    stub = Stub()
    attach_absolute_amp(stub)  # must not raise
    assert ABS_AMP_COORD not in stub.dataset.coords
    stub.dataset.close()


def test_every_amplitude_carrier_declares_its_reference_knob():
    """A carrier that overrides the attach hook must also name the knob its ratio
    multiplies — and it must be a real catalogued KNOB, resolvable from the bare
    field name (catalog.py guarantees field names are unique across channel kinds,
    which is why the declaration is a name and not a (kind, field) pair)."""
    ensure_demo_experiments()
    knob_fields = {f for kind in CHANNELS.values()
                   for f, spec in kind.fields.items() if spec.role == "knob"}
    for name, _params, knob in AMPLITUDE_CARRIERS:
        cls = get(name)
        assert knob in knob_fields, f"{name}: {knob} is not a catalogued knob"
        # NOT always first: pi_pulse_error is ("gate_count", AMP_AXIS)
        assert AMP_AXIS in cls.Contract.sweeps, f"{name}: {AMP_AXIS} is not a sweep"
        assert issubclass(cls.Parameters, AmplitudeSweepParameters), name


def test_the_amplitude_capability_is_derived_from_the_mixin():
    """Every carrier of the window Parameters carries it, and nothing else does."""
    entries = _catalog_by_name()
    carriers = {n for n, e in entries.items() if "amplitude" in e["capabilities"]}
    assert carriers == {name for name, _p, _k in AMPLITUDE_CARRIERS}


# --------------------------------------------------------------------------
# drive_detuning capability: the swept drive-frequency window
# --------------------------------------------------------------------------
#: every drive-frequency window in the registry. The readout-side detuning
#: sweeps share the axis NAME but are relative to readout_freq_hz, and carry
#: the SIBLING readout_detuning capability instead.
DRIVE_DETUNING_CARRIERS = {
    "qubit_resonator_stark",
    "qubit_spectroscopy",
    "qubit_spectroscopy_cryoscope",
    "qubit_spectroscopy_flux_pulse",
}


def test_the_drive_detuning_capability_is_derived_from_the_mixin():
    """Every carrier of the window Parameters carries it, nothing else does,
    and each sweeps the ONE canonical axis — that is the point of the
    capability."""
    entries = _core_catalog()
    carriers = {n for n, e in entries.items()
                if "drive_detuning" in e["capabilities"]}
    assert carriers == DRIVE_DETUNING_CARRIERS
    for name in carriers:
        cls = get(name)
        # NOT always first: flux_pulse is (flux_bias_v, detuning_hz)
        assert DETUNING_AXIS in cls.Contract.sweeps, name
        assert issubclass(cls.Parameters, DriveDetuningSweepParameters), name


def _assert_traversal_order(axis_up, axis_down, start, end):
    """``axis_down`` is ``axis_up`` walked the other way: same points, start
    first, NEVER re-sorted (the dataset keeps the order the probe walked)."""
    assert axis_down[0] == pytest.approx(start)
    assert axis_down[-1] == pytest.approx(end)
    assert axis_down == pytest.approx(axis_up[::-1])
    assert np.all(np.diff(axis_down) < 0)


def test_drive_detuning_edges_are_a_traversal_order():
    """start -> end IN THAT ORDER (decided 2026-09-26): a high -> low pair sweeps high
    -> low. It used to be normalised ascending, only to keep a scqat fit that
    misread a descending axis; scqat now canonicalizes on entry, so the order
    is the caller's and changes no fitted number."""
    up = DriveDetuningSweepParameters(start_drive_detuning_hz=-80e6,
                                      end_drive_detuning_hz=20e6,
                                      num_drive_freq_points=101)
    down = DriveDetuningSweepParameters(start_drive_detuning_hz=20e6,
                                        end_drive_detuning_hz=-80e6,
                                        num_drive_freq_points=101)
    _assert_traversal_order(drive_detuning_sweep(up)[DETUNING_AXIS],
                            drive_detuning_sweep(down)[DETUNING_AXIS], 20e6, -80e6)

    # a zero-width window is a typo, not a measurement
    with pytest.raises(ValidationError, match="zero-width"):
        DriveDetuningSweepParameters(start_drive_detuning_hz=0.0,
                                     end_drive_detuning_hz=0.0)


# --------------------------------------------------------------------------
# readout_detuning capability: the swept readout-frequency window
# --------------------------------------------------------------------------
#: the five carriers — every readout-frequency window in the registry.
#: readout_power sweeps AMPLITUDE at a fixed frequency and is deliberately not
#: one.
READOUT_DETUNING_CARRIERS = {
    "resonator_spectroscopy",
    "resonator_spectroscopy_power_amp",
    "resonator_spectroscopy_power_chain",
    "resonator_spectroscopy_flux",
    "readout_frequency",
}


def test_the_readout_detuning_capability_is_derived_from_the_mixin():
    """Every carrier of the window Parameters carries it, nothing else does,
    and each sweeps the ONE canonical axis — the same axis the drive frame
    emits, because a frame is an origin and not a different quantity."""
    entries = _core_catalog()
    carriers = {n for n, e in entries.items()
                if "readout_detuning" in e["capabilities"]}
    assert carriers == READOUT_DETUNING_CARRIERS
    for name in carriers:
        cls = get(name)
        # NOT always first: the flux map is (flux_bias_v, detuning_hz) and the
        # _amp punchout is (power_dbm, detuning_hz)
        assert DETUNING_AXIS in cls.Contract.sweeps, name
        assert issubclass(cls.Parameters, ReadoutDetuningSweepParameters), name


def test_readout_detuning_edges_are_a_traversal_order():
    """The readout frame's twin of the drive rule — and the frame where sweeping
    DOWN is physics you may want to see: a resonator driven into bistability
    answers differently swept up than down."""
    up = ReadoutDetuningSweepParameters(start_readout_detuning_hz=-15e6,
                                        end_readout_detuning_hz=10e6,
                                        num_readout_freq_points=51)
    down = ReadoutDetuningSweepParameters(start_readout_detuning_hz=10e6,
                                          end_readout_detuning_hz=-15e6,
                                          num_readout_freq_points=51)
    _assert_traversal_order(readout_detuning_sweep(up)[DETUNING_AXIS],
                            readout_detuning_sweep(down)[DETUNING_AXIS], 10e6, -15e6)

    with pytest.raises(ValidationError, match="zero-width"):
        ReadoutDetuningSweepParameters(start_readout_detuning_hz=0.0,
                                       end_readout_detuning_hz=0.0)


def test_window_bounds_is_the_range_check_by_value():
    """Every 'is x inside the window?' test must go through this helper: a
    chained ``start <= x <= end`` is silently ALWAYS FALSE on a descending pair,
    which would fail every good fit while looking like a physics problem."""
    assert window_bounds(-80e6, 20e6) == (-80e6, 20e6)
    assert window_bounds(20e6, -80e6) == (-80e6, 20e6)


def test_the_two_detuning_frames_are_independent_siblings():
    """Neither mixin subclasses the other and their FIELD NAMES are disjoint.

    Unlike the flux frames — where FluxPulseSweepParameters subclasses the
    absolute mixin on purpose, so a pulse carrier derives both capabilities —
    these two are siblings that one experiment could legitimately carry at once
    (a drive x readout frequency map). Shared field names would then MERGE by
    MRO into a single number silently driving both sweeps, and a subclass
    relation would make every readout carrier claim drive_detuning. Both
    failures are silent, so both are pinned here."""
    assert not issubclass(ReadoutDetuningSweepParameters, DriveDetuningSweepParameters)
    assert not issubclass(DriveDetuningSweepParameters, ReadoutDetuningSweepParameters)

    def own(cls):
        return set(cls.model_fields) - set(Parameters.model_fields)

    assert not (own(DriveDetuningSweepParameters)
                & own(ReadoutDetuningSweepParameters))


# --------------------------------------------------------------------------
# flux + amplitude windows: a traversal ORDER too (2026-09-26)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("mixin", [FluxSweepParameters, FluxPulseSweepParameters])
def test_flux_edges_are_a_traversal_order(mixin):
    """Both frames: start_flux_v -> end_flux_v as given, and the only refusal is
    a zero-width window (a long flux tail or hysteresis is exactly why the
    direction must be the caller's)."""
    up = mixin(start_flux_v=-0.1, end_flux_v=0.2, num_flux_points=31)
    down = mixin(start_flux_v=0.2, end_flux_v=-0.1, num_flux_points=31)
    _assert_traversal_order(flux_sweep(up)[FLUX_AXIS], flux_sweep(down)[FLUX_AXIS],
                            0.2, -0.1)
    with pytest.raises(ValidationError, match="zero-width"):
        mixin(start_flux_v=0.05, end_flux_v=0.05)


def test_the_old_flux_names_are_refused():
    """No aliases (no backward compatibility): extra="forbid" rejects the old
    spelling loudly rather than silently sweeping the default window."""
    with pytest.raises(ValidationError, match="min_flux_v"):
        FluxSweepParameters(min_flux_v=-0.1)
    with pytest.raises(ValidationError, match="max_amp_factor"):
        AmplitudeSweepParameters(max_amp_factor=1.2)


def test_amplitude_edges_are_a_traversal_order():
    """The old ``min < max`` validator is gone: a high -> low amplitude sweep is
    legal and played in that order; only zero width is refused."""
    up = AmplitudeSweepParameters(start_amp_factor=0.2, end_amp_factor=1.4,
                                  num_amp_points=13)
    down = AmplitudeSweepParameters(start_amp_factor=1.4, end_amp_factor=0.2,
                                    num_amp_points=13)
    _assert_traversal_order(amp_sweep(up)[AMP_AXIS], amp_sweep(down)[AMP_AXIS],
                            1.4, 0.2)
    with pytest.raises(ValidationError, match="zero-width"):
        AmplitudeSweepParameters(start_amp_factor=1.0, end_amp_factor=1.0)


@pytest.mark.parametrize("edge", ["start_amp_factor", "end_amp_factor"])
@pytest.mark.parametrize("value", [-0.1, 2.0])
def test_amplitude_bounds_hold_on_both_edges(edge, value):
    """Either edge may be the larger one now, so >= 0 and < 2 bind BOTH."""
    other = "end_amp_factor" if edge == "start_amp_factor" else "start_amp_factor"
    with pytest.raises(ValidationError, match=edge):
        AmplitudeSweepParameters(**{edge: value, other: 1.0})


def test_every_amplitude_carrier_bounds_both_edges():
    """Every carrier re-declares the pair with its own defaults, so the schema is
    where a one-sided bound would hide: each edge must keep a lower bound at or
    above 0 and an upper bound at or below 2 (QUA's amplitude_scale range)."""
    for name, entry in _catalog_by_name().items():
        if "amplitude" not in entry["capabilities"]:
            continue
        props = entry["parameters_schema"]["properties"]
        for edge in ("start_amp_factor", "end_amp_factor"):
            spec = props[edge]
            low = spec.get("minimum", spec.get("exclusiveMinimum"))
            high = spec.get("exclusiveMaximum", spec.get("maximum"))
            assert low is not None and low >= 0.0, (name, edge, spec)
            assert high is not None and high <= 2.0, (name, edge, spec)


def test_an_explicit_benchmarking_list_keeps_its_order():
    """The one carrier overriding amp_values() must honour the order too: an
    explicit list is played as given, never sorted."""
    cls = get("qubit_deterministic_benchmarking")
    listed = cls.Parameters(targets=["q0"], amp_prefactors=[1.05, 0.95, 1.0])
    assert list(amp_sweep(listed)[AMP_AXIS]) == [1.05, 0.95, 1.0]
    window = cls.Parameters(targets=["q0"], start_amp_factor=1.1,
                            end_amp_factor=0.9, num_amp_points=5)
    assert amp_sweep(window)[AMP_AXIS] == pytest.approx([1.1, 1.05, 1.0, 0.95, 0.9])
