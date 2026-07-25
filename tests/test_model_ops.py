"""Operational surfaces of the greenfield model: the production-cut lock,
the doctor witnesses, and the report data behind scqo state / scqo device."""

import json

import pytest

from scqo import Session, parse_components
from scqo.checks import FAIL, OK, WARN, all_checks, roster_checks
from scqo.checks import design_checks, lock_checks, vendor_checks
from scqo.checks import capability_checks, wiring_checks
from scqo.device import ComponentInfo
from scqo.lock import LOCK_FILE, LockError, freeze, verify
from scqo.report import (
    design_rows,
    expansion_rows,
    field_rows,
    live_sources,
    qubit_rows,
    state_rows,
)
from scqo.testing import (
    InMemoryDevice,
    SimulatedBackend,
    demo_components,
    demo_design,
    demo_vendor_state,
)
from tests.test_model_roster import EXAMPLE


@pytest.fixture()
def roster():
    return parse_components(EXAMPLE)


@pytest.fixture()
def session(tmp_path):
    r = demo_components(tunable=True)
    d = demo_design(r)
    vendor = InMemoryDevice(r, demo_vendor_state(r, d))
    return Session(SimulatedBackend(vendor), r, design=d,
                   scqo_dir=tmp_path / "scqo", device_name="chipT",
                   setup_name="sim", cooldown_id="cd1")


def _status(checks, topic):
    return [c for c in checks if c.topic == topic]


# ---------------------------------------------------------------- the lock

def test_freeze_writes_the_expanded_signature_set(roster, tmp_path):
    path = freeze(roster, tmp_path, note="production cut")
    data = json.loads(path.read_text())
    assert data["schema"] == 1 and data["note"] == "production cut"
    assert "q1_ro" in data["entities"] and "q1_res" in data["entities"]
    assert data["entities"]["q1_ro"] == ["Channel", "q1_ro", "readout",
                                         ["q1"]]


def test_freeze_happens_once(roster, tmp_path):
    freeze(roster, tmp_path)
    with pytest.raises(LockError, match="already exists"):
        freeze(roster, tmp_path)


def test_unfrozen_device_never_drifts(roster, tmp_path):
    assert verify(roster, tmp_path) == []


def test_appends_are_legal_after_the_cut(roster, tmp_path):
    freeze(roster, tmp_path)
    # a new rider on a frozen line + a new operation on a frozen composite
    grown = parse_components(
        EXAMPLE.replace('drive = ["q1"]', 'drive = ["q1", "q1_q2_c"]')
        .replace('operations = ["iswap"]', 'operations = ["iswap", "cz"]'))
    assert verify(grown, tmp_path) == []
    checks = lock_checks(grown, tmp_path)
    assert all(c.status == OK for c in checks)
    assert any("q1_q2_c_xy" in c.message for c in checks)


def test_removing_a_frozen_name_is_refused(roster, tmp_path):
    freeze(roster, tmp_path)
    shrunk = parse_components(EXAMPLE.replace('[lines.xy3]\ndrive = ["q3"]',
                                              ""))
    drift = verify(shrunk, tmp_path)
    # deleting the line takes its minted channel with it — both are frozen
    assert [d.name for d in drift] == ["q3_xy", "xy3"]
    assert {d.problem for d in drift} == {"missing"}
    assert "retired = true" in drift[0].detail
    assert lock_checks(shrunk, tmp_path)[0].status == FAIL


def test_retiring_keeps_the_name_resolving(roster, tmp_path):
    freeze(roster, tmp_path)
    retired = parse_components(EXAMPLE.replace(
        '[modes.q3]\nkind = "transmon"',
        '[modes.q3]\nkind = "transmon"\nretired = true'))
    assert verify(retired, tmp_path) == []
    assert any("retired" in c.message for c in roster_checks(retired))


