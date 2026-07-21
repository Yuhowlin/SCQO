"""End-to-end test of the abstraction with no instrument installed.

A throwaway concrete experiment (probe = no-op) is enough because the SimulatedBackend
drives ``simulate`` instead of ``probe``.
"""

from __future__ import annotations

import numpy as np

from scqo import Component, Outcome, Roster, Session, register
from scqo.experiments import (
    PairZZCoupler,
    QubitPowerRabi,
    QubitRamsey,
    QubitSpectroscopy,
    QubitSpectroscopyFlux,
    ReadoutFrequency,
    ReadoutPower,
    ResonatorSpectroscopy,
    ResonatorSpectroscopyFlux,
    ResonatorSpectroscopyPowerAmp,
    ResonatorSpectroscopyPowerChain,
    SingleShotReadout,
    QubitRelaxation,
    QubitEcho,
)
from scqo.testing import InMemoryDevice, SimulatedBackend, demo_roster


@register
class DemoResonatorSpectroscopy(ResonatorSpectroscopy):
    """Concrete experiment for tests/demos; no real instrument program."""

    def probe(self):  # never called by SimulatedBackend
        return None


@register
class DemoQubitRamsey(QubitRamsey):
    """Concrete Ramsey for tests/demos; no real instrument program."""

    def probe(self):  # never called by SimulatedBackend
        return None


@register
class DemoQubitPowerRabi(QubitPowerRabi):
    """Concrete power Rabi for tests/demos; no real instrument program."""

    def probe(self):  # never called by SimulatedBackend
        return None


@register
class DemoQubitSpectroscopy(QubitSpectroscopy):
    """Concrete qubit spectroscopy for tests/demos; no real instrument program."""

    def probe(self):  # never called by SimulatedBackend
        return None


@register
class DemoQubitRelaxation(QubitRelaxation):
    """Concrete T1 for tests/demos; no real instrument program."""

    def probe(self):  # never called by SimulatedBackend
        return None


@register
class DemoResonatorSpectroscopyPowerAmp(ResonatorSpectroscopyPowerAmp):
    """Concrete punchout for tests/demos; no real instrument program."""

    def probe(self):  # never called by SimulatedBackend
        return None


@register
class DemoQubitEcho(QubitEcho):
    """Concrete Hahn echo for tests/demos; no real instrument program."""

    def probe(self):  # never called by SimulatedBackend
        return None


@register
class DemoQubitSpectroscopyFlux(QubitSpectroscopyFlux):
    """Concrete flux map for tests/demos; no real instrument program."""

    def probe(self):  # never called by SimulatedBackend
        return None


@register
class DemoSingleShotReadout(SingleShotReadout):
    """Concrete IQ blobs for tests/demos; no real instrument program."""

    def probe(self):  # never called by SimulatedBackend
        return None


@register
class DemoResonatorSpectroscopyFlux(ResonatorSpectroscopyFlux):
    """Concrete resonator flux map for tests/demos; no real instrument program."""

    def probe(self):  # never called by SimulatedBackend
        return None


@register
class DemoReadoutPower(ReadoutPower):
    """Concrete fidelity-vs-amplitude scan for tests/demos; no real instrument program."""

    def probe(self):  # never called by SimulatedBackend
        return None


@register
class DemoReadoutFrequency(ReadoutFrequency):
    """Concrete fidelity-vs-frequency scan for tests/demos; no real instrument program."""

    def probe(self):  # never called by SimulatedBackend
        return None


@register
class DemoResonatorSpectroscopyPowerChain(ResonatorSpectroscopyPowerChain):
    """Concrete absolute punchout for tests/demos; no real instrument program."""

    def probe(self):  # never called by SimulatedBackend
        return None


def _device() -> InMemoryDevice:
    return InMemoryDevice(
        {
            "q0": {"readout_freq": 5.95e9, "drive_freq": 3.87e9, "pi_amp": 0.2, "readout_amp": 0.25,
                   "readout_power_dbm": -25.0},
            "q1": {"readout_freq": 6.05e9, "drive_freq": 4.01e9, "pi_amp": 0.18, "readout_amp": 0.22,
                   "readout_power_dbm": -27.0},
            "q0_q1": {"coupler_decouple_v": 0.08, "coupler_interaction_v": -0.1},
        },
        {"q0_q1": "TransmonPair"},
    )


