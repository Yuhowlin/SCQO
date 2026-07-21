"""The category catalog + component roster: validation, routing, topology."""

from __future__ import annotations

import pytest

from scqo.categories import CATEGORIES, field_categories, pushed_fields
from scqo.roster import COMPONENTS_FILE, Roster, RosterError, load_components

VALID = """\
schema = 1
[components.q1]
physical   = "FixedTransmon"
instrument = "ReadableTransmon"
operations = ["rx", "readout"]
[components.q1.design]
f_01_hz = 4.7e9
anharmonicity_hz = -2.1e8
[components.q2]
physical   = "FluxTunableTransmon"
instrument = "ReadableTransmon"
operations = ["rx", "readout", "flux_bias"]
[components.q1_res]
physical = "Resonator"
[components.q1_res.design]
f_r_hz = 5.9e9
[components.q1_ro]
physical = "ReadoutLine"
members  = { transmon = "q1", resonator = "q1_res" }
[components.q1_xy]
physical = "XYControl"
members  = { transmon = "q1" }
[components.q2_z]
physical = "ZControl"
members  = { transmon = "q2" }
"""

#: A two-qubit extension of VALID: a QCQ pair with and without the optional
#: coupler satellite (the Phase-2 shapes).
PAIR = VALID + """\
[components.q1_q2]
physical   = "Coupling"
instrument = "TransmonPair"
members    = { high = "q1", low = "q2" }
operations = ["coupler_bias", "iswap"]
[components.q2_q3]
physical   = "Coupling"
instrument = "TransmonPair"
members    = { high = "q2", low = "q3", coupler = "q2_q3_c" }
operations = ["coupler_bias"]
[components.q3]
physical   = "FluxTunableTransmon"
instrument = "ReadableTransmon"
operations = ["rx", "readout"]
[components.q2_q3_c]
physical = "FluxTunableTransmon"
[components.q2_q3_c.design]
f_01_hz = 6.1e9
"""


def _roster(tmp_path, body: str = VALID) -> Roster:
    (tmp_path / COMPONENTS_FILE).write_text(body, encoding="utf-8")
    return load_components(tmp_path)


# ------------------------------------------------------------------- catalog

def test_catalog_invariants_hold():
    """Import-time validation ran; spot-check the settled structure."""
    assert CATEGORIES["FixedTransmon"].side == "physical"
    assert CATEGORIES["ReadableTransmon"].side == "instrument"
    assert CATEGORIES["ZControl"].member_roles == {"transmon": ("FluxTunableTransmon",)}
    assert pushed_fields("ReadableTransmon") == (
        "readout_freq", "drive_freq", "pi_amp", "readout_amp",
        "readout_power_dbm", "readout_duration_s", "readout_integration_s",
        "idle_flux_v")
    assert "FixedTransmon" in field_categories()["t1_s"]
    assert "kappa_tot_hz" in CATEGORIES["Resonator"].fields  # the committed rename
    assert "ej_sum_hz" in CATEGORIES["FluxTunableTransmon"].fields


# --------------------------------------------------------------- happy path

def test_valid_roster_loads_and_answers(tmp_path):
    r = _roster(tmp_path)
    assert r.category("q1") == ("FixedTransmon", "ReadableTransmon")
    assert r.category("q1_res") == ("Resonator", None)
    assert r.members("q1_ro") == {"transmon": "q1", "resonator": "q1_res"}
    assert r.operations("q1") == ("rx", "readout")
    assert r.design("q1", "f_01_hz") == 4.7e9
    assert r.design("q1_res", "f_r_hz") == 5.9e9
    assert r.design("q1_res", "kappa_tot_hz") is None
    assert r.names("ReadableTransmon") == ["q1", "q2"]


def test_requires_physical_pruning(tmp_path):
    """idle_flux_v exists only on flux-tunable realizations."""
    r = _roster(tmp_path)
    assert "idle_flux_v" not in r.fields_of("q1")   # FixedTransmon
    assert "idle_flux_v" in r.fields_of("q2")        # FluxTunableTransmon
    assert r.pushed("q1") == ("readout_freq", "drive_freq", "pi_amp",
                              "readout_amp", "readout_power_dbm",
                              "readout_duration_s", "readout_integration_s")
    assert r.pushed("q2") == ("readout_freq", "drive_freq", "pi_amp",
                              "readout_amp", "readout_power_dbm",
                              "readout_duration_s", "readout_integration_s",
                              "idle_flux_v")
    assert r.pushed("q1_res") == ()                  # physical-only component


def test_resolve_routes_by_declaring_side(tmp_path):
    r = _roster(tmp_path)
    assert r.resolve("q1", "t1_s")[0] == "physical"
    assert r.resolve("q1", "readout_freq")[0] == "instrument"
    assert r.resolve("q1_res", "f_r_hz")[0] == "physical"
    assert r.resolve("q2_z", "v_offset_v")[0] == "physical"
    with pytest.raises(KeyError, match="has no field 'f_r_hz'"):
        r.resolve("q1", "f_r_hz")                    # re-homed to the resonator
    with pytest.raises(KeyError, match="unknown component"):
        r.resolve("nope", "t1_s")


