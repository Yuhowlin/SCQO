"""Schema-3 store contracts (scqo.stores) — docs/greenfield-schema.md
sections 1, 6, 10: one shape, two role sets, fresh-start archiving, array
relations enforced at write time."""

import json

import pytest

from scqo import (
    StoreError,
    parse_components,
    physical_store,
    state_store,
)
from tests.test_model_roster import EXAMPLE


@pytest.fixture()
def roster():
    return parse_components(EXAMPLE)


@pytest.fixture()
def stores(tmp_path, roster):
    return (physical_store(tmp_path, roster, setup="qm_a"),
            state_store(tmp_path, roster, setup="qm_a"))


# ------------------------------------------------------------- role routing

def test_role_routes_to_exactly_one_store(stores):
    physical, state = stores
    physical.record("q1_res", "f_r_hz", 5.9359e9)      # fact -> physical
    state.record("q1_xy", "drive_freq_hz", 5.136e9)    # knob -> state
    state.record("q1_ro", "fidelity_g", 0.96)          # monitor -> state
    with pytest.raises(StoreError, match="belongs in physical.json"):
        state.record("q1_res", "f_r_hz", 5.9e9)
    with pytest.raises(StoreError, match="belongs in scqo_state.json"):
        physical.record("q1_xy", "drive_freq_hz", 5.1e9)


def test_flux_channel_spans_both_stores(stores):
    physical, state = stores
    physical.record("q1_z", "flux_offset", 0.0134)
    physical.record("q1_z", "flux_per_phi0", 0.969)
    state.record("q1_z", "idle_flux", 0.118)
    assert physical.get("q1_z", "flux_offset") == 0.0134
    assert state.get("q1_z", "idle_flux") == 0.118


def test_design_only_fields_never_reach_a_store(tmp_path):
    r = parse_components(EXAMPLE.replace('kind = "transmon"',
                                         'kind = "fluxonium"', 1))
    with pytest.raises(Exception, match="design.toml-only"):
        physical_store(tmp_path, r).record("q3", "n_jj", 102)


# ---------------------------------------------------------------- values

def test_shape_validation_scalars_and_arrays(stores):
    physical, state = stores
    with pytest.raises(StoreError, match="float\\[\\]"):
        physical.record("q1_z", "distortion_amp", 0.2)   # scalar in array
    with pytest.raises(StoreError, match="expected a number"):
        physical.record("q1", "f_01_hz", [5.1e9])        # array in scalar
    with pytest.raises(StoreError, match="non-empty list"):
        physical.record("q1_z", "distortion_tau_s", [])
    with pytest.raises(StoreError, match="non-finite"):
        physical.record("q1", "f_01_hz", float("nan"))
    with pytest.raises(StoreError, match="expected a number"):
        physical.record("q1", "f_01_hz", True)


def test_paired_arrays_enforced_at_save_not_per_write(stores):
    """Pair-length equality is a BATCH-END (session) and SAVE invariant, not
    per-write — a redone fit legally changes both partners' length within
    one batch, so individual writes may transiently diverge."""
    physical, _ = stores
    physical.record("q1_z", "distortion_tau_s", [6.0e-7, 8.0e-8])
    physical.record("q1_z", "distortion_amp", [0.02])   # transient: allowed
    with pytest.raises(StoreError, match="unequal paired lengths"):
        physical.save()                                  # the save refuses
    physical.record("q1_z", "distortion_amp", [0.02, -0.01])
    physical.save()                                      # consistent: lands


def test_waveform_requires_its_time_base_first(stores):
    _, state = stores
    with pytest.raises(StoreError, match="iswap_waveform_dt_s first"):
        state.record("q1_q2", "iswap_waveform", [0.0, 0.4, 0.0])
    state.record("q1_q2", "iswap_waveform_dt_s", 5.0e-10)
    state.record("q1_q2", "iswap_waveform", [0.0, 0.4, 0.0])
    assert state.get("q1_q2", "iswap_waveform") == [0.0, 0.4, 0.0]


def test_get_returns_copies_none_until_written(stores):
    physical, _ = stores
    assert physical.get("q1", "f_01_hz") is None
    physical.record("q1_z", "distortion_tau_s", [1e-6])
    physical.get("q1_z", "distortion_tau_s").append(0.0)   # caller mutation
    assert physical.get("q1_z", "distortion_tau_s") == [1e-6]


# ---------------------------------------------------------------- history

def test_history_rows_are_self_describing(stores):
    _, state = stores
    state.record("q1_q2", "iswap_coupler_flux", 0.081, experiment="pair_zz",
                 run_id="r1")
    state.record("q1_q2", "iswap_coupler_flux", 0.083)
    rows = state.history()
    assert rows[0].kind == "qubit_pair" and rows[0].setup == "qm_a"
    assert rows[0].old is None and rows[0].new == 0.081
    assert rows[1].old == 0.081 and rows[1].new == 0.083
    assert rows[0].experiment == "pair_zz" and rows[0].run_id == "r1"


