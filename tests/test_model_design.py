"""design.toml loader contracts (scqo.design) — the datasheet file of
docs/greenfield-schema.md sections 7-8, validated against the expanded
roster."""

import pytest

from scqo import (
    Design,
    DesignError,
    load_design,
    parse_design,
    parse_components,
    seed_value,
)
from tests.test_model_roster import EXAMPLE

#: The design doc's section-8 datasheet, verbatim.
DATASHEET = """
schema = 1

[q1]
f_q_max_hz       = 5.15e9
anharmonicity_hz = -2.0e8

[q2]
f_q_max_hz       = 4.90e9
anharmonicity_hz = -2.0e8

[q3]
f_01_hz          = 4.70e9
anharmonicity_hz = -2.1e8

[q1_q2_c]
f_q_max_hz = 7.5e9

[q1_res]
f_r_hz = 5.93e9
g_hz   = 8.0e7

[q2_res]
f_r_hz = 6.02e9

[q3_res]
f_r_hz = 6.10e9

[q1_q2]
j_hz = 1.0e7
"""


@pytest.fixture(scope="module")
def roster():
    return parse_components(EXAMPLE)


@pytest.fixture(scope="module")
def design(roster) -> Design:
    return parse_design(DATASHEET, roster)


def _expect(text, roster, match):
    with pytest.raises(DesignError, match=match):
        parse_design(text, roster)


# ---------------------------------------------------------------- the happy path

def test_worked_example_datasheet_loads(design):
    assert design.get("q1", "f_q_max_hz") == 5.15e9
    assert design.get("q1_q2", "j_hz") == 1.0e7
    assert design.get("q1", "t1_s") is None          # sparse: undeclared = None


def test_design_on_derived_entities_validates_post_expansion(design):
    assert design.get("q3_res", "f_r_hz") == 6.10e9  # q3_res is rider-minted


def test_compare_is_the_key_for_key_join(design):
    measured = {"q1_res": {"f_r_hz": 5.9359e9}}      # a physical.json values block
    rows = {(e, f): (d, m) for e, f, d, m in design.compare(measured)}
    assert rows[("q1_res", "f_r_hz")] == (5.93e9, 5.9359e9)
    assert rows[("q2_res", "f_r_hz")] == (6.02e9, None)   # not yet measured


# ------------------------------------------------------------------- refusals

def test_unknown_entity_is_a_load_error(roster):
    _expect("schema = 1\n[q9]\nf_01_hz = 4.0e9\n", roster, "unknown entity")


def test_f01_design_illegal_on_flux_tunable_points_at_f_q_max(roster):
    # Bias-dependent f_01 is refused AND the context-free alternative shown.
    _expect('schema = 1\n[q1]\nf_01_hz = 5.0e9\n', roster, "f_q_max_hz")


def test_measured_only_facts_are_not_designable(roster):
    _expect('schema = 1\n[q1_res]\nchi_hz = 3.0e5\n', roster,
            "physical.json")


def test_channel_knobs_are_chosen_not_designed(roster):
    _expect('schema = 1\n[q1_ro]\nreadout_freq_hz = 5.9e9\n', roster,
            "never the datasheet")


def test_non_numeric_and_non_finite_refused(roster):
    _expect('schema = 1\n[q1]\nf_q_max_hz = "high"\n', roster,
            "expected a number")
    _expect('schema = 1\n[q1]\nf_q_max_hz = nan\n', roster, "non-finite")
    _expect('schema = 1\n[q1]\nf_q_max_hz = true\n', roster,
            "expected a number")


def test_schema_stamp_required(roster):
    _expect("[q1]\nf_q_max_hz = 5.0e9\n", roster, "schema = 1 required")
    # The stamp is the INTEGER 1: bool/float lookalikes are refused.
    _expect("schema = true\n[q1]\nf_q_max_hz = 5.0e9\n", roster,
            "schema = 1 required")
    _expect("schema = 1.0\n[q1]\nf_q_max_hz = 5.0e9\n", roster,
            "schema = 1 required")


def test_overflowing_integer_fails_as_non_finite(roster):
    _expect("schema = 1\n[q1]\nf_q_max_hz = 1" + "0" * 400 + "\n", roster,
            "non-finite")


def test_store_legal_fields_are_redirected_to_their_store(roster):
    _expect("schema = 1\n[q1_z]\nflux_offset = 0.013\n", roster,
            "physical.json")
    _expect("schema = 1\n[q1_ro]\nreadout_freq_hz = 5.9e9\n", roster,
            "scqo_state.json")


def test_empty_design_vocabulary_says_so(roster):
    _expect("schema = 1\n[fl1]\nf_r_hz = 6.0e9\n", roster,
            "no field is design-legal")


def test_schema_is_a_reserved_entity_name():
    from scqo import RosterError
    with pytest.raises(RosterError, match="reserved word"):
        parse_components(EXAMPLE + '\n[modes.schema]\nkind = "transmon"\n')


def test_design_is_immutable_after_load(design):
    with pytest.raises(TypeError):
        design.values["q1"]["f_q_max_hz"] = 0.0  # type: ignore[index]


def test_seed_lookup_has_one_exception_surface(roster, design):
    with pytest.raises(DesignError, match="unknown entity"):
        seed_value(roster, design, "ghost_xy", "drive_freq_hz")
    with pytest.raises(DesignError, match="unknown field"):
        seed_value(roster, design, "q1_xy", "no_such_knob")


def test_n_jj_is_design_legal_as_integer():
    r = parse_components(EXAMPLE.replace('kind = "transmon"',
                                         'kind = "fluxonium"', 1))
    d = parse_design('schema = 1\n[q3]\nn_jj = 102\ne_l_hz = 8.0e8\n', r)
    assert d.get("q3", "n_jj") == 102.0


# ------------------------------------------------------------------ file layer

def test_missing_file_is_an_empty_datasheet(tmp_path, roster):
    d = load_design(tmp_path / "design.toml", roster)
    assert d.values == {} and d.get("q1", "f_q_max_hz") is None


def test_bom_is_tolerated(tmp_path, roster):
    p = tmp_path / "design.toml"
    p.write_bytes(b"\xef\xbb\xbf" + DATASHEET.encode())
    assert load_design(p, roster).get("q1", "f_q_max_hz") == 5.15e9


# ----------------------------------------------------------------- seeding

def test_drive_seed_hops_to_the_target_fact(roster, design):
    # q3 is fixed-frequency: drive_freq_hz seeds from its design f_01_hz.
    assert seed_value(roster, design, "q3_xy", "drive_freq_hz") == 4.70e9


def test_readout_seed_hops_via_the_resonator(roster, design):
    assert seed_value(roster, design, "q1_ro", "readout_freq_hz") == 5.93e9


def test_drive_seed_candidates_cover_flux_tunables(roster, design):
    # q1 is flux-tunable: no design f_01_hz, but the candidate list falls
    # through to f_q_max_hz — park-at-sweet-spot is the bring-up seed.
    assert seed_value(roster, design, "q1_xy", "drive_freq_hz") == 5.15e9


def test_seed_is_none_when_undeclared_or_sourceless(roster, design):
    # no candidate declared at all -> None (an empty datasheet).
    empty = parse_design("schema = 1", roster)
    assert seed_value(roster, empty, "q1_xy", "drive_freq_hz") is None
    # pi_amp declares no design_source at all.
    assert seed_value(roster, design, "q1_xy", "pi_amp") is None
