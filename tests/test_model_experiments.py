"""The full ported experiment catalog, end-to-end on the simulated backend:
every registered experiment runs without error on the tunable demo device,
and the re-homed writes of the load-bearing families land on the right
entities."""

import math

import numpy as np
import pytest

from scqo import Session
from scqo import experiments as registry
from scqo.experiment import Experiment
from scqo.testing import (
    InMemoryDevice,
    SimulatedBackend,
    demo_components,
    demo_design,
    demo_vendor_state,
)

#: experiments whose update() is record-only (or has no simulator writes) —
#: zero suggestions is their CORRECT outcome.
RECORD_ONLY = {"qubit_sqrb", "qubit_tomography", "qubit_echo_flux_pulse",
               "qubit_relaxation_flux_pulse", "pair_swap_chevron", "pair_swap_flux_map"}


#: the readout reference an accepted single_shot_readout would have left behind.
#: qubit_thermal_population REFUSES to run without it (one prepared state cannot
#: locate |e> on its own), and seeding it also puts the five
#: attach_readout_positions experiments on scqat's stored-axis reduction instead
#: of PCA — the path the real instruments take, so the offline sweep exercises it.
REFERENCE_BLOBS = {"pos_g_i": 0.0, "pos_g_q": 0.0, "pos_e_i": 4.0, "pos_e_q": 0.0}

#: qubit_parity_switch derives its shot count from record_time_s, and the demo
#: device's estimated shot period is ~3.9 us against a real chip's ~30 us — so
#: the physically correct 30 s default would ask for 7.7M shots here and be
#: refused by max_num_shots. Shorten it for the offline sweep rather than
#: weakening the default: 0.4 s is ~103k shots, the size the suite used before.
PARITY_DEFAULTS = {"qubit_parity_switch": {"record_time_s": 0.4}}


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("gf5d")
    roster = demo_components(tunable=True)
    design = demo_design(roster)
    vendor = InMemoryDevice(roster, demo_vendor_state(roster, design))
    s = Session(SimulatedBackend(vendor), roster, design=design,
                scqo_dir=tmp / "scqo", data_root=tmp / "data",
                device_name="chipT", setup_name="sim",
                cooldown_id="cd1",
                parameter_defaults=PARITY_DEFAULTS)
    s.set_values({f"{q}_ro.{field}": value
                  for q in ("q0", "q1")
                  for field, value in REFERENCE_BLOBS.items()})
    # qubit_parity_switch REFUSES without a governed depletion wait (the shot
    # cadence is its telegraph timebase) and a stored parity splitting (its
    # fixed idle). 250 kHz -> idle = 1 / (2 x 250 kHz) = 2000 ns, on-grid.
    s.set_values({f"{q}_ro.readout_depletion_s": 1e-6 for q in ("q0", "q1")})
    s.set_values({f"{q}_xy.parity_delta_f_hz": 250e3 for q in ("q0", "q1")})
    return s


#: the CORE catalog, taken from the exported classes rather than the live
#: registry — another test's @register must never widen this sweep. Selected by
#: TYPE, not by an exclusion list: __all__ also re-exports the registry
#: functions and the driver-facing capability surface (QubitResetParameters,
#: reset_wait_ns), and a name list would need editing every time one is added.
CORE = sorted(obj.name for obj in map(lambda n: getattr(registry, n), registry.__all__)
              if isinstance(obj, type) and issubclass(obj, Experiment))


@pytest.mark.parametrize("name", CORE)
def test_every_experiment_runs_clean(session, name):
    cls = registry.get(name)
    target = "q0_q1" if cls.target_kinds == ("qubit_pair",) else "q0"
    out = session.run(name, {"targets": [target]}, update="none")
    assert out.get("error") is None, out.get("error")


def _suggest(session, name, target="q0"):
    out = session.run(name, {"targets": [target]})
    assert out.get("error") is None, out.get("error")
    return {(s["entity"], s["field"]) for s in out["suggestions"]}


def _pair_target(name):
    return "q0_q1" if registry.get(name).target_kinds == ("qubit_pair",) else "q0"


@pytest.mark.parametrize("name", sorted(RECORD_ONLY))
def test_record_only_experiments_propose_nothing(session, name):
    """RECORD_ONLY is a CLAIM about these experiments, so check it rather than
    just documenting it: a record-only diagnostic that grows an update() must
    either leave the set or fail here."""
    assert name in CORE, f"{name} is not in the core catalog"
    assert _suggest(session, name, target=_pair_target(name)) == set()


def test_qubit_spectroscopy_writes_channel_knob_and_mode_fact(session):
    assert _suggest(session, "qubit_spectroscopy") == {
        ("q0_xy", "drive_freq_hz"), ("q0", "f_01_hz")}


