"""Operational surfaces of the greenfield model: the production-cut lock,
the doctor witnesses, and the report data behind scqo state / scqo device."""

import json

import pytest

from scqo import Session, parse_components
from scqo.checks import FAIL, OK, WARN, all_checks, roster_checks
from scqo.checks import design_checks, lock_checks, vendor_checks
from scqo.checks import capability_checks, wiring_checks
from scqo.device import ComponentInfo
from scqo.lock import LOCK_FILE, LockError, additions, freeze, verify
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
    assert "fl1.q1" in data["entities"] and "q1_res" in data["entities"]
    assert data["entities"]["fl1.q1"] == ["Channel", "fl1.q1", ["readout"],
                                          ["q1"]]
    # declared on their composite / declared by nobody: never frozen
    assert "q1_q2.iswap" not in data["entities"]
    assert "xy1.q1_q2_c" not in data["entities"]


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
    # the route that was borrowed is now designed wiring: an append
    assert any("xy1.q1_q2_c" in c.message for c in checks)


def test_a_function_added_to_a_frozen_channel_is_an_append(roster, tmp_path):
    """One (line, target) is one channel since 4.0.0, so a rider for a target
    the line already carries GROWS that channel's kinds: every frozen field
    keeps resolving on the same owner - an append. Shrinking them is not."""
    freeze(roster, tmp_path)
    grown_text = EXAMPLE.replace('[lines.zc12]\nflux = ["q1_q2_c"]',
                                 '[lines.zc12]\nflux = ["q1_q2_c"]\n'
                                 'drive = ["q1_q2_c"]')
    grown = parse_components(grown_text)
    assert set(grown.entities["zc12.q1_q2_c"].kinds) == {"drive", "flux"}
    assert verify(grown, tmp_path) == []
    assert "zc12.q1_q2_c (+drive)" in additions(grown, tmp_path)

    (tmp_path / LOCK_FILE).unlink()
    freeze(grown, tmp_path)
    drift = verify(roster, tmp_path)  # the drive function removed again
    assert [(d.name, d.problem) for d in drift] == [("zc12.q1_q2_c", "changed")]


def test_removing_a_frozen_name_is_refused(roster, tmp_path):
    freeze(roster, tmp_path)
    shrunk = parse_components(EXAMPLE.replace('[lines.xy3]\ndrive = ["q3"]',
                                              ""))
    drift = verify(shrunk, tmp_path)
    # deleting the line takes its minted channel with it — both are frozen
    # (the routes it lent were never frozen, so they cannot drift)
    assert [d.name for d in drift] == ["xy3", "xy3.q3"]
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
    # 4.0.0 stores by line: the line is part of a channel's ADDRESS, so a
    # rider moved to another line is another channel and the frozen one goes
    # missing (its stored values and history would stop resolving)
    moved = parse_components(EXAMPLE.replace('readout = ["q1", "q2", "q3"]',
                                             'readout = ["q1", "q2"]')
                             + '\n[lines.fl2]\nreadout = ["q3"]\n')
    drift = verify(moved, tmp_path)
    assert [(d.name, d.problem) for d in drift] == [("fl1.q3", "missing")]
    rekinded = parse_components(EXAMPLE.replace(
        '[modes.q3]\nkind = "transmon"',
        '[modes.q3]\nkind = "fluxonium"'))
    drift = verify(rekinded, tmp_path)
    assert [d.problem for d in drift] == ["changed"]