def test_changing_a_frozen_identity_is_refused(roster, tmp_path):
    freeze(roster, tmp_path)
    moved = parse_components(EXAMPLE.replace('readout = ["q1", "q2", "q3"]',
                                             'readout = ["q1", "q2"]')
                             + '\n[lines.fl2]\nreadout = ["q3"]\n')
    assert verify(moved, tmp_path) == []          # line is wiring, not identity
    rekinded = parse_components(EXAMPLE.replace(
        '[modes.q3]\nkind = "transmon"',
        '[modes.q3]\nkind = "fluxonium"'))
    drift = verify(rekinded, tmp_path)
    assert [d.problem for d in drift] == ["changed"]


def test_corrupt_lock_fails_loudly(roster, tmp_path):
    (tmp_path / LOCK_FILE).write_text("{not json")
    with pytest.raises(LockError):
        verify(roster, tmp_path)
    assert lock_checks(roster, tmp_path)[0].status == FAIL


# ------------------------------------------------------------- the witnesses

def test_roster_checks_flag_unreachable_modes():
    r = parse_components(EXAMPLE + '\n[modes.orphan]\nkind = "transmon"\n')
    warn = [c for c in roster_checks(r) if c.status == WARN]
    assert warn and "orphan" in warn[0].message


def test_roster_checks_flag_an_unbiasable_coupler():
    r = parse_components(EXAMPLE.replace('[lines.zc12]\nflux = ["q1_q2_c"]',
                                         ""))
    warn = [c for c in roster_checks(r) if c.status == WARN]
    assert any("no flux channel" in c.message for c in warn)


def test_design_checks_report_coverage_and_gross_mismatch(session):
    checks = design_checks(session.roster, session.design,
                           {"q0_res": {"f_r_hz": 5.95e9}})
    assert any("1/" in c.message and "measured" in c.message
               for c in checks)
    off = design_checks(session.roster, session.design,
                        {"q0_res": {"f_r_hz": 1.0e9}})
    assert any(c.status == WARN and ">50% off" in c.message for c in off)


def test_empty_datasheet_warns(roster):
    from scqo import Design
    assert design_checks(roster, Design({}))[0].status == WARN


def test_vendor_witness_names_both_gaps(session):
    roster = session.roster
    inventory = {"q0_xy": ComponentInfo(kind="drive", target=("q0",)),
                 "ghost_xy": ComponentInfo(kind="drive", target=("q0",))}
    checks = vendor_checks(roster, inventory)
    messages = " ".join(c.message for c in checks)
    assert "q0_ro" in messages          # roster entity not realized
    assert "ghost_xy" in messages       # vendor entity not in the roster
    assert vendor_checks(roster, None)[0].status == WARN


def test_vendor_kind_disagreement_is_a_failure(session):
    bad = {"q0_xy": ComponentInfo(kind="readout", target=("q0",))}
    assert any(c.status == FAIL for c in vendor_checks(session.roster, bad))


def test_capability_witness_catches_a_live_z_on_a_fixed_qubit():
    r = parse_components(EXAMPLE)          # q3 is a fixed transmon
    inventory = {"q3_z": ComponentInfo(kind="flux", target=("q3",))}
    checks = capability_checks(r, inventory)
    assert checks and checks[0].status == FAIL
    assert "fixed-frequency qubit with a live z element" in checks[0].message


def test_wiring_witness_reads_the_port_annotation(roster):
    ports = {"fl1": {"outputs": ["out1"]}, "xy1": {"outputs": ["out2"]},
             "z1": {"outputs": ["out3"]}, "xyz2": {"outputs": ["out4"]},
             "xy3": {"outputs": ["out5"]}, "zc12": {"outputs": ["out6"]}}
    warn = [c for c in wiring_checks(roster, ports) if c.status == WARN]
    # xyz2 carries drive AND flux on one declared output
    assert any("combined wire" in c.message for c in warn)
    ports["xyz2"] = {"outputs": ["mw1", "lf1"]}
    ports["fl1"] = {"outputs": ["o1", "o2"]}   # multiplexed but 2 outputs
    warn = [c for c in wiring_checks(roster, ports) if c.status == WARN]
    assert any("share" in c.message and "ONE output" in c.message
               for c in warn)
    assert wiring_checks(roster, None)[0].status == WARN