def test_ramsey_writes_drive_freq_fact_twin_and_t2(session):
    assert _suggest(session, "qubit_ramsey") == {
        ("q0_xy", "drive_freq_hz"), ("q0", "f_01_hz"), ("q0", "t2_star_s")}


def test_ramsey_moves_the_drive_toward_the_qubit(session):
    """The neutral sign convention every driver's probe() must realize.

    A qubit sitting ``err`` ABOVE its drive must produce a fringe at
    ``applied + err``, so the correction moves the drive UP by ``err``. A probe that
    realizes the artificial detuning with the opposite handedness inverts this, and
    every accepted update then DOUBLES the residual detuning instead of cancelling
    it -- while the fit itself still looks perfectly clean. That is exactly what the
    QM backend did until its frame ramp was negated (LCHQMDriver
    customized/probes/qubit_ramsey.py), so pin the direction here.
    """
    import numpy as np

    from scqo.experiments._sim import stable_seed

    out = session.run("qubit_ramsey", {"targets": ["q0"]}, update="none")
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]

    # reproduce the residual detuning simulate() drew for this seeded target
    applied = 1.0e6  # QubitRamseyParameters.frequency_detuning_hz default
    err = np.random.default_rng(stable_seed("qubit_ramsey", "q0")).uniform(-0.2, 0.2) * applied
    assert err < 0, "guard: this target's seeded residual is negative, so sign is testable"

    assert fit["detuning_error_hz"] == pytest.approx(err, rel=0.05, abs=0.02 * applied)
    assert (fit["drive_freq_hz"] - fit["old_drive_freq_hz"]
            == pytest.approx(fit["detuning_error_hz"]))
    assert fit["f_01_hz"] == fit["drive_freq_hz"]  # the fact twin rides the same fit


def test_flux_map_writes_the_sweet_spot_on_the_flux_channel(session):
    assert _suggest(session, "resonator_spectroscopy_flux") == {
        ("q0_z", "idle_flux"), ("q0_z", "flux_offset"),
        ("q0_z", "flux_per_phi0"), ("q0_ro", "readout_freq_hz")}


def test_pair_zz_writes_coupler_idle_and_pair_fact(session):
    assert _suggest(session, "pair_zz_coupler", target="q0_q1") == {
        ("q0_q1_c_z", "idle_flux"), ("q0_q1", "zz_hz")}


def test_ramsey_cryoscope_writes_the_paired_distortion_taps(session):
    """The cryoscope proposes ONLY the flux channel's paired distortion facts,
    and the two arrays are proposed with equal length (the paired-array
    invariant the accept batches together)."""
    out = session.run("qubit_ramsey_cryoscope", {"targets": ["q0"]})
    assert out.get("error") is None, out.get("error")
    assert {(s["entity"], s["field"]) for s in out["suggestions"]} == {
        ("q0_z", "distortion_amp"), ("q0_z", "distortion_tau_s")}
    after = {s["field"]: s["after"] for s in out["suggestions"]}
    assert len(after["distortion_amp"]) == len(after["distortion_tau_s"]) > 0


def test_ramsey_cryoscope_fit_values_are_physical(session):
    """The fit carries relative tap amplitudes, positive tau constants in
    SECONDS, the settled level near 1, and the frame declaration (the excursion
    rode on the parked bias, 0.0 on the demo device)."""
    out = session.run("qubit_ramsey_cryoscope", {"targets": ["q0"]}, update="none")
    assert out["outcomes"]["q0"] == "successful"
    fit = out["fit"]["q0"]
    amps, taus = fit["distortion_amp"], fit["distortion_tau_s"]
    assert len(amps) == len(taus) == len(fit["distortion_amp"])
    assert all(math.isfinite(a) for a in amps)
    assert all(0 < t < 1e-6 for t in taus)  # seconds, sub-microsecond
    assert fit["a_dc"] == pytest.approx(1.0, abs=0.05)
    assert fit["old_idle_flux"] == 0.0
    assert fit["flux_pulse_amp_v"] == pytest.approx(0.1)


def test_ramsey_cryoscope_accept_roundtrips_paired_facts(session):
    """Accepting lands both arrays on the flux channel with equal length —
    exercising the per-entity paired batch apply end to end."""
    out = session.run("qubit_ramsey_cryoscope", {"targets": ["q1"]})
    summary = session.accept(out["run_id"])
    assert not summary["errors"]
    physical = session.physical_state()["q1_z"]
    assert physical["distortion_amp"] is not None
    assert len(physical["distortion_amp"]) == len(physical["distortion_tau_s"]) > 0


