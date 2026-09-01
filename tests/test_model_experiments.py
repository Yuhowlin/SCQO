"""The full ported experiment catalog, end-to-end on the simulated backend:
every registered experiment runs without error on the tunable demo device,
and the re-homed writes of the load-bearing families land on the right
entities."""

import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from pydantic import ValidationError

from scqo import Session
from scqo import experiments as registry
from scqo.experiments._window import window_bounds
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
               "qubit_relaxation_flux_pulse", "pair_swap_chevron", "pair_swap_flux_map",
               "qc_n_swap_amp", "qc_n_stark_amp", "qc_unidirectional_trotter",
               "pair_swap_angle", "qc_trotter_compensation",
               "qubit_t1_ade", "qubit_t1_bayesian",
               "broadband_resonator_spectroscopy", "broadband_qubit_spectroscopy",
               "qubit_parametric_drive_amp", "qubit_parametric_drive_time"}


#: the readout reference an accepted single_shot_readout would have left behind.
#: qubit_thermal_population REFUSES to run without it (one prepared state cannot
#: locate |e> on its own), and seeding it also puts the five
#: attach_readout_positions experiments on scqat's stored-axis reduction instead
#: of PCA — the path the real instruments take, so the offline sweep exercises it.
REFERENCE_BLOBS = {"pos_g_i": 0.0, "pos_g_q": 0.0, "pos_e_i": 4.0, "pos_e_q": 0.0}

#: the parity-switch monitors derive their shot count from record_time_s, and
#: the demo device's estimated shot period is ~3.9 us against a real chip's
#: ~30 us — so the physically correct 30 s default would ask for 7.7M shots
#: here and be refused by max_num_shots. Shorten it for the offline sweep
#: rather than weakening the default: 0.4 s is ~103k shots (continuous) /
#: ~68k cycles (discrete, whose minimal cycle carries a second readout).
PARITY_DEFAULTS = {"qubit_parity_switch_continuous": {"record_time_s": 0.4},
                   "qubit_parity_switch_discrete": {"record_time_s": 0.4}}

#: qubit_parametric_drive_time runs scqat's three-stage EP pipeline ONCE PER DRIVE
#: FREQUENCY, so the real 21-point default costs ~15 s per offline run and this
#: file runs it three times. Shrink the sweep for the offline suite rather than
#: weakening the default. The window narrows WITH the point count on purpose: the
#: simulated chevron's linewidth is drawn from the frequency STEP, so holding the
#: step at 2 MHz is what keeps the seeded resonance resolvable at 9 points.
PARAMETRIC_TIME_DEFAULTS = {"qubit_parametric_drive_time": {
    "num_freq_points": 9, "start_parametric_freq_hz": 190e6,
    "end_parametric_freq_hz": 206e6, "num_time_points": 61}}

#: the module fixture's device is a THREE-qubit chain (q0-q1-q2) with a pair
#: over each consecutive couple, because qc_unidirectional_trotter swaps on two
#: pairs that share a relay member and one pair cannot express that. Reusing the
#: same pair twice would run, but it would put a physically impossible chain in
#: the suite; extending the demo device is the honest fix.
CHAIN_QUBITS = ("q0", "q1", "q2")

#: the chain topology on that device — q0 (source) -> q1 (relay, reset) -> q2
#: (sink). Supplied as defaults because targets are the only params the
#: every-experiment sweep passes.
TROTTER_DEFAULTS = {"qc_unidirectional_trotter": {
    "first_pair": "q0_q1", "second_pair": "q1_q2", "reset_qubit": "q1",
    "compensation_amps": {"q0": 0.3, "q1": 0.2, "q2": 0.25}},
    # the compensation scan takes the SAME chain, but its swept qubit may not
    # also appear in the fixed map (two sources of truth for one amplitude), so
    # q0 is the target and only the sink keeps a fixed tone.
    "qc_trotter_compensation": {
        "first_pair": "q0_q1", "second_pair": "q1_q2", "reset_qubit": "q1",
        "compensation_target": "q0", "compensation_amps": {"q2": 0.25}}}

#: what the module fixture runs on; _fresh_parity_session keeps the parity-only set.
OFFLINE_DEFAULTS = {**PARITY_DEFAULTS, **PARAMETRIC_TIME_DEFAULTS,
                    **TROTTER_DEFAULTS}


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("gf5d")
    roster = demo_components(CHAIN_QUBITS, tunable=True, chain=True)
    design = demo_design(roster, CHAIN_QUBITS)
    vendor = InMemoryDevice(roster, demo_vendor_state(roster, design))
    s = Session(SimulatedBackend(vendor), roster, design=design,
                scqo_dir=tmp / "scqo", data_root=tmp / "data",
                device_name="chipT", setup_name="sim",
                cooldown_id="cd1",
                parameter_defaults=OFFLINE_DEFAULTS)
    s.set_values({f"{q}_ro.{field}": value
                  for q in CHAIN_QUBITS
                  for field, value in REFERENCE_BLOBS.items()})
    # the parity-switch monitors REFUSE without a governed depletion wait (the
    # shot cadence is their telegraph timebase) and a stored parity splitting
    # (their fixed idle). 250 kHz -> idle = 1 / (2 x 250 kHz) = 2000 ns, on-grid.
    s.set_values({f"{q}_ro.readout_depletion_s": 1e-6 for q in CHAIN_QUBITS})
    s.set_values({f"{q}_xy.parity_delta_f_hz": 250e3 for q in CHAIN_QUBITS})
    return s


#: the CORE catalog, taken from the exported classes rather than the live
#: registry — another test's @register must never widen this sweep. Selected by
#: TYPE, not by an exclusion list: __all__ also re-exports the registry
#: functions and the driver-facing capability surface (QubitResetParameters,
#: reset_wait_ns), and a name list would need editing every time one is added.
CORE = sorted(obj.name for obj in map(lambda n: getattr(registry, n), registry.__all__)
              if isinstance(obj, type) and issubclass(obj, Experiment))


#: experiments whose targets are not ONE q0-shaped entity: the chain family
#: initializes and reads out every chain qubit inside a single circuit, so the
#: whole chain has to be selected at once.
CHAIN_TARGETS = {"qc_unidirectional_trotter": list(CHAIN_QUBITS),
                 "qc_trotter_compensation": list(CHAIN_QUBITS)}


def _targets_for(name):
    """The offline device's targets for one experiment — a pair, a chain, or q0."""
    if name in CHAIN_TARGETS:
        return list(CHAIN_TARGETS[name])
    return ["q0_q1"] if registry.get(name).target_kinds == ("qubit_pair",) else ["q0"]


@pytest.mark.parametrize("name", CORE)
def test_every_experiment_runs_clean(session, name):
    out = session.run(name, {"targets": _targets_for(name)}, update="none")
    assert out.get("error") is None, out.get("error")


def _suggest(session, name, target="q0"):
    targets = [target] if isinstance(target, str) else list(target)
    out = session.run(name, {"targets": targets})
    assert out.get("error") is None, out.get("error")
    return {(s["entity"], s["field"]) for s in out["suggestions"]}




@pytest.mark.parametrize("name", sorted(RECORD_ONLY))
def test_record_only_experiments_propose_nothing(session, name):
    """RECORD_ONLY is a CLAIM about these experiments, so check it rather than
    just documenting it: a record-only diagnostic that grows an update() must
    either leave the set or fail here."""
    assert name in CORE, f"{name} is not in the core catalog"
    assert _suggest(session, name, target=_targets_for(name)) == set()


def test_parametric_drive_amp_finds_the_seeded_resonance(session):
    """The offline sim hides one sideband line inside the swept window; the
    scqat point-cloud reduction must keep at least one peak and report the
    strongest one INSIDE the swept windows (discriminated form — the dip sits
    in the averaged population itself)."""
    params = {"targets": ["q0"], "use_state_discrimination": True}
    out = session.run("qubit_parametric_drive_amp", params, update="none")
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]
    assert fit["n_good"] >= 1
    cls = registry.get("qubit_parametric_drive_amp")
    p = cls.Parameters(**params)
    # window_bounds, never a chained start <= x <= end: the edges take either
    # order and a reversed pair makes the chain silently always-False.
    f_lo, f_hi = window_bounds(p.start_parametric_freq_hz, p.end_parametric_freq_hz)
    a_lo, a_hi = window_bounds(p.start_parametric_amp_v, p.end_parametric_amp_v)
    assert f_lo <= fit["best_parametric_freq_hz"] <= f_hi
    assert a_lo <= fit["best_parametric_amp_v"] <= a_hi
    # The strongest kept peak must be a real LINE, not a window-wide artifact:
    # the seeded FWHM is a few frequency steps (~6-12 % of the span). This is the
    # assertion with teeth — `best_peak_amplitude` cannot carry it, because
    # scqat's fit_peaks normalizes polarity per slice (it inverts a dip trace and
    # fits a positive lorentzian), so a well-fit dip reports a POSITIVE amplitude
    # and a negative one signals a badly-conditioned fit, not a dip.
    span = f_hi - f_lo
    assert 0.0 < fit["best_fwhm_hz"] < 0.25 * span


def _sweep(session, name, **params):
    """define_sweep straight off the experiment, no acquisition."""
    cls = registry.get(name)
    return cls(session.backend, cls.Parameters(targets=["q0"], **params)).define_sweep()


@pytest.mark.parametrize("name,axis,edges", [
    ("qubit_parametric_drive_amp", "parametric_amp_v",
     ("start_parametric_amp_v", "end_parametric_amp_v", 0.0, 0.3)),
    ("qubit_parametric_drive_amp", "parametric_freq_hz",
     ("start_parametric_freq_hz", "end_parametric_freq_hz", 50e6, 300e6)),
    ("qubit_parametric_drive_time", "parametric_freq_hz",
     ("start_parametric_freq_hz", "end_parametric_freq_hz", 180e6, 220e6)),
    ("qubit_parametric_drive_time", "drive_time_ns",
     ("start_drive_time_ns", "end_drive_time_ns", 16.0, 3000.0)),
])
def test_parametric_window_edges_take_either_order(session, name, axis, edges):
    """The pair DEFINES the window, it does not choose a traversal direction:
    both orders give the identical axis, always ascending. The raw fields are
    left verbatim — only the AXIS is ordered."""
    start, end, lo, hi = edges
    up = _sweep(session, name, **{start: lo, end: hi})[axis]
    down = _sweep(session, name, **{start: hi, end: lo})[axis]
    assert down == pytest.approx(up)
    assert np.all(np.diff(down) > 0), "the emitted axis must ascend"
    cls = registry.get(name)
    reversed_params = cls.Parameters(targets=["q0"], **{start: hi, end: lo})
    assert getattr(reversed_params, start) == hi  # the field is NOT normalised
    assert getattr(reversed_params, end) == lo


