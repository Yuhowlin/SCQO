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
    DETUNING_AXIS,
    END_DRIVE_DETUNING_DESC,
    END_READOUT_DETUNING_DESC,
    FLUX_AXIS,
    MAX_AMP_FACTOR_DESC,
    MAX_FLUX_DESC,
    MAX_FLUX_PULSE_DESC,
    MIN_AMP_FACTOR_DESC,
    MIN_FLUX_DESC,
    MIN_FLUX_PULSE_DESC,
    NUM_AMP_POINTS_DESC,
    NUM_AMP_POINTS_OPTIONAL_DESC,
    NUM_FLUX_DESC,
    NUM_FREQ_POINTS_DESC,
    RESET_METHOD_DESC,
    START_DRIVE_DETUNING_DESC,
    START_READOUT_DETUNING_DESC,
    THERMALIZATION_TIME_DESC,
    AmplitudeSweepParameters,
    DriveDetuningSweepParameters,
    ReadoutDetuningSweepParameters,
    FluxComponentParameters,
    QubitResetParameters,
    StateReadoutParameters,
    drive_detuning_sweep,
    foreign_flux_source,
    readout_detuning_sweep,
    reset_wait_ns,
    window_bounds,
)
from scqo.parameters import Parameters
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
    "qubit_power_rabi": ["state_readout", "qubit_reset", "amplitude"],
    "qubit_deterministic_benchmarking": ["state_readout", "qubit_reset", "amplitude"],
    "qubit_sqrb": ["state_readout", "qubit_reset"],
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
    # the swap maps sweep FLUX but do not carry "flux": that capability is
    # the single-qubit z-bias sweep (FluxSweepParameters, contract axis
    # flux_bias_v), and these sweep a pair's pulse amplitudes instead. Their
    # probes hardcode discrimination, so no state_readout either.
    "pair_swap_chevron": ["qubit_reset"],
    "pair_swap_flux_map": ["qubit_reset"],
    "qc_n_stark_amp": ["qubit_reset"],
    "qc_n_swap_amp": ["qubit_reset"],
    "single_shot_readout": ["qubit_reset"],
    "single_shot_readout_gef": ["qubit_reset"],
    "qubit_thermal_population": ["qubit_reset"],
    # T1 trackers: qubit_reset only — their probes ALWAYS discriminate (the
    # on-FPGA math consumes the state bit), so there is no I/Q-vs-state choice
    # and no StateReadoutParameters mixin.
    "qubit_t1_ade": ["qubit_reset"],
    "qubit_t1_bayesian": ["qubit_reset"],
    "readout_power": ["qubit_reset", "amplitude"],
    "readout_frequency": ["qubit_reset", "readout_detuning"],
    "qubit_spectroscopy": ["qubit_reset", "drive_detuning"],
    "qubit_spectroscopy_overlap": ["qubit_reset", "drive_detuning"],
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
        "drive_detuning", "readout_detuning"]
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
            assert props["min_flux_v"]["description"] == (
                MIN_FLUX_PULSE_DESC if pulse else MIN_FLUX_DESC), name
            assert props["max_flux_v"]["description"] == (
                MAX_FLUX_PULSE_DESC if pulse else MAX_FLUX_DESC), name
            assert props["num_flux_points"]["description"].startswith(NUM_FLUX_DESC), name
        if "amplitude" in entry["capabilities"]:
            # every carrier re-declares the window (each has its own defaults), so
            # the TEXT is the only thing stopping four descriptions drifting apart
            assert props["min_amp_factor"]["description"] == MIN_AMP_FACTOR_DESC, name
            assert props["max_amp_factor"]["description"] == MAX_AMP_FACTOR_DESC, name
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
    flux_carriers = {n: e for n, e in entries.items() if "flux" in e["capabilities"]}
    assert flux_carriers
    for name, entry in flux_carriers.items():
        assert name.endswith("_pulse") == ("flux_pulse" in entry["capabilities"]), name
    # and the refinement never floats free of the capability it refines
    for name, entry in entries.items():
        if "flux_pulse" in entry["capabilities"]:
            assert "flux" in entry["capabilities"], name


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
                   {"targets": ["q0"], "num_averages": 30,
                    "num_readout_freq_points": 51, "depletion_factor": 4.0})
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
    channel = "q0_ro" if knob == "readout_amp" else "q0_xy"
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
    sess.set_values({"q0_xy.pi_amp": 0.15, "q1_xy.pi_amp": 0.35})
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
#: the four carriers — every drive-frequency window in the registry. The
#: readout-side detuning sweeps share the axis NAME but are relative to
#: readout_freq_hz, and carry the SIBLING readout_detuning capability instead.
DRIVE_DETUNING_CARRIERS = {
    "qubit_spectroscopy",
    "qubit_spectroscopy_overlap",
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


def test_drive_detuning_edges_take_either_order():
    """The pair DEFINES the window, it does not choose a traversal direction.

    A reversed pair is accepted and normalised to the SAME ascending axis --
    the two orderings are the identical measurement. Normalising here is what
    keeps scqat safe: `peak_fit` builds its width bound as
    `detuning[-1] - detuning[0]` with no abs(), so a descending axis inverts it
    and the fit degrades silently (a 4 MHz line measured back as 174 MHz).
    """
    up = DriveDetuningSweepParameters(start_drive_detuning_hz=-80e6,
                                      end_drive_detuning_hz=20e6,
                                      num_drive_freq_points=101)
    down = DriveDetuningSweepParameters(start_drive_detuning_hz=20e6,
                                        end_drive_detuning_hz=-80e6,
                                        num_drive_freq_points=101)
    axis_up = drive_detuning_sweep(up)[DETUNING_AXIS]
    axis_down = drive_detuning_sweep(down)[DETUNING_AXIS]
    assert axis_up == pytest.approx(axis_down)          # same measurement
    assert np.all(np.diff(axis_down) > 0)               # ... and ascending
    assert axis_down[0] == pytest.approx(-80e6)
    assert axis_down[-1] == pytest.approx(20e6)
    # the edges themselves are preserved verbatim -- only the AXIS is ordered
    assert down.start_drive_detuning_hz == 20e6

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


def test_readout_detuning_edges_take_either_order():
    """The readout frame's twin of the drive rule — and the frame where writing
    the pair 'downward' is the natural one, since a punchout walks the dip DOWN
    from f_dress0 toward f_bare."""
    up = ReadoutDetuningSweepParameters(start_readout_detuning_hz=-15e6,
                                        end_readout_detuning_hz=10e6,
                                        num_readout_freq_points=51)
    down = ReadoutDetuningSweepParameters(start_readout_detuning_hz=10e6,
                                          end_readout_detuning_hz=-15e6,
                                          num_readout_freq_points=51)
    axis_up = readout_detuning_sweep(up)[DETUNING_AXIS]
    axis_down = readout_detuning_sweep(down)[DETUNING_AXIS]
    assert axis_up == pytest.approx(axis_down)
    assert np.all(np.diff(axis_down) > 0)
    assert axis_down[0] == pytest.approx(-15e6)

    with pytest.raises(ValidationError, match="zero-width"):
        ReadoutDetuningSweepParameters(start_readout_detuning_hz=0.0,
                                       end_readout_detuning_hz=0.0)


def test_window_bounds_is_the_one_ordering_point():
    """Every 'is x inside the window?' test must go through this helper: a
    chained ``start <= x <= end`` is silently ALWAYS FALSE on a reversed pair,
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
