"""Run-viewer: pages render from a real (simulated-run) datastore; the only write
is tag/note editing; file serving never escapes the run folder.

Greenfield migration note: nothing was dropped here — every assertion was
re-pointed at the entity model (``q0_ro.readout_freq_hz`` instead of
``q0.readout_freq``, ``q0_res.g_hz``, ``fidelity_g``/``fidelity_e`` instead of
the deleted ``readout_fidelity``) and at the schema-3 store files (top-level
``"values"``, no ``"config"``).
"""

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

from scqo import Session  # noqa: E402
from scqo.testing import SimulatedBackend, demo_device  # noqa: E402
from scqo.viewer.app import create_app  # noqa: E402


def _scqo_dir(root: Path, dev: str, cid: str, setup: str) -> Path:
    """The per-(cooldown, setup) scqo/ folder holding both value stores."""
    return root / dev / cid / setup / "scqo"


def _session(root: Path, dev: str, *, cid: str = "", setup: str = "",
             persist: bool = True) -> Session:
    """A session on a FRESH demo device (fixed-frequency q0/q1 + pair), bound to
    one (cooldown, setup) era. ``persist=False`` binds the era stamps but keeps
    NO scqo/ folder — the vanished-setup fixture."""
    roster, design, vendor = demo_device()
    return Session(
        SimulatedBackend(vendor), roster, design=design, data_root=root,
        device_name=dev,
        scqo_dir=(_scqo_dir(root, dev, cid, setup)
                  if persist and cid and setup else None),
        state_sync="push", setup_name=setup, cooldown_id=cid,
    )


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
    sess = _session(root, "devV", cid="cdV", setup="sim_main")
    r_res = sess.run("resonator_spectroscopy", {"targets": ["q0"]}, update="apply", tags=["cool1"])
    # a second applied run SUPERSEDES r_res's readout_freq_hz (live-source tests)
    r_res2 = sess.run("resonator_spectroscopy", {"targets": ["q0"]}, update="apply", tags=["cool1"])
    r_ram = sess.run("qubit_ramsey", {"targets": ["q1"], "num_points": 201}, update="apply",
                     tags=["cool1", "special"])
    r_t1 = sess.run("qubit_relaxation", {"targets": ["q1"]}, update="apply", tags=["cool1"])
    # a HUMAN-attached proposal on the T1 run (scqo suggest; left pending)
    sess.suggest(r_t1["run_id"], {"q1.t1_s": 2.4e-5}, comment="read off the decay")
    r_pend = sess.run("resonator_spectroscopy", {"targets": ["q0"]}, tags=["cool1"])  # left pending
    # a q0_res-only physical value -> a "(manual)" source in this context's ledger
    sess.physical.record("q0_res", "g_hz", 80e6)
    sess.physical.save()
    # the SECOND setup of the same device: its own scqo/ folder, its own history
    sess_alt = _session(root, "devV", cid="cdV", setup="sim_alt")
    r_alt = sess_alt.run("resonator_spectroscopy", {"targets": ["q1"]}, update="apply", tags=["cool1"])
    # a run bound to a setup name NOT in the active cycle (bound eras are stamped
    # verbatim, never re-validated): must get NO live credit anywhere
    sess_ghost = _session(root, "devV", cid="cdV", setup="ghost", persist=False)
    r_ghost = sess_ghost.run("resonator_spectroscopy", {"targets": ["q0"]}, update="apply")

    # second physical sample with its own registry + one persisted setup
    (root / "chipZ").mkdir()
    (root / "chipZ" / "cooldowns.toml").write_text(
        '[cdZ]\nstart = 2026-07-02\n'
        '[cdZ.setup.z_main]\nbackend = "simulated"\n',
        encoding="utf-8",
    )
    sess_z = _session(root, "chipZ", cid="cdZ", setup="z_main")
    r_z = sess_z.run("resonator_spectroscopy", {"targets": ["q0"]}, update="apply", tags=["zcool"])
    (root / "devices.toml").write_text(
        '[chipZ]\ndescription = "second sample on the other fridge"\n\n'
        '[paperX]\ndescription = "registry-only sample"\n',
        encoding="utf-8",
    )

    # a registry-less sample: runs exist, no setups -> snapshot-only device page
    sess_b = _session(root, "bare")
    r_bare = sess_b.run("resonator_spectroscopy", {"targets": ["q0"]}, update="apply")

    # three INDEX-FREE samples for the overview pages (none has runs, so no
    # existing test's counts move): a freshly added sample with a cycle but no
    # runs yet, a registry-only entry (paperX above), and a broken registry.
    (root / "freshY").mkdir()
    (root / "freshY" / "cooldowns.toml").write_text(
        '[cdF]\nstart = 2026-07-03\n[cdF.setup.f_main]\nbackend = "simulated"\n',
        encoding="utf-8",
    )
    (root / "brokenB").mkdir()
    (root / "brokenB" / "cooldowns.toml").write_text("not [valid toml\n", encoding="utf-8")

    client = TestClient(create_app(root))
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
    page = c.get("/trends", params={"target": "q1", "quantity": "t1_s", "device": "devV"}).text
    assert "<svg" in page and "<circle" in page
    assert lab["t1"]["run_id"] in page