@pytest.mark.parametrize("name,start,end,value", [
    ("qubit_parametric_drive_amp", "start_parametric_amp_v", "end_parametric_amp_v", 0.2),
    ("qubit_parametric_drive_amp", "start_parametric_freq_hz", "end_parametric_freq_hz", 100e6),
    ("qubit_parametric_drive_time", "start_parametric_freq_hz", "end_parametric_freq_hz", 200e6),
    ("qubit_parametric_drive_time", "start_drive_time_ns", "end_drive_time_ns", 100.0),
])
def test_parametric_zero_width_window_is_refused(name, start, end, value):
    """Two identical edges are a typo, not a measurement — and the refusal fires
    at Parameters construction, not deep inside define_sweep."""
    with pytest.raises(ValidationError, match="zero-width"):
        registry.get(name).Parameters(targets=["q0"], **{start: value, end: value})


def test_parametric_drive_time_finds_the_seeded_sideband(session):
    """The offline sim hides one sideband inside the swept window and the chevron
    oscillates around it; the scqat EP pipeline must converge on most drive
    frequencies and place the most COHERENT one (largest 8*lambda^2/gamma^2) on
    the seeded line — which the sim puts in the middle 40 % of the window, so an
    estimator that latched onto a window-edge artifact fails here."""
    params = {"targets": ["q0"], "use_state_discrimination": True}
    out = session.run("qubit_parametric_drive_time", params, update="none")
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]
    assert 1 <= fit["n_decoh_ok"] <= fit["n_freq"]
    p = registry.get("qubit_parametric_drive_time").Parameters(
        **{**params, **PARAMETRIC_TIME_DEFAULTS["qubit_parametric_drive_time"]})
    lo, hi = window_bounds(p.start_parametric_freq_hz, p.end_parametric_freq_hz)
    assert lo + 0.15 * (hi - lo) <= fit["best_parametric_freq_hz"] <= hi - 0.15 * (hi - lo)
    # a loss rate and a coupling rate, both real and positive
    assert fit["best_gamma_hz"] > 0
    assert fit["best_lambda_hz"] > 0



def test_qubit_sqrb_discriminated_end_to_end(session):
    out = session.run("qubit_sqrb", {"targets": ["q0"], "use_state_discrimination": True}, update="none")
    assert out.get("error") is None, out.get("error")
    assert out["outcomes"]["q0"] == "successful"
    assert 0.9 < out["fit"]["q0"]["gate_fidelity"] <= 1.0


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


@pytest.mark.parametrize("name", ["qubit_drag_equator", "qubit_drag_alternating"])
@pytest.mark.parametrize("gate,knob", [("x180", "drag_beta"), ("x90", "drag_beta_x90")])
def test_drag_writes_the_knob_of_the_target_gate(session, name, gate, knob):
    """target_gate picks the storage pair: the fitted beta lands on drag_beta
    for the pi gate and drag_beta_x90 for the pi/2 — never the other (issue #24:
    the x90 branch crashed on QM and leaked KeyError on the strict views)."""
    out = session.run(name, {"targets": ["q0"], "target_gate": gate})
    assert out.get("error") is None, out.get("error")
    assert {(s["entity"], s["field"]) for s in out["suggestions"]} == {("q0_xy", knob)}


def test_drag_target_gate_rejects_unnormalized_spellings(session):
    """'X90' once slipped through three disagreeing normalizations and wrote the
    x180 knob; the Literal refuses it loudly instead."""
    out = session.run("qubit_drag_equator",
                      {"targets": ["q0"], "target_gate": "X90"}, update="none")
    assert out.get("error") is not None


def test_flux_map_writes_the_sweet_spot_on_the_flux_channel(session):
    """The sweet spot + period always, and the dispersive physics too because the
    demo qubit HAS a standing drive_freq_hz to anchor f_q_max on."""
    assert _suggest(session, "resonator_spectroscopy_flux") == {
        ("q0_z", "idle_flux"), ("q0_z", "flux_offset"),
        ("q0_z", "flux_per_phi0"), ("q0_ro", "readout_freq_hz"),
        ("q0_res", "f_bare_hz"), ("q0_res", "g_hz"), ("q0_res", "g_coeff")}


def test_flux_map_withholds_dispersive_physics_without_an_arch_anchor(tmp_path, monkeypatch):
    """f_r0/g are conditional on an f_q_max the trace CANNOT fit (it fixes only the
    product g^2*f_q_max), so they are proposed only against a known arch top. Take
    away EVERY tier — no standing drive frequency (true bring-up), no fab junction
    resistance, no datasheet — and they must disappear while the flux-periodicity
    proposals, which are robust to the degeneracy, survive."""
    from scqo.design import Design
    from scqo.experiments.resonator_spectroscopy_flux import ResonatorSpectroscopyFlux

    roster = demo_components(tunable=True)
    # vendor knobs still seeded from the datasheet (the instrument is configured),
    # but the SESSION carries no datasheet, so the design tier cannot answer.
    vendor = InMemoryDevice(roster, demo_vendor_state(roster, demo_design(roster)))
    s = Session(SimulatedBackend(vendor), roster, design=Design({}),
                scqo_dir=tmp_path / "scqo", data_root=tmp_path / "data",
                device_name="chipT", setup_name="sim", cooldown_id="cd1")

    real_anchor = ResonatorSpectroscopyFlux.anchor

    def no_drive_freq(self, name, field):
        if field == "drive_freq_hz":
            raise ValueError(f"{name}.drive_freq_hz has no standing value")
        return real_anchor(self, name, field)

    monkeypatch.setattr(ResonatorSpectroscopyFlux, "anchor", no_drive_freq)
    out = s.run("resonator_spectroscopy_flux", {"targets": ["q0"]})
    assert out.get("error") is None, out.get("error")
    assert out["fit"]["q0"]["f_q_max_source"] == "assumed"
    assert {(sg["entity"], sg["field"]) for sg in out["suggestions"]} == {
        ("q0_z", "idle_flux"), ("q0_z", "flux_offset"),
        ("q0_z", "flux_per_phi0"), ("q0_ro", "readout_freq_hz")}


def test_flux_map_f_q_max_falls_back_to_the_fab_resistance(tmp_path, monkeypatch):
    """With no standing drive frequency, the fab's junction resistance predicts the
    arch top (Ambegaokar-Baratoff) and OUTRANKS the datasheet — it carries the
    fabrication scatter the datasheet cannot know. That is enough to make the
    dispersive physics proposable again."""
    from scqo.experiments.resonator_spectroscopy_flux import ResonatorSpectroscopyFlux

    roster = demo_components(tunable=True)
    design = demo_design(roster)
    vendor = InMemoryDevice(roster, demo_vendor_state(roster, design))
    s = Session(SimulatedBackend(vendor), roster, design=design,
                scqo_dir=tmp_path / "scqo", data_root=tmp_path / "data",
                device_name="chipT", setup_name="sim", cooldown_id="cd1")
    s.set_values({"q0.junction_resistance_ohm": 9.0e3, "q0.ec_hz": 0.2e9})

    real_anchor = ResonatorSpectroscopyFlux.anchor

    def no_drive_freq(self, name, field):
        if field == "drive_freq_hz":
            raise ValueError(f"{name}.drive_freq_hz has no standing value")
        return real_anchor(self, name, field)

    monkeypatch.setattr(ResonatorSpectroscopyFlux, "anchor", no_drive_freq)
    out = s.run("resonator_spectroscopy_flux", {"targets": ["q0"]})
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]
    assert fit["f_q_max_source"] == "junction_resistance"
    # 9 kOhm at Delta=43.5 GHz, E_c=0.2 GHz -> ~4.80 GHz (see _transmon_estimate)
    assert fit["f_q_max_hz"] == pytest.approx(4.797e9, rel=1e-3)


@pytest.mark.parametrize("name", ["resonator_spectroscopy_power_amp",
                                  "resonator_spectroscopy_power_chain"])
def test_punchout_derives_the_coupling_from_its_two_branches(session, name):
    """A punchout measures g without any arch fit: g = sqrt(lamb·(f_bare − f_q))
    with f_q the standing drive frequency. That makes it INDEPENDENT of the flux
    map's f_r0 pin, which is why both experiments write g_hz."""
    from scqo.experiments._punchout import G_PUNCHOUT
    from scqo.experiments._transmon_estimate import g_coeff_from_g, g_hz_from_pull

    out = session.run(name, {"targets": ["q0"]}, update="none")
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]
    assert fit["g_source"] == G_PUNCHOUT
    f_q = float(session.device.channel("q0", "drive").drive_freq_hz)
    expected = g_hz_from_pull(fit["lamb_shift_hz"], fit["f_bare_hz"], f_q)
    assert fit["g_hz"] == pytest.approx(expected)
    assert fit["g_coeff"] == pytest.approx(
        g_coeff_from_g(expected, f_q, fit["f_bare_hz"]))
    # the coefficient is dimensionless and O(0.01) for a real transmon
    assert 0.001 < fit["g_coeff"] < 0.2


@pytest.mark.parametrize("name", ["resonator_spectroscopy_power_amp",
                                  "resonator_spectroscopy_power_chain"])
