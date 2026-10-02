"""A tunable coupler as a ``qubit_*`` target: the ``drive_line`` and
``mapped_readout`` capabilities on ``qubit_power_rabi``, end to end on the
simulated backend (docs/coupler-transmon-plan.md).

The tunable demo roster has the pair q0_q1 with its coupler q0_q1_c (a
flux_transmon with a flux wire and nothing else), designed drive lines xy_q0 and
xy_q1, and so the coupler's two BORROWED drive channels xy_q0.q0_q1_c and
xy_q1.q0_q1_c. A vendor realizes a borrowed channel only once it adopts it;
the in-memory stand-in does that with ``InMemoryDevice.adopt``, which must run
BEFORE the Session exists (the recording device seeds from the vendor then).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scqo import Session
from scqo import experiments as registry
from scqo.testing import (
    InMemoryDevice,
    SimulatedBackend,
    demo_components,
    demo_design,
    demo_vendor_state,
)

#: what an adopted borrowed channel carries: the seeds an adoption writes
ROUTE = {"pi_amp": 0.25, "pi_amp_x90": 0.125, "drive_freq_hz": 7.0e9,
         "pi_duration_s": 2e-7, "drag_beta": 0.0, "drag_beta_x90": 0.0}

#: the coupler power Rabi: through q1's line, read through q0
COUPLER_RABI = {"targets": ["q0_q1_c"], "drive_line": "xy_q1", "readout_member": "q0",
                "use_state_discrimination": True}


def _session(tmp_path, *, adopt=("xy_q1.q0_q1_c",), qubits=("q0", "q1"),
             chain=False):
    roster = demo_components(qubits, tunable=True, chain=chain)
    design = demo_design(roster, qubits)
    vendor = InMemoryDevice(roster, demo_vendor_state(roster, design))
    for name in adopt:
        vendor.adopt(name, ROUTE)
    session = Session(SimulatedBackend(vendor), roster, design=design,
                      scqo_dir=tmp_path / "scqo", data_root=tmp_path / "data",
                      device_name="chipT", setup_name="sim", cooldown_id="cd1")
    return session, vendor


def test_power_rabi_calibrates_the_borrowed_channel(tmp_path):
    """The coupler Rabi fits like a qubit's (the map only offsets and scales the
    population) and proposes pi_amp on the BORROWED channel that played it - and
    accepting it lands on the vendor's adopted element, nowhere else."""
    s, vendor = _session(tmp_path)
    out = s.run("qubit_power_rabi", COUPLER_RABI)
    assert out.get("error") is None, out.get("error")
    assert out["outcomes"]["q0_q1_c"] == "successful"
    fit = out["fit"]["q0_q1_c"]
    assert fit["old_pi_amp"] == pytest.approx(0.25)
    assert fit["pi_amp"] == pytest.approx(0.25 * fit["opt_amp_prefactor"])
    assert 0.85 <= fit["opt_amp_prefactor"] <= 1.15  # the sim's miscalibration
    assert {(x["entity"], x["field"]) for x in out["suggestions"]} == {
        ("xy_q1.q0_q1_c", "pi_amp")}

    # the absolute amplitude axis rides the borrowed channel's pi_amp
    ds = s.datastore.open_dataset(out["run_id"])
    absolute = ds["digital_amp"].sel(target="q0_q1_c").values
    assert absolute == pytest.approx(0.25 * ds["amp_prefactor"].values)
    # the member's population, not the coupler's: offset by the map's misses
    assert float(ds["population"].min()) > 0.05

    s.accept(out["run_id"])
    state = vendor.snapshot()
    assert state["xy_q1.q0_q1_c"]["pi_amp"] == pytest.approx(fit["pi_amp"])
    assert state["xy_q1.q1"]["pi_amp"] == pytest.approx(0.1)   # the line's own qubit
    assert state["xy_q0.q0"]["pi_amp"] == pytest.approx(0.1)   # the member


