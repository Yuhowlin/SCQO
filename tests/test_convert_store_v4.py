"""The one-time 4.0.0 store conversion (scripts/convert_store_v4.py) -
docs/store-by-line-plan.md section 5: every 3.x value lands at its 4.0.0
address unchanged, history is re-keyed in place with its row count intact,
pending suggestions refuse, immutable data is never touched, and a re-run
changes nothing."""

from __future__ import annotations

import importlib.util
import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from scqo import parse_components
from scqo.changes import _DDL, ChangeDB, ChangeRecord
from scqo.stores import flatten_values, physical_store, state_store
from scqo.v3_names import v3_address_map

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "convert_store_v4.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("convert_store_v4", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # registered before exec: @dataclass resolves its module's namespace
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


conv = _load_script()

#: Riders, a coupler on its own line, a declared operation, and a broadcast
#: coil reaching two qubits (the 3.x ``<field>__<target>`` grammar).
COMPONENTS = """
schema = 3
[modes.q1]
kind = "flux_transmon"
[modes.q2]
kind = "flux_transmon"
[modes.q1_q2_c]
kind = "flux_transmon"
[composites.q1_q2]
kind       = "qubit_pair"
high       = "q1"
low        = "q2"
coupler    = "q1_q2_c"
operations = ["iswap"]
[lines.fl1]
readout = ["q1", "q2"]
[lines.xy1]
drive = ["q1"]
[lines.xy2]
drive = ["q2"]
[lines.z1]
flux = ["q1"]
[lines.z2]
flux = ["q2"]
[lines.zc12]
flux = ["q1_q2_c"]
[lines.coil]
[channels.coil_z]
kind   = "flux"
target = ["q1", "q2"]
line   = "coil"
"""

STATE_V3 = {
    "q1_xy": {"drive_freq_hz": 5.1e9, "pi_amp": 0.21,
              "thermalization_time_s": 2e-4},
    "q2_xy": {"drive_freq_hz": 4.9e9},
    "q1_ro": {"readout_freq_hz": 7.1e9, "readout_amp": 0.05},
    "q1_z": {"idle_flux": 0.26, "flux_delay_s": 3e-9},
    "q1_q2_c_z": {"idle_flux": 0.08},
    "coil_z": {"idle_flux": 0.01},
    "q1_q2": {"iswap_coupler_flux": 0.12},
    "ghost_xy": {"pi_amp": 0.3},              # a channel the roster lost
}
PHYSICAL_V3 = {
    "q1": {"f_01_hz": 5.1e9, "t1_s": 2.5e-5},
    "q1_res": {"f_dress0_hz": 7.1e9},
    "q1_z": {"flux_offset": 0.26, "flux_per_phi0": 0.96,
             "distortion_amp": [0.1, 0.02], "distortion_tau_s": [1e-8, 1e-7]},
    "coil_z": {"flux_per_phi0__q1": 5.0, "flux_per_phi0__q2": 6.0,
               "distortion_amp__q1": [0.3], "distortion_tau_s__q1": [2e-8],
               "distortion_amp__q2": [0.4], "distortion_tau_s__q2": [3e-8]},
    "q1_q2": {"zz_hz": 1.2e5},
}


def _v1_history(path: Path, rows: list[tuple[str, ChangeRecord]]) -> None:
    """A 3.x (v1) history database, built with the real DDL."""
    db = sqlite3.connect(path)
    try:
        for statement in _DDL:
            db.execute(statement)
        db.execute("INSERT INTO meta (key, value) VALUES "
                   "('changes_schema_version', '1')")
        for store, record in rows:
            ChangeDB.insert(db, [record], store=store)
        db.commit()
    finally:
        db.close()


def _row(entity, field, new, *, kind, coupled_to=None):
    return ChangeRecord(timestamp="2026-09-01T10:00:00+08:00", entity=entity,
                        field=field, old=None, new=new, kind=kind,
                        coupled_to=coupled_to, setup="main", cooldown="cd1")


