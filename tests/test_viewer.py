"""Run-viewer: pages render from a real (simulated-run) datastore; the only write
is tag/note editing; file serving never escapes the run folder."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
# python-multipart backs FastAPI's Form(...) (the tag-edit POST). The QM lock env
# deliberately omits the viewer extras — skip there instead of erroring 14 tests
# (INSTALL §3 blesses the view venv for the suite).
pytest.importorskip("multipart")
from fastapi.testclient import TestClient  # noqa: E402

from scqo import Session, register  # noqa: E402
from scqo.experiments import QubitRamsey, QubitRelaxation, ResonatorSpectroscopy  # noqa: E402
from scqo.testing import InMemoryDevice, SimulatedBackend, demo_roster  # noqa: E402
from scqo.viewer.app import create_app  # noqa: E402


@register
class _VRes(ResonatorSpectroscopy):
    def probe(self):
        return None


@register
class _VRamsey(QubitRamsey):
    def probe(self):
        return None


@register
class _VT1(QubitRelaxation):
    def probe(self):
        return None


def _device() -> InMemoryDevice:
    return InMemoryDevice(
        {
            "q0": {"readout_freq": 5.95e9, "drive_freq": 3.87e9, "pi_amp": 0.2, "readout_amp": 0.25},
            "q1": {"readout_freq": 6.05e9, "drive_freq": 4.01e9, "pi_amp": 0.18, "readout_amp": 0.22},
        }
    )


def _scqo_state(root: Path, dev: str, cid: str, setup: str) -> str:
    return str(root / dev / cid / setup / "scqo" / "scqo_state.json")


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    """A datastore with APPLIED runs on TWO setups of devV (per-(cooldown, setup)
    scqo/ folders), one PENDING run, a run stamped with a VANISHED setup name, a
    second sample (chipZ), a registry-less sample (bare), and a viewer client."""
    root = tmp_path_factory.mktemp("data")
    (root / "devV").mkdir()
    # Cycle registry BEFORE the runs; sessions bind their (cooldown, setup) era
    # explicitly and each context persists its OWN scqo/ folder.
    (root / "devV" / "cooldowns.toml").write_text(
        '[cdV]\nstart = 2026-07-01\nfridge = "BlueforsA"\npackaging = "PCB v3"\n\n'
        '[cdV.setup.sim_main]\nbackend = "simulated"\n'
        '[cdV.setup.sim_alt]\nbackend = "simulated"\n',
        encoding="utf-8",
    )
    sess = Session(
        SimulatedBackend(_device()), demo_roster(), data_root=root, device_name="devV",
        state_path=_scqo_state(root, "devV", "cdV", "sim_main"), state_sync="push",
        setup_name="sim_main", cooldown_id="cdV",
    )
    r_res = sess.run("resonator_spectroscopy", {"targets": ["q0"]}, update="apply", tags=["cool1"])
    # a second applied run SUPERSEDES r_res's readout_freq (live-source tests)
    r_res2 = sess.run("resonator_spectroscopy", {"targets": ["q0"]}, update="apply", tags=["cool1"])
    r_ram = sess.run("qubit_ramsey", {"targets": ["q1"], "num_points": 201}, update="apply",
                     tags=["cool1", "special"])
    r_t1 = sess.run("qubit_relaxation", {"targets": ["q1"]}, update="apply", tags=["cool1"])
    # a HUMAN-attached proposal on the T1 run (scqo suggest; left pending)
    sess.suggest(r_t1["run_id"], {"q1.t1_s": 2.4e-5}, comment="read off the decay")
    r_pend = sess.run("resonator_spectroscopy", {"targets": ["q0"]}, tags=["cool1"])  # left pending
    # a q0_ro-only physical value -> a "(manual)" source in this context's ledger
    sess.physical.record("q0_ro", "g_hz", 80e6)
    sess.physical.save()
    # the SECOND setup of the same device: its own scqo/ folder, its own history
    sess_alt = Session(
        SimulatedBackend(_device()), demo_roster(), data_root=root, device_name="devV",
        state_path=_scqo_state(root, "devV", "cdV", "sim_alt"), state_sync="push",
        setup_name="sim_alt", cooldown_id="cdV",
    )
    r_alt = sess_alt.run("resonator_spectroscopy", {"targets": ["q1"]}, update="apply", tags=["cool1"])
    # a run bound to a setup name NOT in the active cycle (bound eras are stamped
    # verbatim, never re-validated): must get NO live credit anywhere
    sess_ghost = Session(
        SimulatedBackend(_device()), demo_roster(), data_root=root, device_name="devV",
        setup_name="ghost", cooldown_id="cdV",
    )
    r_ghost = sess_ghost.run("resonator_spectroscopy", {"targets": ["q0"]}, update="apply")

    # second physical sample with its own registry + one persisted setup
    (root / "chipZ").mkdir()
    (root / "chipZ" / "cooldowns.toml").write_text(
        '[cdZ]\nstart = 2026-07-02\n'
        '[cdZ.setup.z_main]\nbackend = "simulated"\n',
        encoding="utf-8",
    )
    sess_z = Session(
        SimulatedBackend(_device()), demo_roster(), data_root=root, device_name="chipZ",
        state_path=_scqo_state(root, "chipZ", "cdZ", "z_main"), state_sync="push",
        setup_name="z_main", cooldown_id="cdZ",
    )
    r_z = sess_z.run("resonator_spectroscopy", {"targets": ["q0"]}, update="apply", tags=["zcool"])
    (root / "devices.toml").write_text(
        '[chipZ]\ndescription = "second sample on the other fridge"\n',
        encoding="utf-8",
    )

    # a registry-less sample: runs exist, no setups -> snapshot-only device page
    sess_b = Session(SimulatedBackend(_device()), demo_roster(), data_root=root,
                     device_name="bare")
    r_bare = sess_b.run("resonator_spectroscopy", {"targets": ["q0"]}, update="apply")

    client = TestClient(create_app(root, device_name="devV"))
    return {"client": client, "root": root, "res": r_res, "res2": r_res2, "ram": r_ram,
            "t1": r_t1, "pend": r_pend, "alt": r_alt, "ghost": r_ghost,
            "chipz": r_z, "bare": r_bare}


def test_runs_page_lists_and_filters(lab):
    c = lab["client"]
    page = c.get("/").text
    assert lab["res"]["run_id"] in page and lab["ram"]["run_id"] in page

    filtered = c.get("/", params={"tag": "special"}).text
    assert lab["ram"]["run_id"] in filtered
    assert lab["res"]["run_id"] not in filtered


def test_run_page_shows_fit_figure_and_diff(lab):
    c = lab["client"]
    page = c.get(f"/run/{lab['ram']['run_id']}").text
    assert "t2_star_s" in page  # fit table
    assert "<img" in page and "/file/analysis/" in page  # inline figure
    assert "Device before" in page and "changed" in page  # diff with a highlighted change

    # the figure actually serves
    img_rel = page.split('/file/', 1)[1].split('"', 1)[0]
    resp = c.get(f"/run/{lab['ram']['run_id']}/file/{img_rel}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/png")


def test_file_endpoint_rejects_traversal(lab):
    c = lab["client"]
    resp = c.get(f"/run/{lab['res']['run_id']}/file/../../../scqo_state.json")
    assert resp.status_code == 404
    resp = c.get(f"/run/{lab['res']['run_id']}/file/..%2f..%2frecord.json")
    assert resp.status_code == 404


def test_tag_editing_is_the_only_write(lab):
    c = lab["client"]
    rid = lab["res"]["run_id"]
    resp = c.post(f"/run/{rid}/tags", data={"add": "viewer-tag", "remove": "", "note": "from browser"},
                  follow_redirects=False)
    assert resp.status_code == 303

    record_path = next(Path(lab["root"]).glob(f"devV/*/{rid}/record.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert "viewer-tag" in record["tags"] and record["note"] == "from browser"

    # no other mutating routes exist
    posts = [r.path for r in c.app.routes if hasattr(r, "methods") and "POST" in (r.methods or set())]
    assert posts == [f"/run/{{run_id}}/tags".replace("{{", "{").replace("}}", "}")]


def test_trends_page_charts_t1(lab):
    c = lab["client"]
    page = c.get("/trends", params={"target": "q1", "quantity": "t1_s"}).text
    assert "<svg" in page and "<circle" in page
    assert lab["t1"]["run_id"] in page


def test_device_page_state_and_history(lab):
    c = lab["client"]
    page = c.get("/device").text
    assert "Device: devV" in page  # default = the configured sample
    assert "readout_amp" in page  # last-observed calibration table
    assert "Change history" in page
    assert lab["res"]["run_id"] in page  # history entry links to its run


def test_device_page_history_operator_column(lab):
    """P3 attribution: the change history shows WHO made each change."""
    import getpass

    page = lab["client"].get("/device").text
    assert "<th>operator</th>" in page
    assert getpass.getuser() in page  # this test process's login, stamped on the runs


def test_physical_panel_is_per_setup_section(lab):
    """Physical values live inside their setup's section (per (cooldown, setup)
    context) — flat rows, no setup column. Only sim_main measured physics here."""
    page = lab["client"].get("/device").text
    assert "Physical parameters — sim_main" in page
    values_table = page.split("Physical parameters — sim_main", 1)[1].split("</table>", 1)[0]
    assert "<th>setup</th>" not in values_table  # one context per section: no setup column
    for field in ("t1_s", "t2_star_s", "g_hz"):
        assert f"<td>{field}</td>" in values_table


def test_run_page_shows_suggestions_table(lab):
    c = lab["client"]
    # a pending run: highlighted rows + the decide-at-the-terminal hint
    page = c.get(f"/run/{lab['pend']['run_id']}").text
    assert "Suggested updates" in page
    assert "<b>pending</b>" in page
    assert f"scqo accept {lab['pend']['run_id']}" in page
    # an applied run still shows its audit trail
    page_applied = c.get(f"/run/{lab['res']['run_id']}").text
    assert "Suggested updates" in page_applied and "accepted" in page_applied


def test_device_page_history_survives_values_only_reset(tmp_path):
    """REGRESSION: after the documented values-only reset (delete scqo_state.json,
    keep its .history.jsonl sidecar) the device page must still render the change
    history — the split's guarantee is that provenance is never silently hidden."""
    (tmp_path / "devR").mkdir()
    (tmp_path / "devR" / "cooldowns.toml").write_text(
        '[cdR]\nstart = 2026-07-01\n[cdR.setup.main]\nbackend = "simulated"\n',
        encoding="utf-8")
    sess = Session(
        SimulatedBackend(_device()), demo_roster(), data_root=tmp_path, device_name="devR",
        state_path=_scqo_state(tmp_path, "devR", "cdR", "main"), state_sync="push",
        setup_name="main", cooldown_id="cdR",
    )
    r = sess.run("resonator_spectroscopy", {"targets": ["q0"]}, update="apply")
    Path(_scqo_state(tmp_path, "devR", "cdR", "main")).unlink()  # sidecar survives

    page = TestClient(create_app(tmp_path, device_name="devR")).get("/device").text
    assert "Change history" in page
    assert r["run_id"] in page  # rows render from the surviving sidecar


