"""Roster loader contracts (scqo.roster) — docs/greenfield-schema.md
sections 3-5, 7 + the section-8 worked example as the primary fixture, with the
4.0.0 store-by-line addressing (docs/store-by-line-plan.md section 2)."""

import pytest

from scqo import (
    Channel,
    Mode,
    Roster,
    RosterError,
    parse_components,
)

#: The design doc's worked example (section 8), verbatim topology.
EXAMPLE = """
schema = 3

[modes.q1]
kind = "flux_transmon"
[modes.q2]
kind = "flux_transmon"
[modes.q3]
kind = "transmon"
[modes.q1_q2_c]
kind = "flux_transmon"

[composites.q1_q2]
kind       = "qubit_pair"
high       = "q1"
low        = "q2"
coupler    = "q1_q2_c"
operations = ["iswap"]

[lines.fl1]
readout = ["q1", "q2", "q3"]
[lines.xy1]
drive = ["q1"]
[lines.z1]
flux = ["q1"]
[lines.xyz2]
drive = ["q2"]
flux  = ["q2"]
[lines.xy3]
drive = ["q3"]
[lines.zc12]
flux = ["q1_q2_c"]
"""


@pytest.fixture(scope="module")
def roster() -> Roster:
    return parse_components(EXAMPLE)


# ------------------------------------------------------------- the expansion

def test_expanded_names_match_the_design_doc(roster):
    declared_and_minted = {
        # declared modes + composite + lines
        "q1", "q2", "q3", "q1_q2_c", "q1_q2",
        "fl1", "xy1", "z1", "xyz2", "xy3", "zc12",
        # minted resonators, the declared operation
        "q1_res", "q2_res", "q3_res", "q1_q2.iswap",
    }
    designed = {"fl1.q1", "fl1.q2", "fl1.q3", "xy1.q1", "xyz2.q2", "xy3.q3",
                "z1.q1", "zc12.q1_q2_c"}
    assert set(roster.channels()) == designed
    assert set(roster.entities) - designed - set(
        roster.borrowed_channels()) == declared_and_minted


def test_borrowed_channels_reach_every_drivable_mode_on_every_drive_line(roster):
    """Every drive line lends itself to every mode it does not carry by design
    - no declaration needed, and the line is in the name."""
    assert set(roster.borrowed_channels()) == {
        "xy1.q2", "xy1.q3", "xy1.q1_q2_c",
        "xyz2.q1", "xyz2.q3", "xyz2.q1_q2_c",
        "xy3.q1", "xy3.q2", "xy3.q1_q2_c"}
    ch = roster.entities["xy1.q1_q2_c"]
    assert ch.borrowed and ch.kind == "drive" and ch.line == "xy1"
    fields = roster.fields_of("xy1.q1_q2_c")
    assert {"drive_freq_hz", "pi_amp", "pi_duration_s"} <= set(fields)
    # the target's own business stays on its designed channel
    assert "thermalization_time_s" not in fields
    assert "parity_delta_f_hz" not in fields
    with pytest.raises(RosterError, match="BORROWED"):
        roster.spec("xy1.q1_q2_c", "thermalization_time_s")
    # a borrowed channel is not wiring: no derived op, no default, no lock
    assert "rx" not in roster.operations("q1_q2_c")
    assert "xy1.q1_q2_c" not in roster.signatures()


def test_a_borrowed_channel_must_be_named_with_its_line(roster):
    with pytest.raises(RosterError, match=r"xy1\.q1_q2_c\.pi_amp.*xyz2"):
        roster.resolve_field("q1_q2_c", "pi_amp")
    assert roster.resolve_field("xyz2.q1_q2_c", "pi_amp")[0] == "xyz2.q1_q2_c"


def test_minted_resonators_carry_the_qubit_ref(roster):
    res = roster.entities["q2_res"]
    assert isinstance(res, Mode) and res.kind == "resonator"
    assert res.refs == {"qubit": "q2"}
    assert res.derived is not None and res.derived.line == "fl1"