def test_all_checks_runs_the_whole_battery(session, tmp_path):
    checks = all_checks(session.roster, design=session.design,
                        physical=session.physical_state(),
                        device_dir=tmp_path, inventory=None, ports=None)
    assert {c.topic for c in checks} >= {"roster", "design", "lock",
                                         "vendor", "wiring"}
    assert all(isinstance(c.status, str) for c in checks)


# ---------------------------------------------------------------- reporting

def test_expansion_rows_show_minted_provenance(roster):
    rows = {r["entity"]: r for r in expansion_rows(roster)}
    assert rows["q1_res"]["origin"].startswith("[lines.fl1] readout[0]")
    assert rows["q1"]["origin"] == "declared"
    assert rows["q1_ro"]["via"] == "q1_res"
    assert set(rows["q1"]["operations"]) == {"rx", "readout", "flux_bias"}
    assert rows["fl1"]["carries"] == ["q1_ro", "q2_ro", "q3_ro"]


def test_field_rows_carry_routing_and_seed_story(roster):
    rows = {(r["entity"], r["field"]): r for r in field_rows(roster)}
    ro = rows[("q1_ro", "readout_freq_hz")]
    assert ro["store"] == "scqo_state.json" and ro["pushed"] is True
    assert ro["seed"] == "q1_res.f_r_hz"
    # the drive seed lists BOTH candidate facts (kind decides which applies)
    assert rows[("q1_xy", "drive_freq_hz")]["seed"] == (
        "q1.f_01_hz | q1.f_q_max_hz")
    assert rows[("q1_xy", "pi_amp")]["seed"] is None
    fact = rows[("q1_res", "f_r_hz")]
    assert fact["store"] == "physical.json" and fact["pushed"] is False
    flux = rows[("q1_z", "idle_flux")]
    assert flux["unit"] == "source-native" and flux["portable"] is False
    op = rows[("q1_q2", "iswap_coupler_flux")]
    assert op["why"] == "operation 'iswap'"


def test_state_rows_merge_both_stores_with_sources(session):
    session.set_values({"q0.pi_amp": 0.22, "q0.f_r_hz": 5.951e9})
    sources = live_sources(session.device_state(), session.history())
    rows = {(r["entity"], r["field"]): r
            for r in state_rows(session.roster, session.device_state(),
                                session.physical_state(), sources=sources)}
    assert rows[("q0_xy", "pi_amp")]["store"] == "scqo_state.json"
    assert rows[("q0_xy", "pi_amp")]["source"]["status"] == "manual"
    assert rows[("q0_res", "f_r_hz")]["store"] == "physical.json"


def test_state_rows_surface_orphaned_store_entities(session):
    rows = state_rows(session.roster, {"gone": {"pi_amp": 1.0}}, {})
    assert rows[0]["kind"] == "(orphan)"


def test_qubit_rows_tag_the_closure_role(session):
    session.set_values({"q0.pi_amp": 0.22, "q0.f_r_hz": 5.95e9})
    rows = qubit_rows(session.roster, "q0", session.device_state(),
                      session.physical_state())
    by_member = {r["member"] for r in rows}
    assert {"drive channel", "resonator"} <= by_member
    assert not any(r["entity"].startswith("q1") for r in rows)


def test_design_rows_are_the_comparison_column(session):
    session.set_values({"q0.f_r_hz": 5.96e9})
    rows = {(r["entity"], r["field"]): r
            for r in design_rows(session.roster, session.design,
                                 session.physical_state())}
    row = rows[("q0_res", "f_r_hz")]
    assert row["designed"] == 5.95e9 and row["measured"] == 5.96e9
    assert row["delta"] == pytest.approx(1e7)
    assert rows[("q1_res", "f_r_hz")]["measured"] is None