def test_run_page_marks_operator_suggestion(lab):
    """A human-attached value (Session.suggest / scqo suggest) renders with the
    operator badge; estimator rows never carry it."""
    page = lab["client"].get(f"/run/{lab['t1']['run_id']}").text
    assert 'class="badge operator"' in page
    assert "read off the decay" in page  # the proposal comment is shown
    page_estimator = lab["client"].get(f"/run/{lab['res']['run_id']}").text
    assert "badge operator" not in page_estimator


def test_runs_page_pending_filter_and_updates_column(lab):
    c = lab["client"]
    page = c.get("/", params={"pending": "1"}).text
    assert lab["pend"]["run_id"] in page
    assert lab["res"]["run_id"] not in page  # applied at run time -> nothing pending
    full = c.get("/").text
    assert "3 pending" in full  # the updates column flags the undecided run


def test_trends_offer_descriptor_quantities(lab):
    page = lab["client"].get("/trends").text
    assert "t2_echo_s" in page
    assert "readout_fidelity" in page


def test_runs_page_cooldown_filter_and_column(lab):
    c = lab["client"]
    page = c.get("/", params={"cooldown": "cdV"}).text
    assert lab["res"]["run_id"] in page  # devV runs were stamped with the active cycle
    assert c.get("/", params={"cooldown": "nope"}).text.count("/run/") == 0