def _flux_roster(qubits: tuple[str, ...] = ("q0", "q1")) -> Roster:
    """A flux-capable roster: FluxTunableTransmons with the flux_bias operation and
    a ZControl term per qubit — what the flux experiments' pre-probe gate demands
    (demo_roster's FixedTransmons would be refused before any hardware)."""
    components: dict[str, Component] = {}
    for q in qubits:
        components[q] = Component(
            name=q, physical="FluxTunableTransmon", instrument="ReadableTransmon",
            operations=("rx", "readout", "flux_bias"),
        )
        components[f"{q}_res"] = Component(name=f"{q}_res", physical="Resonator")
        components[f"{q}_ro"] = Component(
            name=f"{q}_ro", physical="ReadoutLine",
            members={"transmon": q, "resonator": f"{q}_res"},
        )
        components[f"{q}_z"] = Component(
            name=f"{q}_z", physical="ZControl", members={"transmon": q},
        )
    return Roster(components)


def test_experiment_runs_and_fits_dip():
    backend = SimulatedBackend(_device())
    exp = DemoResonatorSpectroscopy(
        backend, DemoResonatorSpectroscopy.Parameters(targets=["q0", "q1"], frequency_span_hz=15e6, num_points=201)
    )
    result = exp.run()
    assert result.success
    # recovered dip lies within the swept window for each qubit
    for qubit in ["q0", "q1"]:
        assert abs(result.fit[qubit]["dip_detuning_hz"]) < 15e6 / 2


def test_session_catalog_and_run_are_json():
    sess = Session(SimulatedBackend(_device()), demo_roster())

    catalog = sess.catalog()
    names = {entry["name"] for entry in catalog}
    assert "resonator_spectroscopy" in names
    # schema is real JSON-schema with the declared knobs
    schema = next(e for e in catalog if e["name"] == "resonator_spectroscopy")["parameters_schema"]
    assert "frequency_span_hz" in schema["properties"]

    before = sess.device_state()["q0"]["readout_freq"]
    result = sess.run("resonator_spectroscopy", {"targets": ["q0"], "frequency_span_hz": 15e6},
                      update="apply")
    assert result["outcomes"]["q0"] == Outcome.SUCCESSFUL.value
    # update="apply" wrote the fitted frequency back into the device
    after = sess.device_state()["q0"]["readout_freq"]
    assert after != before
    assert np.isclose(after, result["fit"]["q0"]["readout_freq"])
    # ... and the applied updates carry their audit trail (knob + sample physics)
    assert [(s["field"], s["status"]) for s in result["suggestions"]] == [
        ("readout_freq", "accepted"), ("f_r_hz", "accepted"), ("kappa_tot_hz", "accepted")]


def test_default_run_suggests_but_applies_nothing():
    """The v0.6 default: fitted values become PENDING suggestions — no store changes,
    no history, no vendor push — until a human (or accept) applies them."""
    sess = Session(SimulatedBackend(_device()), demo_roster())

    state_before = sess.device_state()
    result = sess.run("resonator_spectroscopy", {"targets": ["q0"], "frequency_span_hz": 15e6})
    assert result["outcomes"]["q0"] == Outcome.SUCCESSFUL.value
    assert sess.device_state() == state_before  # nothing applied
    assert sess.history() == []
    suggestions = result["suggestions"]
    # cross-component proposals: the sample physics lands on the qubit's Resonator
    assert [(s["component"], s["field"], s["store"]) for s in suggestions] == [
        ("q0", "readout_freq", "instrument"),
        ("q0_res", "f_r_hz", "physical"),
        ("q0_res", "kappa_tot_hz", "physical")]
    assert {s["status"] for s in suggestions} == {"pending"}
    s = suggestions[0]
    assert s["component"] == "q0"
    assert s["before"] == state_before["q0"]["readout_freq"]
    assert np.isclose(s["after"], result["fit"]["q0"]["readout_freq"])


def test_ramsey_generalizes_pattern():
    """Same lifecycle, different sweep/fit/field: time sweep -> T2* + drive_freq update."""
    sess = Session(SimulatedBackend(_device()), demo_roster())

    # both experiments share one catalog/registry
    assert {"resonator_spectroscopy", "qubit_ramsey"} <= {e["name"] for e in sess.catalog()}

    before = sess.device_state()["q1"]["drive_freq"]
    result = sess.run(
        "qubit_ramsey",
        {"targets": ["q1"], "frequency_detuning_hz": 1.0e6, "max_idle_time_ns": 4000, "num_points": 201},
        update="apply",
    )
    assert result["outcomes"]["q1"] == Outcome.SUCCESSFUL.value
    # recovered residual detuning is small (|err| <= 0.2 * applied) and T2* is physical
    assert abs(result["fit"]["q1"]["detuning_error_hz"]) < 0.3e6
    assert 1e-6 < result["fit"]["q1"]["t2_star_s"] < 50e-6
    # update="apply" wrote the corrected drive_freq back (a different field than resonator spec)
    after = sess.device_state()["q1"]["drive_freq"]
    assert after != before
    assert np.isclose(after, result["fit"]["q1"]["drive_freq"])
    # T2* (and the measured f_01 fact) are sample physics: recorded in the
    # PHYSICAL store, not the instrument config
    assert sess.physical_state()["q1"]["t2_star_s"] == result["fit"]["q1"]["t2_star_s"]
    assert sess.physical_state()["q1"]["f_01_hz"] == result["fit"]["q1"]["f_01_hz"]
    assert "t2_star_s" not in sess.device_state()["q1"]


