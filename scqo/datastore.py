"""Measurement datastore — every run saved, findable, and reloadable.

The **run folder is the truth**: each ``Session.run`` writes a self-contained folder
(dataset, parameters, result, device snapshots, scqat analysis artifacts) under::

    <data_root>/<device_name>/<YYYY-MM-DD>/<run_id>/
        record.json          # RunRecord manifest — written LAST = completion marker
        dataset.nc           # canonical contract-form dataset (dims target x sweep)
        parameters.json      # experiment name + validated Parameters
        result.json          # structured Result (outcomes / fit / error)
        device_before.json   # SCQO config snapshot before the run
        device_after.json    # snapshot after the run (differs only if updates were applied)
        analysis/<qubit>/    # scqat estimator artifacts (metadata / plotdata / figures)

``index.sqlite`` at ``data_root`` is a **disposable cache** over those folders: it makes
``find_runs`` fast but holds nothing the folders don't; :func:`reindex` rebuilds it from
scratch by scanning for ``record.json`` files (folders without one are incomplete runs
and are skipped). Deleting the index is always safe — remove all ``index.sqlite*`` files
together (the ``-wal``/``-shm`` siblings too) and rerun ``python -m scqo <data_root>``.

Concurrency note: on a local disk, WAL mode + the 10 s busy retry + short-lived
connections make SIMULTANEOUS same-PC sessions safe — two students on two samples,
or two setups of ONE sample: they share no rows (PK ``(run_id, device)``), no
folders, and no SCQO state/physics files (each (cooldown, setup) has its own
``scqo/`` folder — see :func:`setup_scqo_dir`; even two same-context sessions merge
``physical.json`` under a lock on save). Index writes are ~1 ms per run and simply
queue. Worst case an index write is skipped, the run folder is already on disk and
``reindex`` heals the cache.
If ``data_root`` lives on a network share, keep ALL writing Sessions on one PC
(SQLite WAL is not reliable across SMB/NFS clients); when instruments move to separate
control PCs, give each PC its own local ``data_root`` (= physical per-sample
separation) and aggregate centrally by collecting folders + ``reindex``.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, Field

from ._state_io import _file_lock
from .config import _current_operator

SCHEMA_VERSION = 8  # v8: component cutover — run targets column renamed from qubits
RECORD_FILE = "record.json"
INDEX_FILE = "index.sqlite"
DEVICES_FILE = "devices.toml"  # optional human-edited sample registry (see load_device_registry)
COOLDOWNS_FILE = "cooldowns.toml"  # per device: <data_root>/<device>/cooldowns.toml (see load_cooldowns)
STATE_FILE = "scqo_state.json"  # SCQO calibration values, inside the setup's scqo/
                                # folder; history sits beside it in
                                # scqo_state.history.jsonl (scqo._state_io)
SCQO_SUBDIR = "scqo"  # per (device, cooldown, setup): <device>/<cooldown>/<setup>/scqo/ (see setup_scqo_dir)
BACKEND_CONFIG_SUBDIR = "backend_config"  # the vendor-config sibling: <cooldown>/<setup>/backend_config/
SETUP_BACKENDS = ("qblox", "qm", "simulated")  # legal [<cycle>.setup.<name>] backend values
SETUP_KEYS = ("backend", "note")  # the ONLY keys a setup table may carry (paths are DERIVED)
# Setup names travel as CLI arguments, index values and URL query params — keep them plain.
_SETUP_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id         TEXT NOT NULL,
  started_at     TEXT NOT NULL,
  ended_at       TEXT,
  experiment     TEXT NOT NULL,
  device         TEXT NOT NULL,
  backend        TEXT NOT NULL,
  operator       TEXT NOT NULL DEFAULT '',
  cooldown       TEXT NOT NULL DEFAULT '',
  setup          TEXT NOT NULL DEFAULT '',
  targets        TEXT NOT NULL,
  outcome        TEXT NOT NULL,
  outcomes       TEXT NOT NULL,
  fit            TEXT,
  tags           TEXT NOT NULL DEFAULT '[]',
  note           TEXT NOT NULL DEFAULT '',
  error          TEXT,
  parameters     TEXT NOT NULL,
  updated_device INTEGER NOT NULL DEFAULT 0,
  suggestions    TEXT NOT NULL DEFAULT '[]',
  suggestions_pending INTEGER NOT NULL DEFAULT 0,
  path           TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (run_id, device)
);
CREATE INDEX IF NOT EXISTS idx_runs_experiment ON runs(experiment);
CREATE INDEX IF NOT EXISTS idx_runs_started    ON runs(started_at);
DROP INDEX IF EXISTS idx_runs_device;  -- superseded by the composite below
-- Device-scoped, newest-first pages walk this index and read only the LIMIT rows:
-- O(limit) regardless of how many runs THIS or any other sample has accumulated.
CREATE INDEX IF NOT EXISTS idx_runs_device_started
  ON runs(device, started_at DESC, run_id DESC);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class RunRecord(BaseModel):
    """Manifest of one persisted run (the contents of ``record.json``)."""

    run_id: str
    experiment: str
    device: str
    backend: str
    operator: str = ""  # OS login of whoever ran it (multi-user SSH provenance)
    cooldown: str = ""  # active cooldown-cycle id when the run started ("" = none declared)
    setup: str = ""  # NAME of the setup in effect ("" = none declared / ambiguous unbound)
    targets: list[str]
    started_at: str  # ISO-8601 local time with UTC offset (matches the folder dates)
    ended_at: str
    outcome: str  # summary: successful | partial | failed | no_data
    outcomes: dict[str, str]
    error: str | None = None
    updated_device: bool = False
    #: suggested field updates captured from update() (scqo.suggestions.Suggestion
    #: dicts, kept opaque here): qubit/field/store/before/after + decision status.
    #: Stored in record.json — the truth — so decisions survive any index rebuild.
    suggestions: list[dict] = Field(default_factory=list)
    #: raw per-qubit output-chain values behind readout_power_dbm at run END
    #: (Backend.power_context: vendor-specific keys, provenance only, never
    #: re-applied). record.json-only — deliberately NOT an index column.
    power_context: dict = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    note: str = ""
    path: str  # run folder, relative to data_root (forward slashes)
    schema_version: int = SCHEMA_VERSION


def _summarize(outcomes: dict[str, str]) -> str:
    """Collapse per-qubit outcomes into one searchable verdict."""
    values = set(outcomes.values())
    if not values or values == {"no_data"}:
        return "no_data"
    if values == {"successful"}:
        return "successful"
    if "successful" in values:
        return "partial"
    return "failed"


def _scrub(obj: Any) -> Any:
    """Replace NaN/Inf floats with None so every artifact is STRICT JSON.

    Fit dicts legitimately carry NaN (e.g. an unresolved T2*); ``json.dumps`` would
    emit literal ``NaN``, which Python reads back but strict parsers (browsers,
    SQLite's json_each, datasette) reject.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v) for v in obj]
    return obj


def _dumps(payload: Any) -> str:
    return json.dumps(_scrub(payload), indent=2)


def _write_json(path: Path, payload: Any) -> None:
    """Write strict JSON atomically (unique temp + replace): no half files, no NaN."""
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")  # pid: concurrent taggers
    tmp.write_text(_dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


class DataStore:
    """Folder-backed run store with a rebuildable SQLite index."""

    def __init__(
        self, data_root: str | Path, *, device_name: str = "device",
        setup: str | None = None, cooldown: str | None = None,
    ) -> None:
        self.data_root = Path(data_root).expanduser()
        self.device_name = device_name
        #: (cooldown id, NAMED setup) this store's session was resolved to ("" =
        #: unbound). Bound once by build_session and stamped on every run; see
        #: run_stamps for the semantics.
        self.setup_name = setup or ""
        self.cooldown_id = cooldown or ""
        self.data_root.mkdir(parents=True, exist_ok=True)
        self._db_path = self.data_root / INDEX_FILE
        with self._connect() as db:
            db.executescript(_SCHEMA)
            row = db.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            with self._connect() as db:
                db.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
        elif int(row["value"]) != SCHEMA_VERSION:
            # Old index layout: the index is only a cache, so rebuild it from the folders.
            self.reindex()

    # ------------------------------------------------------------------ folders
    def new_run_dir(self, experiment: str) -> tuple[str, Path]:
        """Allocate a run_id and create its folder (collision-safe via exclusive mkdir).

        The device (sample) name is PART of the run_id: the exclusive-mkdir guard only
        serializes within one device's folder, so two samples starting the same
        experiment in the same second would otherwise mint identical ids — making
        ``/run/{run_id}``, ``tag_run`` and ``load_run`` ambiguous (caught by CI's fast
        runners; the two-students-two-samples scenario). With the device embedded the
        id is globally unique by construction — no cross-device locking needed.
        """
        # Validate the cooldown registry LOUDLY at run START — before any instrument
        # time is spent. (A corrupt registry surfacing only at persist time would
        # discard the measurement as a datastore_error.)
        self.run_stamps()
        now = datetime.now()  # local wall-clock: humans browse folders by lab date
        stamp = now.strftime("%Y%m%d-%H%M%S")
        day_dir = self.data_root / self.device_name / now.strftime("%Y-%m-%d")
        for seq in range(1, 100):
            run_id = f"{stamp}-{self.device_name}-{experiment}-{seq:02d}"
            run_dir = day_dir / run_id
            try:
                run_dir.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                continue
            return run_id, run_dir
        raise RuntimeError(f"could not allocate a unique run dir for {stamp}-{experiment}")

    def run_stamps(self) -> tuple[str, str]:
        """(cooldown id, setup NAME) a run started now should carry — the run's
        environment provenance: device -> cycle -> named setup. ("", "") when no cycle
        registry / no open cycle exists — tolerant by design: library Sessions
        (notebooks) bypass the CLI's loud resolution chain, which is the enforcer.

        A BOUND era (the (cooldown, setup) pair resolved once by build_session) is
        returned VERBATIM and deliberately NOT re-validated here: stamping what the
        session actually used is truthful provenance — if the manager ends/starts a
        cycle mid-measurement, mixing the NEW cycle id with the OLD bound setup would
        stamp a pair that never existed, and re-validating at persist time could
        raise AFTER the measurement (the exact data-loss mode run-START validation
        exists to prevent). A setup bound WITHOUT a cycle id (tests, hand-built
        stores) falls back to the currently active cycle. An unbound store auto-uses
        the active cycle's ONLY setup (notebook parity with the CLI's auto-selection)
        and stamps "" when the cycle has zero or several. Registry CORRUPTION still
        raises loudly (load_cooldowns)."""
        cycles = load_cooldowns(self.data_root, self.device_name)
        if self.setup_name and self.cooldown_id:
            return self.cooldown_id, self.setup_name
        active = active_cooldown(cycles)
        if active is None:
            return "", ""
        cid, cycle = active
        if self.setup_name:
            return cid, self.setup_name
        try:
            name, _ = resolve_setup(cycle)
            return cid, name
        except SetupResolutionError:
            return cid, ""

    def persist_run(
        self,
        *,
        run_id: str,
        run_dir: Path,
        experiment: str,
        params: Any,
        dataset: Any | None,
        result: dict,
        device_before: dict,
        device_after: dict,
        started_at: str,
        ended_at: str,
        backend: str,
        updated_device: bool,
        suggestions: list[dict] | None = None,
        tags: list[str] | None = None,
        note: str = "",
        power_context: dict | None = None,
    ) -> RunRecord:
        """Write the run folder (``record.json`` last) and upsert the index row."""
        params_dump = params.model_dump(mode="json") if hasattr(params, "model_dump") else dict(params)
        parameters = {"experiment": experiment, **params_dump}

        if dataset is not None:
            dataset.to_netcdf(run_dir / "dataset.nc")
        _write_json(run_dir / "parameters.json", parameters)
        _write_json(run_dir / "result.json", result)
        _write_json(run_dir / "device_before.json", device_before)
        _write_json(run_dir / "device_after.json", device_after)

        outcomes = {q: str(o) for q, o in result.get("outcomes", {}).items()}
        cooldown, setup = self.run_stamps()
        record = RunRecord(
            run_id=run_id,
            experiment=experiment,
            device=self.device_name,
            backend=backend,
            operator=_current_operator(),
            cooldown=cooldown,
            setup=setup,
            targets=list(params_dump.get("targets", [])),
            started_at=started_at,
            ended_at=ended_at,
            outcome=_summarize(outcomes),
            outcomes=outcomes,
            error=result.get("error"),
            updated_device=updated_device,
            suggestions=list(suggestions or []),
            power_context=_scrub(dict(power_context or {})),
            tags=list(tags or []),
            note=note,
            path=run_dir.relative_to(self.data_root).as_posix(),
        )
        _write_json(run_dir / RECORD_FILE, record.model_dump(mode="json"))  # completion marker

        with self._connect() as db:
            self._upsert(db, record, parameters, result.get("fit") or {})
        return record

    # -------------------------------------------------------------------- query
    def find_runs(
        self,
        *,
        experiment: str | None = None,
        target: str | None = None,
        tag: str | None = None,
        since: str | None = None,
        until: str | None = None,
        outcome: str | None = None,
        device: str | None = None,
        operator: str | None = None,
        cooldown: str | None = None,
        setup: str | None = None,
        pending: bool | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Query the index; newest first; rows as JSON-able dicts (RunRecord fields + fit)."""
        where, args = [], []
        if experiment is not None:
            where.append("experiment = ?")
            args.append(experiment)
        if target is not None:
            where.append("EXISTS (SELECT 1 FROM json_each(runs.targets) WHERE value = ?)")
            args.append(target)
        if tag is not None:
            where.append("EXISTS (SELECT 1 FROM json_each(runs.tags) WHERE value = ?)")
            args.append(tag)
        if since is not None:
            where.append("started_at >= ?")
            args.append(since)
        if until is not None:
            # A bare date must be day-INCLUSIVE: '2026-07-04' sorts before
            # '2026-07-04T...', so pad it past any timestamp of that day.
            where.append("started_at <= ?")
            args.append(until + "T~" if len(until) == 10 else until)
        if outcome is not None:
            where.append("outcome = ?")
            args.append(outcome)
        if device is not None:
            where.append("device = ?")
            args.append(device)
        if operator is not None:
            where.append("operator = ?")
            args.append(operator)
        if cooldown is not None:
            where.append("cooldown = ?")
            args.append(cooldown)
        if setup is not None:  # setup names are unique per cycle only — combine with cooldown
            where.append("setup = ?")
            args.append(setup)
        if pending is not None:  # True = runs with undecided suggestions, False = none left
            where.append("suggestions_pending > 0" if pending else "suggestions_pending = 0")
        sql = "SELECT * FROM runs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY started_at DESC, run_id DESC LIMIT ?"
        args.append(int(limit))
        with self._connect() as db:
            rows = db.execute(sql, args).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def distinct_experiments(self) -> list[str]:
        """Experiment names present in the index (for filter dropdowns)."""
        with self._connect() as db:
            rows = db.execute("SELECT DISTINCT experiment FROM runs ORDER BY experiment").fetchall()
        return [r["experiment"] for r in rows]

    def distinct_devices(self) -> list[str]:
        """Device (sample) names present in the index (for the viewer's device switcher)."""
        with self._connect() as db:
            rows = db.execute("SELECT DISTINCT device FROM runs ORDER BY device").fetchall()
        return [r["device"] for r in rows]

    def fit_trend(self, qubit: str, quantity: str, limit: int = 500, device: str | None = None) -> list[dict]:
        """One fitted quantity vs time for one qubit (oldest first) — drift at a glance.

        ``quantity`` is a fit key (t1_s, t2_star_s, readout_freq, pi_amp, ...). The
        JSON path is passed as a bound parameter, so arbitrary names are safe.
        ``device`` narrows to one sample — qubit names repeat across samples ("q1"
        exists on every chip), so multi-device data roots should always pass it.
        """
        path = f"$.{qubit}.{quantity}"
        sql = (
            "SELECT run_id, started_at, experiment, json_extract(fit, ?) AS value "
            "FROM runs WHERE json_extract(fit, ?) IS NOT NULL "
        )
        args: list[Any] = [path, path]
        if device is not None:
            sql += "AND device = ? "
            args.append(device)
        sql += "ORDER BY started_at LIMIT ?"
        args.append(int(limit))
        with self._connect() as db:
            rows = db.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def load_run(self, run_id: str) -> dict:
        """Load one run's JSON-able contents (record, parameters, result, figure paths)."""
        run_dir = self._run_dir(run_id)
        out = {
            "record": json.loads((run_dir / RECORD_FILE).read_text(encoding="utf-8")),
            "parameters": json.loads((run_dir / "parameters.json").read_text(encoding="utf-8")),
            "result": json.loads((run_dir / "result.json").read_text(encoding="utf-8")),
            "path": str(run_dir),
            "figures": sorted(str(p) for p in (run_dir / "analysis").rglob("*.png"))
            if (run_dir / "analysis").is_dir()
            else [],
        }
        return out

    def open_dataset(self, run_id: str):
        """Load the run's raw dataset (power-user path; not part of the JSON boundary)."""
        import xarray as xr

        return xr.load_dataset(self._run_dir(run_id) / "dataset.nc")

    # --------------------------------------------------------------------- tags
    def tag_run(
        self,
        run_id: str,
        *,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        note: str | None = None,
    ) -> dict:
        """Retro-tag a run. ``record.json`` (the truth) is updated first, then the index.

        Lock, re-read, edit, write — the same discipline as :meth:`edit_suggestions`,
        and for the same reason: the write covers the WHOLE record, so a snapshot
        loaded before a concurrent writer's suggestion (``scqo suggest``) or accept
        decision landed would silently erase it on the way back out.
        """
        run_dir = self._run_dir(run_id)
        with _file_lock(run_dir / RECORD_FILE):
            record = json.loads((run_dir / RECORD_FILE).read_text(encoding="utf-8"))
            tags = [t for t in record.get("tags", []) if t not in set(remove or [])]
            tags += [t for t in (add or []) if t not in tags]
            record["tags"] = tags
            if note is not None:
                record["note"] = note
            _write_json(run_dir / RECORD_FILE, record)
        with self._connect() as db:
            db.execute(
                "UPDATE runs SET tags = ?, note = ? WHERE run_id = ?",
                (json.dumps(tags), record.get("note", ""), run_id),
            )
        return record

    def update_suggestions(
        self, run_id: str, suggestions: list[dict], updated_device: bool | None = None
    ) -> dict:
        """Retro-REPLACE a run's suggestion list by run_id (see :meth:`edit_suggestions`).

        Whole-list replace: the caller's list wins over anything a concurrent
        writer stored since the caller loaded — use :meth:`edit_suggestions` when
        the run may have several live writers (``scqo suggest`` vs an accept)."""
        return self.edit_suggestions(run_id, lambda _rows: suggestions,
                                     updated_device=updated_device)

    def edit_suggestions(
        self,
        run_id: str,
        editor,
        updated_device: bool | None = None,
    ) -> dict:
        """Edit a run's suggestion list ATOMICALLY: lock, re-read, edit, write.

        ``editor(rows)`` receives the FRESH stored list (never a stale snapshot)
        and returns the list to store. The read-edit-write runs under the run
        record's lock file, so two live writers — an operator attaching a value
        via ``scqo suggest`` while someone else accepts — cannot silently erase
        each other's items or decisions (the list is append-only and positions
        are stable, so index-targeted edits stay valid across writers).

        Same discipline as :meth:`tag_run`: ``record.json`` — the truth — is
        rewritten first, then the index row is patched, so decisions survive any
        index rebuild. ``updated_device=True`` additionally flips the run's flag
        (a later accept makes the run a device-updating run); None leaves it alone.
        """
        run_dir = self._run_dir(run_id)
        with _file_lock(run_dir / RECORD_FILE):
            record = json.loads((run_dir / RECORD_FILE).read_text(encoding="utf-8"))
            record["suggestions"] = _scrub(editor(list(record.get("suggestions", []))))
            if updated_device is not None:
                record["updated_device"] = bool(updated_device)
            _write_json(run_dir / RECORD_FILE, record)
        pending = sum(1 for s in record["suggestions"] if s.get("status") == "pending")
        with self._connect() as db:
            db.execute(
                "UPDATE runs SET suggestions = ?, suggestions_pending = ?,"
                " updated_device = COALESCE(?, updated_device) WHERE run_id = ?",
                (
                    json.dumps(record["suggestions"]),
                    pending,
                    None if updated_device is None else int(updated_device),
                    run_id,
                ),
            )
        return record

    # -------------------------------------------------------------------- index
    def reindex(self) -> int:
        """Drop and rebuild ``index.sqlite`` from the run folders. Returns the row count.

        Folders without a ``record.json`` are incomplete (crashed mid-run) and are
        skipped with a warning; unreadable records are skipped likewise, never fatal.
        """
        count = 0
        with self._connect() as db:
            db.execute("DROP TABLE IF EXISTS runs")
            db.executescript(_SCHEMA)
            db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            for record_path in sorted(self.data_root.glob(f"*/*/*/{RECORD_FILE}")):
                run_dir = record_path.parent
                try:
                    data = json.loads(record_path.read_text(encoding="utf-8"))
                    # THE one data exemption of the no-compat policy: run folders
                    # are truth and are never fresh-started, so pre-cutover
                    # records (key "qubits") stay findable under the new column.
                    if "targets" not in data and "qubits" in data:
                        data["targets"] = data.pop("qubits")
                    record = RunRecord(**data)
                    parameters = json.loads((run_dir / "parameters.json").read_text(encoding="utf-8"))
                    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
                except Exception as err:  # unreadable run folder: skip, keep indexing
                    print(f"scqo.datastore: skipping {run_dir} ({err})", file=sys.stderr)
                    continue
                self._upsert(db, record, parameters, result.get("fit") or {})
                count += 1
        return count

    # ----------------------------------------------------------------- internal
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Short-lived connection: commit on success, always close (Windows file locks)."""
        db = sqlite3.connect(self._db_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass  # e.g. some network filesystems: fall back to the default journal mode
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def _run_dir(self, run_id: str) -> Path:
        with self._connect() as db:
            rows = db.execute("SELECT path FROM runs WHERE run_id = ?", (run_id,)).fetchall()
        if not rows:
            raise KeyError(f"unknown run_id {run_id!r} (try reindex() if the folder exists)")
        if len(rows) > 1:  # same second + experiment on two devices sharing this data_root
            raise KeyError(
                f"run_id {run_id!r} exists on multiple devices: "
                + ", ".join(sorted(r["path"] for r in rows))
            )
        return self.data_root / rows[0]["path"]

    @staticmethod
    def _upsert(db: sqlite3.Connection, record: RunRecord, parameters: dict, fit: dict) -> None:
        db.execute(
            "INSERT OR REPLACE INTO runs (run_id, started_at, ended_at, experiment, device,"
            " backend, operator, cooldown, setup, targets, outcome, outcomes, fit, tags,"
            " note, error, parameters, updated_device, suggestions, suggestions_pending,"
            " path, schema_version)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.run_id,
                record.started_at,
                record.ended_at,
                record.experiment,
                record.device,
                record.backend,
                record.operator,
                record.cooldown,
                record.setup,
                json.dumps(record.targets),
                record.outcome,
                json.dumps(record.outcomes),
                json.dumps(_scrub(fit)),
                json.dumps(record.tags),
                record.note,
                record.error,
                json.dumps(_scrub(parameters)),
                int(record.updated_device),
                json.dumps(_scrub(record.suggestions)),
                sum(1 for s in record.suggestions if s.get("status") == "pending"),
                record.path,
                record.schema_version,
            ),
        )

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        out = dict(row)
        for key in ("targets", "outcomes", "fit", "tags", "parameters", "suggestions"):
            if out.get(key) is not None:
                out[key] = json.loads(out[key])
        out["updated_device"] = bool(out["updated_device"])
        return out


def reindex(data_root: str | Path) -> int:
    """Rebuild ``<data_root>/index.sqlite`` from the run folders (any device_name)."""
    return DataStore(data_root).reindex()


def _load_toml_registry(path: Path) -> dict:
    """Load an optional hand-edited TOML registry ({} if absent).

    Display-only convention: a registry with a typo must not take down the viewer —
    unreadable files warn to stderr and read as empty. (Files that stamp RUNS, like
    cooldowns.toml, deliberately do NOT use this loader.)
    """
    if not path.is_file():
        return {}
    if sys.version_info >= (3, 11):
        import tomllib
    else:  # pragma: no cover - py3.10 fallback
        import tomli as tomllib
    try:
        # utf-8-sig: tolerate a UTF-8 BOM — Windows PowerShell 5.1's
        # `Set-Content -Encoding utf8` writes one, and these registries are
        # exactly the files operators write from PowerShell.
        return tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, tomllib.TOMLDecodeError) as err:
        print(f"warning: ignoring unreadable {path}: {err}", file=sys.stderr)
        return {}


def load_device_registry(data_root: str | Path) -> dict:
    """The optional sample registry ``<data_root>/devices.toml`` -> dict ({} if absent).

    One TOML table per physical sample, holding instrument-INDEPENDENT facts only
    (description, design values, which instrument it is currently mounted on).
    Human-edited; the viewer renders it. Instrument-dependent measured quantities
    never go here — they live in run records with ``backend`` provenance.
    """
    return _load_toml_registry(Path(data_root) / DEVICES_FILE)


def load_cooldowns(data_root: str | Path, device: str) -> dict:
    """The device's cooldown-cycle registry ``<data_root>/<device>/cooldowns.toml``.

    One table per cycle (start, fridge, packaging, note; ``end`` absent = ACTIVE),
    each holding NAMED ``[<id>.setup.<name>]`` sub-tables — the name IS the setup's
    identity (stamped on runs, selected via ``scqo user --setup``). A setup table
    carries EXACTLY: ``backend`` (required, one of :data:`SETUP_BACKENDS`) and an
    optional ``note`` — nothing else. The vendor-config folder is DERIVED from the
    keys (:func:`setup_backend_config_dir`: ``<device>/<cid>/<name>/backend_config/``)
    and injected into the loaded dict as ``setup["instrument_config"]`` for real
    backends, so factories/doctor/viewer read one key that can never dangle. Wiring
    lives in the vendor config folder, not here; a cycle may have ZERO setups (runs
    refuse until the manager hand-adds one).

    Validation is LOUD — this file stamps and drives every run: corrupt TOML, a
    non-table cycle, a non-filename-safe cooldown id, more than one open cycle, the
    retired v0.6 ``[[<id>.setup]]`` array form, a bad or casefold-twin setup name,
    unknown setup keys (``since``/port maps retired in v0.7; ``instrument_config``
    retired in v0.9 — the path is derived), or a missing/unknown backend all raise
    ValueError naming the file and the fix. Folder EXISTENCE is deliberately NOT
    checked here — analysis machines must read registries whose instrument folders
    don't exist locally; the driver factory and ``scqo doctor`` check existence.
    An absent file returns {}.
    """
    path = Path(data_root) / device / COOLDOWNS_FILE
    if not path.is_file():
        return {}
    if sys.version_info >= (3, 11):
        import tomllib
    else:  # pragma: no cover - py3.10 fallback
        import tomli as tomllib
    try:
        # utf-8-sig: tolerate a PowerShell-written UTF-8 BOM (see _load_toml_registry)
        cycles = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except tomllib.TOMLDecodeError as err:
        raise ValueError(f"invalid cooldown registry {path}: {err}") from None
    cids: dict[str, str] = {}
    for cid, cycle in cycles.items():
        if not isinstance(cycle, dict):
            raise ValueError(f"{path}: top-level keys must be cycle tables like [cd8]; {cid!r} is not")
        if not _SETUP_NAME_RE.match(cid):
            # The cooldown id becomes a folder segment (<cooldown>/<setup>/scqo/) and
            # an index value, so it must be filename/query-safe like a setup name.
            raise ValueError(f"{path}: cooldown id {cid!r} must be letters/digits/_/- only "
                             "(it becomes a folder name and an index value)")
        if cid.casefold() in cids:
            # Same reason as the setup-name twin check: on a case-insensitive
            # filesystem (Windows) [cd2] and [CD2] would ALIAS one folder tree —
            # a new cycle would silently inherit and overwrite the ended cycle's
            # state files and vendor wiring snapshot.
            raise ValueError(f"{path}: cooldown ids {cids[cid.casefold()]!r} and {cid!r} differ "
                             "only by letter case — their folders cannot tell them apart; "
                             "pick a new id")
        cids[cid.casefold()] = cid
        setups = cycle.get("setup", {})
        if isinstance(setups, list):
            raise ValueError(
                f"{path}: cycle {cid!r} uses the retired [[{cid}.setup]] array form — setups are "
                f"NAMED sub-tables since v0.7.0: [{cid}.setup.<name>] (one table per measurement "
                "setup; the name is the setup's identity)")
        if not isinstance(setups, dict) or not all(isinstance(s, dict) for s in setups.values()):
            raise ValueError(f"{path}: [{cid}.setup] must contain named setup tables like "
                             f"[{cid}.setup.qblox_main]")
        # Zero setups is legal: an empty cycle loads fine; runs refuse at session-build
        # time with a message naming the hand-edit fix (the manager adds blocks later).
        names: dict[str, str] = {}
        for name, setup in setups.items():
            if not _SETUP_NAME_RE.match(name):
                raise ValueError(f"{path}: setup name {name!r} in cycle {cid!r} must be "
                                 "letters/digits/_/- only (it becomes a CLI argument and an "
                                 "index value)")
            if name.casefold() in names:
                # The setup name is a folder segment (<cooldown>/<name>/...) — on a
                # case-insensitive filesystem (Windows) "Main" and "main" would share
                # one folder, and selections/era guards would confuse humans anyway.
                raise ValueError(f"{path}: setups {names[name.casefold()]!r} and {name!r} in "
                                 f"cycle {cid!r} differ only by letter case — their folders and "
                                 "selections cannot tell them apart; rename one")
            names[name.casefold()] = name
            # Join with the SAME `device` argument the scqo-sibling helpers use
            # (setup_scqo_dir/setup_state_path) — never path.parent.name, which
            # would silently diverge for a device string containing a separator.
            derived = setup_backend_config_dir(data_root, device, cid, name).resolve()
            if "instrument_config" in setup:
                # Retired in v0.9 (the path was a second source of truth that could
                # dangle when folders moved): the folder is DERIVED from the keys.
                raise ValueError(
                    f"{path}: [{cid}.setup.{name}]: 'instrument_config' was retired in v0.9 — "
                    f"the vendor folder is derived from the keys: {derived}. Delete the line "
                    "and keep the vendor files there (canonical names).")
            unknown = sorted(k for k in setup if k not in SETUP_KEYS)
            if unknown:
                raise ValueError(
                    f"{path}: [{cid}.setup.{name}] has unknown key(s): {', '.join(unknown)} — "
                    f"allowed keys: {', '.join(SETUP_KEYS)}. v0.7.0 retired 'since' dates (the "
                    "setup NAME is its identity) and port-map pairs; v0.9 retired "
                    "'instrument_config' (the vendor folder is derived from the keys).")
            backend = setup.get("backend")
            if backend not in SETUP_BACKENDS:
                raise ValueError(f"{path}: [{cid}.setup.{name}]: 'backend' must be one of "
                                 f"{', '.join(SETUP_BACKENDS)}, got {backend!r}")
            if backend != "simulated":
                # Injected (not user-typed) so factories/doctor/viewer keep reading one
                # key; absolute = "one form for all consumers". Simulated has no vendor
                # folder — the key stays absent, as before.
                setup["instrument_config"] = str(derived)
    open_cycles = [cid for cid, cycle in cycles.items() if "end" not in cycle]
    if len(open_cycles) > 1:
        raise ValueError(
            f"{path}: more than one open cycle ({', '.join(open_cycles)}) — 'end' the finished one first"
        )
    return cycles


def active_cooldown(cycles: dict) -> tuple[str, dict] | None:
    """The open cycle (no ``end`` key) as ``(id, cycle)``, or None."""
    for cid, cycle in cycles.items():
        if "end" not in cycle:
            return cid, cycle
    return None


class SetupResolutionError(ValueError):
    """A cycle's setup could not be resolved.

    ``reason`` is ``'none'`` (the cycle has no setups yet), ``'ambiguous'`` (several
    setups and no selection) or ``'unknown'`` (the selected name doesn't exist);
    ``available`` lists the cycle's setup names. The message is generic on purpose —
    CLI callers re-frame it with the device name and the exact fix command.
    """

    def __init__(self, message: str, *, reason: str, available: list[str]):
        super().__init__(message)
        self.reason = reason
        self.available = available


def resolve_setup(cycle: dict, name: str | None = None) -> tuple[str, dict]:
    """The ``(name, setup)`` a session should use: the NAMED one when ``name`` is
    given, else the cycle's ONLY setup — with several setups a selection is mandatory
    (``scqo user --setup``), with zero the manager must hand-add a block first.
    Raises :class:`SetupResolutionError` otherwise (structured ``reason`` +
    ``available`` so each caller can print its own exact fix)."""
    setups = cycle.get("setup", {})
    if not setups:
        # Checked BEFORE a selected name: on an empty cycle the only real fix is
        # hand-adding a block — "unknown selection" would prescribe scqo user
        # --setup, which cannot select anything here.
        raise SetupResolutionError("this cycle has no setups yet", reason="none", available=[])
    if name:
        if name in setups:
            return name, setups[name]
        raise SetupResolutionError(
            f"setup {name!r} does not exist in this cycle "
            f"(available: {', '.join(setups)})",
            reason="unknown", available=list(setups))
    if len(setups) == 1:
        return next(iter(setups.items()))
    raise SetupResolutionError(
        f"this cycle has {len(setups)} setups and none is selected ({', '.join(setups)})",
        reason="ambiguous", available=list(setups))


def _setup_dir(data_root: str | Path, device: str, cooldown: str, setup_name: str) -> Path:
    """The one folder per (device, cooldown, setup): ``<data_root>/<device>/
    <cooldown>/<setup_name>/``. Both ``cooldown`` and ``setup_name`` become path
    segments, so both must be filename-safe (``_SETUP_NAME_RE``)."""
    for part, what in ((cooldown, "cooldown id"), (setup_name, "setup name")):
        if not part or not _SETUP_NAME_RE.match(part):
            raise ValueError(f"a setup path needs a {what} (letters/digits/_/- only), got {part!r}")
    return Path(data_root) / device / cooldown / setup_name


def setup_scqo_dir(data_root: str | Path, device: str, cooldown: str,
                   setup_name: str) -> Path:
    """The SCQO-owned folder for one (device, cooldown, setup):
    ``<data_root>/<device>/<cooldown>/<setup_name>/scqo/``.

    It holds this context's ``scqo_state.json`` (calibration values) and
    ``physical.json`` (measured physics), each with its ``.history.jsonl``
    change-history sidecar. It is the FIXED SIBLING of the
    setup's vendor-config folder (:func:`setup_backend_config_dir`), never inside
    it: the QM backend's vendor folder IS QUAM's state directory and ``Quam.load()``
    ``rglob("*.json")``-merges everything under it, so SCQO files must live OUTSIDE
    that folder — here they always do, by construction."""
    return _setup_dir(data_root, device, cooldown, setup_name) / SCQO_SUBDIR


def setup_backend_config_dir(data_root: str | Path, device: str, cooldown: str,
                             setup_name: str) -> Path:
    """A real setup's vendor-config folder — DERIVED from the registry keys, never
    typed: ``<data_root>/<device>/<cooldown>/<setup_name>/backend_config/``. It holds
    ALL the vendor's config files under canonical names (qblox: ``dut_config.json`` +
    ``hw_config.json``; qm: ``state.json`` + ``wiring.json``); simulated setups have
    none. :func:`load_cooldowns` injects it as ``setup["instrument_config"]`` so
    factories/doctor/viewer read one key regardless."""
    return _setup_dir(data_root, device, cooldown, setup_name) / BACKEND_CONFIG_SUBDIR


def setup_state_path(data_root: str | Path, device: str, cooldown: str,
                     setup_name: str) -> Path:
    """This context's SCQO calibration state file — ``<scqo dir>/scqo_state.json``
    (see :func:`setup_scqo_dir`). The per-device ``<device>/scqo_state.json`` and the
    v0.9 ``.scqo``-in-instrument_config layouts are both retired (fresh start)."""
    return setup_scqo_dir(data_root, device, cooldown, setup_name) / STATE_FILE




if __name__ == "__main__":  # python -m scqo <data_root>
    if len(sys.argv) != 2:
        print("usage: python -m scqo <data_root>", file=sys.stderr)
        raise SystemExit(2)
    print(f"indexed {reindex(sys.argv[1])} runs")
