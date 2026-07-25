"""Kind-catalog invariants of the greenfield model (scqo.model.catalog).

These tests pin the schema contracts of docs/greenfield-schema.md sections 2,
6, 7 — the catalogs themselves are data, so most protection lives in the
import-time lints; here we verify the lints exist (a broken catalog refuses to
build) and the load-bearing lookups behave.
"""

import pytest

from scqo.model.catalog import (
    CHANNELS,
    COMPOSITES,
    DERIVATION,
    FLUX_TUNABLE,
    MODES,
    OP_KNOBS,
    QUBIT_LIKE,
    FieldSpec,
    _validate_fields,
    derived_op,
    op_knob_fields,
)


# ------------------------------------------------------------------ routing

def test_role_routes_every_field_to_exactly_one_store():
    for family in (MODES, COMPOSITES, CHANNELS):
        for kind, spec in family.items():
            for name, fs in spec.fields.items():
                assert fs.role in ("fact", "knob", "monitor"), (kind, name)


def test_modes_and_composites_carry_no_knobs_except_op_families():
    """Standing knobs live on channels; composites get knobs only via declared
    operations (OP_KNOBS). Mode kinds are pure facts."""
    for kind, spec in MODES.items():
        assert all(fs.role == "fact" for fs in spec.fields.values()), kind
    for kind, spec in COMPOSITES.items():
        assert all(fs.role == "fact" for fs in spec.fields.values()), kind


def test_flux_channel_spans_both_stores():
    """The mixed-store entity that justified per-field routing: q*_z holds
    transfer-function facts AND the idle knob under one name."""
    roles = {n: fs.role for n, fs in CHANNELS["flux"].fields.items()}
    assert roles["idle_flux"] == "knob"
    assert roles["flux_offset"] == "fact"
    assert roles["flux_per_phi0"] == "fact"


# ---------------------------------------------------------------- vocabulary

def test_unit_suffix_convention_holds_and_is_enforced():
    # The two renamed hot knobs conform.
    assert "drive_freq_hz" in CHANNELS["drive"].fields
    assert "readout_freq_hz" in CHANNELS["readout"].fields
    # Flux set-points are the stated source-native exemption (no _v lie).
    assert CHANNELS["flux"].fields["idle_flux"].unit == "source-native"
    # The lint actually fires on a violating field — in BOTH directions.
    with pytest.raises(AssertionError, match="unit"):
        _validate_fields("t", {"bad_freq": FieldSpec("Hz", "d", role="knob")})
    with pytest.raises(AssertionError, match="promises unit"):
        _validate_fields("t", {"bad_hz": FieldSpec("", "d", role="knob")})


def test_design_ok_is_fact_only_and_context_free():
    with pytest.raises(AssertionError, match="design_ok"):
        _validate_fields("t", {"k_hz": FieldSpec("Hz", "d", role="knob",
                                                 design_ok=True)})
    # f_01 designable on fixed, NOT on flux-tunable (bias-dependent there).
    assert MODES["transmon"].fields["f_01_hz"].design_ok
    assert not MODES["flux_transmon"].fields["f_01_hz"].design_ok
    assert MODES["flux_transmon"].fields["f_q_max_hz"].design_ok


def test_n_jj_is_design_only():
    fs = MODES["fluxonium"].fields["n_jj"]
    assert fs.design_only and fs.design_ok


def test_deleted_vocabulary_stayed_deleted():
    """The audit's unearned features must not creep back without a writer."""
    all_fields = {n for f in (MODES, COMPOSITES, CHANNELS)
                  for s in f.values() for n in s.fields}
    for dead in ("readout_fidelity", "drive_phase_rad", "centroids",
                 "pi_ef_amp", "threshold_ef", "coupler_decouple_v",
                 "coupler_interaction_v", "idle_flux_v", "v_offset_v",
                 "v_per_phi0_v", "drive_freq", "readout_freq"):
        assert dead not in all_fields, dead
    assert "e_l_hz" not in MODES["flux_transmon"].fields
    assert "e_l_hz" in MODES["fluxonium"].fields