def test_one_topology_lookup(tmp_path):
    r = _roster(tmp_path)
    assert r.one("q1", "ReadoutLine") == "q1_ro"     # direct: term references q1
    assert r.one("q1", "Resonator") == "q1_res"      # one hop through q1_ro
    assert r.one("q2", "ZControl") == "q2_z"
    with pytest.raises(RosterError, match="exactly one Resonator"):
        r.one("q2", "Resonator")                     # q2 has no readout line here


# ------------------------------------------------------------ pairs (Phase 2)

def test_roster_tolerates_powershell_bom(tmp_path):
    """Windows PowerShell 5.1's `Set-Content -Encoding utf8` writes a UTF-8 BOM;
    the roster is hand-edited, so a BOM'd file must load identically."""
    (tmp_path / COMPONENTS_FILE).write_bytes(b"\xef\xbb\xbf" + VALID.encode("utf-8"))
    r = load_components(tmp_path)
    assert r.category("q1") == ("FixedTransmon", "ReadableTransmon")


def test_pair_component_loads(tmp_path):
    """A QCQ pair binds Coupling+TransmonPair on ONE name; the coupler member is
    optional; the satellite is a plain physical-only transmon with design values."""
    r = _roster(tmp_path, PAIR)
    assert r.category("q1_q2") == ("Coupling", "TransmonPair")
    assert r.members("q1_q2") == {"high": "q1", "low": "q2"}          # no coupler: fine
    assert r.members("q2_q3")["coupler"] == "q2_q3_c"
    assert r.names("TransmonPair") == ["q1_q2", "q2_q3"]
    assert r.pushed("q1_q2") == ("coupler_decouple_v", "coupler_interaction_v")
    assert r.resolve("q1_q2", "zz_hz")[0] == "physical"               # Coupling side
    assert r.resolve("q1_q2", "coupler_decouple_v")[0] == "instrument"
    assert r.category("q2_q3_c") == ("FluxTunableTransmon", None)     # satellite
    assert r.design("q2_q3_c", "f_01_hz") == 6.1e9
    # existing single-qubit satellite lookups are unaffected by pair terms
    assert r.one("q1", "Resonator") == "q1_res"


@pytest.mark.parametrize("body, match", [
    # a pair must declare BOTH high and low (only the coupler is optional)
    (PAIR.replace('members    = { high = "q1", low = "q2" }',
                  'members    = { high = "q1" }', 1), "missing member role"),
    # roles are high/low — control/target is per-operation vendor territory
    (PAIR.replace('members    = { high = "q1", low = "q2" }',
                  'members    = { control = "q1", target = "q2" }', 1),
     "unknown member role"),
    # a coupler satellite must be flux-tunable (it IS a transmon, physically)
    (PAIR.replace('[components.q2_q3_c]\nphysical = "FluxTunableTransmon"',
                  '[components.q2_q3_c]\nphysical = "Resonator"', 1),
     "must be one of"),
    # operations outside TransmonPair's vocabulary
    (PAIR.replace('operations = ["coupler_bias", "iswap"]',
                  'operations = ["coupler_bias", "readout"]', 1),
     "outside TransmonPair's vocabulary"),
])
def test_pair_validation_errors(tmp_path, body, match):
    with pytest.raises(RosterError, match=match):
        _roster(tmp_path, body)


# ---------------------------------------------------------------- validation

@pytest.mark.parametrize("body, match", [
    (VALID.replace("[components.q1]", "[components.\"q1.x\"]", 1), "dot-free"),
    (VALID.replace('"FixedTransmon"', '"NoSuchCat"', 1), "not a known category"),
    (VALID.replace('physical   = "FixedTransmon"',
                   'physical   = "ReadableTransmon"', 1), "cannot fill"),
    (VALID.replace('members  = { transmon = "q1", resonator = "q1_res" }',
                   'members  = { transmon = "q1", widget = "q1_res" }'),
     "unknown member role"),
    (VALID.replace('members  = { transmon = "q1", resonator = "q1_res" }',
                   'members  = { transmon = "q1" }'), "missing member"),
    (VALID.replace('members  = { transmon = "q1", resonator = "q1_res" }',
                   'members  = { transmon = "q1", resonator = "ghost" }'),
     "not a declared component"),
    (VALID.replace('members  = { transmon = "q2" }',
                   'members  = { transmon = "q1" }'), "must be one of"),
    (VALID.replace("f_01_hz = 4.7e9", "bogus_hz = 4.7e9"), "not a FixedTransmon field"),
    (VALID.replace("f_01_hz = 4.7e9", "f_01_hz = nan"), "finite"),
    (VALID + '[components.orphan]\noperations = ["rx"]\n', "neither physical nor instrument"),
])
def test_loader_rejects_bad_rosters(tmp_path, body, match):
    with pytest.raises(RosterError, match=match):
        _roster(tmp_path, body)


def test_missing_roster_carries_template(tmp_path):
    with pytest.raises(FileNotFoundError, match="Minimal template"):
        load_components(tmp_path)