def test_runs_page_setup_filter_and_column(lab):
    """Runs stamped with a setup NAME show it in the setup column; ?setup= filters."""
    c = lab["client"]
    page = c.get("/").text
    assert "<th>setup</th>" in page
    assert "<td>sim_main</td>" in _row_chunk(page, lab["res"]["run_id"])
    assert "<td>z_main</td>" in _row_chunk(page, lab["chipz"]["run_id"])
    # the registry-less sample's run carries no setup name
    assert "sim_main" not in _row_chunk(page, lab["bare"]["run_id"])

    filtered = c.get("/", params={"setup": "sim_main"}).text
    assert lab["res"]["run_id"] in filtered and lab["ram"]["run_id"] in filtered
    assert lab["alt"]["run_id"] not in filtered  # the other setup's run
    assert lab["chipz"]["run_id"] not in filtered
    assert c.get("/", params={"setup": "nope"}).text.count("/run/") == 0


def test_device_page_shows_cycle_and_setup(lab):
    page = lab["client"].get("/device").text
    assert "Cooldown cycles" in page
    assert "cdV" in page and "(active)" in page
    assert "PCB v3" in page  # packaging is a cycle fact
    # the ACTIVE cycle's named-setups table: name + backend rendered
    assert "<b>sim_main</b>" in page
    assert "simulated" in page
    assert "(built-in)" in page  # simulated setups carry no instrument_config