@pytest.mark.parametrize("name,axes", [
    ("pair_swap_chevron", ("flux_amp_v", "swap_time_ns")),
    ("pair_swap_flux_map", ("qubit_flux_v", "coupler_flux_v")),
])
def test_pair_swap_maps_summarize_the_transfer(session, name, axes):
    """The record-only payload: the peak of the UNDRIVEN member's population
    and where on the 2D map it sits. Both maps default to drive_side='low', so
    the transfer is read off p_high — reading the driven member instead would
    report its own decay as a swap."""
    out = session.run(name, {"targets": ["q0_q1"]}, update="none")
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0_q1"]
    assert math.isfinite(fit["best_transfer"])
    for axis in axes:
        assert math.isfinite(fit[f"best_{axis}"]), axis
        assert fit[f"n_{axis}"] > 4
    # the simulated arch is resolvable by construction, so it must be FOUND
    assert out["outcomes"]["q0_q1"] == "successful"
    assert fit["p_ee_max"] < 0.1, "single excitation: |ee> stays thermal"


def test_pair_swap_maps_refused_without_the_hardware_they_need(tmp_path):
    """The 2D map rides the pair's tracked coupler on its x axis; the chevron
    does NOT (its pulse rides a member's own flux line), so a coupler-less pair
    refuses one and accepts the other."""
    roster = demo_components(tunable=False)          # pair, NO coupler
    design = demo_design(roster)
    vendor = InMemoryDevice(roster, demo_vendor_state(roster, design))
    s = Session(SimulatedBackend(vendor), roster, design=design,
                scqo_dir=tmp_path / "scqo", device_name="chipT",
                setup_name="sim", cooldown_id="cd1")
    out = s.run("pair_swap_flux_map", {"targets": ["q0_q1"]})
    assert "nothing to sweep on the x axis" in out["error"]
    assert "target validation refused" in out["error"]


def test_shaped_flux_pulse_refuses_a_duration_override():
    """A raised-cosine plays its native length: overriding it truncates the
    edges and changes the pulse area, so the combination is refused at
    parameter-validation time rather than silently mis-shaping the pulse."""
    from scqo.experiments.pair_swap_flux_map import PairSwapFluxMapParameters

    PairSwapFluxMapParameters(targets=["q0_q1"], flux_pulse_shape="flattop_cosine")
    PairSwapFluxMapParameters(targets=["q0_q1"], swap_time_ns=40.0)
    with pytest.raises(ValueError, match="native length"):
        PairSwapFluxMapParameters(targets=["q0_q1"], swap_time_ns=40.0,
                                  flux_pulse_shape="flattop_cosine")


def test_single_shot_proposes_monitors_never_the_aggregate(session):
    """The core module proposes blob positions + per-state fidelities; the
    discriminator KNOBS (rotation/threshold) are a driver concern — a
    discriminating backend overrides update(), exactly like the old module.
    The deleted readout_fidelity aggregate must never reappear."""
    proposed = _suggest(session, "single_shot_readout")
    assert {("q0_ro", "pos_g_i"), ("q0_ro", "pos_g_q"),
            ("q0_ro", "pos_e_i"), ("q0_ro", "pos_e_q"),
            ("q0_ro", "fidelity_g"), ("q0_ro", "fidelity_e")} == proposed
    assert not any(f == "readout_fidelity" for _, f in proposed)


def test_single_shot_reports_counted_and_fitted_populations(session):
    """Two DIFFERENT quantities, never one key with two meanings.

    `p_e_given_g` counts shots hard-assigned to the nearest blob center, so it
    folds the residual population together with the discrimination overlap error.
    `pop_e_prep_g` is the fitted blob WEIGHT, overlap removed. The fit can only
    remove overlap, never add any, so it can never exceed the count."""
    out = session.run("single_shot_readout", {"targets": ["q0"]}, update="none")
    fit = out["fit"]["q0"]
    for key in ("p_e_given_g", "pop_e_prep_g", "p_g_given_e", "pop_g_prep_e"):
        assert math.isfinite(fit[key]), key
        assert 0.0 <= fit[key] <= 1.0, key
    assert fit["pop_e_prep_g"] <= fit["p_e_given_g"]
    assert fit["pop_g_prep_e"] <= fit["p_g_given_e"]
    # ...and they are reportable, so the progress line and /trends can offer them
    from scqo.report import MEASURED_QUANTITIES

    assert {"pop_e_prep_g", "pop_g_prep_e"} <= set(MEASURED_QUANTITIES)


