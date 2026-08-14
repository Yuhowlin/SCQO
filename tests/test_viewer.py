"""Run-viewer: pages render from a real (simulated-run) datastore; the only write
is tag/note editing; file serving never escapes the run folder.

Change-history cutover note: provenance now comes from each context's
``history.sqlite`` (scqo.changes) — the viewer reads it strictly read-only.
Every setup of every cooldown cycle has its own page (``/setup/...``) carrying
the calibration + physical tables with current AND previous run links; the
device page holds the cycles table and the physical-facts matrix; ``/trends``
charts change history (port 1 = one parameter in one setup, latest 50; port 2 =
one physical fact across every context).
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
from scqo.changes import HISTORY_FILE, ChangeDB, ChangeRecord  # noqa: E402
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
    """A datastore with APPLIED runs on TWO setups of devV's active cycle plus a
    CLOSED earlier cycle (cdU/sim_old — the cross-cooldown fixtures), one
    PENDING run, a run stamped with a VANISHED setup name, a second sample
    (chipZ), a registry-less sample (bare), and a viewer client."""
    root = tmp_path_factory.mktemp("data")
    (root / "devV").mkdir()
    # Cycle registry BEFORE the runs; sessions bind their (cooldown, setup) era
    # explicitly and each context persists its OWN scqo/ folder. cdU is a
    # CLOSED cycle that ran first — registry order matches chronology.
    (root / "devV" / "cooldowns.toml").write_text(
        '[cdU]\nstart = 2026-06-01\nend = 2026-06-20\nfridge = "BlueforsA"\n\n'
        '[cdU.setup.sim_old]\nbackend = "simulated"\n\n'
        '[cdV]\nstart = 2026-07-01\nfridge = "BlueforsA"\npackaging = "PCB v3"\n\n'
        '[cdV.setup.sim_main]\nbackend = "simulated"\n'
        '[cdV.setup.sim_alt]\nbackend = "simulated"\n',
        encoding="utf-8",
    )
    # the CLOSED cycle's run lands FIRST (chronological change history)
    sess_old = _session(root, "devV", cid="cdU", setup="sim_old")
    r_old = sess_old.run("resonator_spectroscopy", {"targets": ["q0"]}, update="apply")

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

    # A finished campaign with aggregate suggestions: the q0 FACT is accepted
    # (the setup page gets a campaign-credited value; q0 deliberately, so q1's
    # run-credit assertions stay untouched), the q0_xy knob stays PENDING
    # (badge + filter stay exercisable). statistics.png is written by the CLI
    # layer, not run_campaign — a placeholder keeps matplotlib out of here.
    from scqo import CampaignPlan
    camp = sess.run_campaign(CampaignPlan(
        label="t1_watch", repeat=3, skip_artifacts=True,
        defaults={"targets": ["q0"]},
        steps=[{"experiment": "qubit_relaxation"}]))
    sess.accept_campaign(camp["campaign_id"], entities=["q0"])
    (Path(camp["data_path"]) / "statistics.png").write_bytes(
        b"\x89PNG\r\n\x1a\n placeholder")
    # ...and a hand-made RUNNING campaign: no png, no suggestions yet
    running_id, running_dir = sess.datastore.new_campaign_dir("live_probe")
    sess.datastore.persist_campaign(
        campaign_id=running_id, campaign_dir=running_dir,
        manifest={"started_at": "2026-08-09T09:00:00", "status": "running",
                  "backend": "simulated", "cooldown": "cdV",
                  "setup": "sim_main", "experiments": ["qubit_relaxation"],
                  "targets": ["q0"], "plan": {}, "statistics": {}})
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
    return {"client": client, "root": root, "old": r_old,
            "res": r_res, "res2": r_res2, "ram": r_ram,
            "t1": r_t1, "pend": r_pend, "alt": r_alt, "ghost": r_ghost,
            "chipz": r_z, "bare": r_bare,
            "campaign": camp, "running_id": running_id}


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


# ------------------------------------------------------------------- trends

def test_trends_chart_a_fact_across_the_device(lab):
    """Port 2: a physical fact charts from its change history, run-linked."""
    c = lab["client"]
    page = c.get("/trends", params={"entity": "q1", "field": "t1_s", "device": "devV"}).text
    assert "<svg" in page and "<circle" in page
    assert lab["t1"]["run_id"] in page


def test_trends_menu_offers_only_recorded_parameters(lab):
    """The menu is DB-derived: parameters WITH history appear (facts device-wide,
    state parameters per setup); a catalogued field nothing wrote does not."""
    page = lab["client"].get("/trends", params={"device": "devV"}).text
    assert "q1.t1_s" in page                       # a recorded fact
    assert "q0_res.g_hz" in page                   # the manual physical write
    assert "readout_freq_hz" in page               # a state parameter with rows
    assert "/setup/devV/cdV/sim_main" in page      # state block names its setup
    assert "pump_freq_hz" not in page              # catalogued, never written


def test_port1_trend_is_scoped_to_its_setup(lab):
    """Port 1: the same parameter name in ANOTHER setup shows that setup's
    changes only — an unmeasured context reads as empty, never as a neighbor's."""
    c = lab["client"]
    main = c.get("/trends", params={"device": "devV", "entity": "q0_ro",
                                    "field": "readout_freq_hz",
                                    "cooldown": "cdV", "setup": "sim_main"}).text
    assert "Latest 50 changes" in main
    assert lab["res"]["run_id"] in main and lab["res2"]["run_id"] in main
    assert lab["old"]["run_id"] not in main        # cdU's change stays in cdU
    alt = c.get("/trends", params={"device": "devV", "entity": "q0_ro",
                                   "field": "readout_freq_hz",
                                   "cooldown": "cdV", "setup": "sim_alt"}).text
    assert "No recorded changes" in alt            # sim_alt never wrote q0_ro