def test_multi_device_filter_and_device_page(lab):
    c = lab["client"]
    rid = lab["chipz"]["run_id"]

    only_z = c.get("/", params={"device": "chipZ"}).text
    assert rid in only_z and lab["res"]["run_id"] not in only_z

    page_z = c.get("/device", params={"device": "chipZ"}).text
    assert "Device: chipZ" in page_z
    assert "second sample on the other fridge" in page_z  # devices.toml card rendered
    assert rid in page_z  # history via z_main's per-setup state file


def test_main_initializes_fresh_data_root_but_rejects_typos(tmp_path, monkeypatch):
    """A fresh (existing, empty) data_root gets an empty index automatically; a
    nonexistent path still fails loudly — a typo must never serve an empty lab."""
    import uvicorn

    from scqo.viewer.__main__ import main

    served = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: served.update(kw))

    fresh = tmp_path / "fresh_lab"
    fresh.mkdir()
    assert main(["--data-root", str(fresh), "--host", "127.0.0.1"]) == 0
    assert (fresh / "index.sqlite").is_file()  # empty index created
    assert served["host"] == "127.0.0.1"

    with pytest.raises(SystemExit, match="does not exist"):
        main(["--data-root", str(tmp_path / "typo_lab")])


def _row_chunk(page: str, run_id: str) -> str:
    """The runs-table row fragment following this run's link (up to </tr>)."""
    return page.split(f"/run/{run_id}", 1)[1].split("</tr>", 1)[0]


def test_runs_page_live_column(lab):
    """The updates column names the fields a run keeps LIVE on the device; a
    superseded run carries no live line; a pending run keeps its pending line."""
    page = lab["client"].get("/").text
    live_row = _row_chunk(page, lab["res2"]["run_id"])
    assert "live:" in live_row and "readout_freq (q0)" in live_row
    superseded_row = _row_chunk(page, lab["res"]["run_id"])
    assert "live:" not in superseded_row and "3/3 applied" in superseded_row
    pending_row = _row_chunk(page, lab["pend"]["run_id"])
    assert "3 pending" in pending_row


def test_device_page_values_link_to_source_runs(lab):
    """Strict match: each value links to the run that set it; manual writes are
    marked; the assertions are scoped to the VALUE tables (history links too)."""
    page = lab["client"].get("/device").text
    # slice to the value TABLE itself — the caption above it links the latest run
    state_table = page.split("Current calibration", 1)[1].split("<table>", 1)[1].split("</table>", 1)[0]
    assert f"/run/{lab['res2']['run_id']}" in state_table  # readout_freq -> its run
    assert f"/run/{lab['res']['run_id']}" not in state_table  # superseded: no credit
    physical_table = page.split("Physical parameters", 1)[1].split("</table>", 1)[0]
    assert f"/run/{lab['t1']['run_id']}" in physical_table  # t1_s -> its run
    assert "(manual)" in physical_table  # the notebook-written g_hz


def test_run_page_live_and_superseded_badges(lab):
    c = lab["client"]
    assert "LIVE on device" in c.get(f"/run/{lab['res2']['run_id']}").text
    superseded_page = c.get(f"/run/{lab['res']['run_id']}").text
    assert "LIVE on device" not in superseded_page
    assert f'<a href="/run/{lab["res2"]["run_id"]}" title=' in superseded_page  # superseded -> by whom