def test_device_page_state_and_history(lab):
    c = lab["client"]
    page = c.get("/device", params={"device": "devV"}).text
    assert "Device: devV" in page
    assert "All samples" in page  # the detail page always links back to the overview
    # the calibration table holds what the runs WROTE (schema-3 stores carry a
    # field only once it is recorded — an untouched vendor knob is not state)
    assert "readout_freq_hz" in page
    assert "Change history" in page
    assert lab["res"]["run_id"] in page  # history entry links to its run


def test_device_page_history_operator_column(lab):
    """P3 attribution: the change history shows WHO made each change."""
    import getpass

    page = lab["client"].get("/device", params={"device": "devV"}).text
    assert "<th>operator</th>" in page
    assert getpass.getuser() in page  # this test process's login, stamped on the runs


def test_physical_panel_is_per_setup_section(lab):
    """Physical values live inside their setup's section (per (cooldown, setup)
    context) — flat rows, no setup column. Only sim_main measured physics here."""
    page = lab["client"].get("/device", params={"device": "devV"}).text
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
    sess = _session(tmp_path, "devR", cid="cdR", setup="main")
    r = sess.run("resonator_spectroscopy", {"targets": ["q0"]}, update="apply")
    (_scqo_dir(tmp_path, "devR", "cdR", "main") / "scqo_state.json").unlink()  # sidecar survives

    page = TestClient(create_app(tmp_path)).get("/device", params={"device": "devR"}).text
    assert "Change history" in page
    assert r["run_id"] in page  # rows render from the surviving sidecar
    assert "All samples" in page  # single-sample root: the overview link renders anyway


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
    assert "4 pending" in full  # the updates column flags the undecided run


def test_trends_offer_descriptor_quantities(lab):
    # the quantity menu lives on the device-scoped chart page, not the picker
    page = lab["client"].get("/trends", params={"device": "devV"}).text
    assert "t2_echo_s" in page
    assert "fidelity_g" in page and "fidelity_e" in page


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
    page = lab["client"].get("/device", params={"device": "devV"}).text
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
    assert "live:" in live_row and "readout_freq_hz (q0_ro)" in live_row
    superseded_row = _row_chunk(page, lab["res"]["run_id"])
    assert "live:" not in superseded_row and "4/4 applied" in superseded_row
    pending_row = _row_chunk(page, lab["pend"]["run_id"])
    assert "4 pending" in pending_row