def test_qubit_spectroscopy_finds_peak_and_updates_drive_freq():
    """Two-tone: peak search within the swept window -> coarse drive_freq update."""
    sess = Session(SimulatedBackend(_device()), demo_roster())

    before = sess.device_state()["q0"]["drive_freq"]
    result = sess.run("qubit_spectroscopy", {"targets": ["q0"], "frequency_span_hz": 60e6},
                      update="apply")
    assert result["outcomes"]["q0"] == Outcome.SUCCESSFUL.value
    fit = result["fit"]["q0"]
    assert abs(fit["peak_detuning_hz"]) <= 60e6 / 2  # inside the swept window
    assert fit["fwhm_hz"] > 0 and fit["n_peaks"] >= 1
    after = sess.device_state()["q0"]["drive_freq"]
    assert np.isclose(after, before + fit["peak_detuning_hz"])


def test_qubit_relaxation_records_t1():
    """T1: exponential decay fit -> t1_s recorded into the PHYSICAL store + its
    history (sample physics: the instrument config stays completely untouched)."""
    sess = Session(SimulatedBackend(_device()), demo_roster())

    state_before = sess.device_state()
    result = sess.run("qubit_relaxation", {"targets": ["q0"]}, update="apply")
    assert result["outcomes"]["q0"] == Outcome.SUCCESSFUL.value
    assert 20e-6 * 0.8 < result["fit"]["q0"]["t1_s"] < 60e-6 * 1.2  # sim truth 20-60 us
    assert sess.physical_state()["q0"]["t1_s"] == result["fit"]["q0"]["t1_s"]
    assert sess.device_state() == state_before  # instrument config untouched
    assert sess.history() == []  # ... and no instrument history either
    assert [(h["field"], h["component"]) for h in sess.history(store="physical")] == [("t1_s", "q0")]


def test_amp_punchout_suggests_and_reverts():
    """The fast punchout shares _chain's absolute-window semantics: set-top +
    auto-revert are recorded, the knee is PROPOSED on the absolute dBm axis, and
    the device is unchanged until the suggestion is accepted."""
    sess = Session(SimulatedBackend(_device()), demo_roster())

    before = sess.device_state()["q0"]
    result = sess.run("resonator_spectroscopy_power_amp", {"targets": ["q0"]})
    # default window -50..-20 dBm, default update="suggest"
    assert result["outcomes"]["q0"] == Outcome.SUCCESSFUL.value
    fit = result["fit"]["q0"]
    # the optimum sits below the simulated knee (8-12 dB under the -20 dBm top)
    assert -50.0 < fit["optimal_power_dbm"] <= -26.0
    assert fit["readout_power_dbm"] == fit["optimal_power_dbm"]
    assert fit["old_readout_power_dbm"] == before["readout_power_dbm"]

    # suggest mode: the proposal is pending, the device is UNCHANGED (revert proven)
    assert {(s["field"], s["status"]) for s in result["suggestions"]} == {
        ("readout_power_dbm", "pending"), ("readout_freq", "pending")
    }
    assert sess.device_state()["q0"] == before

    # ...but the set-top/revert pair is honestly recorded, tagged with the experiment
    power_moves = [h for h in sess.history() if h["field"] == "readout_power_dbm"]
    assert [h["new"] for h in power_moves] == [-20.0, before["readout_power_dbm"]]
    assert all(h["experiment"] == "resonator_spectroscopy_power_amp" for h in power_moves)


def test_amp_punchout_apply_writes_absolute_power_and_freq():
    sess = Session(SimulatedBackend(_device()), demo_roster())
    result = sess.run("resonator_spectroscopy_power_amp", {"targets": ["q0"]}, update="apply")
    assert result["outcomes"]["q0"] == Outcome.SUCCESSFUL.value
    after = sess.device_state()["q0"]
    assert np.isclose(after["readout_power_dbm"], result["fit"]["q0"]["readout_power_dbm"])
    assert np.isclose(after["readout_freq"], result["fit"]["q0"]["readout_freq"])
    fields = {h["field"] for h in sess.history()}
    assert {"readout_power_dbm", "readout_freq"} <= fields