def test_minted_readout_channel_binds_its_resonator(roster):
    ro = roster.entities["fl1.q3"]
    assert isinstance(ro, Channel)
    assert ro.via == "q3_res" and ro.target == ("q3",) and ro.line == "fl1"


def test_combined_wire_is_one_channel_with_two_kinds(roster):
    """One (line, target) is one channel whatever it carries: the drive knobs
    and the flux transfer function share xyz2.q2, the flux bias is the line's."""
    ch = roster.entities["xyz2.q2"]
    assert ch.kinds == ("drive", "flux") and ch.kind == "drive"
    fields = roster.fields_of("xyz2.q2")
    assert {"pi_amp", "flux_per_phi0"} <= set(fields)
    assert "idle_flux" in roster.fields_of("xyz2")
    assert roster.default_channel("q2", "drive") == "xyz2.q2"
    assert roster.default_channel("q2", "flux") == "xyz2.q2"


def test_same_kind_twice_on_one_channel_is_a_load_error():
    _expect(EXAMPLE + '\n[channels.x]\nkind = "drive"\ntarget = "q1"\n'
            'line = "xy1"\n', "a second drive channel 'xy1.q1'")


def test_drive_and_readout_never_share_one_channel():
    _expect(EXAMPLE + '\n[channels.x]\nkind = "readout"\ntarget = "q1"\n'
            'line = "xy1"\nvia = "q1_res"\n', "at most one function with knobs")


def test_derived_operations_keyed_on_kind(roster):
    assert set(roster.operations("q1")) == {"rx", "readout", "flux_bias"}
    assert set(roster.operations("q3")) == {"rx", "readout"}
    assert set(roster.operations("q1_q2_c")) == {"flux_bias"}
    assert roster.operations("q1_q2") == ("iswap",)


def test_default_addressing_slots(roster):
    assert roster.default_channel("q1", "drive") == "xy1.q1"
    assert roster.default_channel("q1_q2_c", "flux") == "zc12.q1_q2_c"
    with pytest.raises(RosterError, match="no unique readout"):
        roster.default_channel("q1_q2_c", "readout")


# ------------------------------------------------------------- legal fields

def test_flux_line_holds_the_bias_and_the_channel_the_transfer(roster):
    assert set(roster.fields_of("z1")) == {
        "idle_flux", "flux_delay_s", "distortion_amp", "distortion_tau_s"}
    assert set(roster.fields_of("z1.q1")) == {"flux_offset", "flux_per_phi0"}
    assert roster.fields_of("z1")["idle_flux"].role == "knob"
    assert roster.fields_of("z1")["distortion_amp"].role == "fact"


def test_shorthand_reaches_the_line_through_the_designed_channel(roster):
    assert roster.resolve_field("q1", "idle_flux")[0] == "z1"
    assert roster.resolve_field("q1", "flux_per_phi0")[0] == "z1.q1"
    assert roster.resolve_field("q1", "pi_amp")[0] == "xy1.q1"
    assert roster.resolve_field("q1", "f_dress0_hz")[0] == "q1_res"
    assert roster.resolve_field("q1_q2_c", "idle_flux")[0] == "zc12"


def test_operations_are_entities_of_their_own(roster):
    fields = roster.fields_of("q1_q2.iswap")
    assert "coupler_flux" in fields
    assert "waveform" in fields and "waveform_dt_s" in fields
    assert "coupler_flux" not in roster.fields_of("q1_q2")
    with pytest.raises(RosterError, match="operation 'cz' is not declared"):
        roster.spec("q1_q2.cz", "amp")
    with pytest.raises(RosterError, match=r"q1_q2\.iswap\.coupler_flux"):
        roster.spec("q1_q2", "coupler_flux")


