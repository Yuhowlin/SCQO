"""Measurement datastore — every run saved, findable, and reloadable.

The **run folder is the truth**: each ``Session.run`` writes a self-contained folder
(dataset, parameters, result, device snapshots, scqat analysis artifacts) under::

    <data_root>/<device_name>/<YYYY-MM-DD>/<run_id>/
        record.json          # RunRecord manifest — written LAST = completion marker
        dataset.nc           # canonical contract-form dataset (dims qubit x sweep)
        parameters.json      # experiment name + validated Parameters
        result.json          # structured Result (outcomes / fit / error)
        device_before.json   # SCQO config snapshot before the run
        device_after.json    # snapshot after update()
        analysis/<qubit>/    # scqat estimator artifacts (metadata / plotdata / figures)

``index.sqlite`` at ``data_root`` is a **disposable cache** over those folders: it makes
``find_runs`` fast but holds nothing the folders don't; :func:`reindex` rebuilds it from
scratch by scanning for ``record.json`` files (folders without one are incomplete runs
and are skipped). Deleting the index is always safe — remove all ``index.sqlite*`` files
together (the ``-wal``/``-shm`` siblings too) and rerun ``python -m scqo <data_root>``.

Concurrency note: WAL mode makes one writing Session + any number of readers safe on a
local disk. If ``data_root`` lives on a network share, keep the *writing* Session on one
PC (SQLite WAL is not reliable across SMB/NFS clients); readers can always fall back to
the folders + ``reindex`` on a local copy.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, Field

SCHEMA_VERSION = 2
RECORD_FILE = "record.json"
INDEX_FILE = "index.sqlite"
DEVICES_FILE = "devices.toml"  # optional human-edited sample registry (see load_device_registry)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id         TEXT NOT NULL,
  started_at     TEXT NOT NULL,
  ended_at       TEXT,
  experiment     TEXT NOT NULL,
  device         TEXT NOT NULL,
  backend        TEXT NOT NULL,
  qubits         TEXT NOT NULL,
  outcome        TEXT NOT NULL,
  outcomes       TEXT NOT NULL,
  fit            TEXT,
  tags           TEXT NOT NULL DEFAULT '[]',
  note           TEXT NOT NULL DEFAULT '',
  error          TEXT,
  parameters     TEXT NOT NULL,
  updated_device INTEGER NOT NULL DEFAULT 0,
  path           TEXT NOT NULL,
  schema_version INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (run_id, device)
);
CREATE INDEX IF NOT EXISTS idx_runs_experiment ON runs(experiment);
CREATE INDEX IF NOT EXISTS idx_runs_started    ON runs(started_at);
CREATE INDEX IF NOT EXISTS idx_runs_device     ON runs(device);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class RunRecord(BaseModel):
    """Manifest of one persisted run (the contents of ``record.json``)."""

    run_id: str
    experiment: str
    device: str
    backend: str
    qubits: list[str]
    started_at: str  # ISO-8601 local time with UTC offset (matches the folder dates)
    ended_at: str
    outcome: str  # summary: successful | partial | failed | no_data
    outcomes: dict[str, str]
    error: str | None = None
    updated_device: bool = False
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

    def __init__(self, data_root: str | Path, *, device_name: str = "device") -> None:
        self.data_root = Path(data_root).expanduser()
        self.device_name = device_name
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
        """Allocate a run_id and create its folder (collision-safe via exclusive mkdir)."""
        now = datetime.now()  # local wall-clock: humans browse folders by lab date
        stamp = now.strftime("%Y%m%d-%H%M%S")
        day_dir = self.data_root / self.device_name / now.strftime("%Y-%m-%d")
        for seq in range(1, 100):
            run_id = f"{stamp}-{experiment}-{seq:02d}"
            run_dir = day_dir / run_id
            try:
                run_dir.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                continue
            return run_id, run_dir
        raise RuntimeError(f"could not allocate a unique run dir for {stamp}-{experiment}")

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
        tags: list[str] | None = None,
        note: str = "",
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
        record = RunRecord(
            run_id=run_id,
            experiment=experiment,
            device=self.device_name,
            backend=backend,
            qubits=list(params_dump.get("qubits", [])),
            started_at=started_at,
            ended_at=ended_at,
            outcome=_summarize(outcomes),
            outcomes=outcomes,
            error=result.get("error"),
            updated_device=updated_device,
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
        qubit: str | None = None,
        tag: str | None = None,
        since: str | None = None,
        until: str | None = None,
        outcome: str | None = None,
        device: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Query the index; newest first; rows as JSON-able dicts (RunRecord fields + fit)."""
        where, args = [], []
        if experiment is not None:
            where.append("experiment = ?")
            args.append(experiment)
        if qubit is not None:
            where.append("EXISTS (SELECT 1 FROM json_each(runs.qubits) WHERE value = ?)")
            args.append(qubit)
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
        """Retro-tag a run. ``record.json`` (the truth) is updated first, then the index."""
        run_dir = self._run_dir(run_id)
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
                    record = RunRecord(**json.loads(record_path.read_text(encoding="utf-8")))
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
            " backend, qubits, outcome, outcomes, fit, tags, note, error, parameters,"
            " updated_device, path, schema_version)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.run_id,
                record.started_at,
                record.ended_at,
                record.experiment,
                record.device,
                record.backend,
                json.dumps(record.qubits),
                record.outcome,
                json.dumps(record.outcomes),
                json.dumps(_scrub(fit)),
                json.dumps(record.tags),
                record.note,
                record.error,
                json.dumps(_scrub(parameters)),
                int(record.updated_device),
                record.path,
                record.schema_version,
            ),
        )

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        out = dict(row)
        for key in ("qubits", "outcomes", "fit", "tags", "parameters"):
            if out.get(key) is not None:
                out[key] = json.loads(out[key])
        out["updated_device"] = bool(out["updated_device"])
        return out


def reindex(data_root: str | Path) -> int:
    """Rebuild ``<data_root>/index.sqlite`` from the run folders (any device_name)."""
    return DataStore(data_root).reindex()


def load_device_registry(data_root: str | Path) -> dict:
    """The optional sample registry ``<data_root>/devices.toml`` -> dict ({} if absent).

    One TOML table per physical sample, holding instrument-INDEPENDENT facts only
    (description, design values, which instrument it is currently mounted on).
    Human-edited; the viewer renders it. Instrument-dependent measured quantities
    never go here — they live in run records with ``backend`` provenance.
    """
    path = Path(data_root) / DEVICES_FILE
    if not path.is_file():
        return {}
    if sys.version_info >= (3, 11):
        import tomllib
    else:  # pragma: no cover - py3.10 fallback
        import tomli as tomllib
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as err:
        # A hand-edited registry with a typo must not take down the viewer.
        print(f"warning: ignoring unreadable {path}: {err}", file=sys.stderr)
        return {}


if __name__ == "__main__":  # python -m scqo <data_root>
    if len(sys.argv) != 2:
        print("usage: python -m scqo <data_root>", file=sys.stderr)
        raise SystemExit(2)
    print(f"indexed {reindex(sys.argv[1])} runs")