def test_remediating_a_readout_stays_legal_after_the_cut(tmp_path):
    """The mediator is wiring, not identity: a readout re-routed through
    another resonator keeps its address and its signature."""
    hatch = EXAMPLE + ('\n[channels.c_ro]\nkind = "readout"\n'
                       'target = "q1_q2_c"\nline = "fl1"\nvia = "q1_res"\n')
    freeze(parse_components(hatch), tmp_path)
    remediated = parse_components(hatch.replace('via = "q1_res"',
                                                'via = "q2_res"'))
    assert remediated.entities["fl1.q1_q2_c"].via == "q2_res"
    assert verify(remediated, tmp_path) == []


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
                           {"q0_res": {"f_dress0_hz": 5.95e9}})
    assert any("1/" in c.message and "measured" in c.message
               for c in checks)
    off = design_checks(session.roster, session.design,
                        {"q0_res": {"f_dress0_hz": 1.0e9}})
    assert any(c.status == WARN and ">50% off" in c.message for c in off)


def test_empty_datasheet_warns(roster):
    from scqo import Design
    assert design_checks(roster, Design({}))[0].status == WARN


def test_vendor_witness_names_both_gaps(session):
    roster = session.roster
    inventory = {"xy_q0.q0": ComponentInfo(kind="drive", target=("q0",)),
                 "xy_ghost.q0": ComponentInfo(kind="drive", target=("q0",))}
    checks = vendor_checks(roster, inventory)
    messages = " ".join(c.message for c in checks)
    assert "'fl.q0'" in messages        # roster entity not realized
    assert "xy_ghost.q0" in messages    # vendor entity not in the roster
    assert vendor_checks(roster, None)[0].status == WARN


def test_vendor_witness_demands_designed_wiring_only(session):
    """What the instrument must realize is the DESIGNED wiring. A borrowed
    route exists only once the vendor adopts it, so its absence is no gap
    (docs/store-by-line-plan.md section 2.2); a composite has no knob left to
    realize (its knobs live on its operations)."""
    roster = session.roster
    inventory = {name: ComponentInfo(kind=e.kind, target=e.target)
                 for name, e in roster.channels().items()}
    missing = " ".join(c.message for c in vendor_checks(roster, inventory)
                       if "does not realize" in c.message)
    assert not [b for b in roster.borrowed_channels() if f"'{b}'" in missing]
    assert "'q0_q1'" not in missing


def test_vendor_kind_disagreement_is_a_failure(session):
    bad = {"xy_q0.q0": ComponentInfo(kind="readout", target=("q0",))}
    assert any(c.status == FAIL for c in vendor_checks(session.roster, bad))


def test_capability_witness_catches_a_live_z_on_a_fixed_qubit():
    r = parse_components(EXAMPLE)          # q3 is a fixed transmon
    inventory = {"z3.q3": ComponentInfo(kind="flux", target=("q3",))}
    checks = capability_checks(r, inventory)
    assert checks and checks[0].status == FAIL
    assert "fixed-frequency qubit with a live z element" in checks[0].message


def test_wiring_witness_reads_the_port_annotation(roster):
    ports = {"fl1": {"outputs": ["out1"]}, "xy1": {"outputs": ["out2"]},
             "z1": {"outputs": ["out3"]}, "xyz2": {"outputs": ["out4"]},
             "xy3": {"outputs": ["out5"]}, "zc12": {"outputs": ["out6"]}}
    warn = [c for c in wiring_checks(roster, ports) if c.status == WARN]
    # xyz2 carries drive AND flux (one channel, xyz2.q2, of two kinds) on
    # one declared output
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
    assert rows["fl1.q1"]["origin"].startswith("[lines.fl1] readout[0]")
    assert rows["fl1.q1"]["via"] == "q1_res"
    assert set(rows["q1"]["operations"]) == {"rx", "readout", "flux_bias"}
    assert rows["fl1"]["carries"] == ["fl1.q1", "fl1.q2", "fl1.q3"]
    # the undeclared routes are summarized on the line that lends them
    assert rows["xy1"]["lends"] == ["q1_q2_c", "q2", "q3"]
    assert "xy1.q2" not in rows
    listed = {r["entity"]: r for r in expansion_rows(roster, borrowed=True)}
    assert listed["xy1.q2"]["origin"] == "borrowed"


