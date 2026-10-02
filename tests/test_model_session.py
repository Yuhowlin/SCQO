"""Session state-layer contracts (scqo.session + suggestions):
qubit-closure addressing, set_values, capture -> suggest -> accept/reject,
era + staleness guards, per-qubit assembled views."""

import pytest

from scqo import (
    RosterError,
    Session,
    SuggestionCapture,
)
from scqo.testing import (
    InMemoryDevice,
    SimulatedBackend,
    demo_components,
    demo_design,
    demo_vendor_state,
)


class FakeRunStore:
    """Duck-typed DataStore slice for decision flows (load_run /
    edit_suggestions / run_stamps / device_name)."""

    def __init__(self, record):
        self.record = record
        self.device_name = record["device"]

    def load_run(self, run_id):
        return {"record": self.record}

    def edit_suggestions(self, run_id, editor, updated_device=None):
        self.record["suggestions"] = editor(self.record["suggestions"])
        if updated_device is not None:
            self.record["updated_device"] = updated_device
        return self.record

    def run_stamps(self):
        return ("cd1", "qm_a")


@pytest.fixture()
def session(tmp_path):
    roster = demo_components()
    design = demo_design(roster)
    vendor = InMemoryDevice(roster, demo_vendor_state(roster, design))
    return Session(
        SimulatedBackend(vendor), roster, design=design,
        scqo_dir=tmp_path / "scqo", device_name="chipT",
        setup_name="qm_a", cooldown_id="cd1")


def _attach_run(session, suggestions):
    record = {"device": "chipT", "cooldown": "cd1", "setup": "qm_a",
              "experiment": "demo", "suggestions": suggestions}
    session.datastore = FakeRunStore(record)
    return session.datastore


# ---------------------------------------------------------------- addressing

def test_qubit_closure_addressing(session):
    r = session.roster
    assert r.resolve_field("q0", "pi_amp")[0] == "xy_q0.q0"
    assert r.resolve_field("q0", "readout_freq_hz")[0] == "fl.q0"
    assert r.resolve_field("q0", "f_dress0_hz")[0] == "q0_res"
    assert r.resolve_field("q0", "n_th")[0] == "q0"          # self wins
    assert r.resolve_field("xy_q0.q0", "pi_amp")[0] == "xy_q0.q0"  # explicit
    assert r.resolve_field("xy_q1.q0", "pi_amp")[0] == "xy_q1.q0"  # borrowed
    assert r.resolve_field("q0_q1.iswap", "coupler_flux")[0] == "q0_q1.iswap"
    with pytest.raises(RosterError, match=r"q0_q1\.iswap\.coupler_flux"):
        r.resolve_field("q0_q1", "coupler_flux")  # gate knobs: the operation
    with pytest.raises(RosterError, match="did you mean"):
        r.resolve_field("q0_res", "pi_amp")


# ---------------------------------------------------------------- set_values

def test_set_values_routes_both_stores(session):
    out = session.set_values({"q0.pi_amp": 0.22, "q0.f_dress0_hz": 5.951e9})
    assert not out["errors"]
    assert {(a["entity"], a["field"]) for a in out["applied"]} == {
        ("xy_q0.q0", "pi_amp"), ("q0_res", "f_dress0_hz")}
    assert session.device_state()["xy_q0.q0"]["pi_amp"] == 0.22
    assert session.physical_state()["q0_res"]["f_dress0_hz"] == 5.951e9
    rows = session.history()
    assert rows[-1]["entity"] == "xy_q0.q0" and rows[-1]["experiment"] is None


def test_set_values_splits_the_address_at_its_last_dot(session):
    """An owner may carry a dot itself (a channel <line>.<target>, an
    operation <composite>.<op>): the field is what follows the LAST one."""
    out = session.set_values({"xy_q0.q0.drag_beta": -0.5,
                              "q0_q1.iswap.coupler_flux": 0.05})
    assert not out["errors"]
    assert {(a["entity"], a["field"]) for a in out["applied"]} == {
        ("xy_q0.q0", "drag_beta"), ("q0_q1.iswap", "coupler_flux")}
    assert session.device_state()["q0_q1.iswap"]["coupler_flux"] == 0.05


def test_set_values_validates_all_before_writing_any(session):
    with pytest.raises(ValueError, match="closure"):
        session.set_values({"q0.pi_amp": 0.5, "q0.bogus_field": 1.0})
    assert session.device_state()["xy_q0.q0"]["pi_amp"] == 0.1  # untouched


def test_set_values_dry_run_reports_without_writing(session):
    out = session.set_values({"q0.pi_amp": 0.9}, dry_run=True)
    assert out["items"][0]["entity"] == "xy_q0.q0"
    assert out["items"][0]["current"] == 0.1
    assert session.device_state()["xy_q0.q0"]["pi_amp"] == 0.1