def test_punchout_withholds_the_coupling_without_a_drive_frequency(
        tmp_path, monkeypatch, name):
    """At bring-up the qubit has not answered yet, so there is no f_q to turn the
    Lamb shift into a coupling. Both g values are withheld and NOT proposed —
    the two branch frequencies, which need no qubit, still are."""
    from scqo.experiments import get
    from scqo.experiments._punchout import G_NONE

    roster = demo_components(tunable=True)
    design = demo_design(roster)
    vendor = InMemoryDevice(roster, demo_vendor_state(roster, design))
    s = Session(SimulatedBackend(vendor), roster, design=design,
                scqo_dir=tmp_path / "scqo", data_root=tmp_path / "data",
                device_name="chipT", setup_name="sim", cooldown_id="cd1")
    cls = get(name)
    real_anchor = cls.anchor

    def no_drive_freq(self, target, field):
        if field == "drive_freq_hz":
            raise ValueError(f"{target}.drive_freq_hz has no standing value")
        return real_anchor(self, target, field)

    monkeypatch.setattr(cls, "anchor", no_drive_freq)
    out = s.run(name, {"targets": ["q0"]})
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]
    assert fit["g_source"] == G_NONE
    assert not np.isfinite(fit["g_hz"]) and not np.isfinite(fit["g_coeff"])
    proposed = {(sg["entity"], sg["field"]) for sg in out["suggestions"]}
    assert ("q0_res", "g_hz") not in proposed
    assert ("q0_res", "g_coeff") not in proposed
    assert ("q0_res", "f_bare_hz") in proposed   # the branches still land


def test_the_two_coupling_routes_are_the_same_algebra(session):
    """The punchout route and the flux fit are ONE relation seen twice: feed the
    flux map's own sweet-spot pull (sweet_spot_res − f_bare) and its f_q_max
    through the punchout formula and the flux fit's g must come back.

    Deliberately not a cross-EXPERIMENT agreement test: the two offline
    simulators plant independent truths (the punchout hardcodes an 8 MHz Lamb
    shift, the flux simulator draws g from 70-100 MHz), so they cannot agree
    here and forcing them to would test the placeholders, not the physics. The
    real cross-route check ran on hardware — 0.2-2.4% on 5Q4C q1/q2/q3, inside
    each route's own run-to-run scatter (recorded in the ledger fragment)."""
    from scqo.experiments._transmon_estimate import g_coeff_from_g, g_hz_from_pull

    out = session.run("resonator_spectroscopy_flux", {"targets": ["q0"]},
                      update="none")
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]
    pull = fit["sweet_spot_res_hz"] - fit["f_bare_hz"]
    implied = g_hz_from_pull(pull, fit["f_bare_hz"], fit["f_q_max_hz"])
    assert implied == pytest.approx(fit["g_hz"], rel=1e-6)
    assert g_coeff_from_g(implied, fit["f_q_max_hz"],
                          fit["f_bare_hz"]) == pytest.approx(fit["g_coeff"])


def test_flux_map_projects_a_detuned_park_up_to_the_arch_top(tmp_path):
    """drive_freq_hz is the qubit AT THE PARKED FLUX. Parked off the sweet spot
    with a stored arch, f_q_max is projected up and the provenance tag says so;
    with no stored arch the parked-at-the-sweet-spot assumption stands, tagged
    plainly."""
    from scqo.experiments.resonator_spectroscopy_flux import (
        F_Q_MAX_DRIVE,
        F_Q_MAX_DRIVE_ARCH,
    )

    roster = demo_components(tunable=True)
    design = demo_design(roster)
    vendor = InMemoryDevice(roster, demo_vendor_state(roster, design))
    s = Session(SimulatedBackend(vendor), roster, design=design,
                scqo_dir=tmp_path / "scqo", data_root=tmp_path / "data",
                device_name="chipT", setup_name="sim", cooldown_id="cd1")

    # no stored arch yet -> the standing drive frequency is taken AS the top
    plain = s.run("resonator_spectroscopy_flux", {"targets": ["q0"]}, update="none")
    assert plain["fit"]["q0"]["f_q_max_source"] == F_Q_MAX_DRIVE
    f_q_max_plain = plain["fit"]["q0"]["f_q_max_hz"]

    # store an arch whose sweet spot is a sixth of a period from the park
    idle = float(s.device.channel("q0", "flux").idle_flux)
    s.set_values({"q0_z.flux_offset": idle + 0.1, "q0_z.flux_per_phi0": 0.6})
    detuned = s.run("resonator_spectroscopy_flux", {"targets": ["q0"]}, update="none")
    fit = detuned["fit"]["q0"]
    assert fit["f_q_max_source"] == F_Q_MAX_DRIVE_ARCH
    # projecting UP the arch: the top is above the parked frequency
    assert fit["f_q_max_hz"] > f_q_max_plain


def test_flux_map_design_g_seed_rescales_to_the_chip_frequencies(tmp_path):
    """g ∝ sqrt(f_q·f_r) with a geometry-constant coefficient, so a DESIGN g is
    valid only at the DESIGN frequencies. The fit seed rescales it to the
    chip's actual ones; a MEASURED g rides through untouched (it is the chip's
    own coupling); a missing input degrades to the unscaled design value."""
    from scqo.design import parse_design
    from scqo.experiments.resonator_spectroscopy_flux import (
        _DEFAULT_G_INIT_HZ,
        ResonatorSpectroscopyFlux,
        _g_init_hz,
    )

    roster = demo_components(tunable=True)
    design = parse_design(
        "schema = 1\n"
        "[q0]\nf_q_max_hz = 4.5e9\n"
        "[q0_res]\nf_dress0_hz = 5.9e9\ng_hz = 60e6\n",
        roster,
    )
    vendor = InMemoryDevice(roster, demo_vendor_state(roster, demo_design(roster)))
    s = Session(SimulatedBackend(vendor), roster, design=design,
                scqo_dir=tmp_path / "scqo", data_root=tmp_path / "data",
                device_name="chipT", setup_name="sim", cooldown_id="cd1")
    exp = ResonatorSpectroscopyFlux(
        s.backend, ResonatorSpectroscopyFlux.Parameters(targets=["q0"]))
    exp.device = s.device
    exp.design = s.design
    exp.physical = s.physical

    # a measured bare resonator makes the actual f_r deterministic
    s.set_values({"q0_res.f_bare_hz": 5.93e9})

    # design tier -> rescaled by sqrt((f_q·f_r)/(f_q_design·f_r_design))
    seed = _g_init_hz(exp, "q0", 5.15e9)
    assert seed == pytest.approx(
        60e6 * np.sqrt((5.15e9 * 5.93e9) / (4.5e9 * 5.9e9)))
    # missing actual f_q_max -> the unscaled design value (quiet degrade)
    assert _g_init_hz(exp, "q0", None) == pytest.approx(60e6)
    # measured tier -> untouched, whatever the frequencies say
    s.set_values({"q0_res.g_hz": 88e6})
    assert _g_init_hz(exp, "q0", 5.15e9) == pytest.approx(88e6)

    # no g on any tier -> the code default
    bare = ResonatorSpectroscopyFlux(
        s.backend, ResonatorSpectroscopyFlux.Parameters(targets=["q1"]))
    bare.device = s.device
    bare.design = s.design  # the custom design has no q1 entries at all
    bare.physical = s.physical
    assert _g_init_hz(bare, "q1", 5.15e9) == pytest.approx(_DEFAULT_G_INIT_HZ)


def test_flux_map_proposes_dispersive_physics_when_provenance_tag_is_stripped(session):
    """Campaign finalize replays update() over an AGGREGATED fit that keeps only
    numeric quantities, so the f_q_max_source tag is gone. update() must then
    re-derive the anchor from device state and still propose f_r0/g — dropping them
    would silently discard physics the per-repeat runs did propose."""
    from scqo.experiments.resonator_spectroscopy_flux import ResonatorSpectroscopyFlux
    from scqo.result import Outcome
    from scqo.suggestions import SuggestionCapture

    exp = ResonatorSpectroscopyFlux(
        session.backend, ResonatorSpectroscopyFlux.Parameters(targets=["q0"]))
    exp.device = session.device
    exp.design = session.design
    exp.result = ResonatorSpectroscopyFlux.Result(
        outcomes={"q0": Outcome.SUCCESSFUL},
        fit={"q0": {  # a numeric-only aggregated fit — no f_q_max_source tag
            "flux_offset": 0.1, "sweet_spot_res_hz": 5.93e9,
            "flux_per_phi0": 0.58, "f_bare_hz": 5.921e9, "g_hz": 90e6,
            "f_q_max_hz": 5.14e9}})
    capture = SuggestionCapture(session.device, session.physical, session.roster)
    exp.device = capture
    exp.update()
    fields = {(s.entity, s.field) for s in capture.suggestions}
    assert ("q0_res", "f_bare_hz") in fields and ("q0_res", "g_hz") in fields


@pytest.mark.parametrize("name", ["resonator_spectroscopy_power_amp",
                                  "resonator_spectroscopy_power_chain"])
def test_punchout_writes_the_operating_point_and_both_branches(session, name):
    """A punchout proposes the operating point on the readout CHANNEL and the
    physics the same sweep measured on the RESONATOR mode: the low-power dressed
    dip, the high-power bare one, and — because the standing drive frequency
    turns their gap into a coupling — g_hz with its geometry-constant twin
    g_coeff. Both mechanisms (fast amplitude sweep, chain-stepped) are one
    measurement and must agree on what they write."""
    assert _suggest(session, name) == {
        ("q0_ro", "readout_power_dbm"), ("q0_ro", "readout_freq_hz"),
        ("q0_res", "f_bare_hz"), ("q0_res", "f_dress0_hz"),
        ("q0_res", "g_hz"), ("q0_res", "g_coeff")}


@pytest.mark.parametrize("name", ["resonator_spectroscopy_power_amp",
                                  "resonator_spectroscopy_power_chain"])
def test_punchout_branch_physics(session, name):
    """The dressed dip sits ABOVE the bare one for a qubit below its resonator,
    and their gap is the Lamb shift g^2/Delta. The simulator plants 8 MHz."""
    out = session.run(name, {"targets": ["q0"]}, update="none")
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]
    assert fit["branch_success"]
    assert fit["f_dress0_hz"] > fit["f_bare_hz"]
    assert fit["lamb_shift_hz"] == pytest.approx(
        fit["f_dress0_hz"] - fit["f_bare_hz"])
    assert fit["lamb_shift_hz"] == pytest.approx(8.0e6, rel=0.1)
    # the plateau boundary powers bracket the transition, in order — the
    # record-only "how much of the window was actually plateau" provenance
    assert fit["dress_max_power_dbm"] < fit["bare_min_power_dbm"]
    # the flux the dressed frequency was measured AT — record-only provenance,
    # and what distinguishes a punchout before a flux map from one after it
    assert "old_idle_flux" in fit