def test_port2_trend_crosses_cooldowns_with_context(lab):
    """Port 2: one fact across every context — cdU then cdV, a dashed cooldown
    separator, context-colored points, a legend, and setup-page links."""
    page = lab["client"].get("/trends", params={
        "device": "devV", "entity": "q0_res", "field": "f_r_hz"}).text
    assert lab["old"]["run_id"] in page and lab["res2"]["run_id"] in page
    assert 'class="ctxsep"' in page                # a cooldown boundary
    assert "ctx-0" in page and "ctx-1" in page     # two colored contexts
    assert '/setup/devV/cdU/sim_old' in page       # legend/table link contexts
    assert '/setup/devV/cdV/sim_main' in page
    assert "<th>cooldown</th>" in page             # the cross-context table
    # chronological: the closed cycle's change precedes the active cycle's
    table = page.split("<table", 1)[1]
    assert table.index(lab["old"]["run_id"]) > table.index(lab["res2"]["run_id"])  # newest-first table


def test_bare_trends_redirects_to_sample_overview(lab):
    """Device-less /trends is not a page of its own: a trend needs an explicit
    sample first, and the ONE sample picker is the /device overview (each row
    links its per-sample trends)."""
    resp = lab["client"].get("/trends", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/device"


def test_trends_never_mix_samples(lab):
    c = lab["client"]
    # q0_res.f_r_hz exists on BOTH samples ("q1 exists on every chip" problem):
    # there is NO silent default sample — device-less /trends bounces to the
    # sample overview, and a chart is always explicitly device-scoped.
    bare = c.get("/trends").text  # redirect followed → the /device overview
    assert "<svg" not in bare and "<circle" not in bare
    assert "/trends?device=devV" in bare and "/trends?device=chipZ" in bare
    dev = c.get("/trends", params={"entity": "q0_res", "field": "f_r_hz", "device": "devV"}).text
    assert lab["res"]["run_id"] in dev
    assert lab["chipz"]["run_id"] not in dev
    z = c.get("/trends", params={"entity": "q0_res", "field": "f_r_hz", "device": "chipZ"}).text
    assert lab["chipz"]["run_id"] in z and lab["res"]["run_id"] not in z


# --------------------------------------------------------------- setup pages

def test_setup_page_state_values_and_metadata(lab):
    """The per-setup page carries what the old device-page section held: the
    calibration values, the cycle facts and the setup metadata."""
    page = lab["client"].get("/setup/devV/cdV/sim_main").text
    assert "Setup: sim_main" in page
    assert "All samples" in page                   # breadcrumb to the overview
    assert "readout_freq_hz" in page               # a recorded knob renders
    assert "Current calibration" in page
    assert "simulated" in page and "(built-in)" in page  # backend metadata
    assert "PCB v3" in page                        # its cycle's facts


def test_setup_page_current_and_previous_run_links(lab):
    """Requirement: each parameter row shows ONLY the current value's run and
    the ONE-BEFORE run — here readout_freq_hz was set by res then res2."""
    page = lab["client"].get("/setup/devV/cdV/sim_main").text
    row = next(r for r in page.split("<tr")
               if ">readout_freq_hz</a>" in r and "q0_ro" in r)
    assert f"/run/{lab['res2']['run_id']}" in row  # source = current run
    assert f"/run/{lab['res']['run_id']}" in row   # previous = the run before
    # and no 200-row history tables anymore
    assert "Change history" not in page


def test_setup_page_physical_rows_and_manual_marker(lab):
    page = lab["client"].get("/setup/devV/cdV/sim_main").text
    physical = page.split("Physical parameters", 1)[1]
    assert f"/run/{lab['t1']['run_id']}" in physical  # t1_s -> its run
    assert "(manual)" in physical                     # the notebook-written g_hz
    for field in ("t1_s", "t2_star_s", "g_hz"):
        assert f">{field}</a>" in physical            # rows render, names link


def test_setup_page_param_names_link_scoped_trends(lab):
    page = lab["client"].get("/setup/devV/cdV/sim_main").text
    assert ("/trends?device=devV&entity=q0_ro&field=readout_freq_hz"
            "&cooldown=cdV&setup=sim_main") in page


def test_setup_pages_do_not_bleed_across_setups(lab):
    """Two setups of one device = two independent pages, each holding only its
    own runs' provenance."""
    c = lab["client"]
    main = c.get("/setup/devV/cdV/sim_main").text
    alt = c.get("/setup/devV/cdV/sim_alt").text
    assert lab["res2"]["run_id"] in main and lab["alt"]["run_id"] in alt
    assert lab["alt"]["run_id"] not in main        # no cross-setup bleed
    assert lab["res2"]["run_id"] not in alt


def test_setup_page_latest_run_link_is_its_own(lab):
    """The 'latest run' caption links this SETUP's own latest run — never the
    device-wide newest (here r_ghost, bound to a vanished setup name)."""
    page = lab["client"].get("/setup/devV/cdV/sim_main").text
    caption = page.split("latest run:", 1)[1].split("</p>", 1)[0]
    assert lab["ghost"]["run_id"] not in caption
    assert "/run/20" in caption


def test_setup_page_operator_on_trend_table(lab):
    """P3 attribution: the change trend shows WHO made each change."""
    import getpass

    page = lab["client"].get("/trends", params={
        "device": "devV", "entity": "q0_ro", "field": "readout_freq_hz",
        "cooldown": "cdV", "setup": "sim_main"}).text
    assert "<th>operator</th>" in page
    assert getpass.getuser() in page  # this test process's login, stamped on the runs


def test_setup_page_flags_external_change(lab):
    """Hand-edit chipZ's state file: the strict-match rule must show the value as
    externally changed and credit NO run. (chipZ so devV fixtures stay pristine.)"""
    state_path = Path(lab["root"]) / "chipZ" / "cdZ" / "z_main" / "scqo" / "scqo_state.json"
    data = json.loads(state_path.read_text(encoding="utf-8"))
    data["values"]["q0_ro"]["readout_freq_hz"] = 9.9e9  # another tool wrote the state
    state_path.write_text(json.dumps(data), encoding="utf-8")

    page = lab["client"].get("/setup/chipZ/cdZ/z_main").text
    tampered = next(r for r in page.split("<tr")
                    if ">readout_freq_hz</a>" in r and "q0_ro" in r)
    source_cell = tampered.split("<td")[5]         # the source column
    assert "externally changed" in source_cell
    assert "/run/" not in source_cell              # never a false credit
    # ... while the run's UNTOUCHED value is still credited: one hand-edit must
    # not discredit everything else the same run legitimately set.
    credited = next(r for r in page.split("<tr")
                    if ">readout_depletion_s</a>" in r)
    assert f"/run/{lab['chipz']['run_id']}" in credited


def test_setup_page_credits_a_campaign_accept(lab):
    c = lab["client"]
    cid = lab["campaign"]["campaign_id"]
    page = c.get("/setup/devV/cdV/sim_main").text
    # the accepted q0.t1_s fact links its value to the campaign, not a run
    assert f'href="/campaign/{cid}"' in page
    assert "(campaign)" in page


def test_setup_page_history_survives_values_only_reset(tmp_path):
    """REGRESSION: after the documented values-only reset (delete
    scqo_state.json — history.sqlite survives) the setup page must still render
    run-linked provenance; hiding it would silently break the cutover's
    guarantee that provenance is never collateral damage."""
    (tmp_path / "devR").mkdir()
    (tmp_path / "devR" / "cooldowns.toml").write_text(
        '[cdR]\nstart = 2026-07-01\n[cdR.setup.main]\nbackend = "simulated"\n',
        encoding="utf-8")
    sess = _session(tmp_path, "devR", cid="cdR", setup="main")
    r = sess.run("resonator_spectroscopy", {"targets": ["q0"]}, update="apply")
    (_scqo_dir(tmp_path, "devR", "cdR", "main") / "scqo_state.json").unlink()

    page = TestClient(create_app(tmp_path)).get("/setup/devR/cdR/main").text
    assert "device_after snapshot" in page          # values fell back...
    assert f"/run/{r['run_id']}" in page            # ...provenance did NOT


def test_setup_page_unknown_context_renders_empty_not_404(lab):
    c = lab["client"]
    page = c.get("/setup/devV/cdV/nonexistent")
    assert page.status_code == 200
    assert "No calibration recorded" in page.text
    assert "not in cooldowns.toml" in page.text
    # only a non-filename-safe segment 404s
    assert c.get("/setup/devV/cd%20bad/main").status_code == 404


# --------------------------------------------------------------- device page

def test_device_page_cycles_link_every_setup(lab):
    """EVERY cycle's setups — closed cycles included — are links to their own
    setup pages; the metadata table moved there."""
    page = lab["client"].get("/device", params={"device": "devV"}).text
    assert "Device: devV" in page and "Cooldown cycles" in page
    assert "cdV" in page and "(active)" in page
    assert "PCB v3" in page                        # packaging is a cycle fact
    assert 'href="/setup/devV/cdV/sim_main"' in page
    assert 'href="/setup/devV/cdV/sim_alt"' in page
    assert 'href="/setup/devV/cdU/sim_old"' in page  # the CLOSED cycle links too


def test_device_page_matrix_facts_by_context(lab):
    """The physical-facts matrix: rows = facts linking their port-2 trend,
    columns = contexts linking their setup pages, cells = latest values. The
    ghost run's escape-hatch physics (device-level history) is a flagged
    fourth column — shown, never hidden."""
    page = lab["client"].get("/device", params={"device": "devV"}).text
    assert "Physical properties" in page
    matrix = page.split("Physical properties", 1)[1].split("</table>", 1)[0]
    assert "cdU/sim_old" in matrix and "cdV/sim_main" in matrix
    assert "(device-level)" in matrix              # the ghost run's physics
    assert ("/trends?device=devV&entity=q0_res&field=f_r_hz") in matrix
    row = next(r for r in matrix.split("<tr") if "q0_res.f_r_hz" in r)
    assert row.count("<td") == 5                   # parameter + 4 context cells
    assert ">-</td>" in row                        # sim_alt never measured q0


def test_device_page_matrix_flags_ghost_contexts(tmp_path):
    """A context on disk but not in cooldowns.toml still shows — flagged, never
    hidden (hiding it would make the matrix disagree with the trend chart)."""
    (tmp_path / "devG").mkdir()
    (tmp_path / "devG" / "cooldowns.toml").write_text(
        '[cd1]\nstart = 2026-07-01\n[cd1.setup.main]\nbackend = "simulated"\n',
        encoding="utf-8")
    for cid, setup in (("cd1", "main"), ("cdX", "mystery")):
        db = ChangeDB(_scqo_dir(tmp_path, "devG", cid, setup) / HISTORY_FILE)
        with db.transaction() as con:
            ChangeDB.insert(con, [ChangeRecord(
                timestamp=f"2026-07-0{1 if cid == 'cd1' else 2}T10:00:00+08:00",
                entity="q0", field="t1_s", old=None, new=2.5e-5,
                cooldown=cid, setup=setup)], store="physical")
    page = TestClient(create_app(tmp_path)).get(
        "/device", params={"device": "devG"}).text
    assert "cd1/main" in page
    assert "cdX/mystery" in page                   # the ghost column renders...
    assert "not in cooldowns.toml" in page         # ...flagged


def test_registry_less_device_shows_snapshot_only(lab):
    """No registry = no setup pages: the device page falls back to the last
    run's device_after snapshot for the KNOBS (values only), while the
    escape-hatch device-level facts still render in the matrix."""
    page = lab["client"].get("/device", params={"device": "bare"}).text
    assert "Last observed calibration" in page
    assert "device_after snapshot" in page and lab["bare"]["run_id"] in page
    assert "Current calibration" not in page
    assert "(device-level)" in page  # the escape-hatch physics column


def test_multi_device_filter_and_device_page(lab):
    c = lab["client"]
    rid = lab["chipz"]["run_id"]

    only_z = c.get("/", params={"device": "chipZ"}).text
    assert rid in only_z and lab["res"]["run_id"] not in only_z

    page_z = c.get("/device", params={"device": "chipZ"}).text
    assert "Device: chipZ" in page_z
    assert "second sample on the other fridge" in page_z  # devices.toml card rendered
    assert rid in c.get("/setup/chipZ/cdZ/z_main").text   # provenance on ITS page


def test_viewer_never_creates_history_databases(tmp_path):
    """The read-only invariant: rendering every page of a DB-less device mints
    no history.sqlite anywhere (sqlite3.connect would create one)."""
    (tmp_path / "devN").mkdir()
    (tmp_path / "devN" / "cooldowns.toml").write_text(
        '[cd1]\nstart = 2026-07-01\n[cd1.setup.main]\nbackend = "simulated"\n',
        encoding="utf-8")
    scqo_dir = _scqo_dir(tmp_path, "devN", "cd1", "main")
    scqo_dir.mkdir(parents=True)
    (scqo_dir / "scqo_state.json").write_text(
        '{"schema": 3, "values": {"q0_ro": {"readout_freq_hz": 5.9e9}}}',
        encoding="utf-8")
    c = TestClient(create_app(tmp_path))
    for url in ("/device?device=devN", "/setup/devN/cd1/main",
                "/trends?device=devN",
                "/trends?device=devN&entity=q0&field=t1_s",
                "/trends?device=devN&entity=q0_ro&field=readout_freq_hz"
                "&cooldown=cd1&setup=main"):
        assert c.get(url).status_code == 200
    assert list(tmp_path.rglob(HISTORY_FILE)) == []


# ---------------------------------------------------------- runs / run pages

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


def test_run_page_marks_operator_suggestion(lab):
    """A human-attached value (Session.suggest / scqo suggest) renders with the
    operator badge; estimator rows never carry it."""
    page = lab["client"].get(f"/run/{lab['t1']['run_id']}").text
    assert 'class="badge operator"' in page
    assert "read off the decay" in page  # the proposal comment is shown
    page_estimator = lab["client"].get(f"/run/{lab['res']['run_id']}").text
    assert "badge operator" not in page_estimator


def test_run_page_links_its_setup_page(lab):
    page = lab["client"].get(f"/run/{lab['res']['run_id']}").text
    assert 'href="/setup/devV/cdV/sim_main"' in page


def test_runs_page_pending_filter_and_updates_column(lab):
    c = lab["client"]
    page = c.get("/", params={"pending": "1"}).text
    assert lab["pend"]["run_id"] in page
    assert lab["res"]["run_id"] not in page  # applied at run time -> nothing pending
    full = c.get("/").text
    assert "4 pending" in full  # the updates column flags the undecided run


def test_runs_page_cooldown_filter_and_column(lab):
    c = lab["client"]
    page = c.get("/", params={"cooldown": "cdV"}).text
    assert lab["res"]["run_id"] in page  # devV runs were stamped with the active cycle
    assert c.get("/", params={"cooldown": "nope"}).text.count("/run/") == 0


def test_runs_page_setup_filter_and_column(lab):
    """Runs stamped with a setup NAME link it in the setup column; ?setup= filters."""
    c = lab["client"]
    page = c.get("/").text
    assert "<th>setup</th>" in page
    assert ">sim_main</a>" in _row_chunk(page, lab["res"]["run_id"])
    assert ">z_main</a>" in _row_chunk(page, lab["chipz"]["run_id"])
    # the registry-less sample's run carries no setup name
    assert "sim_main" not in _row_chunk(page, lab["bare"]["run_id"])

    filtered = c.get("/", params={"setup": "sim_main"}).text
    assert lab["res"]["run_id"] in filtered and lab["ram"]["run_id"] in filtered
    assert lab["alt"]["run_id"] not in filtered  # the other setup's run
    assert lab["chipz"]["run_id"] not in filtered
    assert c.get("/", params={"setup": "nope"}).text.count("/run/") == 0


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


def test_run_page_live_and_superseded_badges(lab):
    c = lab["client"]
    assert "LIVE on device" in c.get(f"/run/{lab['res2']['run_id']}").text
    superseded_page = c.get(f"/run/{lab['res']['run_id']}").text
    assert "LIVE on device" not in superseded_page
    assert f'<a href="/run/{lab["res2"]["run_id"]}" title=' in superseded_page  # superseded -> by whom


def test_runs_page_live_credit_is_per_setup(lab):
    """Each run's live credit comes from ITS OWN setup's context; a run whose
    setup name is not in the active cycle gets none at all."""
    page = lab["client"].get("/").text
    assert "live:" in _row_chunk(page, lab["alt"]["run_id"])  # alt's own context credits it
    ghost_row = _row_chunk(page, lab["ghost"]["run_id"])
    assert "live:" not in ghost_row  # applied, but its setup vanished -> no credit


def test_run_page_vanished_setup_shows_no_on_device_state(lab):
    """An applied run bound to a setup absent from the active cycle: the viewer can
    resolve no state for it, so the on-device column stays '-'."""
    page = lab["client"].get(f"/run/{lab['ghost']['run_id']}").text
    assert "Suggested updates" in page and "accepted" in page
    assert "LIVE on device" not in page and "superseded" not in page


# ------------------------------------------------------------------ overview

def test_device_overview_lists_all_samples(lab):
    """Bare /device is the lab-wide sample overview: EVERY known sample appears —
    indexed ones and index-free ones (fresh folder+cycle, registry-only) alike —
    with description, the active cycle's setup LINKS, latest run, trends."""
    page = lab["client"].get("/device").text
    for name in ("devV", "chipZ", "bare", "freshY", "paperX"):
        assert f"/device?device={name}" in page
        assert f"/trends?device={name}" in page
    assert "second sample on the other fridge" in page  # devices.toml description
    assert "registry-only sample" in page  # paperX exists ONLY in devices.toml
    assert 'href="/setup/devV/cdV/sim_main"' in page   # active-cycle setup links
    assert 'href="/setup/freshY/cdF/f_main"' in page   # cycle declared, no runs yet
    assert 'href="/setup/devV/cdU/sim_old"' not in page  # closed cycles: detail page only
    assert "no runs yet" in page
    assert "/run/20" in page  # some sample's latest run is linked


def test_device_overview_tolerates_broken_cooldowns(lab):
    """One sample's broken cooldowns.toml degrades to an inline per-row error —
    the overview still renders every other sample (never a 500)."""
    resp = lab["client"].get("/device")
    assert resp.status_code == 200
    row = resp.text.split("brokenB", 2)[2].split("</tr>", 1)[0]
    assert "cooldowns.toml error:" in row
    assert 'href="/setup/devV/cdV/sim_main"' in resp.text  # healthy rows unaffected


# ---------------------------------------------------------------- campaigns

def test_campaigns_page_lists_filters_and_badges(lab):
    c = lab["client"]
    cid = lab["campaign"]["campaign_id"]
    page = c.get("/campaigns").text
    assert cid in page and lab["running_id"] in page
    assert 'href="/campaigns"' in c.get("/").text  # the nav link

    running_only = c.get("/campaigns", params={"status": "running"}).text
    assert lab["running_id"] in running_only and cid not in running_only

    pending_only = c.get("/campaigns", params={"pending": "1"}).text
    assert cid in pending_only            # the q0_xy knob is still pending
    assert lab["running_id"] not in pending_only

    assert cid not in c.get("/campaigns", params={"device": "chipZ"}).text


def test_campaign_page_statistics_children_and_suggestions(lab):
    c = lab["client"]
    cid = lab["campaign"]["campaign_id"]
    page = c.get(f"/campaign/{cid}").text
    assert "t1_s" in page and "qubit_relaxation" in page  # statistics rows
    assert f"/campaign/{cid}/statistics.png" in page      # the figure img

    # children in execution order, linked
    child_ids = [s["run_id"] for r in lab["campaign"]["repeats"] for s in r["steps"]]
    assert len(child_ids) == 3
    for rid in child_ids:
        assert f"/run/{rid}" in page
    assert page.index(child_ids[0]) < page.index(child_ids[1]) < page.index(child_ids[2])

    # the suggestion block: the accepted fact and the pending knob, grouped
    assert "Suggested updates (1 pending)" in page
    assert "accepted" in page and "thermalization_time_s" in page
    assert f"scqo accept --campaign {cid}" in page


def test_campaign_statistics_png_serves_and_degrades(lab):
    c = lab["client"]
    cid = lab["campaign"]["campaign_id"]
    resp = c.get(f"/campaign/{cid}/statistics.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/png")

    # a running campaign renders (no img, a live note, no suggestions block)
    page = c.get(f"/campaign/{lab['running_id']}")
    assert page.status_code == 200
    assert "still running" in page.text
    assert "statistics.png" not in page.text
    assert c.get(f"/campaign/{lab['running_id']}/statistics.png").status_code == 404

    assert c.get("/campaign/no-such-campaign").status_code == 404
    assert c.get("/campaign/no-such-campaign/statistics.png").status_code == 404


def test_run_page_links_back_to_its_campaign(lab):
    c = lab["client"]
    cid = lab["campaign"]["campaign_id"]
    child = lab["campaign"]["repeats"][1]["steps"][0]["run_id"]
    page = c.get(f"/run/{child}").text
    assert f'href="/campaign/{cid}"' in page and "(r1 s0)" in page
    # a standalone run carries no backlink
    assert "/campaign/" not in c.get(f"/run/{lab['ram']['run_id']}").text


def test_runs_page_campaign_column_and_filter(lab):
    c = lab["client"]
    cid = lab["campaign"]["campaign_id"]
    child_ids = {s["run_id"] for r in lab["campaign"]["repeats"] for s in r["steps"]}
    page = c.get("/", params={"campaign": cid}).text
    assert all(rid in page for rid in child_ids)
    assert lab["ram"]["run_id"] not in page
    # the column cell links the child to its campaign
    assert f'href="/campaign/{cid}"' in page and "r0 s0" in page


# ------------------------------------------------------------- setup exports

def _pdf_pages(data: bytes) -> int:
    """Pages in a matplotlib PDF: /Type /Page objects minus the /Type /Pages
    tree node, whose text also matches the shorter substring."""
    return data.count(b"/Type /Page") - data.count(b"/Type /Pages")


def test_setup_export_html_is_self_contained(lab):
    """The offline export is ONE file a recipient opens with no data drive and
    no viewer: run links are in-page anchors to embedded snapshots, figures are
    data: URIs, and no href leaves the file."""
    resp = lab["client"].get("/setup/devV/cdV/sim_main/export.html")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["content-disposition"] == \
        'attachment; filename="devV_cdV_sim_main.html"'
    page = resp.text
    assert "Calibration" in page and "Physical parameters" in page
    assert ">previous</th>" not in page          # the column is export-excluded
    assert "<nav>" not in page
    # one assertion kills every server route AND every file:// path at once
    assert 'href="/' not in page and 'href="file:' not in page
    rid = lab["res2"]["run_id"]                  # the run crediting readout_freq_hz
    assert f'href="#run-{rid}"' in page          # source link -> in-file anchor...
    assert f'id="run-{rid}"' in page             # ...whose target section exists
    assert "data:image/png;base64," in page      # figures ride inside the file
    assert "Exported" in page and lab["root"].name in page


def test_setup_export_html_embeds_run_fit_and_campaign(lab):
    page = lab["client"].get("/setup/devV/cdV/sim_main/export.html").text
    cid = lab["campaign"]["campaign_id"]
    # the campaign-accepted q0.t1_s: anchor + embedded section with statistics
    assert f'href="#campaign-{cid}"' in page and "(campaign)" in page
    assert f'id="campaign-{cid}"' in page and "qubit_relaxation" in page
    assert "(manual)" in page                    # the notebook-written g_hz
    # the t1 run's embedded snapshot carries its fit table and a way back up
    section = page.split(f'id="run-{lab["t1"]["run_id"]}"', 1)[1] \
                  .split("</section>", 1)[0]
    assert "t1_s" in section and "back to top" in section


def test_setup_export_html_flags_external_change(lab):
    # Re-apply the SAME hand-edit test_setup_page_flags_external_change makes —
    # idempotent, so this test never depends on the other one having run.
    state_path = (Path(lab["root"]) / "chipZ" / "cdZ" / "z_main" / "scqo"
                  / "scqo_state.json")
    data = json.loads(state_path.read_text(encoding="utf-8"))
    data["values"]["q0_ro"]["readout_freq_hz"] = 9.9e9
    state_path.write_text(json.dumps(data), encoding="utf-8")

    page = lab["client"].get("/setup/chipZ/cdZ/z_main/export.html").text
    assert "externally changed" in page          # drifted value: no credit
    # the same run's untouched value keeps its anchor to the embedded snapshot
    assert f'href="#run-{lab["chipz"]["run_id"]}"' in page


def test_setup_export_xlsx_round_trip(lab):
    openpyxl = pytest.importorskip("openpyxl")
    import io

    from scqo.report import FIELD_UNITS
    from scqo.viewer._export import XLSX_MEDIA_TYPE

    resp = lab["client"].get("/setup/devV/cdV/sim_main/export.xlsx")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == XLSX_MEDIA_TYPE
    assert resp.headers["content-disposition"] == \
        'attachment; filename="devV_cdV_sim_main.xlsx"'
    assert resp.content[:2] == b"PK"             # a real zip container

    wb = openpyxl.load_workbook(io.BytesIO(resp.content))
    assert wb.sheetnames == ["Calibration", "Physical parameters"]
    phys = wb["Physical parameters"]
    assert next(phys.iter_rows(max_row=1, values_only=True)) == \
        ("entity", "parameter", "value", "unit", "source")
    rows = {(r[0], r[1]): r for r in phys.iter_rows(min_row=2, values_only=True)}
    t1 = rows[("q1", "t1_s")]
    assert isinstance(t1[2], float)              # values stay native numbers
    assert t1[3] == FIELD_UNITS["t1_s"]
    assert t1[4] == f"run:{lab['t1']['run_id']}"
    assert rows[("q0", "t1_s")][4] == f"campaign:{lab['campaign']['campaign_id']}"
    assert rows[("q0_res", "g_hz")][4] == "manual"

    # a valid-but-unknown context degrades to an empty workbook (page precedent)
    resp = lab["client"].get("/setup/devV/cdV/nonexistent/export.xlsx")
    assert resp.status_code == 200
    wb = openpyxl.load_workbook(io.BytesIO(resp.content))
    assert [ws.max_row for ws in wb.worksheets] == [1, 1]  # headers only


def test_setup_export_pdf_mirrors_the_offline_page(lab):
    """The pdf is a DOCUMENT twin of the offline html — front matter + the two
    parameter tables, then the referenced-run appendix with the figures
    embedded — flowing across pages, not per-source-kind slides."""
    resp = lab["client"].get("/setup/devV/cdV/sim_main/export.pdf")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content[:5] == b"%PDF-"
    # sim_main embeds 4 runs (res2/ram/t1 credited + pend as latest), each with
    # figures — the appendix alone spans pages; exact counts float with figure
    # aspect, the floor does not
    assert _pdf_pages(resp.content) >= 4
    assert len(resp.content) > 50_000            # the figures ride inside

    resp = lab["client"].get("/setup/devV/cdV/nonexistent/export.pdf")
    assert resp.status_code == 200
    assert _pdf_pages(resp.content) == 1         # title + empty-state captions


def test_setup_export_bad_slug_404s_all_three(lab):
    c = lab["client"]
    for ext in ("html", "xlsx", "pdf"):
        assert c.get(f"/setup/devV/cd%20bad/main/export.{ext}").status_code == 404


def test_setup_export_xlsx_missing_openpyxl_is_a_clear_503(lab, monkeypatch):
    """An old viewer venv (extra not reinstalled) must get the reinstall hint,
    never a 500."""
    import sys

    monkeypatch.setitem(sys.modules, "openpyxl", None)
    resp = lab["client"].get("/setup/devV/cdV/sim_main/export.xlsx")
    assert resp.status_code == 503
    assert "openpyxl" in resp.json()["detail"]