# ------------------------------------------------------------------- capture

def test_capture_turns_writes_into_role_stamped_suggestions(session):
    capture = SuggestionCapture(session.device, session.physical,
                                session.roster)
    capture.component("q0").f_01_hz = 3.81e9            # mode fact
    capture.component("xy_q0.q0").pi_amp = 0.23        # channel knob
    capture.component("fl.q0").fidelity_g = 0.97       # channel monitor
    capture.operation("q0_q1", "iswap").write_knob("coupler_flux", 0.05)
    roles = [(s.entity, s.field, s.role) for s in capture.suggestions]
    assert roles == [
        ("q0", "f_01_hz", "fact"),
        ("xy_q0.q0", "pi_amp", "knob"),
        ("fl.q0", "fidelity_g", "monitor"),
        ("q0_q1.iswap", "coupler_flux", "knob"),
    ]
    assert capture.suggestions[1].before == 0.1
    assert capture.suggestions[3].kind == "operation"
    assert session.device_state()["xy_q0.q0"]["pi_amp"] == 0.1  # none applied


def test_capture_typo_fails_loudly(session):
    capture = SuggestionCapture(session.device, session.physical,
                                session.roster)
    with pytest.raises(AttributeError, match="no field"):
        capture.component("xy_q0.q0").readout_freq_hz = 5.9e9


# ------------------------------------------------------------ accept / reject

def _pending(session):
    capture = SuggestionCapture(session.device, session.physical,
                                session.roster)
    capture.component("xy_q0.q0").pi_amp = 0.24
    capture.component("q0_res").f_dress0_hz = 5.952e9
    return [s.model_dump(mode="json") for s in capture.suggestions]


def test_accept_applies_both_stores_and_persists_decisions(session):
    store = _attach_run(session, _pending(session))
    out = session.accept("r1")
    assert not out["errors"] and out["pending_left"] == 0
    assert {(a["entity"], a["field"]) for a in out["applied"]} == {
        ("xy_q0.q0", "pi_amp"), ("q0_res", "f_dress0_hz")}
    assert session.device_state()["xy_q0.q0"]["pi_amp"] == 0.24
    assert session.physical_state()["q0_res"]["f_dress0_hz"] == 5.952e9
    assert store.record["suggestions"][0]["status"] == "accepted"
    assert store.record["updated_device"] is True
    # the applying run is credited on the change record
    assert session.history()[-1]["run_id"] == "r1"


def test_accept_era_guard_and_force(session):
    _attach_run(session, _pending(session))
    session.datastore.record["setup"] = "qblox_b"       # a different era
    with pytest.raises(RuntimeError, match="may not transfer"):
        session.accept("r1")
    out = session.accept("r1", force=True)
    assert not out["errors"] and len(out["applied"]) == 2


def test_accept_staleness_guard_skips_moved_values(session):
    _attach_run(session, _pending(session))
    session.set_values({"q0.pi_amp": 0.19})             # moved since capture
    out = session.accept("r1")
    assert [s["field"] for s in out["stale"]] == ["pi_amp"]
    assert [a["field"] for a in out["applied"]] == ["f_dress0_hz"]
    assert session.device_state()["xy_q0.q0"]["pi_amp"] == 0.19


def test_accept_dry_run_reports_only(session):
    _attach_run(session, _pending(session))
    out = session.accept("r1", dry_run=True)
    assert out["era"]["match"] is True
    assert len(out["items"]) == 2 and out["items"][0]["stale"] is False
    assert session.device_state()["xy_q0.q0"]["pi_amp"] == 0.1


def test_reject_is_metadata_only(session):
    store = _attach_run(session, _pending(session))
    out = session.reject("r1", comment="fit looked off")
    assert out["pending_left"] == 0
    assert store.record["suggestions"][0]["status"] == "rejected"
    assert session.device_state()["xy_q0.q0"]["pi_amp"] == 0.1


def test_pre_greenfield_rows_are_display_only(session):
    _attach_run(session, [{"component": "q0", "field": "readout_freq",
                           "store": "instrument", "before": None,
                           "after": 5.9e9, "status": "pending"}])
    with pytest.raises(ValueError, match="pre-greenfield"):
        session.accept("r1")


def test_operator_suggest_appends_with_sugar_keys(session):
    store = _attach_run(session, [])
    out = session.suggest("r1", {"q0.readout_freq_hz": 5.96e9},
                          comment="read off the dip")
    assert out["added"][0]["entity"] == "fl.q0"
    row = store.record["suggestions"][0]
    assert row["origin"] == "operator" and row["role"] == "knob"
    accept = session.accept("r1")
    assert accept["applied"][0]["entity"] == "fl.q0"
    assert session.device_state()["fl.q0"]["readout_freq_hz"] == 5.96e9