def test_array_values_round_trip_history_and_file(tmp_path, roster):
    physical = physical_store(tmp_path, roster)
    physical.record("q1_z", "distortion_tau_s", [6e-7, 8e-8])
    physical.save()
    again = physical_store(tmp_path, roster)
    assert again.get("q1_z", "distortion_tau_s") == [6e-7, 8e-8]
    assert again.history()[0].new == [6e-7, 8e-8]


# ------------------------------------------------------------ save / merge

def test_values_file_shape_is_the_doc_shape(tmp_path, roster):
    state = state_store(tmp_path, roster)
    state.record("q1_xy", "drive_freq_hz", 5.136e9)
    state.save()
    data = json.loads((tmp_path / "scqo_state.json").read_text())
    assert data == {"schema": 3,
                    "values": {"q1_xy": {"drive_freq_hz": 5.136e9}}}


def test_two_sessions_cannot_erase_each_other(tmp_path, roster):
    a = state_store(tmp_path, roster)
    b = state_store(tmp_path, roster)
    a.record("q1_xy", "pi_amp", 0.209)
    b.record("q2_xy", "pi_amp", 0.214)
    a.save()
    b.save()
    fresh = state_store(tmp_path, roster)
    assert fresh.get("q1_xy", "pi_amp") == 0.209
    assert fresh.get("q2_xy", "pi_amp") == 0.214
    assert len(fresh.history()) == 2


# ------------------------------------------------------------- fresh start

def _write_v2(tmp_path):
    (tmp_path / "scqo_state.json").write_text(json.dumps(
        {"schema": 2, "config": {"q1": {"readout_freq": 5.9e9}}}))
    (tmp_path / "scqo_state.history.jsonl").write_text(
        '{"timestamp": "t", "component": "q1", "field": "readout_freq"}\n')


def test_pre_v3_files_are_archived_aside_never_read(tmp_path, roster):
    _write_v2(tmp_path)
    state = state_store(tmp_path, roster)
    assert state.get("q1_xy", "drive_freq_hz") is None
    assert state.history() == ()
    assert (tmp_path / "scqo_state.json.v2.bak").is_file()
    assert (tmp_path / "scqo_state.history.jsonl.v2.bak").is_file()
    assert not (tmp_path / "scqo_state.json").is_file()


def test_fresh_start_applies_at_the_save_site_too(tmp_path, roster):
    state = state_store(tmp_path, roster)      # nothing on disk yet
    _write_v2(tmp_path)                        # v2 lands AFTER our load
    state.record("q1_xy", "drive_freq_hz", 5.136e9)
    state.save()
    assert (tmp_path / "scqo_state.json.v2.bak").is_file()
    data = json.loads((tmp_path / "scqo_state.json").read_text())
    assert data["schema"] == 3 and "config" not in data


def test_unknown_entity_and_field_use_roster_errors(stores):
    physical, _ = stores
    with pytest.raises(Exception, match="unknown entity"):
        physical.record("ghost", "f_01_hz", 5e9)
    with pytest.raises(Exception, match="unknown field"):
        physical.record("q1", "no_such_fact", 5e9)


# ----------------------------------------------- review-added regressions

def test_values_only_reset_preserves_history(tmp_path, roster):
    """Deleting the values file (a documented reset) must never cost rows."""
    state = state_store(tmp_path, roster)
    state.record("q1_xy", "pi_amp", 0.209)
    state.record("q1_xy", "drag_beta", -1.0)
    state.save()
    (tmp_path / "scqo_state.json").unlink()
    again = state_store(tmp_path, roster)
    again.record("q2_xy", "pi_amp", 0.214)
    again.save()
    fresh = state_store(tmp_path, roster)
    assert len(fresh.history()) == 3
    assert fresh.get("q1_xy", "pi_amp") is None      # values were reset...
    assert fresh.get("q2_xy", "pi_amp") == 0.214     # ...new value persisted


def test_corrupt_values_file_quarantined_history_survives(tmp_path, roster):
    state = state_store(tmp_path, roster)
    state.record("q1_xy", "pi_amp", 0.209)
    state.save()
    (tmp_path / "scqo_state.json").write_text('{"schema": 3, "val')  # torn
    again = state_store(tmp_path, roster)
    assert len(again.history()) == 1                 # provenance intact (DB)
    assert (tmp_path / "scqo_state.json.corrupt.bak").is_file()