def test_single_shot_populations_are_nan_when_the_blobs_degenerate(session, monkeypatch):
    """A one-component fit must yield NaN, not an IndexError, exactly as the
    counted quantities already do."""
    import scqo.experiments.single_shot_readout as module

    real = module.per_qubit_results

    def one_blob(*args, **kwargs):
        out = real(*args, **kwargs)
        for results in out.values():  # collapse to a single center
            results["direct_counts"] = np.ones((2, 1))
            results["gaussian_norms"] = np.ones((2, 1))
        return out

    monkeypatch.setattr(module, "per_qubit_results", one_blob)
    out = session.run("single_shot_readout", {"targets": ["q0"]}, update="none")
    fit = out["fit"]["q0"]
    assert out.get("error") is None
    # NaN, not None: model_dump(mode="json") keeps it: only the PERSISTED json is
    # scrubbed to null (datastore._scrub). Both roads count as missing downstream.
    assert all(math.isnan(fit[k]) for k in ("p_e_given_g", "pop_e_prep_g", "pop_g_prep_e"))
    assert out["outcomes"]["q0"] == "failed"  # NaN fidelity fails the gate


def test_gef_proposes_three_state_monitors(session):
    """Three blob centers and three per-state fidelities, all on the readout
    channel. No discriminator knob: a scalar threshold on one rotated quadrature
    cannot separate three blobs, so single_shot_readout keeps that job."""
    proposed = _suggest(session, "single_shot_readout_gef")
    assert {("q0_ro", f"pos_{letter}_{axis}")
            for letter in ("g", "e", "f") for axis in ("i", "q")} | {
        ("q0_ro", "fidelity_g"), ("q0_ro", "fidelity_e"), ("q0_ro", "fidelity_f")
    } == proposed
    assert not any(f.startswith("readout_") for _, f in proposed)


def test_gef_reports_the_full_confusion_matrix_counted_and_fitted(session):
    """Six off-diagonals, each counted and fitted — the same two-quantity rule the
    two-state run follows, one matrix bigger. The fit only removes overlap, never
    adds any, so no fitted weight can exceed its count."""
    out = session.run("single_shot_readout_gef", {"targets": ["q0"]}, update="none")
    fit = out["fit"]["q0"]
    for prep in ("g", "e", "f"):
        for assigned in ("g", "e", "f"):
            if prep == assigned:
                continue
            counted, fitted = f"p_{assigned}_given_{prep}", f"pop_{assigned}_prep_{prep}"
            assert math.isfinite(fit[counted]), counted
            assert 0.0 <= fit[counted] <= 1.0, counted
            assert math.isfinite(fit[fitted]), fitted
            assert fit[fitted] <= fit[counted] + 1e-9, fitted
    assert 0.5 < fit["readout_fidelity"] <= 1.0
    assert out["outcomes"]["q0"] == "successful"


def test_gef_confusion_is_nan_when_the_blobs_degenerate(session, monkeypatch):
    """A collapsed fit must yield NaN and a failed outcome, not an IndexError —
    the three-state twin of the two-state degenerate case."""
    import scqo.experiments.single_shot_readout_gef as module

    real = module.per_qubit_results

    def one_blob(*args, **kwargs):
        out = real(*args, **kwargs)
        for results in out.values():  # collapse to a single center
            results["direct_counts"] = np.ones((3, 1))
            results["gaussian_norms"] = np.ones((3, 1))
        return out

    monkeypatch.setattr(module, "per_qubit_results", one_blob)
    out = session.run("single_shot_readout_gef", {"targets": ["q0"]}, update="none")
    fit = out["fit"]["q0"]
    assert out.get("error") is None
    assert all(math.isnan(fit[k]) for k in
               ("p_e_given_g", "p_f_given_g", "pop_e_prep_g", "mean_f_i"))
    assert out["outcomes"]["q0"] == "failed"


def test_thermal_population_writes_the_mode_fact(session):
    """n_th is a chip FACT: the population the qubit sits at in the dark, with no
    instrument setting realizing it. Nothing else is proposed — the readout's own
    overlap error belongs to the current knobs, not to the sample."""
    assert _suggest(session, "qubit_thermal_population") == {("q0", "n_th")}


def test_thermal_population_recovers_the_planted_population(session):
    """The pinned-center fit must return the population the simulator planted,
    and re-derive the blob width from the data rather than assuming one."""
    from scqo.experiments._sim import stable_seed

    planted = np.random.default_rng(
        stable_seed("qubit_thermal_population", "q0")).uniform(0.01, 0.05)
    out = session.run("qubit_thermal_population", {"targets": ["q0"]}, update="none")
    fit = out["fit"]["q0"]
    assert fit["pop_e_prep_g"] == pytest.approx(planted, abs=0.015)
    assert fit["pop_e_prep_g"] <= fit["p_e_given_g"]  # counted folds in the overlap
    assert fit["blob_std"] == pytest.approx(1.0, rel=0.3)
    assert out["outcomes"]["q0"] == "successful"