# --------------------------------------------------------------------- views

def test_qubit_state_assembles_the_closure(session):
    session.set_values({"q0.f_dress0_hz": 5.951e9})
    view = session.qubit_state("q0")
    assert view["xy_q0.q0"]["pi_amp"] == 0.1
    assert view["q0_res"]["f_dress0_hz"] == 5.951e9
    assert "fl.q0" in view and "xy_q1.q1" not in view
    assert "xy_q1.q0" not in view      # a borrowed route is not the closure


def test_qubit_state_includes_the_line_that_biases_it(tmp_path):
    """Since 4.0.0 a qubit's bias lives on its flux LINE (z_q0.idle_flux, the
    target of the q0.idle_flux shorthand), no longer on a channel of q0 - the
    assembled per-qubit view must keep showing it, as report.qubit_rows does,
    and must not pull in the coupler's line."""
    roster = demo_components(tunable=True)
    design = demo_design(roster)
    s = Session(SimulatedBackend(InMemoryDevice(
        roster, demo_vendor_state(roster, design))), roster, design=design,
        scqo_dir=tmp_path / "scqo", device_name="chipT", setup_name="qm_a",
        cooldown_id="cd1")
    view = s.qubit_state("q0")
    assert view["z_q0"]["idle_flux"] == 0.0
    assert "zc_q0_q1" not in view


def test_history_store_routing(session):
    session.set_values({"q0.pi_amp": 0.3, "q0.f_dress0_hz": 5.95e9})
    assert [r["entity"] for r in session.history()] == ["xy_q0.q0"]
    assert [r["entity"] for r in session.history("physical")] == ["q0_res"]
    with pytest.raises(ValueError, match="state"):
        session.history("instrument")


# ----------------------------------------------- review-added regressions

def test_capture_proposes_waveform_with_dt_in_same_update(session):
    capture = SuggestionCapture(session.device, session.physical,
                                session.roster)
    view = capture.operation("q0_q1", "iswap")
    view.write_knob("waveform_dt_s", 5e-10)           # dt only PROPOSED
    view.write_knob("waveform", [0.0, 0.4])           # must still capture
    assert len(capture.suggestions) == 2


def test_accept_applies_waveform_group_even_stored_out_of_order(session):
    capture = SuggestionCapture(session.device, session.physical,
                                session.roster)
    view = capture.operation("q0_q1", "iswap")
    view.write_knob("waveform_dt_s", 5e-10)
    view.write_knob("waveform", [0.0, 0.4])
    rows = [s.model_dump(mode="json") for s in capture.suggestions]
    _attach_run(session, list(reversed(rows)))        # waveform stored FIRST
    out = session.accept("r1")
    assert not out["errors"] and len(out["applied"]) == 2
    assert session.device_state()["q0_q1.iswap"]["waveform"] == [0.0, 0.4]


@pytest.fixture()
def flux_session(tmp_path):
    """A session over the design-doc EXAMPLE roster (has flux channels with
    the paired distortion arrays)."""
    from scqo import parse_components
    from tests.test_model_roster import EXAMPLE
    roster = parse_components(EXAMPLE)
    vendor = InMemoryDevice(roster, {})
    return Session(SimulatedBackend(vendor), roster,
                   scqo_dir=tmp_path / "scqo", device_name="5q",
                   setup_name="qm_a", cooldown_id="cd1")


def test_paired_lengths_change_together_in_one_call(flux_session):
    s = flux_session
    s.set_values({"z1.distortion_tau_s": [1e-6, 2e-6],
                  "z1.distortion_amp": [0.1, 0.2]})
    # the redone 3-tap fit: BOTH sides change length in one batch
    out = s.set_values({"z1.distortion_tau_s": [1e-6, 2e-6, 4e-6],
                        "z1.distortion_amp": [0.1, 0.2, 0.05]})
    assert not out["errors"]
    assert s.physical_state()["z1"]["distortion_amp"] == [0.1, 0.2, 0.05]
    s.physical.save()                                 # save invariant holds


def test_one_sided_pair_length_change_is_refused(flux_session):
    s = flux_session
    s.set_values({"z1.distortion_tau_s": [1e-6, 2e-6],
                  "z1.distortion_amp": [0.1, 0.2]})
    with pytest.raises(ValueError, match="unequal paired lengths"):
        s.set_values({"z1.distortion_amp": [0.1, 0.2, 0.3]})


