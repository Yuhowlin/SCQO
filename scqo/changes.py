"""Per-context change-history database — ``history.sqlite``.

One SQLite file per (cooldown, setup) context, sitting in the context's
``scqo/`` folder next to ``physical.json`` and ``scqo_state.json`` and
serving BOTH stores through the ``store`` column (``'physical'`` |
``'state'``). It replaces the two ``*.history.jsonl`` sidecars: appends are
O(new rows) instead of a whole-file rewrite, readers get indexed queries
instead of a full parse, and a Session no longer loads any history at
construction.

The file is TRUTH, not a cache: manual ``scqo set`` writes, accept
timestamps and external-change evidence exist nowhere else, so — unlike
``index.sqlite`` — it is never dropped on a version bump and must never be
deleted. It is deliberately PER CONTEXT because lab aggregation is a folder
copy + reindex, never a database-level merge (INSTALL "Merging data from
another server"): a context has exactly one writing machine, so per-context
files merge across servers by copy for the same reason run folders do. A
per-device database would collide the moment two operators share a device
and cooldown but drive different setups from their own data_roots.

Write discipline mirrors :meth:`scqo.datastore.DataStore._connect`:
short-lived connections, WAL (best effort), ``BEGIN IMMEDIATE`` for the one
write transaction, always closed (Windows file locks; closing the last
connection also checkpoints and removes the ``-wal``/``-shm`` side files, so
an idle context folder stays copy-clean). Read methods NEVER create the
file: they stat first and connect ``mode=ro``, which is what lets the
read-only viewer browse an unwritten tree without minting databases.

Ordering contract everywhere: ``ORDER BY timestamp, seq`` — ``seq`` (the
rowid) breaks same-timestamp ties by insertion order, reproducing the old
sidecar merge's stable-sort rule. Lexicographic timestamp order ==
chronological order rests on the lab's fixed UTC offset (see
:func:`scqo.stores._now`). ``old``/``new`` are JSON-encoded so ``float[]``
waveforms round-trip; the index deliberately excludes them.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields as _dc_fields
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

#: The per-context database file name (inside the context's ``scqo/`` folder;
#: the device-level escape-hatch physical store gets one next to its
#: ``physical.json`` at the device folder).
HISTORY_FILE = "history.sqlite"

#: Version of THIS database's schema — independent of the run index's
#: ``datastore.SCHEMA_VERSION``. A higher stamp on disk refuses loudly on
#: write (the file is truth; never drop-and-rebuild) and degrades on read.
CHANGES_SCHEMA_VERSION = 1

_STORES = ("physical", "state")


class ChangesError(RuntimeError):
    """A history database that cannot be used safely must fail loudly."""


@dataclass(frozen=True)
class ChangeRecord:
    """One recorded change — provenance for the AI loop's memory."""

    timestamp: str
    entity: str
    field: str
    old: float | list[float] | None
    new: float | list[float]
    #: the entity's kind, stamped at write time so rows stay self-describing
    #: even if the roster later changes.
    kind: str | None = None
    experiment: str | None = None
    #: run_id of the datastore run that caused this change.
    run_id: str | None = None
    #: campaign_id of the campaign-level accept that caused this change
    #: (Session.accept_campaign applies an AGGREGATE, so no single run_id
    #: can be credited — the campaign record is the finer truth there).
    campaign_id: str | None = None
    #: OS login of whoever made the change (None only when undeterminable).
    operator: str | None = None
    #: Set when this change is the echo of writing another field (one vendor
    #: knob feeds several neutral fields): names the causing field.
    coupled_to: str | None = None
    #: NAMED setup the writing session was bound to.
    setup: str | None = None
    #: Cooldown cycle the writing session was bound to. Informational — for
    #: a per-context database the FOLDER is the context identity; the stamp
    #: keeps rows self-describing when queried in aggregate.
    cooldown: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_RECORD_KEYS = frozenset(f.name for f in _dc_fields(ChangeRecord))

_DDL = (
    """CREATE TABLE IF NOT EXISTS changes (
        seq INTEGER PRIMARY KEY,
        timestamp TEXT NOT NULL,
        cooldown TEXT NOT NULL DEFAULT '',
        setup TEXT NOT NULL DEFAULT '',
        store TEXT NOT NULL CHECK (store IN ('physical','state')),
        entity TEXT NOT NULL,
        field TEXT NOT NULL,
        old TEXT,
        new TEXT NOT NULL,
        kind TEXT,
        experiment TEXT,
        run_id TEXT,
        campaign_id TEXT,
        operator TEXT,
        coupled_to TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS idx_changes_key
       ON changes(store, entity, field, timestamp)""",
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
)

_COLUMNS = ("timestamp", "cooldown", "setup", "store", "entity", "field",
            "old", "new", "kind", "experiment", "run_id", "campaign_id",
            "operator", "coupled_to")