def test_device_page_flags_external_change(lab):
    """Hand-edit chipZ's state file: the strict-match rule must show the value as
    externally changed and credit NO run. (chipZ so devV fixtures stay pristine.)"""
    state_path = Path(lab["root"]) / "chipZ" / "cdZ" / "z_main" / "scqo" / "scqo_state.json"
    data = json.loads(state_path.read_text(encoding="utf-8"))
    data["config"]["q0"]["readout_freq"] = 9.9e9  # another tool wrote the config
    state_path.write_text(json.dumps(data), encoding="utf-8")

    page = lab["client"].get("/device", params={"device": "chipZ"}).text
    state_table = page.split("Current calibration", 1)[1].split("<table>", 1)[1].split("</table>", 1)[0]
    assert "(externally changed)" in state_table
    assert f"/run/{lab['chipz']['run_id']}" not in state_table  # never a false credit


def test_device_page_renders_one_section_per_setup(lab):
    """Two setups of one device = two independent calibration sections, each
    captioned with its own state file and holding only its own runs' history."""
    page = lab["client"].get("/device").text
    assert "setup <b>sim_main</b>" in page and "setup <b>sim_alt</b>" in page
    main_sec = page.split("setup <b>sim_main</b>", 1)[1].split("setup <b>sim_alt</b>", 1)[0]
    alt_sec = page.split("setup <b>sim_alt</b>", 1)[1]
    # each section names its own scqo/ folder path and shows only its own runs
    assert "sim_main" in main_sec and "scqo_state.json" in main_sec and lab["res2"]["run_id"] in main_sec
    assert "sim_alt" in alt_sec and lab["alt"]["run_id"] in alt_sec
    assert lab["alt"]["run_id"] not in main_sec  # no cross-setup bleed
    assert lab["res2"]["run_id"] not in alt_sec


def test_device_section_latest_run_link_is_per_setup(lab):
    """The 'latest run' caption in each per-setup calibration section must link
    that SETUP's own latest run — never the device-wide newest (here r_ghost, a
    run bound to a setup no longer in the active cycle)."""
    page = lab["client"].get("/device").text
    main_sec = page.split("setup <b>sim_main</b>", 1)[1].split("setup <b>sim_alt</b>", 1)[0]
    caption = main_sec.split("latest run:", 1)[1].split("</p>", 1)[0]
    assert lab["ghost"]["run_id"] not in caption  # not the foreign device-wide latest
    assert "/run/20" in caption  # a real sim_main run is linked


def test_runs_page_live_credit_is_per_setup(lab):
    """Each run's live credit comes from ITS OWN setup's state file; a run whose
    setup name is not in the active cycle gets none at all."""
    page = lab["client"].get("/").text
    assert "live:" in _row_chunk(page, lab["alt"]["run_id"])  # alt's own file credits it
    ghost_row = _row_chunk(page, lab["ghost"]["run_id"])
    assert "live:" not in ghost_row  # applied, but its setup vanished -> no credit


def test_run_page_vanished_setup_shows_no_on_device_state(lab):
    """An applied run bound to a setup absent from the active cycle: the viewer can
    resolve no state file for it, so the on-device column stays '-'."""
    page = lab["client"].get(f"/run/{lab['ghost']['run_id']}").text
    assert "Suggested updates" in page and "accepted" in page
    assert "LIVE on device" not in page and "superseded" not in page


def test_registry_less_device_shows_snapshot_only(lab):
    """No registry = no resolvable setups: the device page falls back to the last
    run's device_after snapshot and offers no per-setup calibration section."""
    page = lab["client"].get("/device", params={"device": "bare"}).text
    assert "Last observed calibration" in page
    assert "device_after snapshot" in page and lab["bare"]["run_id"] in page
    assert "Current calibration" not in page


def test_trends_never_mix_samples(lab):
    c = lab["client"]
    # q0 readout_freq exists on BOTH samples ("q1 exists on every chip" problem):
    # the default trend is scoped to the configured device, not the union.
    dev = c.get("/trends", params={"target": "q0", "quantity": "readout_freq"}).text
    assert lab["res"]["run_id"] in dev
    assert lab["chipz"]["run_id"] not in dev
    z = c.get("/trends", params={"target": "q0", "quantity": "readout_freq", "device": "chipZ"}).text
    assert lab["chipz"]["run_id"] in z and lab["res"]["run_id"] not in z