def test_field_rows_carry_routing_and_seed_story(roster):
    rows = {(r["entity"], r["field"]): r for r in field_rows(roster)}
    ro = rows[("fl1.q1", "readout_freq_hz")]
    assert ro["store"] == "scqo_state.json" and ro["pushed"] is True
    assert ro["seed"] == "q1_res.f_dress0_hz"
    # the drive seed lists BOTH candidate facts (kind decides which applies)
    assert rows[("xy1.q1", "drive_freq_hz")]["seed"] == (
        "q1.f_01_hz | q1.f_q_max_hz")
    assert rows[("xy1.q1", "pi_amp")]["seed"] is None
    fact = rows[("q1_res", "f_dress0_hz")]
    assert fact["store"] == "physical.json" and fact["pushed"] is False
    flux = rows[("z1", "idle_flux")]               # the LINE's knob
    assert flux["unit"] == "source-native" and flux["portable"] is False
    assert flux["store"] == "scqo_state.json" and flux["pushed"] is True
    transfer = rows[("z1.q1", "flux_per_phi0")]    # the channel's fact
    assert transfer["store"] == "physical.json" and transfer["pushed"] is False
    op = rows[("q1_q2.iswap", "coupler_flux")]
    assert op["why"] == "operation 'iswap' of 'q1_q2'"
    # borrowed routes share one field set: left out unless asked for
    assert ("xy1.q1_q2_c", "pi_amp") not in rows
    listed = {(r["entity"], r["field"]): r
              for r in field_rows(roster, borrowed=True)}
    assert listed[("xy1.q1_q2_c", "pi_amp")]["why"] == "borrowed drive channel"
    # ...and a route seeds from ITS target, the coupler
    assert listed[("xy1.q1_q2_c", "drive_freq_hz")]["seed"] == (
        "q1_q2_c.f_01_hz | q1_q2_c.f_q_max_hz")


def test_state_rows_merge_both_stores_with_sources(session):
    session.set_values({"q0.pi_amp": 0.22, "q0.f_dress0_hz": 5.951e9})
    sources = live_sources(session.device_state(), session.history())
    rows = {(r["entity"], r["field"]): r
            for r in state_rows(session.roster, session.device_state(),
                                session.physical_state(), sources=sources)}
    assert rows[("xy_q0.q0", "pi_amp")]["store"] == "scqo_state.json"
    assert rows[("xy_q0.q0", "pi_amp")]["source"]["status"] == "manual"
    assert rows[("q0_res", "f_dress0_hz")]["store"] == "physical.json"
    assert rows[("z_q0", "idle_flux")]["kind"] == "line"


def test_state_rows_surface_orphaned_store_entities(session):
    rows = state_rows(session.roster, {"gone": {"pi_amp": 1.0}}, {})
    assert rows[0]["kind"] == "(orphan)"


def test_qubit_rows_tag_the_closure_role(session):
    session.set_values({"q0.pi_amp": 0.22, "q0.f_dress0_hz": 5.95e9})
    rows = qubit_rows(session.roster, "q0", session.device_state(),
                      session.physical_state())
    by_member = {r["member"] for r in rows}
    # the bias lives on q0's own flux line, so the line joins the closure
    assert {"drive channel", "readout channel", "flux line",
            "resonator"} <= by_member
    # nothing of q1's closure (or the pair's) leaks in
    assert {r["entity"] for r in rows} <= {
        "q0", "xy_q0.q0", "fl.q0", "z_q0.q0", "z_q0", "q0_res"}


def test_design_rows_are_the_comparison_column(session):
    session.set_values({"q0.f_dress0_hz": 5.96e9})
    rows = {(r["entity"], r["field"]): r
            for r in design_rows(session.roster, session.design,
                                 session.physical_state())}
    row = rows[("q0_res", "f_dress0_hz")]
    assert row["designed"] == 5.95e9 and row["measured"] == 5.96e9
    assert row["delta"] == pytest.approx(1e7)
    assert rows[("q1_res", "f_dress0_hz")]["measured"] is None