def test_amp_punchout_multi_qubit_shares_the_absolute_window():
    """q0 (-25 dBm) and q1 (-27 dBm) sweep the SAME absolute window (each chain is
    solved for the -20 dBm top); each proposes its own absolute optimum and reverts
    to its own standing power."""
    sess = Session(SimulatedBackend(_device()), demo_roster())
    before = sess.device_state()
    result = sess.run("resonator_spectroscopy_power_amp", {"targets": ["q0", "q1"]})
    for q in ("q0", "q1"):
        assert result["outcomes"][q] == Outcome.SUCCESSFUL.value
        fit = result["fit"][q]
        assert -50.0 < fit["readout_power_dbm"] <= -26.0
        assert fit["old_readout_power_dbm"] == before[q]["readout_power_dbm"]
    assert sess.device_state() == before  # both reverted (suggest mode)


def test_amp_punchout_refuses_unconfigured_chain():
    """readout_power_dbm unknown -> structured failure (the revert target would be
    undefined), identical semantics to _chain; the old relative fallback is gone."""
    device = InMemoryDevice(
        {"q0": {"readout_freq": 5.95e9, "drive_freq": 3.87e9, "pi_amp": 0.2,
                "readout_amp": 0.25, "readout_power_dbm": None}}
    )
    sess = Session(SimulatedBackend(device), demo_roster(qubits=("q0",)))
    result = sess.run("resonator_spectroscopy_power_amp", {"targets": ["q0"]})
    assert result["outcomes"]["q0"] == Outcome.FAILED.value
    assert "readout_power_dbm is unknown" in (result["error"] or "")
    assert result["suggestions"] == []


def test_qubit_echo_records_t2_echo():
    """Echo: exponential envelope fit -> t2_echo_s recorded into the PHYSICAL store
    + its history (sample physics: instrument config untouched)."""
    sess = Session(SimulatedBackend(_device()), demo_roster())

    state_before = sess.device_state()
    result = sess.run("qubit_echo", {"targets": ["q0"]}, update="apply")
    assert result["outcomes"]["q0"] == Outcome.SUCCESSFUL.value
    assert 30e-6 * 0.8 < result["fit"]["q0"]["t2_echo_s"] < 80e-6 * 1.2  # sim truth 30-80 us
    assert sess.physical_state()["q0"]["t2_echo_s"] == result["fit"]["q0"]["t2_echo_s"]
    assert sess.device_state() == state_before
    assert [h["field"] for h in sess.history(store="physical")] == ["t2_echo_s"]


def test_qubit_flux_map_recovers_arch():
    """2D flux map: point-cloud + transmon arch fit -> sweet spot inside the swept
    window, arch top at the current drive_freq; the arch parameters land in the
    PHYSICAL store — the arch facts (ej_sum_hz, f_q_max_hz) on the transmon, the
    volts-to-flux transfer (v_offset_v, v_per_phi0_v) on its ZControl component —
    no instrument knob."""
    sess = Session(SimulatedBackend(_device()), _flux_roster())

    state_before = sess.device_state()
    result = sess.run("qubit_spectroscopy_flux", {"targets": ["q0"]}, update="apply")
    assert result["outcomes"]["q0"] == Outcome.SUCCESSFUL.value
    fit = result["fit"]["q0"]
    assert -0.3 <= fit["v_offset_v"] <= 0.3  # sim hides it inside the window
    # simulate() pins the arch top to the current drive_freq
    assert fit["f01_at_sweet_spot_hz"] == np.float64(fit["f01_at_sweet_spot_hz"])
    assert abs(fit["f01_at_sweet_spot_hz"] - fit["old_drive_freq"]) < 40e6
    assert fit["ej_sum_hz"] > 0
    assert sess.device_state() == state_before  # instrument config untouched
    assert sess.history() == []
    physical = sess.physical_state()
    assert physical["q0"]["ej_sum_hz"] == fit["ej_sum_hz"]
    assert physical["q0"]["f_q_max_hz"] == fit["f01_at_sweet_spot_hz"]
    assert physical["q0_z"]["v_offset_v"] == fit["v_offset_v"]
    assert physical["q0_z"]["v_per_phi0_v"] == fit["v_per_phi0_v"]