def test_punchout_feeds_the_flux_map_a_measured_bare_frequency(tmp_path):
    """THE LOOP this feature exists for: punchout measures f_bare_hz directly (the
    high-power branch), and once accepted the flux map PINS it instead of fitting
    it free — which is what makes the flux map's g quantitative."""
    roster = demo_components(tunable=True)
    design = demo_design(roster)
    vendor = InMemoryDevice(roster, demo_vendor_state(roster, design))
    s = Session(SimulatedBackend(vendor), roster, design=design,
                scqo_dir=tmp_path / "scqo", data_root=tmp_path / "data",
                device_name="chipT", setup_name="sim", cooldown_id="cd1")

    # 1. punchout, accepting what it proposes (f_bare_hz among it)
    punch = s.run("resonator_spectroscopy_power_amp", {"targets": ["q0"]},
                  update="apply")
    assert punch.get("error") is None, punch.get("error")
    measured_bare = s.physical.get("q0_res", "f_bare_hz")
    assert measured_bare is not None, "punchout did not land f_bare_hz"

    # 2. the flux map now finds a MEASURED bare frequency and pins it
    flux = s.run("resonator_spectroscopy_flux", {"targets": ["q0"]}, update="none")
    assert flux.get("error") is None, flux.get("error")
    fit = flux["fit"]["q0"]
    assert fit["f_bare_source"] == "measured"
    assert fit["f_bare_hz"] == pytest.approx(measured_bare)  # held, not re-fitted


def test_flux_map_pins_a_measured_bare_frequency(tmp_path):
    """A STORED f_bare_hz is pinned as a fit constant — that is what breaks the
    f_r0/g degeneracy — and is therefore NOT proposed back: it was an input, not a
    fresh measurement of itself."""
    roster = demo_components(tunable=True)
    design = demo_design(roster)
    vendor = InMemoryDevice(roster, demo_vendor_state(roster, design))
    s = Session(SimulatedBackend(vendor), roster, design=design,
                scqo_dir=tmp_path / "scqo", data_root=tmp_path / "data",
                device_name="chipT", setup_name="sim", cooldown_id="cd1")
    measured_bare = 7.0985e9
    s.set_values({"q0_res.f_bare_hz": measured_bare})

    out = s.run("resonator_spectroscopy_flux", {"targets": ["q0"]})
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]
    assert fit["f_bare_source"] == "measured"
    assert fit["f_bare_hz"] == pytest.approx(measured_bare)   # held, not fitted
    fields = {(sg["entity"], sg["field"]) for sg in out["suggestions"]}
    assert ("q0_res", "f_bare_hz") not in fields              # never echoed back
    assert ("q0_res", "g_hz") in fields                       # but g still lands


def test_flux_map_only_seeds_a_designed_bare_frequency_and_warns(tmp_path):
    """A DESIGNED f_bare_hz is a nominal number carrying resonator fab scatter, and
    the whole flux signal is only the few-MHz pull — so it seeds the fit rather than
    pinning it, says so out loud, and tags the run as design-seeded."""
    from scqo.design import parse_design

    roster = demo_components(tunable=True)
    design = demo_design(roster)
    vendor = InMemoryDevice(roster, demo_vendor_state(roster, design))
    # same datasheet, plus a designed bare frequency for q0's resonator
    seeded = parse_design(
        "\n".join(["schema = 1", "[q0]", "f_q_max_hz = 3.8e9",
                   "[q0_res]", "f_dress0_hz = 7.1e9", "f_bare_hz = 7.09e9"]), roster)
    s = Session(SimulatedBackend(vendor), roster, design=seeded,
                scqo_dir=tmp_path / "scqo", data_root=tmp_path / "data",
                device_name="chipT", setup_name="sim", cooldown_id="cd1")

    with pytest.warns(UserWarning, match="design.toml"):
        out = s.run("resonator_spectroscopy_flux", {"targets": ["q0"]})
    assert out.get("error") is None, out.get("error")
    assert out["fit"]["q0"]["f_bare_source"] == "design"
    # a design-sourced fact tags the run, exactly as a design-seeded anchor does
    record = s.load_run(out["run_id"])["record"]
    assert "seeded:q0_res.f_bare_hz" in record["tags"]


def test_flux_map_ec_defaults_when_unsourced(session):
    """No stored ec_hz and none in the demo design -> the code default 0.2 GHz
    feeds the arch and is recorded (record-only, never proposed)."""
    out = session.run("resonator_spectroscopy_flux", {"targets": ["q0"]})
    assert out.get("error") is None, out.get("error")
    assert out["fit"]["q0"]["ec_hz"] == pytest.approx(0.2e9)
    # ec is a sourced INPUT, not measured physics -> never suggested.
    assert ("q0", "ec_hz") not in {(s["entity"], s["field"]) for s in out["suggestions"]}


def test_flux_map_ec_sourced_from_stored_fact(session):
    """A stored ec_hz fact wins over the code default and is the value the arch
    was fitted with (the fact tier of the fact->design->default precedence)."""
    session.set_values({"q0.ec_hz": 1.9e8})
    out = session.run("resonator_spectroscopy_flux", {"targets": ["q0"]})
    assert out.get("error") is None, out.get("error")
    assert out["fit"]["q0"]["ec_hz"] == pytest.approx(1.9e8)


def test_fact_helper_precedence(session):
    """Experiment.fact() reads stored fact -> design.toml -> code default, in
    that order (the primitive anchor() lacks — anchor serves knobs, raises on
    facts). g_hz resolves through the qubit closure to the attached resonator."""
    from scqo.design import Design
    from scqo.experiments.resonator_spectroscopy_flux import ResonatorSpectroscopyFlux

    exp = ResonatorSpectroscopyFlux(
        session.backend, ResonatorSpectroscopyFlux.Parameters(targets=["q0"]))
    exp.device = session.device

    # (1) nothing stored, empty design -> the code default.
    exp.design = Design({})
    exp.physical = None
    assert exp.fact("q0", "ec_hz", 0.2e9) == pytest.approx(0.2e9)

    # (2) design declares it -> design wins over the default.
    exp.design = Design({"q0": {"ec_hz": 1.7e8}})
    assert exp.fact("q0", "ec_hz", 0.2e9) == pytest.approx(1.7e8)

    # (3) a stored fact wins over the design value.
    session.set_values({"q0.ec_hz": 2.1e8})
    exp.physical = session.physical
    assert exp.fact("q0", "ec_hz", 0.2e9) == pytest.approx(2.1e8)

    # qubit-closure addressing: q0 + g_hz -> the attached resonator q0_res.
    session.set_values({"q0_res.g_hz": 8.5e7})
    assert exp.fact("q0", "g_hz", 50e6) == pytest.approx(8.5e7)


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


def test_spectroscopy_cryoscope_writes_the_paired_distortion_taps(session):
    """The long-time spectroscopy cryoscope proposes the SAME paired flux-channel
    distortion facts as the Ramsey one, with equal length."""
    out = session.run("qubit_spectroscopy_cryoscope", {"targets": ["q0"]})
    assert out.get("error") is None, out.get("error")
    assert {(s["entity"], s["field"]) for s in out["suggestions"]} == {
        ("q0_z", "distortion_amp"), ("q0_z", "distortion_tau_s")}
    after = {s["field"]: s["after"] for s in out["suggestions"]}
    assert len(after["distortion_amp"]) == len(after["distortion_tau_s"]) > 0


def test_spectroscopy_cryoscope_fit_values_are_physical(session):
    """Relative tap amplitudes, positive sub-100us tau constants, a nonzero parked
    center offset, and the frame declaration (excursion rode on the parked bias)."""
    out = session.run("qubit_spectroscopy_cryoscope", {"targets": ["q0"]}, update="none")
    assert out["outcomes"]["q0"] == "successful"
    fit = out["fit"]["q0"]
    amps, taus = fit["distortion_amp"], fit["distortion_tau_s"]
    assert len(amps) == len(taus) > 0
    assert all(math.isfinite(a) for a in amps)
    assert all(0 < t < 1e-4 for t in taus)  # seconds, sub-100 us (long-time tails)
    assert fit["center_offset_hz"] != 0.0  # the drive was centered (nominal on demo)
    assert fit["old_idle_flux"] == 0.0
    assert fit["flux_pulse_amp_v"] == pytest.approx(0.1)


def test_cryoscope_fit_tau_seeds_flow_through_and_validate(session):
    """fit_tau_seeds (prior-knowledge taus, seconds) reach the estimator on BOTH
    cryoscopes — the run succeeds with them set — and bad values are refused."""
    out = session.run(
        "qubit_spectroscopy_cryoscope",
        {"targets": ["q0"], "fit_tau_seeds": [5e-6, 4e-7]},
        update="none",
    )
    assert out["outcomes"]["q0"] == "successful"
    out = session.run(
        "qubit_ramsey_cryoscope",
        {"targets": ["q0"], "fit_tau_seeds": [30e-9]},
        update="none",
    )
    assert out["outcomes"]["q0"] == "successful"
    from scqo.experiments.qubit_ramsey_cryoscope import QubitRamseyCryoscopeParameters
    from scqo.experiments.qubit_spectroscopy_cryoscope import (
        QubitSpectroscopyCryoscopeParameters,
    )

    for cls in (QubitRamseyCryoscopeParameters, QubitSpectroscopyCryoscopeParameters):
        cls(targets=["q0"])  # None default stays legal
        for bad in ([], [0.0], [-1e-9]):
            with pytest.raises(ValueError, match="fit_tau_seeds"):
                cls(targets=["q0"], fit_tau_seeds=bad)


def test_spectroscopy_cryoscope_accept_roundtrips_paired_facts(session):
    """Accepting lands both arrays on the flux channel with equal length — the same
    paired batch apply the Ramsey cryoscope uses (REPLACE, last-writer-wins)."""
    out = session.run("qubit_spectroscopy_cryoscope", {"targets": ["q1"]})
    summary = session.accept(out["run_id"])
    assert not summary["errors"]
    physical = session.physical_state()["q1_z"]
    assert physical["distortion_amp"] is not None
    assert len(physical["distortion_amp"]) == len(physical["distortion_tau_s"]) > 0


