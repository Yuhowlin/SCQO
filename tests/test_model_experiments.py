"""The full ported experiment catalog, end-to-end on the simulated backend:
every registered experiment runs without error on the tunable demo device,
and the re-homed writes of the load-bearing families land on the right
entities."""

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
RECORD_ONLY = {"qubit_sqrb", "qubit_tomography", "qubit_echo_flux",
               "qubit_relaxation_flux"}


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("gf5d")
    roster = demo_components(tunable=True)
    design = demo_design(roster)
    vendor = InMemoryDevice(roster, demo_vendor_state(roster, design))
    return Session(SimulatedBackend(vendor), roster, design=design,
                   scqo_dir=tmp / "scqo", data_root=tmp / "data",
                   device_name="chipT", setup_name="sim",
                   cooldown_id="cd1")


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


def test_qubit_spectroscopy_writes_channel_knob_and_mode_fact(session):
    assert _suggest(session, "qubit_spectroscopy") == {
        ("q0_xy", "drive_freq_hz"), ("q0", "f_01_hz")}


def test_ramsey_writes_drive_freq_fact_twin_and_t2(session):
    assert _suggest(session, "qubit_ramsey") == {
        ("q0_xy", "drive_freq_hz"), ("q0", "f_01_hz"), ("q0", "t2_star_s")}


def test_flux_map_writes_the_sweet_spot_on_the_flux_channel(session):
    assert _suggest(session, "resonator_spectroscopy_flux") == {
        ("q0_z", "idle_flux"), ("q0_z", "flux_offset"),
        ("q0_z", "flux_per_phi0"), ("q0_ro", "readout_freq_hz")}


def test_pair_zz_writes_coupler_idle_and_pair_fact(session):
    assert _suggest(session, "pair_zz_coupler", target="q0_q1") == {
        ("q0_q1_c_z", "idle_flux"), ("q0_q1", "zz_hz")}


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


def test_arch_fit_writes_mode_facts_and_transfer_function(session):
    proposed = _suggest(session, "qubit_spectroscopy_flux_pulse")
    assert ("q0", "ej_sum_hz") in proposed
    assert ("q0", "f_q_max_hz") in proposed
    assert ("q0_z", "flux_offset") in proposed


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