def test_thermal_population_refused_without_a_stored_reference(tmp_path):
    """One prepared state cannot say where |e> is, so a device with no stored
    blob centers is refused BEFORE any hardware runs, naming the prerequisite."""
    roster = demo_components(tunable=True)
    design = demo_design(roster)
    vendor = InMemoryDevice(roster, demo_vendor_state(roster, design))
    s = Session(SimulatedBackend(vendor), roster, design=design,
                scqo_dir=tmp_path / "scqo", device_name="chipT",
                setup_name="sim", cooldown_id="cd1")
    out = s.run("qubit_thermal_population", {"targets": ["q0"]})
    assert "single_shot_readout" in out["error"]
    assert out["outcomes"]["q0"] == "failed"


def test_thermal_population_refuses_active_reset():
    """Active reset removes exactly the population being measured — refused at
    parameter-validation time, so a campaign plan dies at preflight."""
    from scqo.experiments.qubit_thermal_population import QubitThermalPopulationParameters

    QubitThermalPopulationParameters(targets=["q0"])
    with pytest.raises(ValueError, match="active"):
        QubitThermalPopulationParameters(targets=["q0"], reset_method="active")


def test_arch_fit_writes_mode_facts_and_transfer_function(session):
    proposed = _suggest(session, "qubit_spectroscopy_flux_pulse")
    assert ("q0", "ej_sum_hz") in proposed
    assert ("q0", "f_q_max_hz") in proposed
    assert ("q0_z", "flux_offset") in proposed
    # ... and the operating point: without this the fit is bookkeeping only and
    # accepting it can never re-centre the next map (the bug this frame work fixed).
    assert ("q0_z", "idle_flux") in proposed


def test_arch_fit_re_references_its_relative_window_to_absolute(session):
    """The ``_pulse`` frame contract, end to end.

    The window is measured from ``idle_flux``, so the estimator's sweet spot is
    an EXCURSION; ``flux_offset`` (a chip fact, shared with the absolute
    resonator map) and the proposed ``idle_flux`` must both be the re-referenced
    ABSOLUTE set-point.

    The nonzero seeding is the whole point: the demo device seeds
    ``idle_flux = 0.0``, where ``0 + excursion == excursion`` and this test
    would pass against the very bug it guards.
    """
    parked = 0.11
    session.set_values({"q0_z.idle_flux": parked})
    out = session.run("qubit_spectroscopy_flux_pulse", {"targets": ["q0"]})
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]

    assert fit["old_idle_flux"] == pytest.approx(parked)
    assert fit["flux_offset_from_idle"] != pytest.approx(0.0)  # guard: a real excursion
    assert fit["flux_offset"] == pytest.approx(
        fit["old_idle_flux"] + fit["flux_offset_from_idle"])

    # the fact and the knob are one number on one plane
    proposals = {(s["entity"], s["field"]): s["after"] for s in out["suggestions"]}
    assert proposals[("q0_z", "idle_flux")] == pytest.approx(fit["flux_offset"])
    assert proposals[("q0_z", "flux_offset")] == pytest.approx(fit["flux_offset"])


def test_pair_zz_refused_on_a_coupler_less_pair(tmp_path):
    """The old coupler_bias gate's greenfield successor: a pair without a
    tracked coupler is refused pre-probe, never mid-sweep."""
    roster = demo_components(tunable=False)          # pair, NO coupler
    design = demo_design(roster)
    vendor = InMemoryDevice(roster, demo_vendor_state(roster, design))
    s = Session(SimulatedBackend(vendor), roster, design=design,
                scqo_dir=tmp_path / "scqo", device_name="chipT",
                setup_name="sim", cooldown_id="cd1")
    out = s.run("pair_zz_coupler", {"targets": ["q0_q1"]})
    assert "declares no coupler role" in out["error"]
    assert "target validation refused" in out["error"]


def test_foreign_flux_component_is_record_only(session):
    """Kind-agnostic foreign flux: sweeping the COUPLER's z against q0 is a
    legal crosstalk map — fits saved, zero suggestions."""
    out = session.run("resonator_spectroscopy_flux",
                      {"targets": ["q0"], "flux_component": "q0_q1_c"})
    assert out.get("error") is None
    assert out["suggestions"] == []


def test_accept_roundtrip_on_the_pair(session):
    out = session.run("pair_zz_coupler", {"targets": ["q0_q1"]})
    summary = session.accept(out["run_id"])
    assert not summary["errors"]
    assert session.device_state()["q0_q1_c_z"]["idle_flux"] is not None
    assert session.physical_state()["q0_q1"]["zz_hz"] is not None


