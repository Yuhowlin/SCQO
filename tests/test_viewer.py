"""Run-viewer: pages render from a real (simulated-run) datastore; the only write
is tag/note editing; file serving never escapes the run folder."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from scqo import Session, register  # noqa: E402
from scqo.experiments import QubitRamsey, ResonatorSpectroscopy, T1Relaxation  # noqa: E402
from scqo.testing import InMemoryDevice, SimulatedBackend  # noqa: E402
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
class _VT1(T1Relaxation):
    def probe(self):
        return None


def _device() -> InMemoryDevice:
    return InMemoryDevice(
        {
            "q0": {"readout_freq": 5.95e9, "drive_freq": 3.87e9, "pi_amp": 0.2, "readout_amp": 0.25},
            "q1": {"readout_freq": 6.05e9, "drive_freq": 4.01e9, "pi_amp": 0.18, "readout_amp": 0.22},
        }
    )


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    """A datastore with three runs (res spec, ramsey, t1), a SECOND sample sharing the
    same data_root (multi-device paths), and a viewer client over it all."""
    root = tmp_path_factory.mktemp("data")
    state = root / "scqo_state.json"
    sess = Session(
        SimulatedBackend(_device()), data_root=root, device_name="devV",
        state_path=str(state), state_sync="push",
    )
    r_res = sess.run("resonator_spectroscopy", {"qubits": ["q0"]}, tags=["cool1"])
    r_ram = sess.run("qubit_ramsey", {"qubits": ["q1"], "num_points": 201}, tags=["cool1", "special"])
    r_t1 = sess.run("t1_relaxation", {"qubits": ["q1"]}, tags=["cool1"])

    # second physical sample; its state file follows the
    # <data_root>/<device>/scqo_state.json convention the viewer resolves
    (root / "chipZ").mkdir()
    sess_z = Session(
        SimulatedBackend(_device()), data_root=root, device_name="chipZ",
        state_path=str(root / "chipZ" / "scqo_state.json"), state_sync="push",
    )
    r_z = sess_z.run("resonator_spectroscopy", {"qubits": ["q0"]}, tags=["zcool"])
    (root / "devices.toml").write_text(
        '[chipZ]\ndescription = "second sample on the other fridge"\nmounted_on = "qm"\n',
        encoding="utf-8",
    )

    client = TestClient(create_app(root, device_name="devV", state_path=state))
    return {"client": client, "root": root, "res": r_res, "ram": r_ram, "t1": r_t1, "chipz": r_z}


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
    page = c.get("/trends", params={"qubit": "q1", "quantity": "t1_s"}).text
    assert "<svg" in page and "<circle" in page
    assert lab["t1"]["run_id"] in page


def test_device_page_state_and_history(lab):
    c = lab["client"]
    page = c.get("/device").text
    assert "Device: devV" in page  # default = the configured sample
    assert "readout_amp" in page  # last-observed calibration table
    assert "Change history" in page
    assert lab["res"]["run_id"] in page  # history entry links to its run


def test_multi_device_filter_and_device_page(lab):
    c = lab["client"]
    rid = lab["chipz"]["run_id"]

    only_z = c.get("/", params={"device": "chipZ"}).text
    assert rid in only_z and lab["res"]["run_id"] not in only_z

    page = c.get("/device", params={"device": "chipZ"}).text
    assert "Device: chipZ" in page
    assert "second sample on the other fridge" in page  # devices.toml card rendered
    assert rid in page  # history via the <data_root>/<device>/scqo_state.json convention


def test_trends_never_mix_samples(lab):
    c = lab["client"]
    # q0 readout_freq exists on BOTH samples ("q1 exists on every chip" problem):
    # the default trend is scoped to the configured device, not the union.
    dev = c.get("/trends", params={"qubit": "q0", "quantity": "readout_freq"}).text
    assert lab["res"]["run_id"] in dev
    assert lab["chipz"]["run_id"] not in dev
    z = c.get("/trends", params={"qubit": "q0", "quantity": "readout_freq", "device": "chipZ"}).text
    assert lab["chipz"]["run_id"] in z and lab["res"]["run_id"] not in z
