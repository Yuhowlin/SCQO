"""Kind-catalog invariants of the greenfield model (scqo.catalog).

These tests pin the schema contracts of docs/greenfield-schema.md sections 2,
6, 7 and the 4.0.0 store-by-line split (docs/store-by-line-plan.md section 2.4) —
the catalogs themselves are data, so most protection lives in the import-time
lints; here we verify the lints exist (a broken catalog refuses to build) and
the load-bearing lookups behave.
"""

import pytest

from scqo.catalog import (
    ALL_FIELD_NAMES,
    BORROWABLE,
    CHANNELS,
    COMPOSITES,
    DERIVATION,
    FLUX_TUNABLE,
    MODES,
    OPERATION_FIELDS,
    QUBIT_LIKE,
    FieldSpec,
    _validate_fields,
    derived_op,
)


# ------------------------------------------------------------------ routing

def test_role_routes_every_field_to_exactly_one_store():
    for family in (MODES, COMPOSITES, CHANNELS):
        for kind, spec in family.items():
            for name, fs in spec.fields.items():
                assert fs.role in ("fact", "knob", "monitor"), (kind, name)
    for kind, spec in CHANNELS.items():
        for name, fs in spec.line_fields.items():
            assert fs.role in ("fact", "knob", "monitor"), (kind, name)
    # gate knobs, plus the one measured read-back the chain analysis consults
    assert {f for f, fs in OPERATION_FIELDS.items() if fs.role != "knob"} == {
        "theta_rad"}
    assert OPERATION_FIELDS["theta_rad"].role == "monitor"


def test_modes_and_composites_carry_no_knobs():
    """Standing knobs live on lines and channels; a composite's gate knobs live
    on its declared operations (<pair>.<op>). Mode kinds are pure facts."""
    for kind, spec in MODES.items():
        assert all(fs.role == "fact" for fs in spec.fields.values()), kind
    for kind, spec in COMPOSITES.items():
        assert all(fs.role == "fact" for fs in spec.fields.values()), kind


def test_flux_line_and_flux_channel_split_the_old_flux_channel():
    """4.0.0: what exists once per WIRE (the DC offset, the delay, the impulse
    response) belongs to the flux LINE; the per-target transfer function to the
    channel <line>.<target>. A line still spans both stores."""
    line = {n: fs.role for n, fs in CHANNELS["flux"].line_fields.items()}
    assert line == {"idle_flux": "knob", "flux_delay_s": "knob",
                    "distortion_amp": "fact", "distortion_tau_s": "fact"}
    channel = {n: fs.role for n, fs in CHANNELS["flux"].fields.items()}
    assert channel == {"flux_offset": "fact", "flux_per_phi0": "fact"}
    for kind in ("drive", "readout", "pump"):
        assert CHANNELS[kind].line_fields == {}, kind


def test_flux_offset_declares_its_reference_plane():
    """``flux_offset`` is written by BOTH an absolute-frame experiment
    (resonator_spectroscopy_flux, which sets the DC offset) and idle-relative
    ones (the ``_pulse`` family, which play on top of the standing bias). One
    slot, one plane — so the FieldSpec doc must say which, and say that a
    relative carrier re-references before writing. Losing that sentence is how
    the two frames silently start sharing a slot again."""
    offset_doc = CHANNELS["flux"].fields["flux_offset"].doc
    assert "ABSOLUTE" in offset_doc
    assert "idle_flux" in offset_doc
    assert "re-reference" in offset_doc
    # and the knob names itself as the origin the relative frame measures from
    assert "ORIGIN" in CHANNELS["flux"].line_fields["idle_flux"].doc


# ---------------------------------------------------------------- vocabulary

def test_unit_suffix_convention_holds_and_is_enforced():
    # The two renamed hot knobs conform.
    assert "drive_freq_hz" in CHANNELS["drive"].fields
    assert "readout_freq_hz" in CHANNELS["readout"].fields
    # Flux set-points are the stated source-native exemption (no _v lie).
    assert CHANNELS["flux"].line_fields["idle_flux"].unit == "source-native"
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
    all_fields = set(ALL_FIELD_NAMES)
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
    assert not CHANNELS["pump"].rider
    assert not any(ch == "pump" for ch, _ in DERIVATION)