def test_capture_offers_the_line_and_route_helpers(flux_session):
    """update() reaches a bias through the target's flux LINE and a borrowed
    route through its line; capture offers the same helpers as the live
    device, so each proposal lands on the owning entity."""
    s = flux_session
    capture = SuggestionCapture(s.device, s.physical, s.roster)
    capture.flux_line("q1").idle_flux = 0.12
    capture.line("zc12").idle_flux = 0.08
    capture.channel_on("xy1", "q1_q2_c").pi_amp = 0.3
    assert [(x.entity, x.field, x.role) for x in capture.suggestions] == [
        ("z1", "idle_flux", "knob"), ("zc12", "idle_flux", "knob"),
        ("xy1.q1_q2_c", "pi_amp", "knob")]
    assert [x.kind for x in capture.suggestions] == ["line", "line", "drive"]


def test_the_qubit_shorthand_reaches_its_flux_line(flux_session):
    """q1 is z1's only flux target, so ``q1.<line field>`` resolves to the
    LINE (the taps of the wire, its bias) - and ``q1.flux_per_phi0`` to the
    channel z1.q1 - exactly as the explicit addresses would."""
    s = flux_session
    out = s.set_values({"q1.distortion_tau_s": [1e-6],
                        "q1.distortion_amp": [0.1],
                        "q1.flux_per_phi0": 0.969}, dry_run=True)
    assert [(i["entity"], i["field"]) for i in out["items"]] == [
        ("z1", "distortion_tau_s"), ("z1", "distortion_amp"),
        ("z1.q1", "flux_per_phi0")]
    with pytest.raises(ValueError, match="both resolve to"):
        s.set_values({"q1.distortion_amp": [0.1], "z1.distortion_amp": [0.2]})


def test_set_values_dt_and_waveform_one_call_needs_dt_order(session):
    out = session.set_values({"q0_q1.iswap.waveform_dt_s": 5e-10,
                              "q0_q1.iswap.waveform": [0.1, 0.2]})
    assert not out["errors"] and len(out["applied"]) == 2


def test_set_values_waveform_without_any_dt_refused(session):
    with pytest.raises(ValueError, match="order the assignments dt"):
        session.set_values({"q0_q1.iswap.waveform": [0.1, 0.2]})


def test_suggest_relations_deferred_to_accept(session):
    _attach_run(session, [])
    out = session.suggest("r1", {"q0_q1.iswap.waveform_dt_s": 5e-10,
                                 "q0_q1.iswap.waveform": [0.1, 0.2]})
    assert len(out["added"]) == 2
    accept = session.accept("r1")
    assert not accept["errors"] and len(accept["applied"]) == 2


def test_aliased_assignment_keys_are_refused(session):
    with pytest.raises(ValueError, match="both resolve to"):
        session.set_values({"q0.pi_amp": 0.2, "xy_q0.q0.pi_amp": 0.3})


def test_suggest_refuses_pre_greenfield_mixed_lists(session):
    _attach_run(session, [{"component": "q0", "field": "x",
                           "store": "instrument", "before": None,
                           "after": 1.0, "status": "pending"}])
    with pytest.raises(ValueError, match="pre-greenfield"):
        session.suggest("r1", {"q0.pi_amp": 0.2})


def test_qubit_state_refuses_non_modes_with_a_hint(session):
    with pytest.raises(KeyError, match="did you mean 'q0'"):
        session.qubit_state("xy_q0.q0")


def test_capture_operation_is_an_operation_view(session):
    """Capture mirrors the live surface: an operation is an OperationView (the
    generic knob pair, isinstance-routed like the live one); the composite
    keeps only its facts, written attribute-style - and a composite fact is
    no operation field."""
    from scqo import OperationView
    capture = SuggestionCapture(session.device, session.physical,
                                session.roster)
    op = capture.operation("q0_q1", "iswap")
    assert isinstance(op, OperationView)
    with pytest.raises(RosterError, match="unknown field"):
        op.write_knob("zz_hz", 1e3)                   # a fact of the pair
    pair = capture.component("q0_q1")
    assert not isinstance(pair, OperationView)
    pair.zz_hz = 1.2e3                                # attribute surface OK
    assert capture.suggestions[-1].role == "fact"
    assert capture.suggestions[-1].entity == "q0_q1"


def test_ambiguous_channel_addressing_names_the_candidates():
    """A second DESIGNED drive line to q1 is legal wiring; the shorthand then
    refuses and names both channels."""
    from scqo import parse_components
    from tests.test_model_roster import EXAMPLE
    r = parse_components(EXAMPLE + '\n[lines.xy1b]\n'
                         '[channels.q1_xy2]\nkind = "drive"\n'
                         'target = "q1"\nline = "xy1b"\n')
    with pytest.raises(Exception, match=r"several drive channels.*"
                                        r"'xy1\.q1', 'xy1b\.q1'"):
        r.resolve_field("q1", "pi_amp")