# ---------------------------------------------------------------- parity


def _fresh_parity_session(tmp_path, *, splitting=True, depletion=True):
    """A session seeded like the module fixture, with the parity prerequisites
    individually controllable (the REFUSE tests need them absent)."""
    roster = demo_components(tunable=True)
    design = demo_design(roster)
    vendor = InMemoryDevice(roster, demo_vendor_state(roster, design))
    s = Session(SimulatedBackend(vendor), roster, design=design,
                scqo_dir=tmp_path / "scqo", data_root=tmp_path / "data",
                device_name="chipT", setup_name="sim", cooldown_id="cd1",
                parameter_defaults=PARITY_DEFAULTS)
    s.set_values({f"q0_ro.{field}": value
                  for field, value in REFERENCE_BLOBS.items()})
    if depletion:
        s.set_values({"q0_ro.readout_depletion_s": 1e-6})
    if splitting:
        s.set_values({"q0_xy.parity_delta_f_hz": 250e3})
    return s


def test_ramsey_beat_proposes_the_parity_splitting(session):
    """ramsey_model='beat' forces the two-frequency fit; the splitting lands
    as a drive-channel monitor proposal on top of the usual three, and its
    value is the sim's planted branch separation."""
    from scqo.experiments._sim import stable_seed

    out = session.run("qubit_ramsey", {
        "targets": ["q0"], "ramsey_model": "beat",
        "max_idle_time_ns": 10000, "num_points": 201})
    assert out.get("error") is None, out.get("error")
    proposed = {(s["entity"], s["field"]) for s in out["suggestions"]}
    assert proposed == {("q0_xy", "drive_freq_hz"), ("q0", "f_01_hz"),
                        ("q0", "t2_star_s"), ("q0_xy", "parity_delta_f_hz")}
    # replay the sim's draws (err, t2_star, then delta on the beat branch)
    rng = np.random.default_rng(stable_seed("qubit_ramsey", "q0"))
    rng.uniform(-0.2, 0.2)
    rng.uniform(5e-6, 15e-6)
    delta = rng.uniform(0.3, 0.5) * 1.0e6
    assert out["fit"]["q0"]["parity_delta_f_hz"] == pytest.approx(delta, rel=0.1)


def test_parity_switch_writes_parity_rate_fact(session):
    assert _suggest(session, "qubit_parity_switch") == {("q0", "parity_rate_hz")}


def test_parity_switch_recovers_the_planted_rate(session):
    """The offline loop closes: the sim's Markov flip probability over the
    attached shot period comes back out of the PSD knee, at the idle the
    seeded 250 kHz splitting implies."""
    from scqo.experiments._sim import stable_seed

    out = session.run("qubit_parity_switch", {"targets": ["q0"]}, update="none")
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]
    assert fit["idle_time_ns"] == pytest.approx(2000.0)  # 1 / (2 x 250 kHz)
    assert fit["parity_delta_f_hz"] == pytest.approx(250e3)
    assert "outlier_probability" in fit  # the discriminated-path marker
    assert 0.3 < fit["p_parity_odd"] < 0.7
    rng = np.random.default_rng(stable_seed("qubit_parity_switch", "q0"))
    p_flip = rng.uniform(0.002, 0.01)
    expected = p_flip / fit["shot_period_s"]
    assert fit["parity_rate_hz"] == pytest.approx(expected, rel=0.2)


def test_parity_switch_refuses_without_the_splitting(tmp_path):
    s = _fresh_parity_session(tmp_path, splitting=False)
    out = s.run("qubit_parity_switch", {"targets": ["q0"]})
    assert "parity_delta_f_hz" in str(out.get("error"))
    assert "ramsey_model='beat'" in str(out.get("error"))
    # ... but an explicit idle override runs without the stored splitting
    ok = s.run("qubit_parity_switch",
               {"targets": ["q0"], "idle_time_ns": 1000.0}, update="none")
    assert ok.get("error") is None, ok.get("error")
    assert ok["fit"]["q0"]["idle_time_ns"] == pytest.approx(1000.0)
    assert math.isnan(ok["fit"]["q0"]["parity_delta_f_hz"])