def test_cryoscope_apply_hint_comes_from_the_backend_hook():
    """The vendor command is BACKEND knowledge (SCQO knows no OPX): the hint asks
    the duck-typed distortion_apply_command hook, names the manual step when a
    backend declares none, and degrades a BROKEN hook to a line — a hint may
    never take a measurement's writeback down with it."""
    from pathlib import Path

    from scqo.experiments._distortion_hint import apply_hint_lines, run_id_of

    hooked = SimpleNamespace(
        distortion_apply_command=lambda t, run: f"vendor-apply {t} {run}")
    assert any("q0: vendor-apply q0 run-7" in line for line in
               apply_hint_lines("qubit_ramsey_cryoscope", hooked, ["q0"], "run-7"))

    bare = "\n".join(apply_hint_lines("qubit_ramsey_cryoscope", SimpleNamespace(), ["q0"]))
    assert "declares no distortion_apply_command" in bare
    assert "distortion_amp + distortion_tau_s into its output filter by hand" in bare

    def boom(target, run_id):
        raise RuntimeError("no state loaded")

    broken = apply_hint_lines("qubit_ramsey_cryoscope",
                              SimpleNamespace(distortion_apply_command=boom), ["q0"])
    assert any("RuntimeError: no state loaded" in line for line in broken)

    # nothing proposed (every target failed) -> nothing printed
    assert apply_hint_lines("qubit_ramsey_cryoscope", hooked, []) == []
    # the run id is the run FOLDER's name; no artifacts (--skip-artifacts) = none
    assert run_id_of(None) is None
    assert run_id_of(Path("data") / "chipT" / "2026-08-21" / "RUN-1" / "analysis") == "RUN-1"


def test_cryoscope_prints_the_apply_hint_on_writeback(session, capsys, monkeypatch):
    """Both cryoscopes print the hint when they propose taps: on STDERR (stdout
    stays parseable JSON), one line per SUCCESSFUL target, carrying the backend's
    command addressed to this very run."""
    monkeypatch.setattr(session.backend, "distortion_apply_command",
                        lambda target, run_id: f"vendor-apply {target} {run_id}",
                        raising=False)
    for name in ("qubit_ramsey_cryoscope", "qubit_spectroscopy_cryoscope"):
        out = session.run(name, {"targets": ["q0"]})
        captured = capsys.readouterr()
        assert f"# {name}: the fitted taps are FACTS" in captured.err
        assert f"vendor-apply q0 {out['run_id']}" in captured.err
        assert "vendor-apply" not in captured.out


def test_spectroscopy_cryoscope_window_and_drive_len_validation():
    """The detuning window is the drive_detuning capability's explicit
    [start, end] range (asymmetric allowed, edges in either order, axis always
    emitted ascending), and the spectroscopy tone's shape parameters carry their
    own bounds (drive_len_ns on the 4 ns grid at or above 16 ns). define_sweep
    reads only params, so a stub backend exercises it."""
    cls = registry.get("qubit_spectroscopy_cryoscope")

    # the default window reproduces the old symmetric +/-100 MHz, 101 points
    default = cls(SimpleNamespace(device=None), cls.Parameters(targets=["q0"]))
    det = default.define_sweep()["detuning_hz"]
    assert det[0] == pytest.approx(-100e6) and det[-1] == pytest.approx(100e6)
    assert det.size == 101

    # an asymmetric, one-sided window flows through ascending
    asym = cls(SimpleNamespace(device=None),
               cls.Parameters(targets=["q0"], start_drive_detuning_hz=-70e6,
                              end_drive_detuning_hz=0.0, num_drive_freq_points=71))
    det = asym.define_sweep()["detuning_hz"]
    assert det[0] == pytest.approx(-70e6) and det[-1] == pytest.approx(0.0)
    assert det.size == 71
    assert np.all(np.diff(det) > 0)  # ascending — peak_fit's gamma bound needs it

    # the SAME window written the other way round is the same measurement: the
    # edges define the window, the axis is normalised ascending either way
    rev = cls(SimpleNamespace(device=None),
              cls.Parameters(targets=["q0"], start_drive_detuning_hz=0.0,
                             end_drive_detuning_hz=-70e6, num_drive_freq_points=71))
    assert rev.define_sweep()["detuning_hz"] == pytest.approx(det)

    # only a zero-width window is refused
    with pytest.raises(ValueError, match="zero-width"):
        cls.Parameters(targets=["q0"], start_drive_detuning_hz=10e6, end_drive_detuning_hz=10e6)

    # drive_len_ns: at or above the 16 ns floor, on the 4 ns grid
    with pytest.raises(ValueError):
        cls.Parameters(targets=["q0"], drive_len_ns=10)   # below the 16 ns floor
    with pytest.raises(ValueError):
        cls.Parameters(targets=["q0"], drive_len_ns=18)   # off the 4 ns grid


def test_spectroscopy_cryoscope_drive_shape_parameters():
    """The spectroscopy tone is a built pi pulse, SQUARE by default: the center
    precision that decides this measurement is set by the linewidth (5Q4C q1,
    2026-08-19 — scatter 0.064 / 0.128 / 0.164 MHz for square / gaussian / cosine
    at one setting, tracking their 7.4 / 14.6 / 16.2 MHz widths), and square is the
    narrowest per ns. The smooth envelopes stay selectable for a line that looks
    WRONG rather than wide, with their knobs bounded."""
    cls = registry.get("qubit_spectroscopy_cryoscope")

    default = cls.Parameters(targets=["q0"])
    assert default.drive_shape == "square"
    assert default.drive_sigma_frac == pytest.approx(0.25)
    assert default.drive_amp_factor == pytest.approx(1.0)

    for shape in ("square", "cosine", "gaussian"):
        assert cls.Parameters(targets=["q0"], drive_shape=shape).drive_shape == shape
    with pytest.raises(ValueError):
        cls.Parameters(targets=["q0"], drive_shape="lorentzian")

    # sigma is a FRACTION of the length: a whole-length sigma is not a gaussian,
    # and a vanishing one is a spike the 4 ns grid cannot render.
    for bad in (0.05, 0.0, 0.6, -0.1):
        with pytest.raises(ValueError):
            cls.Parameters(targets=["q0"], drive_sigma_frac=bad)
    assert cls.Parameters(targets=["q0"], drive_sigma_frac=0.5).drive_sigma_frac == 0.5

    # the pi-area multiplier is strictly positive (0 plays no tone at all)
    for bad in (0.0, -1.0):
        with pytest.raises(ValueError):
            cls.Parameters(targets=["q0"], drive_amp_factor=bad)