def test_per_leg_couplings_legal_on_single_coupler_pair(roster):
    assert "j_high_c_hz" in roster.fields_of("q1_q2")


def test_design_only_fields_excluded_from_store_legality():
    text = EXAMPLE.replace('kind = "transmon"', 'kind = "fluxonium"', 1)
    r = parse_components(text)
    assert "n_jj" not in r.fields_of("q3")
    assert "n_jj" in r.fields_of("q3", design=True)


def test_drive_and_readout_lines_have_no_fields(roster):
    assert roster.fields_of("fl1") == {}
    assert roster.fields_of("xy1") == {}


def test_spec_gives_exact_cause_on_unknown_field(roster):
    with pytest.raises(RosterError, match="unknown field"):
        roster.spec("q1", "readout_freq_hz")  # channel field, not mode field


# ----------------------------------------------------------- load errors

def _expect(text: str, match: str) -> None:
    with pytest.raises(RosterError, match=match):
        parse_components(text)


def test_flux_rider_on_fixed_transmon_is_a_load_error():
    _expect(EXAMPLE + '\n[lines.z3]\nflux = ["q3"]\n',
            r"no \(flux x transmon\) row")


def test_explicit_flux_channel_on_fixed_transmon_same_error():
    _expect(EXAMPLE + '\n[channels.q3_sneak]\n'
            'kind = "flux"\ntarget = "q3"\nline = "z1"\n',
            r"no \(flux x transmon\) row")


def test_pump_rider_is_refused():
    _expect(EXAMPLE + '\n[lines.p1]\npump = ["q1"]\n', "explicit-only")


def test_a_second_designed_line_makes_the_shorthand_ambiguous():
    """Two designed drive lines to one qubit are legal wiring; the shorthand
    then refuses and the channel must be named."""
    r = parse_components(EXAMPLE + '\n[lines.xyB]\ndrive = ["q1"]\n')
    assert {"xy1.q1", "xyB.q1"} <= set(r.channels())
    with pytest.raises(RosterError, match="several drive channels"):
        r.resolve_field("q1", "pi_amp")


def test_a_second_readout_rider_for_one_qubit_needs_the_hatch():
    _expect(EXAMPLE + '\n[lines.fl2]\nreadout = ["q1"]\n',
            "a second readout rider")


def test_declared_name_colliding_with_minted_resonator():
    _expect(EXAMPLE + '\n[modes.q1_res]\nkind = "resonator"\nqubit = "q1"\n',
            "one name, one entity")


def test_an_entity_may_not_be_named_like_a_field():
    """In the nested store files a key under a line or a composite is a field
    or a sub-entity - the two vocabularies may never meet."""
    _expect(EXAMPLE.replace("[lines.z1]", "[lines.idle_flux]").replace(
        'line = "z1"', 'line = "idle_flux"'), "is a field name")


def test_a_channel_label_may_not_reuse_an_entity_name():
    _expect(EXAMPLE + '\n[channels.q2]\n'
            'kind = "pump"\ntarget = "q1"\nline = "xy1"\n',
            "one name, one meaning")


def test_via_required_when_no_resonator_matches():
    _expect(EXAMPLE + '\n[channels.c_ro]\n'
            'kind = "readout"\ntarget = "q1_q2_c"\nline = "fl1"\n',
            "no resonator has qubit")


def test_via_required_when_two_resonators_claim_the_qubit():
    extra = ('\n[modes.q1_purcell]\nkind = "resonator"\nqubit = "q1"\n'
             '[lines.fl2]\n'
             '[channels.q1_ro2]\nkind = "readout"\ntarget = "q1"\n'
             'line = "fl2"\n')
    _expect(EXAMPLE + extra, "several resonators claim")


def test_via_is_a_readout_only_key():
    _expect(EXAMPLE + '\n[channels.d2]\nkind = "drive"\ntarget = "q1"\n'
            'line = "xy1"\nvia = "q1_res"\n', "unknown key")


