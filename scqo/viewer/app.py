"""The SCQO run-viewer — the lab's daily data GUI (port 8080 by convention).

Reads ONLY the datastore (run folders + index), the per-(cooldown, setup)
SCQO values files — ``scqo_state.json`` + ``physical.json`` in each context's
``<device>/<cooldown>/<setup>/scqo/`` folder (always under data_root, resolved
by ``scqo.datastore.setup_scqo_dir``) — and each context's ``history.sqlite``
change-history database (:mod:`scqo.changes`), strictly read-only: the viewer
never creates, migrates or writes a history database. No Session, no backend,
no vendor imports — it runs anywhere the data drive is mounted. The single
mutating route is tag/note editing, which writes to ``record.json`` + the
index exactly like ``scqo tag`` (never instruments, never measurement data).
"""

from __future__ import annotations

import json
import sqlite3
from html import escape
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..changes import (
    HISTORY_FILE,
    ChangeDB,
    collect_fact_matrix,
    collect_fact_series,
)
from ..datastore import (
    STATE_FILE,
    DataStore,
    active_cooldown,
    known_devices,
    load_cooldowns,
    load_device_registry,
    setup_scqo_dir,
)
# STATISTICS_PNG is just the filename constant — _campaign_plot imports
# matplotlib lazily, so this costs nothing and keeps one naming authority.
from ..cli._campaign_plot import STATISTICS_PNG
from ..provenance import live_run_map, live_sources, summarize_live
# Catalog-derived field orders + units live in report.py (renderer-free, no
# fastapi) — one definition shared with the CLI's own reports.
from ..report import (
    FIELD_UNITS,
    INSTRUMENT_FIELD_ORDER,
    PHYSICAL_FIELD_ORDER,
    campaign_statistics_rows,
)
from ..stores import PHYSICAL_FILE