@pytest.mark.parametrize("name,axes", [
    ("pair_swap_chevron", ("flux_amp_v", "swap_time_ns")),
    ("pair_swap_flux_map", ("qubit_flux_v", "coupler_flux_v")),
    ("qc_n_swap_amp", ("flux_amp_v", "swap_count")),
    ("qc_n_stark_amp", ("stark_amp", "swap_count")),
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


def _trotter(session, **params):
    out = session.run("qc_unidirectional_trotter",
                      {"targets": list(CHAIN_QUBITS), **params}, update="none")
    assert out.get("error") is None, out.get("error")
    return out


def test_unidirectional_trotter_transports_source_to_sink(session):
    """The whole point of the sequence, read off the three summaries: the source
    empties, the sink shows a transient, and the relay stays near |0> because it
    is reset every round. The sink does NOT approach 1 — the second swap also
    drains it into the relay — so this pins the SHAPE, not a full transfer."""
    fit = _trotter(session, max_rounds=20)["fit"]
    source, relay, sink = CHAIN_QUBITS
    assert fit[source]["p_initial"] > 0.9          # the prep landed
    assert fit[source]["p_final"] < 0.1            # ...and drained away
    assert fit[relay]["p_max"] < 0.1, "the relay is dumped every round"
    assert fit[sink]["p_initial"] < 0.05           # nothing there before the rounds
    assert fit[sink]["p_max"] > 0.05               # ...and something arrives
    assert fit[sink]["n_at_max"] > 0               # transport takes rounds
    assert fit[sink]["p_max"] == fit[source]["sink_p_max"], (
        "the chain verdict number is repeated on every row")


def test_unidirectional_trotter_round_axis_starts_at_the_bare_prep(session):
    """N=0 is the prep-only baseline (the qc_* family convention), so the axis
    carries max_rounds + 1 points and the first one is the un-Trottered state."""
    out = _trotter(session, max_rounds=6)
    assert out["fit"][CHAIN_QUBITS[0]]["n_round_count"] == 7.0
    ds = session.datastore.open_dataset(out["run_id"])
    assert list(ds["round_count"].values) == list(range(7))


def test_unidirectional_trotter_shot_mode_reconstructs_the_joint(session):
    """Shot mode keeps per-qubit levels from the SAME shot, so the joint chain
    distribution comes for free — a second figure, no second acquisition."""
    out = _trotter(session, max_rounds=8, num_averages=200, readout_mode="shot")
    ds = session.datastore.open_dataset(out["run_id"])
    assert "state" in ds.data_vars and ds.sizes["shot_idx"] == 200
    figures = {Path(p).name for p in session.load_run(out["run_id"])["figures"]}
    assert "qc_unidirectional_trotter.png" in figures
    assert "qc_unidirectional_trotter_joint.png" in figures
    # average mode acquires no shots, so it gets the transport figure only
    plain = _trotter(session, max_rounds=8)
    plain_figs = {Path(p).name for p in session.load_run(plain["run_id"])["figures"]}
    assert plain_figs == {"qc_unidirectional_trotter.png"}
    # both modes see the same physics
    for qubit in CHAIN_QUBITS:
        assert out["fit"][qubit]["p_max"] == pytest.approx(
            plain["fit"][qubit]["p_max"], abs=0.06)


BROKEN_CHAINS = [
    ({"first_pair": "q0_q1", "second_pair": "q0_q1"}, "share 2 member"),
    ({"first_pair": "q0_q1", "second_pair": "nope"}, "not in the roster"),
    ({"first_pair": "q0", "second_pair": "q1_q2"}, "not a qubit_pair"),
    # the channel-existence gates: a resonator mode has neither a z line to
    # play the parametric reset on, nor an xy line for a Stark tone; the tracked
    # coupler has flux but no drive, so it isolates the second gate alone.
    ({"reset_qubit": "q0_res"}, "no flux channel"),
    ({"compensation_amps": {"q0_q1_c": 0.2}}, "no drive channel"),
]


@pytest.mark.parametrize("override,message", BROKEN_CHAINS)
def test_unidirectional_trotter_refuses_a_broken_chain(session, override, message):
    """The topology gate runs in define_sweep — the earliest hook that sees both
    the roster and the params — so a mis-wired chain costs no instrument time."""
    out = session.run("qc_unidirectional_trotter",
                      {"targets": list(CHAIN_QUBITS), **override}, update="none")
    assert message in (out.get("error") or ""), out.get("error")


def test_unidirectional_trotter_needs_the_sink_to_judge_transport(session):
    """A run that does not read the sink out cannot see transport, so it reports
    FAILED rather than passing on the qubits it did measure."""
    out = session.run("qc_unidirectional_trotter",
                      {"targets": ["q0", "q1"]}, update="none")
    assert out.get("error") is None, out.get("error")
    assert set(out["outcomes"].values()) == {"failed"}
    assert math.isnan(out["fit"]["q0"]["sink_p_max"])


READOUT_SWEEPS = [
    ("readout_power", "readout_amp", {"num_amp_points": 5}),
    ("readout_frequency", "readout_freq_hz", {"num_readout_freq_points": 5}),
]


@pytest.mark.parametrize("name,knob,params", READOUT_SWEEPS)
def test_readout_sweeps_average_mode_drops_the_shot_axis(session, name, knob, params):
    """readout_mode='average': the probe returns one FPGA-averaged I/Q point per
    prepared state, so the dataset carries no shot axis at all and the contract's
    alternative form is the one that conforms."""
    cls = registry.get(name)
    out = session.run(name, {"targets": ["q0"], "readout_mode": "average",
                             "num_shots": 200, **params}, update="none")
    assert out.get("error") is None, out.get("error")

    ds = session.datastore.open_dataset(out["run_id"])
    assert "shot_idx" not in ds.dims
    assert set(ds["I"].dims) == {"target", *cls.Contract.sweeps}
    cls.Contract.validate(ds)  # the averaged form is accepted, not tolerated
    ds.close()


@pytest.mark.parametrize("name,knob,params", READOUT_SWEEPS)
def test_readout_sweeps_average_mode_optimizes_separation_not_fidelity(
    session, name, knob, params
):
    """Nothing is fitted in average mode, so there IS no fidelity: the answer is
    the largest blob separation, and the run record says so rather than quietly
    reporting a fidelity it never measured."""
    out = session.run(name, {"targets": ["q0"], "readout_mode": "average",
                             "num_shots": 200, **params})
    assert out.get("error") is None, out.get("error")

    fit = out["fit"]["q0"]
    assert math.isnan(fit["best_fidelity"])
    assert math.isfinite(fit["best_separation"]) and fit["best_separation"] > 0
    # the knob is still proposed — the sweep answered its question
    assert ("q0_ro", knob) in {(s["entity"], s["field"]) for s in out["suggestions"]}


@pytest.mark.parametrize("name,knob,params", READOUT_SWEEPS)
def test_readout_sweeps_shot_mode_still_reports_a_fidelity(session, name, knob, params):
    """The default path is unchanged: per-shot data, a mixture fit, a fidelity."""
    out = session.run(name, {"targets": ["q0"], "num_shots": 300, **params})
    assert out.get("error") is None, out.get("error")

    fit = out["fit"]["q0"]
    assert math.isfinite(fit["best_fidelity"]) and fit["best_fidelity"] > 0.5
    assert math.isfinite(fit["best_separation"])
    assert ("q0_ro", knob) in {(s["entity"], s["field"]) for s in out["suggestions"]}


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


def test_parity_switch_continuous_writes_parity_rate_fact(session):
    assert _suggest(session, "qubit_parity_switch_continuous") == {("q0", "parity_rate_hz")}


def test_parity_switch_continuous_recovers_the_planted_rate(session):
    """The offline loop closes: the sim's Markov flip probability over the
    attached shot period comes back out of the PSD knee, at the idle the
    seeded 250 kHz splitting implies."""
    from scqo.experiments._sim import stable_seed

    out = session.run("qubit_parity_switch_continuous", {"targets": ["q0"]}, update="none")
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]
    assert fit["idle_time_ns"] == pytest.approx(2000.0)  # 1 / (2 x 250 kHz)
    assert fit["parity_delta_f_hz"] == pytest.approx(250e3)
    assert "outlier_probability" in fit  # the discriminated-path marker
    assert 0.3 < fit["p_parity_odd"] < 0.7
    rng = np.random.default_rng(stable_seed("qubit_parity_switch_continuous", "q0"))
    p_flip = rng.uniform(0.002, 0.01)
    expected = p_flip / fit["shot_period_s"]
    assert fit["parity_rate_hz"] == pytest.approx(expected, rel=0.2)


def test_parity_switch_continuous_refuses_without_the_splitting(tmp_path):
    s = _fresh_parity_session(tmp_path, splitting=False)
    out = s.run("qubit_parity_switch_continuous", {"targets": ["q0"]})
    assert "parity_delta_f_hz" in str(out.get("error"))
    assert "ramsey_model='beat'" in str(out.get("error"))
    # ... but an explicit idle override runs without the stored splitting
    ok = s.run("qubit_parity_switch_continuous",
               {"targets": ["q0"], "idle_time_ns": 1000.0}, update="none")
    assert ok.get("error") is None, ok.get("error")
    assert ok["fit"]["q0"]["idle_time_ns"] == pytest.approx(1000.0)
    assert math.isnan(ok["fit"]["q0"]["parity_delta_f_hz"])


def test_parity_switch_continuous_two_stage_chain(tmp_path):
    """The full workflow on a fresh device: beat ramsey -> apply -> the parity
    monitor derives its idle from the just-measured splitting -> apply -> the
    rate lands in physical state."""
    s = _fresh_parity_session(tmp_path, splitting=False)
    out1 = s.run("qubit_ramsey", {
        "targets": ["q0"], "ramsey_model": "beat",
        "max_idle_time_ns": 10000, "num_points": 201}, update="apply")
    assert out1.get("error") is None, out1.get("error")
    delta = out1["fit"]["q0"]["parity_delta_f_hz"]

    out2 = s.run("qubit_parity_switch_continuous", {"targets": ["q0"]}, update="apply")
    assert out2.get("error") is None, out2.get("error")
    fit = out2["fit"]["q0"]
    assert fit["parity_delta_f_hz"] == pytest.approx(delta)
    assert fit["idle_time_ns"] == pytest.approx(
        max(16.0, round(1e9 / (2.0 * delta) / 4.0) * 4.0))
    assert s.physical_state()["q0"]["parity_rate_hz"] is not None


def test_parity_switch_continuous_reports_the_odd_fraction(session):
    """p_switch explains a FAILED run whose fit otherwise looks healthy, so it
    has to reach the run record — and it must not be confused with
    p_parity_odd, which sits near 0.5 on every healthy run.

    The simulator plants a per-shot parity switch probability in (0.002, 0.01),
    so p_switch lands in that range; p_parity_odd is ~0.5 because the chip
    spends about half its time in each parity."""
    out = session.run("qubit_parity_switch_continuous", {"targets": ["q0"]}, update="none")
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


def test_parity_switch_continuous_derives_shots_from_record_time(session):
    """record_time_s is the knob, because the spectrum's lowest frequency is
    8 / record_time_s and THAT is what limits how slow a rate is measurable.
    The shot count follows from the estimated shot period."""
    out = session.run("qubit_parity_switch_continuous",
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
    half = session.run("qubit_parity_switch_continuous",
                       {"targets": ["q0"], "record_time_s": 0.4}, update="none")
    assert (half["fit"]["q0"]["psd_freq_min_hz"]
            == pytest.approx(2 * fit["psd_freq_min_hz"], rel=0.1))


def test_parity_switch_continuous_refuses_an_absurd_record_time(session):
    """The ceiling guards the DERIVED count: a long record on a fast-cadence
    qubit asks for more shots than an instrument will hold."""
    out = session.run("qubit_parity_switch_continuous",
                      {"targets": ["q0"], "record_time_s": 3600.0})
    assert "max_num_shots" in str(out.get("error"))
    assert "record_time_s" in str(out.get("error"))


def test_parity_switch_continuous_num_shots_overrides_and_bypasses_the_ceiling(session):
    """num_shots wins outright, exactly as idle_time_ns bypasses
    max_derived_idle_ns -- the ceiling is on the derived value only."""
    out = session.run(
        "qubit_parity_switch_continuous",
        {"targets": ["q0"], "num_shots": 120000, "max_num_shots": 1000},
        update="none")
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]
    assert round(fit["record_time_s"] / fit["shot_period_s"]) == 120000


def test_parity_switch_continuous_reports_the_spectral_reach(session):
    """A corner near the lowest bin is the readable symptom of 'record for
    longer', so the margin has to reach the run record."""
    out = session.run("qubit_parity_switch_continuous", {"targets": ["q0"]}, update="none")
    fit = out["fit"]["q0"]
    assert fit["corner_margin_low"] == pytest.approx(
        fit["psd_corner_hz"] / fit["psd_freq_min_hz"], rel=1e-6)
    assert fit["psd_contrast"] > 3.0          # the gate that replaced p_switch


def test_parity_switch_continuous_reports_the_mapping_fidelity_twice(session):
    """Under the INDEPENDENT model, A and B are the reference model's 4F^2 and
    (1-F^2)dt terms, so the same fit yields F two independent ways. Both must
    reach the run record with their ratio -- the ratio is the only number that
    notices the correlated-noise model failure that psd_contrast is blind to.
    (The default is now 'constrained', where the floor/ratio are NaN; this is
    the OPT-IN model, so it must be requested by name.)"""
    out = session.run("qubit_parity_switch_continuous",
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


def test_parity_switch_continuous_default_model_is_constrained(session):
    """The DEFAULT run fits the reference single-F model, whose fingerprint is
    one mapping_fidelity plus NaN for the plateau/floor cross-check (the coupled
    model cannot produce it) and a finite residual as its quality number. The
    fitted-model STRING is a string, so it lives in the scqat metadata artifact
    and the run parameters, not in the float-only fit dict."""
    out = session.run("qubit_parity_switch_continuous", {"targets": ["q0"]}, update="none")
    fit = out["fit"]["q0"]
    assert math.isfinite(fit["mapping_fidelity"])          # the single fitted F
    assert math.isnan(fit["mapping_fidelity_floor"])        # no independent floor
    assert math.isnan(fit["mapping_fidelity_ratio"])
    assert math.isfinite(fit["psd_fit_residual"])           # its quality number


def test_parity_switch_continuous_independent_model_selectable(session):
    """The opt-in model is reachable by name and still recovers the rate."""
    base = session.run("qubit_parity_switch_continuous", {"targets": ["q0"]}, update="none")
    indep = session.run("qubit_parity_switch_continuous",
                        {"targets": ["q0"], "psd_model": "independent"},
                        update="none")
    assert indep["outcomes"]["q0"] == "successful"
    # both models see the same planted telegraph, so the rate agrees
    assert indep["fit"]["q0"]["parity_rate_hz"] == pytest.approx(
        base["fit"]["q0"]["parity_rate_hz"], rel=0.3)


def test_parity_switch_continuous_idle_multiple_stretches_the_derived_idle(session):
    """idle = N / (2 x parity_delta_f_hz). The seeded 250 kHz gives a 2000 ns
    base, so N=3 must play 6000 ns."""
    out = session.run("qubit_parity_switch_continuous",
                      {"targets": ["q0"], "idle_multiple": 3,
                       "max_derived_idle_ns": 100000.0}, update="none")
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]
    assert fit["idle_time_ns"] == pytest.approx(6000.0)
    assert fit["idle_multiple"] == 3


def test_parity_switch_continuous_explicit_idle_is_not_multiplied(session):
    """idle_time_ns is the escape hatch: it wins outright, so idle_multiple
    must NOT scale it (silently tripling an explicit request would be the worst
    kind of surprise)."""
    out = session.run("qubit_parity_switch_continuous",
                      {"targets": ["q0"], "idle_time_ns": 1000.0,
                       "idle_multiple": 5}, update="none")
    assert out.get("error") is None, out.get("error")
    assert out["fit"]["q0"]["idle_time_ns"] == pytest.approx(1000.0)


def test_parity_switch_continuous_even_idle_multiple_warns_but_runs(session):
    """Even N puts both parities on the same pole (sin(N pi/2) == 0), so the
    run carries no signal. The user asked for it to be allowed, so it must warn
    loudly rather than refuse -- and then fail honestly in the fit."""
    with pytest.warns(UserWarning, match="EVEN"):
        out = session.run("qubit_parity_switch_continuous",
                          {"targets": ["q0"], "idle_multiple": 2,
                           "max_derived_idle_ns": 100000.0}, update="none")
    assert out.get("error") is None, out.get("error")
    assert out["fit"]["q0"]["idle_time_ns"] == pytest.approx(4000.0)


def test_parity_switch_continuous_ceiling_names_the_multiple(session):
    """Two faults reach the same ceiling and need different remedies: here the
    BASE is fine and only the multiple pushed it over, so the message must say
    so instead of blaming a stale splitting."""
    out = session.run("qubit_parity_switch_continuous",
                      {"targets": ["q0"], "idle_multiple": 21})
    err = str(out.get("error"))
    assert "idle_multiple" in err
    assert "base" in err.lower()


# ------------------------------------------------------- parity (discrete)


def test_parity_switch_discrete_writes_parity_rate_fact(session):
    assert _suggest(session, "qubit_parity_switch_discrete") == {
        ("q0", "parity_rate_hz")}


def test_parity_switch_discrete_recovers_the_planted_rate(session):
    """The offline loop closes for the two-measurement variant: the sim's
    per-cycle Markov flip probability over the attached cycle period comes
    back out of the PSD knee, from the WITHIN-CYCLE m1 XOR m2 reduction."""
    from scqo.experiments._sim import stable_seed

    out = session.run("qubit_parity_switch_discrete", {"targets": ["q0"]},
                      update="none")
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]
    # same idle derivation as continuous: 1 / (2 x 250 kHz), on-grid
    assert fit["idle_time_ns"] == pytest.approx(2000.0)
    assert fit["parity_delta_f_hz"] == pytest.approx(250e3)
    assert "outlier_probability" in fit  # the discriminated-path marker
    assert 0.3 < fit["p_parity_odd"] < 0.7
    # the sim's QND chain is exact and the blobs are 10 sigma apart, so the
    # inter-cycle health check reads clean
    assert fit["p_intercycle_flip"] < 0.01
    rng = np.random.default_rng(stable_seed("qubit_parity_switch_discrete", "q0"))
    p_flip = rng.uniform(0.002, 0.01)
    expected = p_flip / fit["shot_period_s"]
    assert fit["parity_rate_hz"] == pytest.approx(expected, rel=0.2)
    assert out["outcomes"]["q0"] == "successful"