def test_multi_target_readout_requires_via():
    _expect(EXAMPLE + '\n[channels.joint]\nkind = "readout"\n'
            'target = ["q1", "q2"]\nline = "fl1"\n', "multi-target readout")


def test_channel_line_must_be_declared():
    _expect(EXAMPLE + '\n[channels.x]\nkind = "drive"\ntarget = "q1"\n'
            'line = "ghost"\n', "not a declared")


def test_unknown_target_is_a_load_error():
    _expect(EXAMPLE.replace('drive = ["q3"]', 'drive = ["q9"]'),
            "not a mode or composite")


def test_role_arity_scalar_only_roles():
    _expect(EXAMPLE.replace('high       = "q1"',
                            'high       = ["q1", "q2"]'),
            "exactly one name")


def test_missing_required_role():
    _expect(EXAMPLE.replace('low        = "q2"\n', ''), "requires the 'low'")


def test_design_section_is_refused_with_a_pointer():
    _expect(EXAMPLE + '\n[design.q1]\nf_q_max_hz = 5.0e9\n',
            "design values live in design.toml")


def test_derived_key_is_never_hand_written():
    _expect(EXAMPLE + '\n[channels.x]\nkind = "drive"\ntarget = "q1"\n'
            'line = "xy1"\nderived = true\n', "never hand-written")


def test_schema_stamp_is_required():
    _expect(EXAMPLE.replace("schema = 3", "schema = 1"), "schema = 3 required")


# ------------------------------------------------- multi-target compilation

COIL = EXAMPLE + """
[lines.coil]
[channels.coil_z]
kind   = "flux"
target = ["q1", "q2", "q1_q2_c"]
line   = "coil"
"""


def test_broadcast_flux_coil_one_bias_per_line_facts_per_target():
    """ONE DC offset for the wire; each SQUID it reaches keeps its own
    transfer function on its own channel (the retired __<target> grammar)."""
    r = parse_components(COIL)
    assert set(r.fields_of("coil")) == {
        "idle_flux", "flux_delay_s", "distortion_amp", "distortion_tau_s"}
    for t in ("q1", "q2", "q1_q2_c"):
        ch = r.entities[f"coil.{t}"]
        assert ch.broadcast and ch.origins == {"flux": "coil_z"}
        assert set(r.fields_of(f"coil.{t}")) == {"flux_offset",
                                                 "flux_per_phi0"}
    assert r.fields_of("coil")["distortion_amp"].paired_with == "distortion_tau_s"


def test_broadcast_channels_never_consume_the_default_slot():
    r = parse_components(COIL)
    assert r.default_channel("q1", "flux") == "z1.q1"
    assert r.resolve_field("q1", "idle_flux")[0] == "z1"


def test_pump_targets_composites_and_lists():
    text = EXAMPLE + """
[channels.pump_zz]
kind   = "pump"
target = "q1_q2"
line   = "zc12"
"""
    r = parse_components(text)
    assert r.entities["zc12.pump_zz"].target == ("q1_q2",)
    assert "pump_freq_hz" in r.fields_of("zc12.pump_zz")


def test_lock_signatures_are_exactly_the_doc_identity(roster):
    """Doc section 7: the lock compares (name, kind, target(s)) — nothing
    more, so doc-legal post-cut appends never change a frozen signature."""
    sigs = roster.signatures()
    assert "fl1.q1" in sigs and "q1_res" in sigs      # derived names freeze too
    assert sigs["fl1.q1"] == ("Channel", "fl1.q1", ("readout",), ("q1",))
    assert sigs["xyz2.q2"] == ("Channel", "xyz2.q2", ("drive", "flux"), ("q2",))
    assert sigs["q1_q2"] == ("Composite", "q1_q2", "qubit_pair")
    assert sigs["q1_res"] == ("Mode", "q1_res", "resonator")
    # operations (declared on their composite) and borrowed channels are
    # never locked
    assert "q1_q2.iswap" not in sigs and "xy1.q2" not in sigs
    # Appending an operation to a frozen composite is a legal append.
    r2 = parse_components(EXAMPLE.replace('operations = ["iswap"]',
                                          'operations = ["iswap", "cz"]'))
    assert r2.signatures()["q1_q2"] == sigs["q1_q2"]