def _check_store(store: str) -> None:
    if store not in _STORES:
        raise ValueError(f"store must be one of {_STORES}, got {store!r}")


def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
    """A DB row as a plain dict with ``old``/``new`` json-decoded."""
    d = dict(row)
    d.pop("rn", None)  # window-function helper column, never part of the row
    d["old"] = None if d["old"] is None else json.loads(d["old"])
    d["new"] = json.loads(d["new"])
    return d


def record_from_row(row: dict[str, Any]) -> ChangeRecord:
    """A queried row back as a :class:`ChangeRecord` (``'' -> None`` for the
    context stamps, exactly inverting the insert; ``seq``/``store`` drop)."""
    kw = {k: v for k, v in row.items() if k in _RECORD_KEYS}
    for key in ("setup", "cooldown"):
        if kw.get(key) == "":
            kw[key] = None
    return ChangeRecord(**kw)


class ChangeDB:
    """One context's ``history.sqlite`` — path holder, no live connection.

    Every operation opens its own short-lived connection (thread-safe by
    construction; safe under FastAPI/TestClient thread pools). Write access
    goes through :meth:`transaction`; the read methods are safe on a path
    that does not exist yet and NEVER create it.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------ write side

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One ``BEGIN IMMEDIATE`` write transaction: DDL + version gate +
        caller's work commit together; a raise rolls the whole thing back
        (close without commit), so a vetoed save leaves zero rows behind."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self._path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            try:
                db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError:
                pass  # e.g. some network filesystems: default journal mode
            db.execute("BEGIN IMMEDIATE")
            for statement in _DDL:
                db.execute(statement)
            self._gate_version(db)
            yield db
            db.commit()
        finally:
            db.close()

    def _gate_version(self, db: sqlite3.Connection) -> None:
        row = db.execute(
            "SELECT value FROM meta WHERE key = 'changes_schema_version'"
        ).fetchone()
        if row is None:
            db.execute(
                "INSERT INTO meta (key, value) VALUES "
                "('changes_schema_version', ?)",
                (str(CHANGES_SCHEMA_VERSION),))
            return
        found = int(row["value"])
        if found > CHANGES_SCHEMA_VERSION:
            raise ChangesError(
                f"{self._path} was written by a newer scqo (history schema "
                f"v{found} > v{CHANGES_SCHEMA_VERSION}) — upgrade this "
                f"machine; the file is change-history TRUTH and is never "
                f"dropped or rebuilt")
        # found < current: in-place upgrade hook (nothing to do at v1).

    @staticmethod
    def insert(db: sqlite3.Connection, records: Sequence[ChangeRecord], *,
               store: str) -> None:
        """Append records inside an open :meth:`transaction`."""
        _check_store(store)
        db.executemany(
            f"INSERT INTO changes ({', '.join(_COLUMNS)}) "
            f"VALUES ({', '.join('?' * len(_COLUMNS))})",
            [(r.timestamp, r.cooldown or "", r.setup or "", store,
              r.entity, r.field,
              None if r.old is None else json.dumps(r.old),
              json.dumps(r.new),
              r.kind, r.experiment, r.run_id, r.campaign_id,
              r.operator, r.coupled_to)
             for r in records])

    @staticmethod
    def latest_new(db: sqlite3.Connection, *, store: str,
                   keys: Iterable[tuple[str, str]]
                   ) -> dict[tuple[str, str], Any]:
        """Latest ``new`` value per (entity, field) key, inside an open
        :meth:`transaction` — sees the caller's own uncommitted inserts, so
        a saving session's rows win their timestamp ties via higher seq."""
        _check_store(store)
        out: dict[tuple[str, str], Any] = {}
        for entity, field in keys:
            row = db.execute(
                "SELECT new FROM changes WHERE store = ? AND entity = ? "
                "AND field = ? ORDER BY timestamp DESC, seq DESC LIMIT 1",
                (store, entity, field)).fetchone()
            if row is not None:
                out[(entity, field)] = json.loads(row["new"])
        return out

    # ------------------------------------------------------------- read side
    #
    # Every read method returns empty when the file does not exist and opens
    # mode=ro when it does — a reader can NEVER mint or mutate the database.

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection | None]:
        if not self._path.is_file():
            yield None
            return
        db = sqlite3.connect(
            f"{self._path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            # A file whose first-ever transaction rolled back exists but has
            # no schema — an empty history, not an error.
            if db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                          "AND name='changes'").fetchone() is None:
                yield None
            else:
                yield db
        finally:
            db.close()

    def context_history(self, store: str, *, entity: str | None = None,
                        limit: int | None = None) -> list[dict[str, Any]]:
        """This context's rows of one store, ascending (oldest first);
        ``limit`` keeps the LAST N. ``entity`` narrows to one entity."""
        _check_store(store)
        where = "store = ?"
        args: list[Any] = [store]
        if entity is not None:
            where += " AND entity = ?"
            args.append(entity)
        with self._read() as db:
            if db is None:
                return []
            if limit is None:
                rows = db.execute(
                    f"SELECT * FROM changes WHERE {where} "
                    f"ORDER BY timestamp, seq", args).fetchall()
            else:
                rows = db.execute(
                    f"SELECT * FROM changes WHERE {where} "
                    f"ORDER BY timestamp DESC, seq DESC LIMIT ?",
                    args + [int(limit)]).fetchall()[::-1]
        return [_decode_row(r) for r in rows]

    def param_series(self, entity: str, field: str, *,
                     store: str | None = None, limit: int = 50
                     ) -> list[dict[str, Any]]:
        """Latest ``limit`` changes of ONE parameter, NEWEST first (the
        viewer's port-1 drilldown). ``store=None`` searches both stores —
        a field name belongs to exactly one role, so this is a convenience,
        not an ambiguity."""
        if store is None:
            where, args = "store IN ('physical','state')", []
        else:
            _check_store(store)
            where, args = "store = ?", [store]
        with self._read() as db:
            if db is None:
                return []
            rows = db.execute(
                f"SELECT * FROM changes WHERE {where} AND entity = ? "
                f"AND field = ? ORDER BY timestamp DESC, seq DESC LIMIT ?",
                args + [entity, field, int(limit)]).fetchall()
        return [_decode_row(r) for r in rows]

    def latest_two(self, store: str
                   ) -> dict[tuple[str, str], list[dict[str, Any]]]:
        """Per (entity, field): ``[latest, previous]`` (previous ``None``
        when the key has a single record) — the setup page's current +
        one-before provenance in one query."""
        _check_store(store)
        with self._read() as db:
            if db is None:
                return {}
            rows = db.execute(
                "SELECT * FROM ("
                "  SELECT *, ROW_NUMBER() OVER ("
                "    PARTITION BY entity, field "
                "    ORDER BY timestamp DESC, seq DESC) AS rn "
                "  FROM changes WHERE store = ?"
                ") WHERE rn <= 2 ORDER BY entity, field, rn",
                (store,)).fetchall()
        out: dict[tuple[str, str], list] = {}
        for r in rows:
            pair = out.setdefault((r["entity"], r["field"]), [None, None])
            pair[r["rn"] - 1] = _decode_row(r)
        return out

    def context_facts(self) -> list[dict[str, Any]]:
        """Latest physical-fact row per (entity, field) — one matrix column
        of the device page."""
        with self._read() as db:
            if db is None:
                return []
            rows = db.execute(
                "SELECT * FROM ("
                "  SELECT *, ROW_NUMBER() OVER ("
                "    PARTITION BY entity, field "
                "    ORDER BY timestamp DESC, seq DESC) AS rn "
                "  FROM changes WHERE store = 'physical'"
                ") WHERE rn = 1 ORDER BY entity, field",
                ).fetchall()
        return [_decode_row(r) for r in rows]

    def fact_series(self, entity: str, field: str) -> list[dict[str, Any]]:
        """This context's FULL chronological series of one physical fact
        (the viewer's port-2 building block)."""
        with self._read() as db:
            if db is None:
                return []
            rows = db.execute(
                "SELECT * FROM changes WHERE store = 'physical' "
                "AND entity = ? AND field = ? ORDER BY timestamp, seq",
                (entity, field)).fetchall()
        return [_decode_row(r) for r in rows]


