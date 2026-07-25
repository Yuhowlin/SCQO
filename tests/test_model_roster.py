"""Roster loader contracts (scqo.roster) — docs/greenfield-schema.md
sections 3-5, 7 + the section-8 worked example as the primary fixture."""

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
    assert set(roster.entities) == {
        # declared modes + composite + lines
        "q1", "q2", "q3", "q1_q2_c", "q1_q2",
        "fl1", "xy1", "z1", "xyz2", "xy3", "zc12",
        # minted resonators + channels
        "q1_res", "q2_res", "q3_res",
        "q1_ro", "q2_ro", "q3_ro",
        "q1_xy", "q2_xy", "q3_xy",
        "q1_z", "q2_z", "q1_q2_c_z",
    }


def test_minted_resonators_carry_the_qubit_ref(roster):
    res = roster.entities["q2_res"]
    assert isinstance(res, Mode) and res.kind == "resonator"
    assert res.refs == {"qubit": "q2"}
    assert res.derived is not None and res.derived.line == "fl1"


def test_minted_readout_channel_binds_its_resonator(roster):
    ro = roster.entities["q3_ro"]
    assert isinstance(ro, Channel)
    assert ro.via == "q3_res" and ro.target == ("q3",) and ro.line == "fl1"


def test_combined_wire_two_channels_one_line(roster):
    assert roster.entities["q2_xy"].line == "xyz2"
    assert roster.entities["q2_z"].line == "xyz2"


def test_derived_operations_keyed_on_kind(roster):
    assert set(roster.operations("q1")) == {"rx", "readout", "flux_bias"}
    assert set(roster.operations("q3")) == {"rx", "readout"}
    assert set(roster.operations("q1_q2_c")) == {"flux_bias"}
    assert roster.operations("q1_q2") == ("iswap",)


def test_default_addressing_slots(roster):
    assert roster.default_channel("q1", "drive") == "q1_xy"
    assert roster.default_channel("q1_q2_c", "flux") == "q1_q2_c_z"
    with pytest.raises(RosterError, match="no unique readout"):
        roster.default_channel("q1_q2_c", "readout")


# ------------------------------------------------------------- legal fields

def test_flux_channel_spans_both_stores(roster):
    fields = roster.fields_of("q1_z")
    assert {"idle_flux", "flux_offset", "flux_per_phi0"} <= set(fields)


def test_operations_instantiate_full_name_knobs(roster):
    fields = roster.fields_of("q1_q2")
    assert "iswap_coupler_flux" in fields
    assert "iswap_waveform" in fields and "iswap_waveform_dt_s" in fields
    assert "cz_coupler_flux" not in fields  # cz not declared
    with pytest.raises(RosterError, match="operation 'cz' is not declared"):
        roster.spec("q1_q2", "cz_amp")


def test_per_leg_couplings_legal_on_single_coupler_pair(roster):
    assert "j_high_c_hz" in roster.fields_of("q1_q2")


def test_design_only_fields_excluded_from_store_legality():
    text = EXAMPLE.replace('kind = "transmon"', 'kind = "fluxonium"', 1)
    r = parse_components(text)
    assert "n_jj" not in r.fields_of("q3")
    assert "n_jj" in r.fields_of("q3", design=True)


def test_lines_have_no_fields(roster):
    assert roster.fields_of("fl1") == {}


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


def test_second_same_kind_rider_collides_with_provenance():
    _expect(EXAMPLE + '\n[lines.xyB]\ndrive = ["q1"]\n',
            "already minted")


def test_declared_name_colliding_with_minted_resonator():
    _expect(EXAMPLE + '\n[modes.q1_res]\nkind = "resonator"\nqubit = "q1"\n',
            "one name, one entity")


def test_line_name_colliding_with_derived_channel():
    _expect(EXAMPLE.replace("[lines.z1]", "[lines.q1_z]"),
            "one name, one entity")


def test_explicit_channel_colliding_with_derived_name():
    _expect(EXAMPLE + '\n[channels.q1_xy]\n'
            'kind = "drive"\ntarget = "q1"\nline = "xy1"\n',
            "one name, one entity")


def test_via_required_when_no_resonator_matches():
    _expect(EXAMPLE + '\n[channels.c_ro]\n'
            'kind = "readout"\ntarget = "q1_q2_c"\nline = "fl1"\n',
            "no resonator has qubit")


def test_via_required_when_two_resonators_claim_the_qubit():
    extra = ('\n[modes.q1_purcell]\nkind = "resonator"\nqubit = "q1"\n'
             '[channels.q1_ro2]\nkind = "readout"\ntarget = "q1"\n'
             'line = "fl1"\n')
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


def test_broadcast_flux_channel_one_knob_per_target_facts():
    r = parse_components(COIL)
    fields = r.fields_of("coil_z")
    assert "idle_flux" in fields                      # ONE knob
    assert "flux_offset" not in fields                # bare per-target fact illegal
    assert "flux_per_phi0__q1" in fields              # __<target> instances
    assert "flux_per_phi0__q1_q2_c" in fields
    # paired arrays re-point per target — the equal-length check never dangles
    assert fields["distortion_amp__q1"].paired_with == "distortion_tau_s__q1"


def test_multi_target_channel_never_consumes_the_default_slot():
    r = parse_components(COIL)
    assert r.default_channel("q1", "flux") == "q1_z"


def test_pump_targets_composites_and_lists():
    text = EXAMPLE + """
[channels.pump_zz]
kind   = "pump"
target = "q1_q2"
line   = "zc12"
"""
    r = parse_components(text)
    assert r.entities["pump_zz"].target == ("q1_q2",)
    assert "pump_freq_hz" in r.fields_of("pump_zz")


def test_lock_signatures_are_exactly_the_doc_identity(roster):
    """Doc section 7: the lock compares (name, kind, target(s)) — nothing
    more, so doc-legal post-cut appends never change a frozen signature."""
    sigs = roster.signatures()
    assert "q1_ro" in sigs and "q1_res" in sigs      # derived names freeze too
    assert sigs["q1_ro"] == ("Channel", "q1_ro", "readout", ("q1",))
    assert sigs["q1_q2"] == ("Composite", "q1_q2", "qubit_pair")
    assert sigs["q1_res"] == ("Mode", "q1_res", "resonator")
    assert "fl1" not in sigs["q1_ro"]                # line/provenance excluded
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
    assert r.entities["mem_ro"].via == "buf"
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
    ch = r.entities["q1_q2_c_ro"]
    assert ch.via == "q1_res" and ch.line == "fl1"
    assert "readout" in r.operations("q1_q2_c")
    assert r.default_channel("q1_q2_c", "readout") == "q1_q2_c_ro"


def test_operation_name_colliding_with_static_catalogs_is_refused():
    _expect(EXAMPLE.replace('operations = ["iswap"]', 'operations = ["pi"]'),
            "already exist in the static catalogs")


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
    _expect(EXAMPLE + '\n[channels.x_ro]\nkind = "readout"\ntarget = "q1"\n'
            'line = "fl1"\nvia = "fl1"\n', "not a declared mode")


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