HISTORY_V1 = [
    ("state", _row("q1_xy", "pi_amp", 0.21, kind="drive")),
    ("state", _row("q1_ro", "readout_amp", 0.05, kind="readout",
                   coupled_to="readout_power_dbm")),
    ("state", _row("q1_z", "idle_flux", 0.26, kind="flux")),
    ("state", _row("q1_q2", "iswap_coupler_flux", 0.12, kind="qubit_pair")),
    ("state", _row("ghost_xy", "pi_amp", 0.3, kind="drive")),
    ("physical", _row("q1", "f_01_hz", 5.1e9, kind="flux_transmon")),
    ("physical", _row("q1_z", "flux_per_phi0", 0.96, kind="flux")),
]


@pytest.fixture
def root(tmp_path) -> Path:
    """A 3.x data root: one device, one context, a setup snapshot."""
    dev = tmp_path / "chip"
    ctx = dev / "cd1" / "main" / "scqo"
    ctx.mkdir(parents=True)
    (dev / "components.toml").write_text(COMPONENTS, encoding="utf-8")
    (ctx / "scqo_state.json").write_text(
        json.dumps({"schema": 3, "values": STATE_V3}), encoding="utf-8")
    (ctx / "physical.json").write_text(
        json.dumps({"schema": 3, "values": PHYSICAL_V3}), encoding="utf-8")
    _v1_history(ctx / "history.sqlite", HISTORY_V1)
    snap = dev / "setup_snapshots" / "0123456789abcdef" / "scqo"
    snap.mkdir(parents=True)
    (snap / "scqo_state.json").write_text(
        json.dumps({"schema": 3, "values": STATE_V3}), encoding="utf-8")
    return tmp_path


def _ctx(root: Path) -> Path:
    return root / "chip" / "cd1" / "main" / "scqo"


def _run(root: Path, *, apply: bool, reject: bool = False) -> tuple[int, str]:
    out = io.StringIO()
    code = conv.convert(root, apply=apply, reject=reject, out=out)
    return code, out.getvalue()