def test_riders_declare_drive_readout_flux():
    assert [k for k, s in CHANNELS.items() if s.rider] == [
        "drive", "readout", "flux"]


def test_the_pre_4_0_suffix_map_stays_frozen_for_old_data():
    """Old run data names channels q1_xy / q1_ro / q1_z forever; reading it is
    the one place those names are still understood (scqo.v3_names)."""
    from scqo.v3_names import V3_RIDER_SUFFIXES
    assert V3_RIDER_SUFFIXES == {"drive": "_xy", "readout": "_ro", "flux": "_z"}


def test_only_drive_is_borrowable_and_the_targets_own_fields_stay_home():
    """A borrowed channel (a coupler through a neighbour's line) carries the
    route's own knobs, never what belongs to the target itself."""
    assert BORROWABLE == ("drive",)
    assert CHANNELS["drive"].designed_only == {"thermalization_time_s",
                                              "parity_delta_f_hz"}
    assert all(not s.designed_only for k, s in CHANNELS.items() if k != "drive")


def test_via_is_readout_only():
    assert CHANNELS["readout"].via_ok
    assert not CHANNELS["drive"].via_ok
    assert not CHANNELS["flux"].via_ok
    assert not CHANNELS["pump"].via_ok


def test_flux_tunable_mirrors_the_table():
    assert set(FLUX_TUNABLE) == {mk for ch, mk in DERIVATION if ch == "flux"}


# ------------------------------------------------------------------ families

def test_operation_fields_are_the_gate_knobs():
    """An operation's fields are plain names on <pair>.<op> - no flattening."""
    assert "coupler_flux" in OPERATION_FIELDS
    assert "drive_freq_hz" in OPERATION_FIELDS
    assert "waveform" in OPERATION_FIELDS and "waveform_dt_s" in OPERATION_FIELDS
    order = list(OPERATION_FIELDS)
    assert order.index("waveform_dt_s") < order.index("waveform")  # dt first


def test_waveform_arrays_require_dt_companion():
    with pytest.raises(AssertionError, match="_dt_s"):
        _validate_fields("t", {
            "x_waveform": FieldSpec("", "d", role="knob", shape="float[]")})
    with pytest.raises(AssertionError, match="_dt_s"):
        _validate_fields("t", {
            "waveform": FieldSpec("", "d", role="knob", shape="float[]")})


def test_field_names_never_carry_a_dot_or_dunder():
    """A dot separates an entity from its field in an address; __ was the
    retired parameter grammar."""
    for bad in ("a.b", "a__b"):
        with pytest.raises(AssertionError, match="never appear"):
            _validate_fields("t", {bad: FieldSpec("", "d", role="knob")})
    assert not any("." in n or "__" in n for n in ALL_FIELD_NAMES)


def test_paired_arrays_are_declared_and_typed():
    flux = CHANNELS["flux"].line_fields
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
        for name in (*spec.fields, *spec.line_fields):
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


def test_parity_fields_declared():
    """The parity pair: the rate is a transmon-family FACT (like t1_s), the
    beat splitting a drive-channel MONITOR (it drifts with the offset charge,
    so it is not chip-in-the-dark physics). Fluxonium is deliberately excluded
    — no charge-Ramsey beat exists there to derive the idle from."""
    from scqo.catalog import CHANNELS, MODES

    fact = MODES["transmon"].fields["parity_rate_hz"]
    assert fact.role == "fact" and fact.unit == "Hz"
    assert "parity_rate_hz" in MODES["flux_transmon"].fields
    assert "parity_rate_hz" not in MODES["fluxonium"].fields

    monitor = CHANNELS["drive"].fields["parity_delta_f_hz"]
    assert monitor.role == "monitor" and monitor.unit == "Hz"