def test_parity_switch_two_stage_chain(tmp_path):
    """The full workflow on a fresh device: beat ramsey -> apply -> the parity
    monitor derives its idle from the just-measured splitting -> apply -> the
    rate lands in physical state."""
    s = _fresh_parity_session(tmp_path, splitting=False)
    out1 = s.run("qubit_ramsey", {
        "targets": ["q0"], "ramsey_model": "beat",
        "max_idle_time_ns": 10000, "num_points": 201}, update="apply")
    assert out1.get("error") is None, out1.get("error")
    delta = out1["fit"]["q0"]["parity_delta_f_hz"]

    out2 = s.run("qubit_parity_switch", {"targets": ["q0"]}, update="apply")
    assert out2.get("error") is None, out2.get("error")
    fit = out2["fit"]["q0"]
    assert fit["parity_delta_f_hz"] == pytest.approx(delta)
    assert fit["idle_time_ns"] == pytest.approx(
        max(16.0, round(1e9 / (2.0 * delta) / 4.0) * 4.0))
    assert s.physical_state()["q0"]["parity_rate_hz"] is not None


def test_parity_switch_reports_the_odd_fraction(session):
    """p_switch explains a FAILED run whose fit otherwise looks healthy, so it
    has to reach the run record — and it must not be confused with
    p_parity_odd, which sits near 0.5 on every healthy run.

    The simulator plants a per-shot parity switch probability in (0.002, 0.01),
    so p_switch lands in that range; p_parity_odd is ~0.5 because the chip
    spends about half its time in each parity."""
    out = session.run("qubit_parity_switch", {"targets": ["q0"]}, update="none")
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]
    assert 0.0 < fit["p_switch"] < 0.05
    # the shot count is DERIVED from record_time_s now, so recover it from the
    # recorded timings rather than pasting a literal
    n_shots = round(fit["record_time_s"] / fit["shot_period_s"])
    assert fit["p_switch"] == pytest.approx(
        fit["n_parity_switches"] / (n_shots - 2), rel=1e-6)
    assert fit["p_parity_odd"] == pytest.approx(0.5, abs=0.15)
    assert out["outcomes"]["q0"] == "successful"


def test_parity_switch_derives_shots_from_record_time(session):
    """record_time_s is the knob, because the spectrum's lowest frequency is
    8 / record_time_s and THAT is what limits how slow a rate is measurable.
    The shot count follows from the estimated shot period."""
    out = session.run("qubit_parity_switch",
                      {"targets": ["q0"], "record_time_s": 0.8}, update="none")
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]
    assert fit["requested_record_time_s"] == pytest.approx(0.8)
    # achieved uses the period the probe really scheduled, so it is close to
    # the request but not identical -- and both are recorded on purpose
    assert fit["record_time_s"] == pytest.approx(0.8, rel=0.05)
    n_shots = round(fit["record_time_s"] / fit["shot_period_s"])
    assert n_shots == pytest.approx(0.8 / fit["shot_period_s"], rel=0.01)
    # doubling the record halves the spectrum's low edge
    half = session.run("qubit_parity_switch",
                       {"targets": ["q0"], "record_time_s": 0.4}, update="none")
    assert (half["fit"]["q0"]["psd_freq_min_hz"]
            == pytest.approx(2 * fit["psd_freq_min_hz"], rel=0.1))


def test_parity_switch_refuses_an_absurd_record_time(session):
    """The ceiling guards the DERIVED count: a long record on a fast-cadence
    qubit asks for more shots than an instrument will hold."""
    out = session.run("qubit_parity_switch",
                      {"targets": ["q0"], "record_time_s": 3600.0})
    assert "max_num_shots" in str(out.get("error"))
    assert "record_time_s" in str(out.get("error"))


def test_parity_switch_num_shots_overrides_and_bypasses_the_ceiling(session):
    """num_shots wins outright, exactly as idle_time_ns bypasses
    max_derived_idle_ns -- the ceiling is on the derived value only."""
    out = session.run(
        "qubit_parity_switch",
        {"targets": ["q0"], "num_shots": 120000, "max_num_shots": 1000},
        update="none")
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]
    assert round(fit["record_time_s"] / fit["shot_period_s"]) == 120000


def test_parity_switch_reports_the_spectral_reach(session):
    """A corner near the lowest bin is the readable symptom of 'record for
    longer', so the margin has to reach the run record."""
    out = session.run("qubit_parity_switch", {"targets": ["q0"]}, update="none")
    fit = out["fit"]["q0"]
    assert fit["corner_margin_low"] == pytest.approx(
        fit["psd_corner_hz"] / fit["psd_freq_min_hz"], rel=1e-6)
    assert fit["psd_contrast"] > 3.0          # the gate that replaced p_switch