def test_device_page_values_link_to_source_runs(lab):
    """Strict match: each value links to the run that set it; manual writes are
    marked; the assertions are scoped to the VALUE tables (history links too)."""
    page = lab["client"].get("/device", params={"device": "devV"}).text
    # slice to the value TABLE itself — the caption above it links the latest run
    state_table = page.split("Current calibration", 1)[1].split("<table>", 1)[1].split("</table>", 1)[0]
    assert f"/run/{lab['res2']['run_id']}" in state_table  # readout_freq_hz -> its run
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
    data["values"]["q0_ro"]["readout_freq_hz"] = 9.9e9  # another tool wrote the state
    state_path.write_text(json.dumps(data), encoding="utf-8")

    page = lab["client"].get("/device", params={"device": "chipZ"}).text
    state_table = page.split("Current calibration", 1)[1].split("<table>", 1)[1].split("</table>", 1)[0]
    # Fields are COLUMNS here, so scope to CELLS: provenance is per VALUE, and
    # this run writes more than one knob (readout_freq_hz + readout_depletion_s).
    q0_ro = next(r for r in state_table.split("<tr")
                 if "q0_ro" in r and "externally changed" in r)
    cells = q0_ro.split("<td")
    tampered = [c for c in cells if "externally changed" in c]
    credited = [c for c in cells if f"/run/{lab['chipz']['run_id']}" in c]

    assert tampered
    assert not any("/run/" in c for c in tampered)  # never a false credit
    # ... while the run's UNTOUCHED value is still credited: one hand-edit must
    # not discredit everything else the same run legitimately set.
    assert credited


def test_device_page_renders_one_section_per_setup(lab):
    """Two setups of one device = two independent calibration sections, each
    captioned with its own state file and holding only its own runs' history."""
    page = lab["client"].get("/device", params={"device": "devV"}).text
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
    page = lab["client"].get("/device", params={"device": "devV"}).text
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
    # q0 readout_freq_hz exists on BOTH samples ("q1 exists on every chip" problem):
    # there is NO silent default sample — device-less /trends bounces to the
    # sample overview, and a chart is always explicitly device-scoped.
    bare = c.get("/trends").text  # redirect followed → the /device overview
    assert "<svg" not in bare and "<circle" not in bare
    assert "/trends?device=devV" in bare and "/trends?device=chipZ" in bare
    dev = c.get("/trends", params={"target": "q0", "quantity": "readout_freq_hz", "device": "devV"}).text
    assert lab["res"]["run_id"] in dev
    assert lab["chipz"]["run_id"] not in dev
    z = c.get("/trends", params={"target": "q0", "quantity": "readout_freq_hz", "device": "chipZ"}).text
    assert lab["chipz"]["run_id"] in z and lab["res"]["run_id"] not in z


def test_device_overview_lists_all_samples(lab):
    """Bare /device is the lab-wide sample overview: EVERY known sample appears —
    indexed ones and index-free ones (fresh folder+cycle, registry-only) alike —
    with description, active cooldown, latest run, and detail + trends links."""
    page = lab["client"].get("/device").text
    for name in ("devV", "chipZ", "bare", "freshY", "paperX"):
        assert f"/device?device={name}" in page
        assert f"/trends?device={name}" in page
    assert "second sample on the other fridge" in page  # devices.toml description
    assert "registry-only sample" in page  # paperX exists ONLY in devices.toml
    assert "cdV (2 setups)" in page
    assert "cdF (1 setup)" in page  # freshY: cycle declared, nothing measured yet
    assert "no runs yet" in page
    assert "/run/20" in page  # some sample's latest run is linked


def test_device_overview_tolerates_broken_cooldowns(lab):
    """One sample's broken cooldowns.toml degrades to an inline per-row error —
    the overview still renders every other sample (never a 500)."""
    resp = lab["client"].get("/device")
    assert resp.status_code == 200
    row = resp.text.split("brokenB", 2)[2].split("</tr>", 1)[0]
    assert "cooldowns.toml error:" in row
    assert "cdV (2 setups)" in resp.text  # the healthy rows are unaffected


def test_bare_trends_redirects_to_sample_overview(lab):
    """Device-less /trends is not a page of its own: a trend needs an explicit
    sample first, and the ONE sample picker is the /device overview (each row
    links its per-sample trends)."""
    resp = lab["client"].get("/trends", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/device"