def test_single_shot_readout_fidelity():
    """First per-shot experiment: GMM on the IQ blobs -> fidelity consistent with the
    simulated flip probabilities; the fidelity is RECORDED (record-only), while the
    confusion probabilities stay run-record-only (instrument-dependent by decision)."""
    sess = Session(SimulatedBackend(_device()), demo_roster())

    result = sess.run("single_shot_readout", {"targets": ["q0"], "num_shots": 1500},
                      update="apply")
    assert result["outcomes"]["q0"] == Outcome.SUCCESSFUL.value
    fit = result["fit"]["q0"]
    # sim: 3.5-5 sigma separation, 1-5% thermal flips, 3-8% decay flips
    assert 0.85 < fit["readout_fidelity"] <= 1.0
    assert 0.0 <= fit["p_e_given_g"] < 0.12
    assert 0.0 <= fit["p_g_given_e"] < 0.15
    assert sess.device_state()["q0"]["readout_fidelity"] == fit["readout_fidelity"]
    assert "p_e_given_g" not in sess.device_state()["q0"]  # stays run-record-only
    assert [h["field"] for h in sess.history()] == ["readout_fidelity"]


def test_resonator_flux_map_recovers_dispersive_model():
    """Resonator-vs-flux: dip trace + dispersive fit -> sweet spot inside the swept
    window, coupling g near the simulated range; the dispersive quantities land in
    the PHYSICAL store on their owning components (ZControl / Resonator /
    ReadoutLine) — no instrument knob. f_r0/g are proposed only when the fit was
    constrained by a supplied f_q_max_hz, and the assumed/input f_q_max is never
    recorded as measured physics."""
    sess = Session(SimulatedBackend(_device()), _flux_roster())

    # Unconstrained fit (default f_q_max_hz=None): only the robust flux-periodicity
    # quantities are proposed — g would be conditional on an ASSUMED f_q_max.
    result = sess.run("resonator_spectroscopy_flux", {"targets": ["q0"]}, update="apply")
    assert result["outcomes"]["q0"] == Outcome.SUCCESSFUL.value
    assert set(sess.physical_state()) == {"q0_z"}
    assert set(sess.physical_state()["q0_z"]) == {"v_offset_v", "v_per_phi0_v"}

    # Constrained fit: the caller supplies the qubit sweet-spot frequency
    state_before = sess.device_state()
    result = sess.run("resonator_spectroscopy_flux",
                      {"targets": ["q0"], "f_q_max_hz": 3.87e9}, update="apply")
    assert result["outcomes"]["q0"] == Outcome.SUCCESSFUL.value
    fit = result["fit"]["q0"]
    assert -0.3 <= fit["v_offset_v"] <= 0.3
    assert 40e6 < fit["g_hz"] < 200e6  # sim truth 70-100 MHz, loose fit tolerance
    assert abs(fit["f_r0_hz"] - fit["old_readout_freq"]) < 20e6  # bare near dressed
    assert sess.device_state() == state_before  # instrument config untouched
    assert sess.history() == []
    physical = sess.physical_state()
    assert physical["q0_z"]["v_offset_v"] == fit["v_offset_v"]
    assert physical["q0_z"]["v_per_phi0_v"] == fit["v_per_phi0_v"]
    assert physical["q0_res"]["f_r0_hz"] == fit["f_r0_hz"]
    assert physical["q0_ro"]["g_hz"] == fit["g_hz"]
    assert "q0" not in physical  # f_q_max is an input/assumption, never recorded


def test_readout_power_picks_fidelity_optimum_and_updates_amp():
    """Per-shot fidelity vs amplitude: best point below the simulated flip knee,
    readout_amp written back."""
    sess = Session(SimulatedBackend(_device()), demo_roster())

    before = sess.device_state()["q0"]
    result = sess.run(
        "readout_power", {"targets": ["q0"], "num_amp_points": 8, "num_shots": 400},
        update="apply",
    )
    assert result["outcomes"]["q0"] == Outcome.SUCCESSFUL.value
    fit = result["fit"]["q0"]
    assert 0.4 <= fit["best_amp_factor"] <= 1.8
    assert fit["best_fidelity"] > 0.8
    after = sess.device_state()["q0"]
    assert np.isclose(after["readout_amp"], before["readout_amp"] * fit["best_amp_factor"])
    assert {"readout_amp"} <= {h["field"] for h in sess.history()}