# --------------------------------------------------- review-added contracts

def test_retired_is_legal_in_every_section():
    text = EXAMPLE.replace('[modes.q3]\nkind = "transmon"',
                           '[modes.q3]\nkind = "transmon"\nretired = true')
    r = parse_components(text)
    assert r.entities["q3"].retired and not r.entities["q1"].retired


def test_readout_rider_on_a_cavity_names_the_actionable_fix():
    text = EXAMPLE + ('\n[modes.mem]\nkind = "cavity"\n'
                      '[lines.flm]\nreadout = ["mem"]\n')
    _expect(text, "explicit \\[channels")


def test_cavity_readout_works_through_the_explicit_hatch():
    text = EXAMPLE + ('\n[modes.mem]\nkind = "cavity"\n'
                      '[modes.buf]\nkind = "cavity"\n'
                      '[channels.mem_ro]\nkind = "readout"\n'
                      'target = "mem"\nline = "fl1"\nvia = "buf"\n')
    r = parse_components(text)
    assert r.entities["fl1.mem"].via == "buf"
    assert "readout" in r.operations("mem")


def test_coupler_read_through_neighbor_resonator():
    """The doc's flagship escape-hatch example, positively."""
    r = parse_components(EXAMPLE + """
[channels.q1_q2_c_ro]
kind   = "readout"
target = "q1_q2_c"
line   = "fl1"
via    = "q1_res"
""")
    ch = r.entities["fl1.q1_q2_c"]
    assert ch.via == "q1_res" and ch.line == "fl1"
    assert ch.origins == {"readout": "q1_q2_c_ro"}
    assert "readout" in r.operations("q1_q2_c")
    assert r.default_channel("q1_q2_c", "readout") == "fl1.q1_q2_c"


def test_an_operation_may_not_be_named_like_a_field():
    _expect(EXAMPLE.replace('operations = ["iswap"]', 'operations = ["zz_hz"]'),
            "is a field name")
    # no flattening any more: 'pi' is a fine operation name now
    r = parse_components(EXAMPLE.replace('operations = ["iswap"]',
                                         'operations = ["pi"]'))
    assert "q1_q2.pi" in r.entities


def test_entity_cannot_fill_two_roles():
    _expect(EXAMPLE.replace('low        = "q2"', 'low        = "q1"'),
            "one entity, one role")


def test_non_table_section_is_a_roster_error():
    _expect("schema = 3\nmodes = 3\n", "must be a table")


def test_operations_accepts_a_scalar():
    r = parse_components(EXAMPLE.replace('operations = ["iswap"]',
                                         'operations = "iswap"'))
    assert r.operations("q1_q2") == ("iswap",)


def test_declared_declared_collision_names_both_sections():
    _expect(EXAMPLE + '\n[composites.q3]\nkind = "qubit_pair"\n'
            'high = "q1"\nlow = "q2"\n',
            r"\[modes.q3\].*\[composites.q3\]")


def test_design_only_store_write_names_the_cause():
    r = parse_components(EXAMPLE.replace('kind = "transmon"',
                                         'kind = "fluxonium"', 1))
    with pytest.raises(RosterError, match="design.toml-only"):
        r.spec("q3", "n_jj")


def test_pump_list_may_not_contain_composites():
    _expect(EXAMPLE + '\n[channels.p2]\nkind = "pump"\n'
            'target = ["q1", "q1_q2"]\nline = "zc12"\n', "spelled alone")