def create_app(data_root: str | Path) -> FastAPI:
    # The viewer is a MACHINE-level service: it serves every sample in the
    # data_root and must never inherit the launching account's personal sample
    # selection (user.toml). DataStore's device_name is a write-side default the
    # viewer never exercises — its read/tag paths are run_id- or device-keyed.
    store = DataStore(data_root)
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    app = FastAPI(title="SCQO run viewer", docs_url=None, redoc_url=None)

    def _run_dir(record: dict) -> Path:
        return store.data_root / record["path"]

    def _read_json(path: Path) -> dict | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _values_of(path: Path) -> dict:
        return (_read_json(path) or {}).get("values", {})

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

    def _latest_pairs(scqo_dir: Path | None, store_name: str) -> dict:
        """One context's {(entity, field): [latest, previous|None]} from its
        history database; {} where the context or database is absent/unreadable
        (degrade-never-500 — ChangeDB reads never create the file)."""
        if scqo_dir is None:
            return {}
        try:
            return ChangeDB(scqo_dir / HISTORY_FILE).latest_two(store_name)
        except (OSError, sqlite3.Error):
            return {}

    def _sources_from_pairs(values: dict, pairs: dict) -> dict:
        """live_sources fed from the database's latest row per key — a
        one-record-per-key list is a valid history for its forward pass."""
        return live_sources(values, [p[0] for p in pairs.values() if p and p[0]])

    def _context_sources(scqo_dir: Path | None) -> tuple[dict, dict]:
        """(state sources, physical sources) of one context."""
        if scqo_dir is None:
            return {}, {}
        inst = _sources_from_pairs(_values_of(scqo_dir / STATE_FILE),
                                   _latest_pairs(scqo_dir, "state"))
        phys = _sources_from_pairs(_values_of(scqo_dir / PHYSICAL_FILE),
                                   _latest_pairs(scqo_dir, "physical"))
        return inst, phys

    def _device_contexts(dev: str) -> tuple[list[tuple[str, str, ChangeDB]],
                                            set[tuple[str, str]]]:
        """Every history context of one device, registry order first:
        cooldowns.toml cycles × setups, then disk-discovered ghosts (a context
        folder whose cycle/setup was removed from the registry), then the
        device-level escape-hatch database (context ("", "")). Returns
        (contexts, ghost keys). All tolerant — a broken registry or an
        unreadable folder degrades to what can be seen."""
        contexts: list[tuple[str, str, ChangeDB]] = []
        seen: set[tuple[str, str]] = set()
        try:
            cycles = load_cooldowns(store.data_root, dev)
        except (ValueError, OSError):
            cycles = {}
        for cid, cycle in cycles.items():
            for name in cycle.get("setup", {}):
                d = _scqo_dir(dev, cid, name)
                if d is not None:
                    contexts.append((cid, name, ChangeDB(d / HISTORY_FILE)))
                    seen.add((cid, name))
        ghosts: set[tuple[str, str]] = set()
        ddir = store.data_root / dev
        try:
            found = sorted(ddir.glob(f"*/*/scqo/{HISTORY_FILE}"))
        except OSError:
            found = []
        for p in found:
            cid, name = p.relative_to(ddir).parts[:2]
            if (cid, name) not in seen:
                contexts.append((cid, name, ChangeDB(p)))
                ghosts.add((cid, name))
        dev_level = ddir / HISTORY_FILE
        if dev_level.is_file():
            contexts.append(("", "", ChangeDB(dev_level)))
            ghosts.add(("", ""))
        return contexts, ghosts

    def _known_names() -> list[str]:
        """Every sample this lab knows: indexed runs ∪ registry/cooldowns/folders —
        a freshly added sample with no runs yet must still appear."""
        return sorted(set(store.distinct_devices()) | known_devices(store.data_root))

    def _sample_overview() -> list[dict]:
        """One row per known sample for the bare /device overview.
        Tolerant like device_page: a broken cooldowns.toml becomes an inline
        per-row error, never a 500. The per-sample find_runs is O(limit) via the
        composite index, and a lab holds single-digit samples — no caching."""
        registry = load_device_registry(store.data_root)
        rows = []
        for name in _known_names():
            cooldown_error, cid, setups = "", "", []
            try:
                cycles = load_cooldowns(store.data_root, name)
            except ValueError as err:
                cycles, cooldown_error = {}, str(err)
            active = active_cooldown(cycles)
            if active:
                cid, setups = active[0], list(active[1].get("setup", {}))
            latest = store.find_runs(device=name, limit=1)
            entry = registry.get(name)
            rows.append({
                "name": name,
                "description": entry.get("description", "") if isinstance(entry, dict) else "",
                "active_cycle": cid, "setups": setups,
                "cooldown_error": cooldown_error,
                "latest": latest[0] if latest else None,
            })
        return rows

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
        campaign: str = "",
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
            campaign=campaign or None,
            pending=True if pending else None,
            limit=limit,
        )
        # Which of these runs CONTRIBUTE to current values ("the runs the device is
        # built from") — resolved against each run's OWN (device, cooldown, setup)
        # context, so a run credits against exactly the state + physics it
        # measured (even a past cooldown's). run_ids are globally unique by construction.
        live_by_run: dict[str, str] = {}
        for dev, cd, sname in {(r["device"], r["cooldown"], r["setup"]) for r in rows}:
            inst, phys = _context_sources(_scqo_dir(dev, cd, sname))
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
                            "campaign": campaign, "pending": pending,
                            "limit": limit},
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
                diff.append({"entity": q, "field": field, "before": b, "after": a,
                             "changed": b != a})
        # Is each ACCEPTED value still the one the device runs? (aligned with the
        # suggestions list; None for non-accepted rows / no source info). Resolved
        # against the RUN's OWN (cooldown, setup) context — its state + physics.
        scqo_dir = _scqo_dir(record["device"], record.get("cooldown") or "",
                             record.get("setup") or "")
        inst_sources, phys_sources = _context_sources(scqo_dir)
        on_device = []
        for s in record.get("suggestions", []):
            sources = phys_sources if s.get("role") == "fact" else inst_sources
            src = sources.get(s.get("entity"), {}).get(s.get("field"))
            if s.get("status") != "accepted" or src is None:
                on_device.append(None)
            elif src["status"] == "run" and src["run_id"] == run_id:
                on_device.append({"kind": "live"})
            elif src["status"] == "run":
                on_device.append({"kind": "superseded", "run_id": src["run_id"]})
            elif src["status"] == "campaign":
                on_device.append({"kind": "campaign",
                                  "campaign_id": src["campaign_id"]})
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

    def _param_rows(values: dict, pairs: dict, order: tuple | list) -> list[dict]:
        """Long-format parameter rows of one store on the setup page: current
        value + unit + crediting source + the PREVIOUS record (the one-before
        run link), fields in catalog order then extras."""
        sources = _sources_from_pairs(values, pairs)
        observed = {f for fields in values.values() for f in fields}
        fields = ([f for f in order if f in observed]
                  + sorted(observed - set(order)))
        rows = []
        for entity in sorted(values):
            for f in fields:
                if f not in values[entity]:
                    continue
                pair = pairs.get((entity, f)) or [None, None]
                rows.append({
                    "entity": entity, "field": f, "value": values[entity][f],
                    "unit": FIELD_UNITS.get(f, ""),
                    "source": sources.get(entity, {}).get(f),
                    "previous": pair[1],
                })
        return rows

    @app.get("/setup/{device}/{cooldown}/{setup_name}", response_class=HTMLResponse)
    def setup_page(request: Request, device: str, cooldown: str, setup_name: str):
        # A (device, cooldown, setup) triple names one on-disk context — a path
        # identity like /run/<id>. Any well-formed triple renders (an unknown
        # one shows an empty page, the /device?device= precedent); only a
        # non-filename-safe segment 404s.
        scqo_dir = _scqo_dir(device, cooldown, setup_name)
        if scqo_dir is None:
            raise HTTPException(
                404, "not a valid context (cooldown/setup are letters/digits/_/-)")
        cooldown_error = ""
        try:
            cycles = load_cooldowns(store.data_root, device)
        except (ValueError, OSError) as err:
            cycles, cooldown_error = {}, str(err)
        cycle = cycles.get(cooldown) or {}
        setup_meta = cycle.get("setup", {}).get(setup_name)
        active = active_cooldown(cycles)
        own_latest = store.find_runs(device=device, cooldown=cooldown,
                                     setup=setup_name, limit=1)
        own_latest = own_latest[0] if own_latest else None
        # Current calibration: the values file is the authority; a context with
        # runs but no state file falls back to its own latest run's snapshot.
        # Provenance/previous always resolve from the history database — it
        # survives a values-only reset.
        state_data = _read_json(scqo_dir / STATE_FILE)
        authority, snapshot_run = "", None
        if state_data:
            state_values = state_data.get("values") or {}
            authority = "state"
        elif own_latest:
            state_values = _read_json(_run_dir(own_latest) / "device_after.json") or {}
            authority, snapshot_run = "snapshot", own_latest
        else:
            state_values = {}
        state_rows = _param_rows(state_values,
                                 _latest_pairs(scqo_dir, "state"),
                                 INSTRUMENT_FIELD_ORDER)
        phys_rows = _param_rows(_values_of(scqo_dir / PHYSICAL_FILE),
                                _latest_pairs(scqo_dir, "physical"),
                                PHYSICAL_FIELD_ORDER)
        return templates.TemplateResponse(
            request,
            "setup.html",
            {"device": device, "cooldown": cooldown, "setup_name": setup_name,
             "cycle": cycle, "setup_meta": setup_meta,
             "in_registry": setup_meta is not None,
             "is_active": bool(active and active[0] == cooldown),
             "cooldown_error": cooldown_error,
             "authority": authority, "snapshot_run": snapshot_run,
             "latest_run": own_latest,
             "state_rows": state_rows, "physical_rows": phys_rows,
             "state_path": str(scqo_dir / STATE_FILE)},
        )

    def _chart_rows(rows: list[dict], *, with_context: bool
                    ) -> tuple[list[dict], list[int], list[dict]]:
        """Change rows -> chart rows ({value, title, cls}) + separator indices
        (cooldown boundaries, cross-context view only) + a context legend.
        Non-numeric values keep their x slot but draw no point."""
        def fmt(v):
            return f"{v:.6g}" if isinstance(v, (int, float)) else str(v)

        pts: list[dict] = []
        separators: list[int] = []
        classes: dict[tuple[str, str], str] = {}
        prev_cd: str | None = None
        for r in rows:
            ctx = (r.get("cooldown", ""), r.get("setup", ""))
            cls = ""
            if with_context:
                if ctx not in classes:
                    classes[ctx] = f"ctx-{len(classes) % 6}"
                cls = classes[ctx]
                if prev_cd is not None and ctx[0] != prev_cd:
                    separators.append(len(pts))
                prev_cd = ctx[0]
            try:
                value = float(r["new"])
            except (TypeError, ValueError):
                value = None
            source = r.get("run_id") or r.get("campaign_id") or "manual"
            prefix = (f"{ctx[0] or '(device)'}/{ctx[1] or '-'} · "
                      if with_context else "")
            pts.append({
                "value": value, "cls": cls,
                "title": (f"{prefix}{r['timestamp'][:19]} · "
                          f"{fmt(r['old'])} → {fmt(r['new'])} · {source}"),
            })
        legend = [{"cooldown": cd, "setup": s, "cls": c}
                  for (cd, s), c in classes.items()]
        return pts, separators, legend

    @app.get("/trends", response_class=HTMLResponse)
    def trends_page(request: Request, device: str = "", entity: str = "",
                    field: str = "", cooldown: str = "", setup: str = ""):
        # A trend is ALWAYS explicitly device-scoped — qubit names repeat
        # across samples ("q1" exists on every chip); device-less /trends
        # bounces to the ONE sample picker. Data source: the change-history
        # databases (accepted lineage), never run fits.
        if not device:
            return RedirectResponse(url="/device")
        contexts, ghosts = _device_contexts(device)
        if not (entity and field):
            # The menu offers only parameters that actually HAVE history.
            fact_keys = sorted({(c["entity"], c["field"])
                                for c in collect_fact_matrix(contexts)})
            state_menu = []
            for cid, name, db in contexts:
                if not (cid and name):
                    continue  # the device-level context has no port-1 page
                try:
                    keys = sorted(db.latest_two("state"))
                except (OSError, sqlite3.Error):
                    keys = []
                if keys:
                    # not "keys" — that name is shadowed by dict.keys in Jinja
                    state_menu.append({"cooldown": cid, "setup": name,
                                       "params": keys})
            return templates.TemplateResponse(
                request,
                "trends.html",
                {"device": device, "devices": store.distinct_devices(),
                 "mode": "menu", "entity": "", "field": "",
                 "cooldown": "", "setup": "",
                 "fact_keys": fact_keys, "state_menu": state_menu,
                 "rows": [], "svg": "", "legend": [], "truncated": False},
            )
        if cooldown and setup:
            # Port 1: this setup's latest 50 changes of one parameter.
            scqo_dir = _scqo_dir(device, cooldown, setup)
            try:
                series = (ChangeDB(scqo_dir / HISTORY_FILE)
                          .param_series(entity, field, limit=50)
                          if scqo_dir is not None else [])
            except (OSError, sqlite3.Error):
                series = []
            truncated = len(series) == 50
            rows = series[::-1]  # newest-first -> chronological
            for r in rows:
                r["cooldown"], r["setup"] = cooldown, setup
            mode = "setup"
        else:
            # Port 2: one PHYSICAL fact across every context of the device.
            rows = collect_fact_series(contexts, entity, field)
            truncated = False
            mode = "device"
        pts, separators, legend = _chart_rows(rows,
                                              with_context=(mode == "device"))
        first = rows[0]["timestamp"][:16] if rows else ""
        last = rows[-1]["timestamp"][:16] if rows else ""
        svg = _trend_svg(pts, separators=separators, xlabels=(first, last))
        return templates.TemplateResponse(
            request,
            "trends.html",
            {"device": device, "devices": store.distinct_devices(),
             "mode": mode, "entity": entity, "field": field,
             "cooldown": cooldown, "setup": setup,
             "fact_keys": [], "state_menu": [],
             "rows": list(reversed(rows)),  # table newest-first
             "svg": svg, "legend": legend, "truncated": truncated},
        )

    @app.get("/device", response_class=HTMLResponse)
    def device_page(request: Request, device: str = ""):
        # Bare /device is the lab-wide sample overview; a sample's detail page
        # is always explicitly addressed (?device=<name>).
        if not device:
            return templates.TemplateResponse(
                request,
                "devices.html",
                {"samples": _sample_overview(), "data_root": str(store.data_root)},
            )
        dev = device
        latest = store.find_runs(device=dev, limit=1)
        registry = load_device_registry(store.data_root)
        # Cooldown cycles: the registry validates loudly at RUN time; the
        # viewer must render regardless. Every cycle's setups link to their
        # own setup pages — historical cycles stay browsable.
        cooldown_error = ""
        try:
            cycles = load_cooldowns(store.data_root, dev)
        except ValueError as err:
            cycles, cooldown_error = {}, str(err)
        active = active_cooldown(cycles)
        # The physical-facts matrix: rows = facts, one column per context that
        # holds any fact history (registry order, ghosts flagged and last).
        contexts, ghosts = _device_contexts(dev)
        cells = collect_fact_matrix(contexts)
        with_data = {(c["cooldown"], c["setup"]) for c in cells}
        columns = [{"cooldown": cd, "setup": s, "ghost": (cd, s) in ghosts}
                   for cd, s, _db in contexts if (cd, s) in with_data]
        by_cell = {(c["entity"], c["field"], c["cooldown"], c["setup"]): c
                   for c in cells}
        observed = {c["field"] for c in cells}
        field_order = ([f for f in PHYSICAL_FIELD_ORDER if f in observed]
                       + sorted(observed - set(PHYSICAL_FIELD_ORDER)))
        matrix_rows = []
        for ent in sorted({c["entity"] for c in cells}):
            for f in field_order:
                row = [by_cell.get((ent, f, col["cooldown"], col["setup"]))
                       for col in columns]
                if any(row):
                    matrix_rows.append({"entity": ent, "field": f,
                                        "cells": row})
        # Registry-less sample with runs: a trimmed values-only snapshot table
        # (its knobs have no setup page to live on; the device-level history
        # context, if any, only holds facts).
        snapshot_rows = []
        if not cycles and latest:
            snapshot = _read_json(_run_dir(latest[0]) / "device_after.json") or {}
            snapshot_rows = [
                {"entity": q, "field": f, "value": v}
                for q in sorted(snapshot)
                for f, v in sorted(snapshot[q].items())]
        return templates.TemplateResponse(
            request,
            "device.html",
            {"device": dev, "devices": _known_names(),
             "latest": latest[0] if latest else None,
             "registry": registry.get(dev) or {},
             "cycles": cycles, "active_cycle": active[0] if active else None,
             "cooldown_error": cooldown_error,
             "matrix_columns": columns, "matrix_rows": matrix_rows,
             "snapshot_rows": snapshot_rows,
             "snapshot_run": latest[0] if snapshot_rows else None},
        )

    def _load_campaign(campaign_id: str) -> dict:
        """load_campaign with every failure mode a 404 — unknown id (KeyError),
        or a torn/unreadable campaign.json (the degrade-never-500 doctrine)."""
        try:
            return store.load_campaign(campaign_id)
        except KeyError:
            raise HTTPException(404, f"unknown campaign_id {campaign_id!r}")
        except (OSError, json.JSONDecodeError):
            raise HTTPException(
                404, f"campaign {campaign_id!r} has no readable campaign.json")

    @app.get("/campaigns", response_class=HTMLResponse)
    def campaigns_page(request: Request, device: str = "", status: str = "",
                       pending: str = "", limit: int = 50):
        rows = store.find_campaigns(device=device or None,
                                    status=status or None,
                                    pending=True if pending else None,
                                    limit=limit)
        return templates.TemplateResponse(
            request,
            "campaigns.html",
            {"rows": rows, "devices": _known_names(),
             "filters": {"device": device, "status": status,
                         "pending": pending, "limit": limit},
             "data_root": str(store.data_root)},
        )

    @app.get("/campaign/{campaign_id}", response_class=HTMLResponse)
    def campaign_page(request: Request, campaign_id: str):
        loaded = _load_campaign(campaign_id)
        manifest = loaded["manifest"]
        # execution order, unlimited — find_runs(campaign=...) is newest-first
        # and capped at 50, the documented wrong tool here
        children = store.campaign_runs(campaign_id)
        suggestions = manifest.get("suggestions") or []
        by_experiment: dict[str, list[dict]] = {}
        for s in suggestions:
            by_experiment.setdefault(s.get("experiment") or "-", []).append(s)
        return templates.TemplateResponse(
            request,
            "campaign.html",
            {"manifest": manifest,
             "stats_rows": campaign_statistics_rows(
                 manifest.get("statistics") or {}),
             "children": children,
             "suggestions_by_experiment": by_experiment,
             "pending": sum(1 for s in suggestions
                            if s.get("status") == "pending"),
             "problems": manifest.get("suggestion_problems") or [],
             "running": manifest.get("status") == "running",
             "has_png": (Path(loaded["path"]) / STATISTICS_PNG).is_file(),
             "path": loaded["path"]},
        )

    @app.get("/campaign/{campaign_id}/statistics.png")
    def campaign_figure(campaign_id: str):
        # campaign_id is only an index lookup key, never joined into a path —
        # the folder comes back from the row — and the filename is a constant;
        # the containment guard stays anyway (the run_file doctrine).
        base = Path(_load_campaign(campaign_id)["path"]).resolve()
        target = (base / STATISTICS_PNG).resolve()
        if base != target and base not in target.parents:
            raise HTTPException(404, "not in this campaign's folder")
        if not target.is_file():
            raise HTTPException(404, "no statistics.png yet")
        return FileResponse(target)

    return app