def test_retired_sidecar_is_ignored_never_read(tmp_path, roster):
    """Pre-database ``*.history.jsonl`` files are dead: never read, never
    imported, left in place for the operator to delete at leisure."""
    (tmp_path / "scqo_state.history.jsonl").write_text(
        '{"timestamp": "t", "entity": "q1_xy", "field": "pi_amp", '
        '"old": null, "new": 0.111}\n')              # even a v3-shaped row
    state = state_store(tmp_path, roster)
    assert state.history() == ()                     # fresh start
    state.record("q1_xy", "pi_amp", 0.209)
    state.save()
    assert [r.new for r in state.history()] == [0.209]
    assert (tmp_path / "scqo_state.history.jsonl").is_file()  # untouched


def test_same_key_latest_timestamp_wins_regardless_of_save_order(
        tmp_path, roster, monkeypatch):
    import scqo.stores as stores_mod
    clock = iter(["2026-07-25T10:00:01+08:00", "2026-07-25T10:00:02+08:00"])
    monkeypatch.setattr(stores_mod, "_now", lambda: next(clock))
    older = state_store(tmp_path, roster)
    newer = state_store(tmp_path, roster)
    older.record("q1_xy", "pi_amp", 0.111)           # t1
    newer.record("q1_xy", "pi_amp", 0.222)           # t2 > t1
    newer.save()
    older.save()                                     # saves LAST, is OLDER
    fresh = state_store(tmp_path, roster)
    assert fresh.get("q1_xy", "pi_amp") == 0.222     # newest record wins
    times = [r.timestamp for r in fresh.history()]
    assert times == sorted(times)                    # file is time-ordered


def test_non_dirty_values_are_not_resurrected(tmp_path, roster):
    state = state_store(tmp_path, roster)
    state.record("q1_xy", "pi_amp", 0.209)
    state.save()
    holder = state_store(tmp_path, roster)           # holds pi_amp in memory
    # a deliberate values-only reset by another actor:
    (tmp_path / "scqo_state.json").write_text(
        '{"schema": 3, "values": {}}\n')
    holder.record("q2_xy", "pi_amp", 0.214)          # touches a DIFFERENT key
    holder.save()
    fresh = state_store(tmp_path, roster)
    assert fresh.get("q1_xy", "pi_amp") is None      # reset respected
    assert fresh.get("q2_xy", "pi_amp") == 0.214


def test_merged_paired_arrays_cannot_go_unequal(tmp_path, roster):
    a = physical_store(tmp_path, roster)
    b = physical_store(tmp_path, roster)
    a.record("q1_z", "distortion_tau_s", [1e-6, 2e-6])
    a.save()
    b.record("q1_z", "distortion_amp", [0.1, 0.2, 0.3])  # valid in B's view
    with pytest.raises(StoreError, match="unequal paired lengths"):
        b.save()
    fresh = physical_store(tmp_path, roster)
    assert fresh.get("q1_z", "distortion_amp") is None   # nothing committed
    assert len(fresh.history()) == 1                     # veto rolled back


def test_hand_mangled_file_values_are_dropped_at_load(tmp_path, roster):
    (tmp_path / "physical.json").write_text(
        '{"schema": 3, "values": {"q1": {"f_01_hz": Infinity, "t1_s": 2e-5},'
        ' "q1_z": {"distortion_amp": 0.5}}}')        # NaN token + wrong shape
    physical = physical_store(tmp_path, roster)
    assert physical.get("q1", "f_01_hz") is None     # non-finite dropped
    assert physical.get("q1", "t1_s") == 2e-5        # good value kept
    assert physical.get("q1_z", "distortion_amp") is None  # scalar in float[]


def test_history_rows_do_not_alias_live_values(stores):
    physical, _ = stores
    physical.record("q1_z", "distortion_tau_s", [1e-6])
    row = physical.history()[0]
    row.new.append(9.9)                              # a consumer misbehaving
    assert physical.get("q1_z", "distortion_tau_s") == [1e-6]


def test_record_stamps_campaign_id(tmp_path, roster):
    """A campaign-level accept stamps campaign_id (run_id stays None); the row
    survives the database round trip, and a plain record() leaves it None."""
    physical = physical_store(tmp_path, roster)
    physical.record("q1", "f_01_hz", 5.1e9, experiment="qubit_ramsey",
                    campaign_id="20260809-100000-000-devA-stab-01")
    row = physical.history()[-1]
    assert row.campaign_id == "20260809-100000-000-devA-stab-01"
    assert row.run_id is None
    assert row.as_dict()["campaign_id"] == row.campaign_id
    physical.save()

    again = physical_store(tmp_path, roster)
    assert again.history()[-1].campaign_id == row.campaign_id
    again.record("q1", "f_01_hz", 5.2e9)  # no kwarg -> no stamp
    assert again.history()[-1].campaign_id is None