def test_a_drive_line_alone_drives_a_qubit_through_another_line(tmp_path):
    """drive_line without readout_member: a qubit driven through a neighbour's
    line and read by its own resonator (a microwave-crosstalk Rabi). The
    calibration lands on that borrowed channel, not on the qubit's own."""
    s, _ = _session(tmp_path, adopt=("xy_q1.q0",))
    out = s.run("qubit_power_rabi", {"targets": ["q0"], "drive_line": "xy_q1"})
    assert out.get("error") is None, out.get("error")
    assert {(x["entity"], x["field"]) for x in out["suggestions"]} == {
        ("xy_q1.q0", "pi_amp")}


def test_an_unadopted_borrowed_channel_is_refused_before_any_run(tmp_path):
    """The backend must REALIZE the channel: an unadopted one is refused by the
    roster gate with the vendor's own words, before a run folder exists."""
    s, _ = _session(tmp_path, adopt=())
    out = s.run("qubit_power_rabi", COUPLER_RABI)
    assert "before any hardware" in out["error"]
    assert "BORROWED" in out["error"] and "adopt" in out["error"]
    assert out.get("run_id") is None


def test_a_coupler_without_the_fields_is_told_how_to_reach_it(tmp_path):
    s, _ = _session(tmp_path)
    out = s.run("qubit_power_rabi", {"targets": ["q0_q1_c"]})
    err = out["error"]
    assert "lacks operation(s) ['readout', 'rx']" in err
    assert "drive_line=xy_q0|xy_q1" in err and "readout_member=q0|q1" in err


@pytest.mark.parametrize("params, words", [
    ({"drive_line": "zc_q0_q1"}, "has no drive channel to 'q0_q1_c'"),
    ({"drive_line": "xy_q9"}, "is not a line of this roster"),
])
def test_a_line_that_cannot_drive_the_target_is_refused(tmp_path, params, words):
    s, _ = _session(tmp_path)
    out = s.run("qubit_power_rabi", {**COUPLER_RABI, **params})
    assert words in out["error"], out["error"]


def test_the_member_must_belong_to_the_couplers_pair(tmp_path):
    s, _ = _session(tmp_path, qubits=("q0", "q1", "q2"), chain=True,
                    adopt=("xy_q1.q0_q1_c", "xy_q1.q0"))
    out = s.run("qubit_power_rabi", {**COUPLER_RABI, "readout_member": "q2"})
    assert "'q2' is not a member of 'q0_q1_c'" in out["error"], out["error"]
    # a target that couples no pair cannot be read through a member at all
    out = s.run("qubit_power_rabi", {"targets": ["q0"], "drive_line": "xy_q1",
                                     "readout_member": "q1",
                                     "use_state_discrimination": True})
    assert "is the coupler of no pair" in out["error"], out["error"]


@pytest.mark.parametrize("overrides, words", [
    ({"targets": ["q0_q1_c", "q1_q2_c"]}, "ONE target per run"),
    ({"targets": ["q0"], "readout_member": "q0"}, "other than the member"),
    ({"use_state_discrimination": False}, "use_state_discrimination=true"),
    ({"reset_method": "active"}, "reset_method='thermal'"),
    ({"thermalization_time_ns": 400000.0}, "no thermalization_time_ns override"),
    ({"drive_line": "xy_q1.q0_q1_c"}, "name the LINE only"),
])
def test_the_v1_limits_are_refused_by_name(overrides, words):
    params = registry.get("qubit_power_rabi").Parameters
    with pytest.raises(ValidationError, match=words):
        params(**{**COUPLER_RABI, **overrides})


def test_without_the_fields_a_qubit_run_is_untouched(tmp_path):
    """The defaults are today's behaviour: the designed channel is calibrated."""
    s, _ = _session(tmp_path)
    out = s.run("qubit_power_rabi", {"targets": ["q0"]})
    assert out.get("error") is None, out.get("error")
    assert {(x["entity"], x["field"]) for x in out["suggestions"]} == {
        ("xy_q0.q0", "pi_amp")}