def test_parity_switch_discrete_cycle_period_sets_the_timebase(session):
    """cycle_period_ns IS the telegraph timebase: the recorded period must be
    exactly the request (the sequence fits inside 20 us on the demo device)
    and the cycle count follows record_time_s at that slower cadence."""
    out = session.run("qubit_parity_switch_discrete",
                      {"targets": ["q0"], "cycle_period_ns": 20000.0},
                      update="none")
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]
    assert fit["shot_period_s"] == pytest.approx(2e-5)
    assert fit["cycle_period_ns"] == pytest.approx(20000.0)
    n_cycles = round(fit["record_time_s"] / fit["shot_period_s"])
    assert n_cycles == pytest.approx(0.4 / 2e-5, rel=0.01)
    # ... and the default run reports NaN there (no pad requested)
    base = session.run("qubit_parity_switch_discrete", {"targets": ["q0"]},
                       update="none")
    assert math.isnan(base["fit"]["q0"]["cycle_period_ns"])


def test_parity_switch_discrete_dataset_carries_both_measurements(session):
    """Two measurements per cycle: every cycle is its own parity sample (the
    series has n entries, not n-1), and the per-slot diagnostics reach the
    run record."""
    out = session.run("qubit_parity_switch_discrete", {"targets": ["q0"]},
                      update="none")
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]
    for key in ("p_intercycle_flip", "p_m1_high", "p_m2_high"):
        assert math.isfinite(fit[key]), key
    n_cycles = round(fit["record_time_s"] / fit["shot_period_s"])
    # p_switch = transitions / (n_samples - 1) with n_samples == n_cycles —
    # the per-cycle reduction's fingerprint (continuous divides by n - 2)
    assert fit["p_switch"] == pytest.approx(
        fit["n_parity_switches"] / (n_cycles - 1), rel=1e-6)


def test_t1_ade_recovers_the_planted_t1(session):
    """The ADE closed form must track the T1 the simulator planted, with a
    finite analytic sigma and the bootstrap cross-check on the same shots."""
    from scqo.experiments._sim import stable_seed

    planted = np.random.default_rng(stable_seed("qubit_t1_ade", "q0")).uniform(20e-6, 60e-6)
    out = session.run("qubit_t1_ade", {"targets": ["q0"]}, update="none")
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]
    assert fit["t1_median_s"] == pytest.approx(planted, rel=0.1)
    assert math.isfinite(fit["t1_sigma_median_s"])
    assert math.isfinite(fit["t1_boot_sigma_median_s"])  # shots were streamed
    assert fit["n_valid"] >= fit["n_blocks"] * 0.9
    assert out["outcomes"]["q0"] == "successful"


def test_t1_bayesian_recovers_the_planted_t1(session):
    """The adaptive posterior must converge from the default prior to the
    planted T1, and the interleaved validation fit must agree with it."""
    from scqo.experiments._sim import stable_seed

    planted = np.random.default_rng(
        stable_seed("qubit_t1_bayesian", "q0")).uniform(20e-6, 60e-6)
    out = session.run("qubit_t1_bayesian", {"targets": ["q0"]}, update="none")
    assert out.get("error") is None, out.get("error")
    fit = out["fit"]["q0"]
    assert fit["t1_median_s"] == pytest.approx(planted, rel=0.2)
    assert fit["k_final_median"] > 5  # the u = 1/k trick let k grow past ~7
    assert fit["t1_lin_s"] == pytest.approx(planted, rel=0.2)
    assert fit["validation_disagrees"] == 0.0
    assert out["outcomes"]["q0"] == "successful"


def test_broadband_resonator_spectroscopy_runs_and_marks_dips(session):
    """Broadband resonator spectroscopy must sweep across multi-LO bands,
    detect candidate dips, and mark them without producing state suggestions."""
    out = session.run(
        "broadband_resonator_spectroscopy",
        {
            "targets": ["q0"],
            "start_freq_hz": 4.0e9,
            "stop_freq_hz": 8.0e9,
            "bandwidth_per_lo_hz": 500e6,
            "num_points_per_lo": 101,
        },
        update="none",
    )
    assert out.get("error") is None, out.get("error")
    assert out["outcomes"]["q0"] == "successful"
    fit = out["fit"]["q0"]
    assert "dips" in fit
    assert "resonator_frequencies_hz" in fit
    assert len(fit["resonator_frequencies_hz"]) > 0
    # Must be RECORD_ONLY — no suggestions proposed
    assert len(out.get("suggestions", [])) == 0


def test_broadband_resonator_spectroscopy_multi_targets(session):
    """Broadband resonator spectroscopy must handle multiple targets in a single sweep."""
    out = session.run(
        "broadband_resonator_spectroscopy",
        {
            "targets": ["q0", "q1"],
            "start_freq_hz": 4.0e9,
            "stop_freq_hz": 8.0e9,
            "bandwidth_per_lo_hz": 500e6,
            "num_points_per_lo": 101,
        },
        update="none",
    )
    assert out.get("error") is None, out.get("error")
    assert out["outcomes"]["q0"] == "successful"
    assert out["outcomes"]["q1"] == "successful"
    assert out["fit"]["q0"]["resonator_frequencies_hz"] == out["fit"]["q1"]["resonator_frequencies_hz"]