# -------------------------------------------------- cross-context aggregation
#
# A device's history is the union of its context databases. The caller (the
# viewer) enumerates the contexts — registry order plus disk-discovered
# ghosts — and these helpers merge chronologically. Rows are keyed by the
# FOLDER context passed in, not the rows' own stamps: the folder is the
# context identity; a stray stamp must not mint a phantom column.

def collect_fact_matrix(
    contexts: Sequence[tuple[str, str, ChangeDB]],
) -> list[dict[str, Any]]:
    """Latest physical-fact rows of every context, context attached —
    one entry per (cooldown, setup, entity, field) cell of the matrix."""
    out: list[dict[str, Any]] = []
    for cooldown, setup, db in contexts:
        for row in db.context_facts():
            row["cooldown"], row["setup"] = cooldown, setup
            out.append(row)
    return out


def collect_fact_series(
    contexts: Sequence[tuple[str, str, ChangeDB]],
    entity: str, field: str,
) -> list[dict[str, Any]]:
    """One physical fact's chronological series across ALL contexts (the
    cross-cooldown trend). ``seq`` only orders within one database, so
    cross-context ties fall back to the context key for determinism."""
    rows: list[dict[str, Any]] = []
    for cooldown, setup, db in contexts:
        for row in db.fact_series(entity, field):
            row["cooldown"], row["setup"] = cooldown, setup
            rows.append(row)
    rows.sort(key=lambda r: (r["timestamp"], r["cooldown"], r["setup"],
                             r["seq"]))
    return rows
