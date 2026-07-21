"""The SCQO run-viewer — the lab's daily data GUI (port 8080 by convention).

Reads ONLY the datastore (run folders + index) and the per-(cooldown, setup)
SCQO files — ``scqo_state.json`` + ``physical.json`` (each with its
``.history.jsonl`` sidecar) in each context's
``<device>/<cooldown>/<setup>/scqo/`` folder (always under data_root, resolved by
``scqo.datastore.setup_scqo_dir``). No Session, no backend, no vendor imports — it
runs anywhere the data drive is mounted. The single mutating route is tag/note
editing, which writes to ``record.json`` + the index exactly like ``scqo tag``
(never instruments, never measurement data).
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .._state_io import read_history
from ..categories import CATEGORIES
from ..datastore import (
    STATE_FILE,
    DataStore,
    active_cooldown,
    load_cooldowns,
    load_device_registry,
    setup_scqo_dir,
)
from ..physical import PHYSICAL_FILE
from ..provenance import live_run_map, live_sources, summarize_live

#: quantities never tracked as device state (instrument-dependent; recorded decision)
_FIT_ONLY_TRENDS = ("p_e_given_g",)
#: fit quantities offered as one-click trend links (free-text also accepted):
#: measured physics first, then calibration knobs — derived from the field tables.
def _catalog_fields(side: str, push: bool | None = None) -> list[str]:
    """Catalog field names of one side in declaration order (dedup across categories)."""
    out: list[str] = []
    for spec in CATEGORIES.values():
        if spec.side != side:
            continue
        for f, fs in spec.fields.items():
            if push is not None and fs.push != push:
                continue
            if f not in out:
                out.append(f)
    return out


PHYSICAL_FIELD_ORDER = _catalog_fields("physical")
INSTRUMENT_FIELD_ORDER = _catalog_fields("instrument")

TREND_QUANTITIES = (
    *PHYSICAL_FIELD_ORDER,
    *_catalog_fields("instrument", push=False),
    *_catalog_fields("instrument", push=True),
    *_FIT_ONLY_TRENDS,
)


def create_app(
    data_root: str | Path,
    device_name: str = "device",
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

    def _scqo_dir(dev: str, cooldown: str, setup: str) -> Path | None:
        """The (device, cooldown, setup) ``scqo/`` folder, or None when the stamps
        are not a valid context (a setup-less run, a blank cooldown). Never raises —
        it runs on the home page over arbitrary run stamps."""
        if not cooldown or not setup:
            return None
        try:
            return setup_scqo_dir(store.data_root, dev, cooldown, setup)
        except ValueError:
            return None

    def _active_scqo_dirs(dev: str) -> tuple[str, dict[str, Path]]:
        """``(active cooldown id, {setup name: scqo dir})`` for the device's ACTIVE
        cycle. ``("", {})`` when the registry is absent/broken or has no active cycle
        — the viewer must render regardless, and this runs on the home page, so a
        broken (ValueError) or unreadable (OSError) registry degrades, never 500s."""
        try:
            active = active_cooldown(load_cooldowns(store.data_root, dev))
        except (ValueError, OSError):
            return "", {}
        if not active:
            return "", {}
        cid, cycle = active
        return cid, {name: _scqo_dir(dev, cid, name) for name in cycle.get("setup", {})}

    def _read_history(values_path: Path) -> list[dict]:
        """A values file's change history (``.history.jsonl`` sidecar, or embedded
        in pre-split files — :mod:`scqo._state_io`); [] where unreadable: the
        viewer must render regardless."""
        try:
            return read_history(values_path)
        except (OSError, json.JSONDecodeError):
            return []

    def _instrument_sources(scqo_dir: Path | None) -> dict:
        """Live-source map over a context's ``scqo_state.json`` ({} where absent).
        The state file is the viewer's current-value authority — the strict-match
        rule credits a run only while its recorded value still equals the live one."""
        data = (_read_json(scqo_dir / STATE_FILE) if scqo_dir else None) or {}
        history = _read_history(scqo_dir / STATE_FILE) if scqo_dir else []
        return live_sources(data.get("config", {}), history)

    def _physical_sources(scqo_dir: Path | None) -> dict:
        """Live-source map over a context's ``physical.json`` ({} where absent). The
        store is one (cooldown, setup) context, so values are flat and its whole
        history belongs to it — no setup slicing needed."""
        data = (_read_json(scqo_dir / PHYSICAL_FILE) if scqo_dir else None) or {}
        history = _read_history(scqo_dir / PHYSICAL_FILE) if scqo_dir else []
        return live_sources(data.get("values", {}), history)

    @app.get("/", response_class=HTMLResponse)
    def runs_page(
        request: Request,
        experiment: str = "",
        target: str = "",
        tag: str = "",
        outcome: str = "",
        since: str = "",
        until: str = "",
        device: str = "",
        operator: str = "",
        cooldown: str = "",
        setup: str = "",
        pending: str = "",
        limit: int = 50,
    ):
        rows = store.find_runs(
            experiment=experiment or None,
            target=target or None,
            tag=tag or None,
            outcome=outcome or None,
            since=since or None,
            until=until or None,
            device=device or None,
            operator=operator or None,
            cooldown=cooldown or None,
            setup=setup or None,
            pending=True if pending else None,
            limit=limit,
        )
        # Which of these runs CONTRIBUTE to current values ("the runs the device is
        # built from") — resolved against each run's OWN (device, cooldown, setup)
        # scqo/ folder, so a run credits against exactly the state + physics files it
        # measured (even a past cooldown's). run_ids are globally unique by construction.
        live_by_run: dict[str, str] = {}
        for dev, cd, sname in {(r["device"], r["cooldown"], r["setup"]) for r in rows}:
            scqo_dir = _scqo_dir(dev, cd, sname)
            inst = _instrument_sources(scqo_dir)
            phys = _physical_sources(scqo_dir)
            for rid, pairs in live_run_map(inst, phys).items():
                live_by_run[rid] = summarize_live(pairs)
        return templates.TemplateResponse(
            request,
            "runs.html",
            {
                "rows": rows,
                "live_by_run": live_by_run,
                "experiments": store.distinct_experiments(),
                "devices": store.distinct_devices(),
                "filters": {"experiment": experiment, "target": target, "tag": tag,
                            "outcome": outcome, "since": since, "until": until,
                            "device": device, "operator": operator,
                            "cooldown": cooldown, "setup": setup,
                            "pending": pending, "limit": limit},
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
                diff.append({"component": q, "field": field, "before": b, "after": a,
                             "changed": b != a})
        # Is each ACCEPTED value still the one the device runs? (aligned with the
        # suggestions list; None for non-accepted rows / no source info). Resolved
        # against the RUN's OWN (cooldown, setup) scqo/ folder — its state + physics.
        scqo_dir = _scqo_dir(record["device"], record.get("cooldown") or "",
                             record.get("setup") or "")
        inst_sources = _instrument_sources(scqo_dir)
        phys_sources = _physical_sources(scqo_dir)
        on_device = []
        for s in record.get("suggestions", []):
            sources = phys_sources if s.get("store") == "physical" else inst_sources
            src = sources.get(s.get("component"), {}).get(s.get("field"))
            if s.get("status") != "accepted" or src is None:
                on_device.append(None)
            elif src["status"] == "run" and src["run_id"] == run_id:
                on_device.append({"kind": "live"})
            elif src["status"] == "run":
                on_device.append({"kind": "superseded", "run_id": src["run_id"]})
            elif src["status"] == "external":
                on_device.append({"kind": "external"})
            else:  # manual
                on_device.append({"kind": "manual"})
        return templates.TemplateResponse(
            request,
            "run.html",
            {
                "record": record,
                "parameters": loaded["parameters"],
                "result": loaded["result"],
                "figures": figures,
                "suggestions": record.get("suggestions", []),
                "on_device": on_device,
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
    def trends_page(request: Request, target: str = "q1", quantity: str = "t1_s", device: str = ""):
        # qubit names repeat across samples ("q1" exists on every chip), so the
        # trend defaults to the configured device rather than mixing samples.
        dev = device or device_name
        rows = store.fit_trend(target, quantity, device=dev) if target and quantity else []
        svg = _trend_svg(rows)
        return templates.TemplateResponse(
            request,
            "trends.html",
            {"target": target, "quantity": quantity, "rows": rows, "svg": svg,
             "quantities": TREND_QUANTITIES, "device": dev,
             "devices": store.distinct_devices()},
        )

    def _phys_panel(scqo_dir: Path | None) -> dict:
        """A section's physical block from its ``scqo/physical.json`` (flat, one
        context): stable field order + per-qubit rows + live-source provenance."""
        data = (_read_json(scqo_dir / PHYSICAL_FILE) if scqo_dir else None) or {}
        history = _read_history(scqo_dir / PHYSICAL_FILE) if scqo_dir else []
        values = data.get("values", {})
        observed = {f for fields in values.values() for f in fields}
        fields = [f for f in PHYSICAL_FIELD_ORDER if f in observed] + sorted(observed - set(PHYSICAL_FIELD_ORDER))
        sources = live_sources(values, history)
        rows = [
            {"component": q, "field": f, "value": values[q][f],
             "source": sources.get(q, {}).get(f)}
            for q in sorted(values) for f in fields if f in values[q]
        ]
        return {"rows": rows, "history": list(reversed(history))[:200]}

    def _state_section(dev: str, cooldown: str, name: str, backend: str) -> dict:
        """One device-page block per ACTIVE-cycle setup: its ``scqo/`` folder's
        calibration state (the authority since v0.6 — reflects deferred accepts) or
        its latest run's device_after snapshot, plus that context's physical panel."""
        scqo_dir = _scqo_dir(dev, cooldown, name)
        # This context's OWN latest run — backs both the snapshot fallback and the
        # caption link, so a section never credits a foreign setup's run as "latest".
        own_latest = store.find_runs(device=dev, cooldown=cooldown, setup=name, limit=1)
        own_latest = own_latest[0] if own_latest else None
        data = _read_json(scqo_dir / STATE_FILE) if scqo_dir else None
        state, authority, snapshot_run = {}, "", None
        # History is read UNCONDITIONALLY: the sidecar survives a values-only
        # reset (INSTALL's cleaning ladder), so the page must keep showing the
        # provenance even while the values file is gone (snapshot authority).
        history = (list(reversed(_read_history(scqo_dir / STATE_FILE)))[:200]
                   if scqo_dir else [])
        if data:
            state = data.get("config") or {}
            authority = "state"
        elif own_latest:  # no state file yet: that context's last run snapshot
            state = _read_json(_run_dir(own_latest) / "device_after.json") or {}
            authority, snapshot_run = "snapshot", own_latest
        # Stable column order: descriptor order first, then any extra observed
        # fields. (Fields are heterogeneous per qubit — only measured qubits carry
        # a value — so the first qubit's keys are NOT a valid header.)
        observed = {f for fields in state.values() for f in fields}
        phys = _phys_panel(scqo_dir)
        return {
            "name": name, "backend": backend,
            "state": state, "authority": authority, "snapshot_run": snapshot_run,
            "latest_run": own_latest,
            "state_fields": [f for f in INSTRUMENT_FIELD_ORDER if f in observed] + sorted(observed - set(INSTRUMENT_FIELD_ORDER)),
            "sources": _instrument_sources(scqo_dir),
            "history": history, "state_path": str(scqo_dir / STATE_FILE) if scqo_dir else "",
            "physical_rows": phys["rows"], "physical_history": phys["history"],
        }

    @app.get("/device", response_class=HTMLResponse)
    def device_page(request: Request, device: str = ""):
        dev = device or device_name
        latest = store.find_runs(device=dev, limit=1)
        registry = load_device_registry(store.data_root)
        # Cooldown cycles + the ACTIVE cycle's named setups. The registry validates
        # loudly at RUN time; the viewer must render regardless. No user context
        # here, so ALL setups are shown — never "the selected one".
        cooldown_error = ""
        try:
            cycles = load_cooldowns(store.data_root, dev)
        except ValueError as err:
            cycles, cooldown_error = {}, str(err)
        active = active_cooldown(cycles)
        cid = active[0] if active else ""
        setups = active[1].get("setup", {}) if active else {}
        # One section per ACTIVE-cycle setup, each carrying that (cooldown, setup)
        # context's calibration state AND physical values. No resolvable setups ->
        # a single snapshot-only section from the device's latest run.
        sections = [_state_section(dev, cid, name, s.get("backend", ""))
                    for name, s in setups.items()]
        if not sections and latest:
            snapshot = _read_json(_run_dir(latest[0]) / "device_after.json") or {}
            observed = {f for fields in snapshot.values() for f in fields}
            sections = [{
                "name": "", "backend": latest[0].get("backend", ""),
                "state": snapshot, "authority": "snapshot", "snapshot_run": latest[0],
                "latest_run": latest[0],
                "state_fields": [f for f in INSTRUMENT_FIELD_ORDER if f in observed] + sorted(observed - set(INSTRUMENT_FIELD_ORDER)),
                "sources": {}, "history": [], "state_path": "",
                "physical_rows": [], "physical_history": [],
            }]
        return templates.TemplateResponse(
            request,
            "device.html",
            {"sections": sections,
             "latest": latest[0] if latest else None,
             "device": dev, "devices": store.distinct_devices(),
             "registry": registry.get(dev) or {},
             "cycles": cycles, "active_cycle": active[0] if active else None,
             "setups": setups,
             "cooldown_error": cooldown_error},
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