def _trend_svg(rows: list[dict], *, separators: tuple | list = (),
               xlabels: tuple[str, str] = ("", ""),
               width: int = 860, height: int = 300) -> str:
    """Server-rendered SVG polyline of value vs record index (no JS, no assets).

    ``rows`` are chart rows ``{value: float | None, title: str, cls: str}`` —
    a None value keeps its x slot but draws no point (a non-numeric change).
    ``separators`` are row indices where a dashed vertical divider lands (a
    cooldown boundary in the cross-context view); ``cls`` colors each point by
    context (``ctx-0``..``ctx-5`` in base.html).
    """
    pts = [(i, r["value"]) for i, r in enumerate(rows) if r["value"] is not None]
    if not pts:
        return ""
    pad, w, h = 60, width, height
    ys = [y for _, y in pts]
    y_lo, y_hi = min(ys), max(ys)
    if y_hi == y_lo:
        y_lo, y_hi = y_lo - abs(y_lo) * 0.05 - 1e-30, y_hi + abs(y_hi) * 0.05 + 1e-30
    span_x = max(len(rows) - 1, 1)

    def sx(i: float) -> float:
        return pad + (w - 2 * pad) * (i / span_x)

    def sy(y: float) -> float:
        return h - pad - (h - 2 * pad) * ((y - y_lo) / (y_hi - y_lo))

    line = " ".join(f"{sx(i):.1f},{sy(y):.1f}" for i, y in pts)

    def _circle(i: int, y: float) -> str:
        cls = rows[i].get("cls") or ""
        cls_attr = f' class="{cls}"' if cls else ""
        return (f'<circle cx="{sx(i):.1f}" cy="{sy(y):.1f}" r="4"{cls_attr}>'
                f'<title>{escape(rows[i]["title"])}</title></circle>')

    circles = "".join(_circle(i, y) for i, y in pts)
    seps = "".join(
        f'<line x1="{sx(i - 0.5):.1f}" y1="{pad}" x2="{sx(i - 0.5):.1f}" '
        f'y2="{h - pad}" class="ctxsep"/>'
        for i in separators
    )
    first, last = xlabels
    return (
        f'<svg viewBox="0 0 {w} {h}" role="img">'
        f'<line x1="{pad}" y1="{h - pad}" x2="{w - pad}" y2="{h - pad}" class="axis"/>'
        f'<line x1="{pad}" y1="{pad}" x2="{pad}" y2="{h - pad}" class="axis"/>'
        f'<text x="{pad - 8}" y="{sy(y_hi) + 4}" text-anchor="end" class="tick">{y_hi:.4g}</text>'
        f'<text x="{pad - 8}" y="{sy(y_lo) + 4}" text-anchor="end" class="tick">{y_lo:.4g}</text>'
        f'<text x="{pad}" y="{h - pad + 18}" class="tick">{first}</text>'
        f'<text x="{w - pad}" y="{h - pad + 18}" text-anchor="end" class="tick">{last}</text>'
        f'{seps}<polyline points="{line}" class="trend"/>{circles}</svg>'
    )