def test_via_must_be_a_mode():
    _expect(EXAMPLE + '\n[lines.fl2]\n[channels.x_ro]\nkind = "readout"\n'
            'target = "q1"\nline = "fl2"\nvia = "fl1"\n', "not a declared mode")


def test_dunder_names_are_reserved_for_the_field_grammar():
    _expect(EXAMPLE.replace("[modes.q3]", "[modes.q__3]"),
            "invalid entity name")


def test_minted_entity_errors_cite_provenance_not_phantom_sections():
    text = EXAMPLE.replace('drive = ["q3"]', 'drive = ["q9"]')
    with pytest.raises(RosterError, match=r"\[lines.xy3\] drive\[0\]"):
        parse_components(text)


def test_composite_cycle_is_a_load_error():
    from scqo import Composite
    from scqo.roster import _check_dag
    a = Composite(name="a", kind="qubit_pair", roles={"high": ("b",)})
    b = Composite(name="b", kind="qubit_pair", roles={"high": ("a",)})
    with pytest.raises(RosterError, match="cycle"):
        _check_dag({"a": a, "b": b})


def test_bom_is_tolerated_in_the_hand_edited_file(tmp_path):
    from scqo import load_components
    p = tmp_path / "components.toml"
    p.write_bytes(b"\xef\xbb\xbf" + EXAMPLE.encode())
    assert "q1" in load_components(p).entities


# ------------------------------------------------ refusals that teach 4.0.0

def test_a_3x_name_says_where_it_went(roster):
    """4.0.0 never accepts a 3.x name; the refusal names the new address."""
    with pytest.raises(RosterError, match=r"3\.x name of z1\.q1, with .*"
                                          r"idle_flux on its line z1"):
        roster.fields_of("q1_z")
    with pytest.raises(RosterError, match=r"3\.x name of xy1\.q1 "):
        roster.resolve_field("q1_xy", "pi_amp")
    with pytest.raises(RosterError, match=r"3\.x name of xyz2\.q2 "):
        roster.resolve_field("q2_xy", "pi_amp")
    with pytest.raises(RosterError,
                       match=r"3\.x spelling of q1_q2\.iswap\.coupler_flux"):
        roster.resolve_field("q1_q2", "iswap_coupler_flux")


def test_a_borrowed_route_refuses_a_target_owned_field_exactly(roster):
    with pytest.raises(RosterError, match="is a BORROWED channel and "
                       "thermalization_time_s belongs to 'q1_q2_c'"):
        roster.resolve_field("xy1.q1_q2_c", "thermalization_time_s")


def test_a_lone_broadcast_channel_is_not_called_ambiguous():
    r = parse_components(
        'schema = 3\n[modes.a]\nkind = "flux_transmon"\n'
        '[modes.b]\nkind = "flux_transmon"\n[lines.coil]\n'
        '[channels.coil_z]\nkind = "flux"\ntarget = ["a", "b"]\nline = "coil"\n')
    with pytest.raises(RosterError, match=r"'coil\.a' is a broadcast channel, "
                                          r"which never answers the shorthand "
                                          r"- address coil\.idle_flux"):
        r.resolve_field("a", "idle_flux")
    with pytest.raises(RosterError, match=r"address coil\.a\.flux_per_phi0"):
        r.resolve_field("a", "flux_per_phi0")


def test_the_borrowed_routes_in_use_are_listed_first(roster):
    with pytest.raises(RosterError) as plain:
        roster.resolve_field("q1_q2_c", "pi_amp")
    text = str(plain.value)
    assert text.index("xy1.q1_q2_c") < text.index("xyz2.q1_q2_c")  # by name
    with pytest.raises(RosterError) as used:
        roster.resolve_field("q1_q2_c", "pi_amp",
                             valued=lambda e, f: e == "xyz2.q1_q2_c")
    text = str(used.value)
    assert text.index("xyz2.q1_q2_c") < text.index("xy1.q1_q2_c")