# ---------------------------------------------------------------------------
# pair_swap_angle - the swap ANGLE, read off the oscillation period in N
# ---------------------------------------------------------------------------

def _angle(session, **params):
    out = session.run("pair_swap_angle",
                      {"targets": ["q0_q1"], **params}, update="none")
    assert out.get("error") is None, out.get("error")
    return out


def test_pair_swap_angle_fits_an_angle_per_coupler_value(session):
    """Every coupler amplitude must yield a converged angle on clean data, and
    the curve must span the range the simulator planted."""
    out = _angle(session)
    fit = out["fit"]["q0_q1"]
    assert out["outcomes"]["q0_q1"] == "successful"
    assert fit["n_theta_ok"] == fit["n_coupler_flux_v"]
    # the planted window is theta_lo in [0.25, 0.40] rising to theta_hi in
    # [1.10, 1.40]; both stay inside the integer-N Nyquist limit (pi/2)
    assert 0.2 < fit["theta_min_rad"] < 0.45
    assert 1.05 < fit["theta_max_rad"] < math.pi / 2
    assert fit["theta_min_rad"] < fit["theta_max_rad"]


def test_pair_swap_angle_solves_the_curve_for_a_requested_angle(session):
    """A target angle inside the measured range is INTERPOLATED, and the volts
    it reports are inside the swept window."""
    out = _angle(session, target_theta_rad=0.8)
    fit = out["fit"]["q0_q1"]
    assert fit["best_theta_rad"] == pytest.approx(0.8)
    assert fit["best_is_interpolated"] == 1.0
    assert 0.0 <= fit["best_coupler_flux_v"] <= 0.1


def test_pair_swap_angle_without_a_target_measures_only_the_curve(session):
    out = _angle(session)
    fit = out["fit"]["q0_q1"]
    assert math.isnan(fit["target_theta_rad"])
    assert math.isnan(fit["best_coupler_flux_v"])
    # ...but the curve itself is still there, so the run is not wasted
    assert fit["n_theta_ok"] > 0


def test_pair_swap_angle_shot_mode_gives_the_same_angles(session):
    """Shot mode reduces to the SAME joint distribution, so the calibration must
    not depend on which readout mode was used."""
    average = _angle(session, num_coupler_points=7)["fit"]["q0_q1"]
    shot = _angle(session, num_coupler_points=7, num_averages=400,
                  readout_mode="shot")["fit"]["q0_q1"]
    assert shot["n_theta_ok"] == average["n_theta_ok"]
    assert shot["theta_max_rad"] == pytest.approx(average["theta_max_rad"], abs=0.1)


def test_pair_swap_angle_is_record_only(session):
    assert len(_angle(session).get("suggestions", [])) == 0


def test_pair_swap_angle_needs_a_coupler(session):
    """The angle knob IS the coupler, so a pair without one is refused before
    any instrument time is booked -- unlike qc_n_swap_amp, which needs none."""
    out = session.run("pair_swap_angle", {"targets": ["q0_q2"]}, update="none")
    assert out.get("error") is not None


# ---------------------------------------------------------------------------
# qc_trotter_compensation - the differential-phase scan over the chain
# ---------------------------------------------------------------------------

def _compensation(session, **params):
    out = session.run("qc_trotter_compensation",
                      {"targets": list(CHAIN_QUBITS), **params}, update="none")
    assert out.get("error") is None, out.get("error")
    return out


def test_trotter_compensation_finds_an_optimum_inside_the_window(session):
    """The simulator plants the phase-nulling amplitude inside the swept range,
    so the scan must find it and beat the worst amplitude by a real factor."""
    out = _compensation(session, max_rounds=12)
    fit = out["fit"]["q2"]
    assert set(out["outcomes"].values()) == {"successful"}
    assert 0.0 < fit["best_compensation_amp"] < 1.0
    assert fit["contrast"] > 2.0
    assert fit["best_sink_p_max"] > fit["worst_sink_p_max"]


def test_trotter_compensation_n_at_max_reads_the_phase_condition(session):
    """The second discriminator: when the rounds cancel only the LAST one
    survives, so the sink peaks at N=1; when they add, the peak moves out."""
    fit = _compensation(session, max_rounds=12)["fit"]["q2"]
    assert fit["best_n_at_max"] >= 3.0


def test_trotter_compensation_reports_the_curve_at_the_optimum(session):
    """The slice at the optimum IS the population-vs-N curve, so each qubit's
    row summarises its own transport there -- the source drains, the sink fills."""
    fit = _compensation(session, max_rounds=12)["fit"]
    assert fit["q0"]["p_initial"] > 0.9 and fit["q0"]["p_final"] < 0.3
    assert fit["q2"]["p_initial"] < 0.1
    assert fit["q2"]["p_max"] == pytest.approx(fit["q2"]["best_sink_p_max"])
    # every row carries the run-wide optimum, so one target reads on its own
    assert fit["q1"]["best_compensation_amp"] == fit["q2"]["best_compensation_amp"]


def test_trotter_compensation_is_record_only(session):
    assert len(_compensation(session, max_rounds=6).get("suggestions", [])) == 0


COMPENSATION_REFUSALS = [
    # the relay's tone fires AFTER the reset, onto an emptied qubit
    ({"compensation_target": "q1"}, "must be the chain source"),
    # one amplitude, one source of truth
    ({"compensation_target": "q0", "compensation_amps": {"q0": 0.4}},
     "two sources of truth"),
    # a pair the chain does not declare
    ({"swap_coupler_flux": {"q0_q2": 0.05}}, "neither first_pair"),
]


@pytest.mark.parametrize("override,message", COMPENSATION_REFUSALS)
def test_trotter_compensation_refuses_by_name(session, override, message):
    """Every gate runs in define_sweep -- the earliest hook that sees both the
    roster and the params -- so a mis-specified scan costs no instrument time."""
    out = session.run("qc_trotter_compensation",
                      {"targets": list(CHAIN_QUBITS), **override}, update="none")
    assert message in (out.get("error") or ""), out.get("error")


def test_swap_coupler_flux_is_accepted_by_the_chain_and_the_scan(session):
    """The angle knob threads through BOTH chain experiments unchanged, which is
    what makes a (theta_1, theta_2) campaign a parameter sweep rather than a
    re-registration."""
    flux = {"q0_q1": 0.04, "q1_q2": 0.05}
    chain = session.run("qc_unidirectional_trotter",
                        {"targets": list(CHAIN_QUBITS), "max_rounds": 6,
                         "swap_coupler_flux": flux}, update="none")
    assert chain.get("error") is None, chain.get("error")
    scan = _compensation(session, max_rounds=6, swap_coupler_flux=flux)
    assert scan["fit"]["q2"]["n_compensation_amp"] > 0


# ---------------------------------------------------------------------------
# pair_swap_angle - the inter-swap phase, and the tone that nulls it
# ---------------------------------------------------------------------------

def test_pair_swap_angle_refuses_a_compensation_outside_the_pair(session):
    """Only the DIFFERENCE between the two members is observable, so a tone on a
    spectator changes nothing measurable here -- refused rather than played."""
    out = session.run("pair_swap_angle",
                      {"targets": ["q0_q1"], "compensation_amps": {"q2": 0.3}},
                      update="none")
    assert "not a member" in (out.get("error") or ""), out.get("error")


def test_pair_swap_angle_gap_moves_the_fitted_angle(session):
    """THE contamination this experiment has to be honest about: the members
    accumulate a relative phase through the gap, and an exchange followed by a Z
    rotation does not commute -- so the SAME swap reports a different angle at a
    different gap. Pinned because a phase-free simulator would let the offline
    suite pass while hardware silently mismeasured (5Q4C 2026-09-01)."""
    tight = _angle(session, num_coupler_points=7, operation_gap_ns=0)["fit"]["q0_q1"]
    loose = _angle(session, num_coupler_points=7, operation_gap_ns=20)["fit"]["q0_q1"]
    assert tight["theta_min_rad"] != pytest.approx(loose["theta_min_rad"], abs=0.02)


def test_pair_swap_angle_compensation_can_only_lower_the_angle(session):
    """cos(theta_eff) = cos(phi/2) cos(theta), so theta_eff >= theta ALWAYS: a
    compensation scan can push the reported angle DOWN toward the exchange angle
    and never below it. That one-sidedness is what makes the MINIMUM over the
    scan the right estimator of theta, and it is why a single uncompensated run
    is an upper bound rather than a measurement."""
    # PIN the coupler: a degenerate window makes every row the same physical
    # point, so theta_min_rad is one well-defined angle rather than a minimum
    # over a coupler axis that would already pick the most favourable phase.
    pinned = dict(min_coupler_flux_v=0.05, max_coupler_flux_v=0.05,
                  num_coupler_points=5, operation_gap_ns=0)
    amps = (0.0, 0.1, 0.2, 0.25, 0.3, 0.4, 0.6)
    scan = {}
    for member in ("q0", "q1"):
        for amp in amps:
            fit = _angle(session, compensation_amps={member: amp},
                         **pinned)["fit"]["q0_q1"]
            scan[(member, amp)] = fit["theta_min_rad"]

    bare = scan[("q0", 0.0)]
    assert scan[("q1", 0.0)] == pytest.approx(bare)     # no tone either way
    best_key = min(scan, key=scan.get)
    best = scan[best_key]
    # some compensation beats no compensation, and by a real margin
    assert best < bare - 0.05, scan
    # the optimum is INTERIOR to the scan, which is what distinguishes a genuine
    # phase null from a monotone drift: over-compensating climbs back up
    member, amp = best_key
    assert 0.0 < amp < amps[-1], scan
    assert scan[(member, amps[-1])] > best, scan


def test_pair_swap_angle_compensation_reaches_the_probe(session):
    """A compensated run must still produce a full calibration, not merely not
    crash: every coupler value keeps a converged fit."""
    fit = _angle(session, num_coupler_points=7, operation_gap_ns=20,
                 compensation_amps={"q0": 0.3})["fit"]["q0_q1"]
    assert fit["n_theta_ok"] == fit["n_coupler_flux_v"]