# ------------------------------------------------- the (channel x mode) table

def test_derivation_rows_match_the_design_doc():
    assert derived_op("drive", "transmon") == "rx"
    assert derived_op("drive", "fluxonium") == "rx"
    assert derived_op("drive", "cavity") == "displace"
    assert derived_op("readout", "cavity") == "readout"
    assert derived_op("flux", "flux_transmon") == "flux_bias"


def test_absent_row_is_illegal_capability_by_construction():
    with pytest.raises(KeyError):
        derived_op("flux", "transmon")     # fixed-frequency: no flux, ever
    with pytest.raises(KeyError):
        derived_op("flux", "cavity")
    with pytest.raises(KeyError):
        derived_op("drive", "resonator")


def test_pump_is_any_target_no_op_and_explicit_only():
    assert derived_op("pump", "cavity") is None
    assert derived_op("pump", "transmon") is None
    assert CHANNELS["pump"].rider_suffix is None
    assert not any(ch == "pump" for ch, _ in DERIVATION)


def test_rider_suffixes_are_the_frozen_map():
    assert CHANNELS["drive"].rider_suffix == "_xy"
    assert CHANNELS["readout"].rider_suffix == "_ro"
    assert CHANNELS["flux"].rider_suffix == "_z"


def test_via_is_readout_only():
    assert CHANNELS["readout"].via_ok
    assert not CHANNELS["drive"].via_ok
    assert not CHANNELS["flux"].via_ok
    assert not CHANNELS["pump"].via_ok


def test_flux_tunable_mirrors_the_table():
    assert set(FLUX_TUNABLE) == {mk for ch, mk in DERIVATION if ch == "flux"}


# ------------------------------------------------------------------ families

def test_op_knob_family_instantiates_full_names():
    fields = op_knob_fields("cz")
    assert "cz_coupler_flux" in fields
    assert "cz_drive_freq_hz" in fields
    assert "cz_waveform" in fields and "cz_waveform_dt_s" in fields
    assert all(fs.role == "knob" for fs in fields.values())


def test_waveform_arrays_require_dt_companion():
    with pytest.raises(AssertionError, match="_dt_s"):
        _validate_fields("t", {
            "x_waveform": FieldSpec("", "d", role="knob", shape="float[]")})


def test_paired_arrays_are_declared_and_typed():
    flux = CHANNELS["flux"].fields
    assert flux["distortion_amp"].paired_with == "distortion_tau_s"
    assert flux["distortion_tau_s"].shape == "float[]"
    with pytest.raises(AssertionError, match="paired_with"):
        _validate_fields("t", {
            "a": FieldSpec("", "d", role="fact", shape="float[]",
                           paired_with="missing")})


# ------------------------------------------------------------------ addressing

def test_field_names_unique_across_channel_and_mode_catalogs():
    """The invariant behind `scqo set q1.pi_amp` default addressing."""
    seen = {}
    for kind, spec in CHANNELS.items():
        for name in spec.fields:
            assert name not in seen, (name, seen.get(name), kind)
            seen[name] = kind
    mode_fields = {n for s in MODES.values() for n in s.fields}
    assert not (mode_fields & set(seen))


def test_composite_roles_typed_and_qubit_pair_shape():
    qp = COMPOSITES["qubit_pair"]
    assert set(qp.roles) == {"high", "low", "coupler"}
    assert qp.roles["coupler"].optional and qp.roles["coupler"].list_ok
    assert not qp.roles["high"].list_ok
    assert set(qp.roles["high"].allows) == set(QUBIT_LIKE)
    cat = COMPOSITES["cat_system"]
    assert set(cat.roles) == {"memory", "buffer"}
    assert cat.roles["memory"].allows == ("cavity",)


def test_resonator_ref_is_the_single_dispersive_record():
    assert MODES["resonator"].refs == {"qubit": QUBIT_LIKE}
    assert all(not s.refs for k, s in MODES.items() if k != "resonator")