def test_parity_switch_reports_the_mapping_fidelity_twice(session):
    """Under the INDEPENDENT model, A and B are the reference model's 4F^2 and
    (1-F^2)dt terms, so the same fit yields F two independent ways. Both must
    reach the run record with their ratio -- the ratio is the only number that
    notices the correlated-noise model failure that psd_contrast is blind to.
    (The default is now 'constrained', where the floor/ratio are NaN; this is
    the OPT-IN model, so it must be requested by name.)"""
    out = session.run("qubit_parity_switch",
                      {"targets": ["q0"], "psd_model": "independent"},
                      update="none")
    fit = out["fit"]["q0"]
    f_amp = fit["mapping_fidelity"]
    f_floor = fit["mapping_fidelity_floor"]
    # derived from the SAME fit, not recomputed
    assert f_amp == pytest.approx(
        math.sqrt(2.0 * math.pi * fit["psd_corner_hz"] * fit["psd_amplitude"]),
        rel=1e-6)
    assert f_floor == pytest.approx(
        math.sqrt(1.0 - 2.0 * fit["psd_white_floor"] / fit["shot_period_s"]),
        rel=1e-6)
    assert fit["mapping_fidelity_ratio"] == pytest.approx(f_amp / f_floor,
                                                          rel=1e-6)
    # the offline simulator plants a clean telegraph, so the two must agree
    assert fit["mapping_fidelity_ratio"] == pytest.approx(1.0, abs=0.2)


def test_parity_switch_default_model_is_constrained(session):
    """The DEFAULT run fits the reference single-F model, whose fingerprint is
    one mapping_fidelity plus NaN for the plateau/floor cross-check (the coupled
    model cannot produce it) and a finite residual as its quality number. The
    fitted-model STRING is a string, so it lives in the scqat metadata artifact
    and the run parameters, not in the float-only fit dict."""
    out = session.run("qubit_parity_switch", {"targets": ["q0"]}, update="none")
    fit = out["fit"]["q0"]
    assert math.isfinite(fit["mapping_fidelity"])          # the single fitted F
    assert math.isnan(fit["mapping_fidelity_floor"])        # no independent floor
    assert math.isnan(fit["mapping_fidelity_ratio"])
    assert math.isfinite(fit["psd_fit_residual"])           # its quality number


def test_parity_switch_independent_model_selectable(session):
    """The opt-in model is reachable by name and still recovers the rate."""
    base = session.run("qubit_parity_switch", {"targets": ["q0"]}, update="none")
    indep = session.run("qubit_parity_switch",
                        {"targets": ["q0"], "psd_model": "independent"},
                        update="none")
    assert indep["outcomes"]["q0"] == "successful"
    # both models see the same planted telegraph, so the rate agrees
    assert indep["fit"]["q0"]["parity_rate_hz"] == pytest.approx(
        base["fit"]["q0"]["parity_rate_hz"], rel=0.3)


def test_parity_switch_idle_multiple_stretches_the_derived_idle(session):
    """idle = N / (2 x parity_delta_f_hz). The seeded 250 kHz gives a 2000 ns
    base, so N=3 must play 6000 ns."""
    out = session.run("qubit_parity_switch",
                      {"targets": ["q0"], "idle_multiple": 3,
                       "max_derived_idle_ns": 100000.0}, update="none")
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]
    assert fit["idle_time_ns"] == pytest.approx(6000.0)
    assert fit["idle_multiple"] == 3


def test_parity_switch_explicit_idle_is_not_multiplied(session):
    """idle_time_ns is the escape hatch: it wins outright, so idle_multiple
    must NOT scale it (silently tripling an explicit request would be the worst
    kind of surprise)."""
    out = session.run("qubit_parity_switch",
                      {"targets": ["q0"], "idle_time_ns": 1000.0,
                       "idle_multiple": 5}, update="none")
    assert out.get("error") is None, out.get("error")
    assert out["fit"]["q0"]["idle_time_ns"] == pytest.approx(1000.0)


def test_parity_switch_even_idle_multiple_warns_but_runs(session):
    """Even N puts both parities on the same pole (sin(N pi/2) == 0), so the
    run carries no signal. The user asked for it to be allowed, so it must warn
    loudly rather than refuse -- and then fail honestly in the fit."""
    with pytest.warns(UserWarning, match="EVEN"):
        out = session.run("qubit_parity_switch",
                          {"targets": ["q0"], "idle_multiple": 2,
                           "max_derived_idle_ns": 100000.0}, update="none")
    assert out.get("error") is None, out.get("error")
    assert out["fit"]["q0"]["idle_time_ns"] == pytest.approx(4000.0)


def test_parity_switch_ceiling_names_the_multiple(session):
    """Two faults reach the same ceiling and need different remedies: here the
    BASE is fine and only the multiple pushed it over, so the message must say
    so instead of blaming a stale splitting."""
    out = session.run("qubit_parity_switch",
                      {"targets": ["q0"], "idle_multiple": 21})
    err = str(out.get("error"))
    assert "idle_multiple" in err
    assert "base" in err.lower()