def _bytes(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _rows(path: Path) -> list[tuple]:
    db = sqlite3.connect(path)
    try:
        return db.execute("SELECT store, entity, field, kind, coupled_to "
                          "FROM changes ORDER BY seq").fetchall()
    finally:
        db.close()


def test_a_dry_run_reports_the_moves_and_touches_nothing(root):
    before = _bytes(root)
    code, text = _run(root, apply=False)
    assert code == 0
    assert "q1_xy -> xy1.q1" in text
    assert "q1_z -> z1, z1.q1" in text          # one 3.x channel, two owners
    assert "would be converted" in text
    assert _bytes(root) == before


def test_every_value_lands_at_its_new_address_unchanged(root):
    code, text = _run(root, apply=True)
    assert code == 0, text
    roster = parse_components(COMPONENTS)
    amap = v3_address_map(roster)
    for name, old in (("scqo_state.json", STATE_V3),
                      ("physical.json", PHYSICAL_V3)):
        data = json.loads((_ctx(root) / name).read_text(encoding="utf-8"))
        assert data["schema"] == 4
        flat = flatten_values(data["values"])
        for entity, fields in old.items():
            for field, value in fields.items():
                new = amap.get((entity, field))
                if new is None or flat.get(new[0], {}).get(new[1]) != value:
                    # no 4.0.0 address, or its slot is taken: KEPT, never lost
                    assert flat[entity][field] == value, (entity, field)
                else:
                    assert flat[new[0]][new[1]] == value
    state = flatten_values(json.loads(
        (_ctx(root) / "scqo_state.json").read_text(encoding="utf-8"))["values"])
    assert state["z1"] == {"idle_flux": 0.26, "flux_delay_s": 3e-9}
    assert state["zc12"] == {"idle_flux": 0.08}
    assert state["coil"] == {"idle_flux": 0.01}
    assert state["q1_q2.iswap"] == {"coupler_flux": 0.12}
    assert state["ghost_xy"] == {"pi_amp": 0.3}          # kept, reported
    assert "KEPT ghost_xy.pi_amp (no 4.0.0 address)" in text
    physical = flatten_values(json.loads(
        (_ctx(root) / "physical.json").read_text(encoding="utf-8"))["values"])
    assert physical["z1"] == {"distortion_amp": [0.1, 0.02],
                              "distortion_tau_s": [1e-8, 1e-7]}
    assert physical["z1.q1"] == {"flux_offset": 0.26, "flux_per_phi0": 0.96}
    assert physical["coil.q1"] == {"flux_per_phi0": 5.0}
    assert physical["coil.q2"] == {"flux_per_phi0": 6.0}
    # a broadcast wire has ONE impulse response: the first 3.x copy takes the
    # line's slot, the second is kept under its old name
    assert physical["coil"] == {"distortion_amp": [0.3],
                                "distortion_tau_s": [2e-8]}
    assert physical["coil_z"]["distortion_amp__q2"] == [0.4]
    for name in ("scqo_state.json", "physical.json", "history.sqlite"):
        assert (_ctx(root) / f"{name}.v3.bak").is_file()
    assert json.loads((_ctx(root) / "scqo_state.json.v3.bak").read_text(
        encoding="utf-8")) == {"schema": 3, "values": STATE_V3}


def test_history_is_rekeyed_in_place_with_its_row_count(root):
    _run(root, apply=True)
    rows = _rows(_ctx(root) / "history.sqlite")
    assert len(rows) == len(HISTORY_V1)
    assert rows == [
        ("state", "xy1.q1", "pi_amp", "drive", None),
        ("state", "fl1.q1", "readout_amp", "readout", "readout_power_dbm"),
        ("state", "z1", "idle_flux", "line", None),
        ("state", "q1_q2.iswap", "coupler_flux", "operation", None),
        ("state", "ghost_xy", "pi_amp", "drive", None),       # no new address
        ("physical", "q1", "f_01_hz", "flux_transmon", None),  # identity
        ("physical", "z1.q1", "flux_per_phi0", "flux", None),
    ]
    db = sqlite3.connect(_ctx(root) / "history.sqlite")
    try:
        assert db.execute("SELECT value FROM meta WHERE key = "
                          "'changes_schema_version'").fetchone() == ("2",)
    finally:
        db.close()
    # the backup is the untouched 3.x database
    assert [r[1] for r in _rows(_ctx(root) / "history.sqlite.v3.bak")] == [
        r.entity for _s, r in HISTORY_V1]


def test_a_4x_session_reads_the_converted_context(root):
    _run(root, apply=True)
    roster = parse_components(COMPONENTS)
    state = state_store(_ctx(root), roster, setup="main", cooldown="cd1")
    physical = physical_store(_ctx(root), roster, setup="main", cooldown="cd1")
    assert state.get("z1", "idle_flux") == 0.26
    assert state.get("xy1.q1", "thermalization_time_s") == 2e-4
    assert physical.get("z1.q1", "flux_per_phi0") == 0.96
    assert [r.entity for r in state.history()][:4] == [
        "xy1.q1", "fl1.q1", "z1", "q1_q2.iswap"]
    # the v2 gate opens: a 4.0.0 write lands on the converted database
    state.record("z1", "idle_flux", 0.25)
    state.save()
    assert state.history()[-1].entity == "z1"


def test_a_rerun_changes_nothing(root):
    _run(root, apply=True)
    after = _bytes(root)
    code, text = _run(root, apply=True)
    assert code == 0
    assert "nothing to convert" in text
    assert _bytes(root) == after


def test_setup_snapshots_are_immutable(root):
    snap = root / "chip" / "setup_snapshots" / "0123456789abcdef" / "scqo"
    before = (snap / "scqo_state.json").read_bytes()
    _run(root, apply=True)
    assert (snap / "scqo_state.json").read_bytes() == before
    assert not (snap / "scqo_state.json.v3.bak").exists()


def test_pending_suggestions_refuse_before_anything_is_written(root):
    run = root / "chip" / "2026-09-01" / "20260901-100000-000-chip-x-01"
    run.mkdir(parents=True)
    (run / "record.json").write_text(json.dumps({"suggestions": [
        {"entity": "q1_xy", "field": "pi_amp", "status": "pending"},
        {"entity": "q1_ro", "field": "readout_amp", "status": "accepted"}]}),
        encoding="utf-8")
    campaign = root / "chip" / "campaigns" / "20260901-cmp"
    campaign.mkdir(parents=True)
    (campaign / "campaign.json").write_text(json.dumps({"suggestions": [
        {"entity": "q1_xy", "field": "pi_amp", "status": "pending"}]}),
        encoding="utf-8")
    before = _bytes(root)
    code, text = _run(root, apply=True)
    assert code == 2
    assert "run chip/20260901-100000-000-chip-x-01: 1 pending" in text
    assert "campaign chip/20260901-cmp: 1 pending" in text
    assert "nothing was written" in text
    assert _bytes(root) == before


#: A second, 4.0.0-native device whose run leaves real pending suggestions in
#: an indexed run folder (the reject path goes through the datastore).
DEMO = """
schema = 3
[modes.q0]
kind = "transmon"
[modes.q1]
kind = "transmon"
[lines.fl]
readout = ["q0", "q1"]
[lines.xy_q0]
drive = ["q0"]
[lines.xy_q1]
drive = ["q1"]
"""


def _pending_run(root: Path) -> str:
    from scqo import Session
    from scqo.testing import (InMemoryDevice, SimulatedBackend, demo_design,
                              demo_vendor_state)

    (root / "demo").mkdir()
    (root / "demo" / "components.toml").write_text(DEMO, encoding="utf-8")
    roster = parse_components(DEMO)
    design = demo_design(roster)
    vendor = InMemoryDevice(roster, demo_vendor_state(roster, design))
    sess = Session(SimulatedBackend(vendor), roster, design=design,
                   scqo_dir=root / "demo" / "cd1" / "sim" / "scqo",
                   data_root=root, device_name="demo", setup_name="sim",
                   cooldown_id="cd1")
    result = sess.run("qubit_relaxation", {"targets": ["q0"]})
    return result["run_id"]


def test_reject_pending_clears_the_blockers_through_the_datastore(root):
    from scqo.datastore import DataStore

    run_id = _pending_run(root)
    store = DataStore(root, device_name="demo")
    assert [r["run_id"] for r in store.find_runs(pending=True)] == [run_id]

    before = _bytes(root)
    code, text = _run(root, apply=False, reject=True)     # dry run: says so
    assert code == 0 and "would reject" in text
    assert _bytes(root) == before

    code, text = _run(root, apply=True, reject=True)
    assert code == 0, text
    assert "rejected" in text and "converted" in text
    record = store.load_run(run_id)["record"]
    assert record["suggestions"]
    assert {s["status"] for s in record["suggestions"]} == {"rejected"}
    assert all(s["comment"] == conv.REJECT_COMMENT
               for s in record["suggestions"])
    assert store.find_runs(pending=True) == []           # the index agrees


def test_a_roster_4x_cannot_load_refuses(root):
    """A device whose components.toml 4.0.0 rejects cannot be mapped: the
    apply refuses as a whole rather than converting half a data root."""
    comp = root / "chip" / "components.toml"
    # a mode named like a field is refused by the 4.0.0 roster
    comp.write_text(COMPONENTS + '\n[modes.pi_amp]\nkind = "transmon"\n',
                    encoding="utf-8")
    before = _bytes(root)
    code, text = _run(root, apply=True)
    assert code == 2 and "does not load under 4.0.0" in text
    assert _bytes(root) == before