def test_readout_frequency_picks_fidelity_optimum_and_updates_freq():
    """Per-shot fidelity vs frequency: best detuning near the simulated contrast
    peak, readout_freq written back."""
    sess = Session(SimulatedBackend(_device()), demo_roster())

    before = sess.device_state()["q0"]
    result = sess.run(
        "readout_frequency", {"targets": ["q0"], "num_freq_points": 9, "num_shots": 400},
        update="apply",
    )
    assert result["outcomes"]["q0"] == Outcome.SUCCESSFUL.value
    fit = result["fit"]["q0"]
    # sim hides the peak within +-span/6 = +-0.83 MHz; grid step is 0.625 MHz
    assert abs(fit["frequency_shift_hz"]) <= 1.5e6
    after = sess.device_state()["q0"]
    assert np.isclose(after["readout_freq"], before["readout_freq"] + fit["frequency_shift_hz"])
    assert {"readout_freq"} <= {h["field"] for h in sess.history()}


def test_power_rabi_generalizes_pattern():
    """Amplitude sweep -> cosine fit -> updates a third device field: pi_amp."""
    sess = Session(SimulatedBackend(_device()), demo_roster())

    assert "qubit_power_rabi" in {e["name"] for e in sess.catalog()}

    before = sess.device_state()["q0"]["pi_amp"]
    result = sess.run("qubit_power_rabi", {"targets": ["q0"], "max_amp_factor": 2.0, "num_points": 201},
                      update="apply")
    assert result["outcomes"]["q0"] == Outcome.SUCCESSFUL.value
    # recovered pi factor is near 1 (simulated miscalibration was within +-15%)
    assert 0.8 < result["fit"]["q0"]["pi_amp_factor"] < 1.2
    after = sess.device_state()["q0"]["pi_amp"]
    # the writeback ran (recorded in history) and applied the fitted value. We assert via
    # history rather than `after != before` because a reproducibly near-perfect simulated
    # calibration can leave the value numerically unchanged while still being written.
    assert any(h["component"] == "q0" and h["field"] == "pi_amp" for h in sess.history())
    assert np.isclose(after, result["fit"]["q0"]["pi_amp"])


def test_chain_punchout_suggests_and_reverts():
    """The v0.8 absolute punchout: sweep-top set + auto-revert are recorded, the knee
    is PROPOSED (suggest mode) on the absolute dBm axis, and the device is unchanged
    until the suggestion is accepted."""
    sess = Session(SimulatedBackend(_device()), demo_roster())

    before = sess.device_state()["q0"]
    result = sess.run(
        "resonator_spectroscopy_power_chain",
        {"targets": ["q0"], "max_power_dbm": -15.0, "min_power_dbm": -45.0},
    )  # default update="suggest"
    assert result["outcomes"]["q0"] == Outcome.SUCCESSFUL.value
    fit = result["fit"]["q0"]
    # the optimum sits below the simulated knee (8-12 dB under the -15 dBm top)
    assert -45.0 < fit["optimal_power_dbm"] <= -21.0
    assert fit["readout_power_dbm"] == fit["optimal_power_dbm"]
    assert fit["old_readout_power_dbm"] == before["readout_power_dbm"]

    # suggest mode: the proposal is pending, the device is UNCHANGED (revert proven)
    assert {(s["field"], s["status"]) for s in result["suggestions"]} == {
        ("readout_power_dbm", "pending"), ("readout_freq", "pending")
    }
    assert sess.device_state()["q0"] == before

    # ...but the set-top/revert pair is honestly recorded, tagged with the experiment
    # (no datastore configured here, so run_id stays None — the context tag remains)
    power_moves = [h for h in sess.history() if h["field"] == "readout_power_dbm"]
    assert [h["new"] for h in power_moves] == [-15.0, before["readout_power_dbm"]]
    assert all(h["experiment"] == "resonator_spectroscopy_power_chain" for h in power_moves)


def test_chain_punchout_accept_applies_the_power():
    sess = Session(SimulatedBackend(_device()), demo_roster())
    result = sess.run(
        "resonator_spectroscopy_power_chain",
        {"targets": ["q0"], "max_power_dbm": -15.0, "min_power_dbm": -45.0},
        update="apply",
    )
    assert result["outcomes"]["q0"] == Outcome.SUCCESSFUL.value
    after = sess.device_state()["q0"]
    assert np.isclose(after["readout_power_dbm"], result["fit"]["q0"]["readout_power_dbm"])
    assert np.isclose(after["readout_freq"], result["fit"]["q0"]["readout_freq"])


def test_chain_punchout_refuses_unconfigured_chain():
    """A qubit whose readout_power_dbm is unknown refuses to run (the revert target
    would be undefined) — a structured failure, not a crash."""
    device = InMemoryDevice(
        {"q0": {"readout_freq": 5.95e9, "drive_freq": 3.87e9, "pi_amp": 0.2,
                "readout_amp": 0.25, "readout_power_dbm": None}}
    )
    sess = Session(SimulatedBackend(device), demo_roster(qubits=("q0",)))
    result = sess.run(
        "resonator_spectroscopy_power_chain",
        {"targets": ["q0"], "max_power_dbm": -15.0, "min_power_dbm": -45.0},
    )
    assert result["outcomes"]["q0"] == Outcome.FAILED.value
    assert "readout_power_dbm is unknown" in (result["error"] or "")
    assert result["suggestions"] == []


