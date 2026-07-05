"""The SCQO run-viewer — the lab's daily data GUI (port 8080 by convention).

Reads ONLY the datastore (run folders + index) and the scqo state JSON (history).
No Session, no backend, no vendor imports — it runs anywhere the data drive is
mounted. The single mutating route is tag/note editing, which writes to
``record.json`` + the index exactly like ``tag_run.py`` (never instruments, never
measurement data).
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..datastore import DataStore, load_device_registry

#: fit quantities offered as one-click trend links (free-text also accepted)
TREND_QUANTITIES = ("t1_s", "t2_star_s", "readout_freq", "drive_freq", "pi_amp", "readout_amp")


def create_app(
    data_root: str | Path,
    device_name: str = "device",
    state_path: str | Path | None = None,
) -> FastAPI:
    store = DataStore(data_root, device_name=device_name)
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    app = FastAPI(title="SCQO run viewer", docs_url=None, redoc_url=None)

    def _run_dir(record: dict) -> Path:
        return store.data_root / record["path"]

    def _read_json(path: Path) -> dict | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    @app.get("/", response_class=HTMLResponse)
    def runs_page(
        request: Request,
        experiment: str = "",
        qubit: str = "",
        tag: str = "",
        outcome: str = "",
        since: str = "",
        until: str = "",
        device: str = "",
        limit: int = 50,
    ):
        rows = store.find_runs(
            experiment=experiment or None,
            qubit=qubit or None,
            tag=tag or None,
            outcome=outcome or None,
            since=since or None,
            until=until or None,
            device=device or None,
            limit=limit,
        )
        return templates.TemplateResponse(
            request,
            "runs.html",
            {
                "rows": rows,
                "experiments": store.distinct_experiments(),
                "devices": store.distinct_devices(),
                "filters": {"experiment": experiment, "qubit": qubit, "tag": tag,
                            "outcome": outcome, "since": since, "until": until,
                            "device": device, "limit": limit},
                "data_root": str(store.data_root),
            },
        )

    @app.get("/run/{run_id}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: str):
        try:
            loaded = store.load_run(run_id)
        except KeyError:
            raise HTTPException(404, f"unknown run_id {run_id!r}")
        record = loaded["record"]
        run_dir = _run_dir(record)
        figures = [str(Path(p).relative_to(run_dir).as_posix()) for p in loaded["figures"]]
        before = _read_json(run_dir / "device_before.json") or {}
        after = _read_json(run_dir / "device_after.json") or {}
        diff = []
        for q in sorted(set(before) | set(after)):
            for field in sorted(set(before.get(q, {})) | set(after.get(q, {}))):
                b, a = before.get(q, {}).get(field), after.get(q, {}).get(field)
                diff.append({"qubit": q, "field": field, "before": b, "after": a,
                             "changed": b != a})
        return templates.TemplateResponse(
            request,
            "run.html",
            {
                "record": record,
                "parameters": loaded["parameters"],
                "result": loaded["result"],
                "figures": figures,
                "diff": diff,
                "path": str(run_dir),
            },
        )

    @app.get("/run/{run_id}/file/{relpath:path}")
    def run_file(run_id: str, relpath: str):
        try:
            loaded = store.load_run(run_id)
        except KeyError:
            raise HTTPException(404, f"unknown run_id {run_id!r}")
        base = _run_dir(loaded["record"]).resolve()
        target = (base / relpath).resolve()
        # strict containment: never serve anything outside this run's folder
        if base != target and base not in target.parents:
            raise HTTPException(404, "not in this run's folder")
        if not target.is_file():
            raise HTTPException(404, "no such file")
        return FileResponse(target)

    @app.post("/run/{run_id}/tags")
    def edit_tags(run_id: str, add: str = Form(""), remove: str = Form(""), note: str = Form(None)):
        # The viewer's ONLY write: record.json + index, same as tag_run.py.
        try:
            store.tag_run(
                run_id,
                add=[t for t in add.replace(",", " ").split() if t],
                remove=[t for t in remove.replace(",", " ").split() if t],
                note=note if note is not None and note != "" else None,
            )
        except KeyError:
            raise HTTPException(404, f"unknown run_id {run_id!r}")
        return RedirectResponse(url=f"/run/{run_id}", status_code=303)

    @app.get("/trends", response_class=HTMLResponse)
    def trends_page(request: Request, qubit: str = "q1", quantity: str = "t1_s", device: str = ""):
        # qubit names repeat across samples ("q1" exists on every chip), so the
        # trend defaults to the configured device rather than mixing samples.
        dev = device or device_name
        rows = store.fit_trend(qubit, quantity, device=dev) if qubit and quantity else []
        svg = _trend_svg(rows)
        return templates.TemplateResponse(
            request,
            "trends.html",
            {"qubit": qubit, "quantity": quantity, "rows": rows, "svg": svg,
             "quantities": TREND_QUANTITIES, "device": dev,
             "devices": store.distinct_devices()},
        )

    def _state_file_for(device: str) -> Path | None:
        """The device's scqo state JSON: the configured path for the default device,
        the ``<data_root>/<device>/scqo_state.json`` convention for any other."""
        if device == device_name and state_path:
            return Path(state_path)
        candidate = store.data_root / device / "scqo_state.json"
        return candidate if candidate.is_file() else None

    @app.get("/device", response_class=HTMLResponse)
    def device_page(request: Request, device: str = ""):
        dev = device or device_name
        latest = store.find_runs(device=dev, limit=1)
        state = None
        if latest:
            state = _read_json(_run_dir(latest[0]) / "device_after.json")
        history: list[dict] = []
        sfile = _state_file_for(dev)
        if sfile and sfile.is_file():
            data = _read_json(sfile) or {}
            history = list(reversed(data.get("history", [])))[:200]
        registry = load_device_registry(store.data_root)
        return templates.TemplateResponse(
            request,
            "device.html",
            {"state": state or {}, "latest": latest[0] if latest else None,
             "history": history, "state_path": str(sfile or ""),
             "device": dev, "devices": store.distinct_devices(),
             "registry": registry.get(dev) or {}},
        )

    return app


def _trend_svg(rows: list[dict], width: int = 860, height: int = 300) -> str:
    """Server-rendered SVG polyline of value vs run index (no JS, no assets)."""
    pts = [(i, float(r["value"])) for i, r in enumerate(rows) if r["value"] is not None]
    if not pts:
        return ""
    pad, w, h = 60, width, height
    ys = [y for _, y in pts]
    y_lo, y_hi = min(ys), max(ys)
    if y_hi == y_lo:
        y_lo, y_hi = y_lo - abs(y_lo) * 0.05 - 1e-30, y_hi + abs(y_hi) * 0.05 + 1e-30
    span_x = max(len(pts) - 1, 1)

    def sx(i: float) -> float:
        return pad + (w - 2 * pad) * (i / span_x)

    def sy(y: float) -> float:
        return h - pad - (h - 2 * pad) * ((y - y_lo) / (y_hi - y_lo))

    line = " ".join(f"{sx(i):.1f},{sy(y):.1f}" for i, y in pts)
    circles = "".join(
        f'<circle cx="{sx(i):.1f}" cy="{sy(y):.1f}" r="4"><title>{rows[i]["run_id"]}\n{y:.6g}</title></circle>'
        for i, y in pts
    )
    first, last = rows[0]["started_at"][:16], rows[-1]["started_at"][:16]
    return (
        f'<svg viewBox="0 0 {w} {h}" role="img">'
        f'<line x1="{pad}" y1="{h - pad}" x2="{w - pad}" y2="{h - pad}" class="axis"/>'
        f'<line x1="{pad}" y1="{pad}" x2="{pad}" y2="{h - pad}" class="axis"/>'
        f'<text x="{pad - 8}" y="{sy(y_hi) + 4}" text-anchor="end" class="tick">{y_hi:.4g}</text>'
        f'<text x="{pad - 8}" y="{sy(y_lo) + 4}" text-anchor="end" class="tick">{y_lo:.4g}</text>'
        f'<text x="{pad}" y="{h - pad + 18}" class="tick">{first}</text>'
        f'<text x="{w - pad}" y="{h - pad + 18}" text-anchor="end" class="tick">{last}</text>'
        f'<polyline points="{line}" class="trend"/>{circles}</svg>'
    )