def test_flux_experiment_refused_on_fixed_frequency_roster():
    """The pre-probe roster gate: a flux experiment on the demo (fixed-frequency)
    roster is refused BEFORE any hardware, as a structured all-failed result."""
    sess = Session(SimulatedBackend(_device()), demo_roster())
    result = sess.run("qubit_spectroscopy_flux", {"targets": ["q0"]})
    assert result["outcomes"]["q0"] == Outcome.FAILED.value
    assert result["error"].startswith("target validation refused the run before any hardware")
    assert "lacks operation(s) ['flux_bias']" in result["error"]
    assert not result.get("suggestions")  # refused pre-capture: nothing proposed


def test_punchout_figures_carry_chain_provenance(tmp_path):
    """Chain-stepped provenance: when the backend reports an output chain, the
    punchout plotdata artifacts carry the PER-POINT digital_amp/chain_setting vars
    (+ chain_name) that draw the amp/chain subplot; the simulated (no-chain)
    backend produces artifacts WITHOUT them (figures unchanged)."""
    import numpy as np_
    import xarray as xr

    class _ChainBackend(SimulatedBackend):
        def power_context(self, qubits):
            # per-point capture: derive att/amp from each qubit's CURRENT power
            # (the raw per-point write set it), like a real backend would
            out = {}
            for q in qubits:
                p = self._device.component(q).readout_power_dbm
                att = int(min(60, max(0, 2 * ((5.0 - p - 6.02) // 2))))
                out[q] = {"output_att_db": att,
                          "pulse_amp": 10.0 ** ((p - 5.0 + att) / 20.0)}
            return out

    sess = Session(_ChainBackend(_device()), demo_roster(),
                   data_root=tmp_path / "data", device_name="devA")
    result = sess.run(
        "resonator_spectroscopy_power_chain",
        {"targets": ["q0"], "max_power_dbm": -15.0, "min_power_dbm": -45.0},
    )
    assert result["outcomes"]["q0"] == Outcome.SUCCESSFUL.value
    run_dir = tmp_path / "data" / sess.load_run(result["run_id"])["record"]["path"]
    plotdata = xr.load_dataset(
        run_dir / "analysis" / "q0" / "resonator_spectroscopy_power_plotdata.nc"
    )
    assert plotdata.attrs["chain_name"] == "output_att (dB)"
    assert plotdata.attrs["mode_label"] == "chain-stepped (slow)"
    assert plotdata.attrs["power_axis_kind"] == "absolute dBm"
    amp = plotdata["digital_amp"].values
    att = plotdata["chain_setting"].values
    assert amp.shape == plotdata["power"].shape and np_.all(np_.isfinite(amp))
    assert np_.all(amp <= 0.51)                      # per-point canonical policy
    assert att[0] >= att[-1]                          # ascending power -> att steps DOWN
    assert list(run_dir.glob("analysis/q0/*.png"))    # the figure rendered

    # the FAST punchout emits the SAME provenance form (one shared figure format):
    # amp sweeps down from the top, the chain setting stays flat
    result_amp = sess.run("resonator_spectroscopy_power_amp", {"targets": ["q0"]})
    run_dir_amp = tmp_path / "data" / sess.load_run(result_amp["run_id"])["record"]["path"]
    plotdata_amp = xr.load_dataset(
        run_dir_amp / "analysis" / "q0" / "resonator_spectroscopy_power_plotdata.nc"
    )
    assert plotdata_amp.attrs["chain_name"] == "output_att (dB)"
    assert plotdata_amp.attrs["mode_label"] == "amplitude sweep (fast)"
    assert plotdata_amp.attrs["power_axis_kind"] == "absolute dBm"
    amp2 = plotdata_amp["digital_amp"].values
    att2 = plotdata_amp["chain_setting"].values
    assert np_.all(np_.diff(amp2) > 0)                # amp grows with power (prefactor -> 1 at the top)
    assert np_.allclose(att2, att2[0])                # chain FIXED during the sweep
    assert list(run_dir_amp.glob("analysis/q0/*.png"))

    # plain simulated backend: no chain -> no provenance vars, but the labels are
    # still attached (the figure stays self-identifying)
    sess2 = Session(SimulatedBackend(_device()), demo_roster(),
                    data_root=tmp_path / "data2", device_name="devB")
    result2 = sess2.run(
        "resonator_spectroscopy_power_chain",
        {"targets": ["q0"], "max_power_dbm": -15.0, "min_power_dbm": -45.0},
    )
    run_dir2 = tmp_path / "data2" / sess2.load_run(result2["run_id"])["record"]["path"]
    plotdata2 = xr.load_dataset(
        run_dir2 / "analysis" / "q0" / "resonator_spectroscopy_power_plotdata.nc"
    )
    assert "digital_amp" not in plotdata2.data_vars
    assert "chain_name" not in plotdata2.attrs
    assert plotdata2.attrs["mode_label"] == "chain-stepped (slow)"
    assert plotdata2.attrs["power_axis_kind"] == "absolute dBm"


def test_bring_up_anchor_seeds_from_design(tmp_path):
    """A fresh chip (no standing readout_freq): the sweep anchors on the
    roster's DESIGN value and the run is tagged searchably (seeded:...)."""
    from scqo.testing import demo_roster

    device = InMemoryDevice({
        "q0": {"readout_freq": None, "drive_freq": 3.87e9, "pi_amp": 0.2,
               "readout_amp": 0.25, "readout_power_dbm": -25.0}})
    sess = Session(SimulatedBackend(device), demo_roster(("q0",)),
                   data_root=tmp_path, device_name="chipT")
    result = sess.run("resonator_spectroscopy",
                      {"targets": ["q0"], "frequency_span_hz": 15e6})
    assert result["outcomes"]["q0"] == "successful"
    record = sess.load_run(result["run_id"])["record"]
    assert "seeded:q0_res.f_r_hz" in record["tags"]  # findable: scqo find --tag
    # the fitted absolute frequency sits within the design-anchored window
    assert abs(result["fit"]["q0"]["readout_freq"] - 5.95e9) < 15e6


@register
class DemoPairZZCoupler(PairZZCoupler):
    """Concrete pair ZZ map for tests/demos; no real instrument program."""

    def probe(self):  # never called by SimulatedBackend
        return None


def test_pair_zz_coupler_end_to_end():
    """The Phase-2 pair pipeline: a TransmonPair target runs on the simulated
    backend, the fit is keyed by the PAIR name, and the two proposals route to
    their declaring sides (coupler_decouple_v -> instrument/TransmonPair,
    zz_hz -> physical/Coupling) through ONE component view."""
    sess = Session(SimulatedBackend(_device()), demo_roster())
    result = sess.run("pair_zz_coupler", {"targets": ["q0_q1"]}, update="apply")
    assert result["outcomes"]["q0_q1"] == Outcome.SUCCESSFUL.value
    assert list(result["fit"]) == ["q0_q1"]  # fit keyed by the RUN TARGET name
    routed = {(s["field"], s["store"], s["category"], s["status"])
              for s in result["suggestions"]}
    assert ("coupler_decouple_v", "instrument", "TransmonPair", "accepted") in routed
    assert ("zz_hz", "physical", "Coupling", "accepted") in routed
    assert np.isclose(sess.device_state()["q0_q1"]["coupler_decouple_v"],
                      result["fit"]["q0_q1"]["coupler_zero_v"])
    assert "zz_hz" in sess.physical_state().get("q0_q1", {})
    # wrong-category target: machine-refused before any hardware
    refused = sess.run("pair_zz_coupler", {"targets": ["q0"]})
    assert "targets TransmonPair" in refused["error"]


def test_flux_component_runs_record_only():
    """An assigned flux source (here the pair's coupler) lets a FLUX-LESS target
    run the flux map — and the run is record-only: fits land, ZERO suggestions."""
    sess = Session(SimulatedBackend(_device()), demo_roster())
    result = sess.run(
        "resonator_spectroscopy_flux",
        {"targets": ["q0"], "flux_component": "q0_q1",
         "num_flux_points": 6, "num_freq_points": 41},
    )
    assert result["outcomes"]["q0"] == Outcome.SUCCESSFUL.value
    assert result["suggestions"] == []  # foreign-flux map: crosstalk data only
    # an unknown source is machine-refused pre-probe
    refused = sess.run("resonator_spectroscopy_flux",
                       {"targets": ["q0"], "flux_component": "nope"})
    assert "not in this device's roster" in refused["error"]
    # without a source the flux-less target still refuses (own-z rule intact)
    refused = sess.run("resonator_spectroscopy_flux", {"targets": ["q0"]})
    assert "lacks operation(s) ['flux_bias']" in refused["error"]
