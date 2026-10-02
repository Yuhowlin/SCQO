"""One-time conversion of a data root's value stores to SCQO 4.0.0.

Usage (from the 4.0.0 SCQO checkout, with SCQO importable)::

    python scripts/convert_store_v4.py <data_root>            # dry run: what moves
    python scripts/convert_store_v4.py <data_root> --apply    # convert

4.0.0 addresses every stored value by its OWNER - a line (``z1``), a channel
``<line>.<target>`` (``xy1.q1``), an operation ``<composite>.<op>`` - where 3.x
used rider-minted channel names (``q1_xy``, ``q1_z``) and flattened field names
(``iswap_coupler_flux``, ``flux_per_phi0__q1``). docs/store-by-line-plan.md
section 5 is the spec. Per context folder (``<device>/<cooldown>/<setup>/scqo/``,
plus the device-level ``<device>/physical.json`` + ``history.sqlite``):

* ``physical.json`` / ``scqo_state.json`` (schema 3) -> schema 4: every value is
  re-addressed through :mod:`scqo.v3_names` (computed from the device's
  ``components.toml``, whose syntax did not change) and nested by owner. Values
  are carried unchanged. A value with NO 4.0.0 address - an entity or field the
  roster no longer has - is KEPT under its old name and reported, never dropped.
* ``history.sqlite`` (v1) -> v2 in one transaction: each row's entity and field
  are re-addressed, its ``kind`` restamped to the new owner's kind, and
  ``coupled_to`` re-pointed (a field of the same owner, else ``owner.field``).
  The row count is checked unchanged before the commit.

Every file is copied to ``*.v3.bak`` first (an existing backup is kept: the
oldest wins). Converted files are skipped, so the command can be re-run and a
second run changes nothing. It REFUSES while any run or campaign in the data
root holds a PENDING suggestion: those carry 3.x names that 4.0.0 cannot apply.
Decide them first on the 3.x install (``scqo accept``), or pass
``--reject-pending`` to reject every one of them, with a comment naming this
cutover, through the datastore's own locked suggestion editor (the index row is
patched with the record, as ``scqo accept --reject`` does).

Never touched: run folders, dataset.nc, setup snapshots, campaign folders,
components.toml, design.toml, index.sqlite - immutable run data keeps its 3.x
names and 4.0.0 reads it through the same map.

Shipped with 4.0.0 only; the next release deletes this script.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

from scqo._state_io import _file_lock
from scqo.changes import (CHANGES_SCHEMA_VERSION, HISTORY_FILE,
                          V3_CHANGES_SCHEMA_VERSION)
from scqo.roster import Roster, RosterError, load_components
from scqo.stores import (PHYSICAL_FILE, STATE_FILE, STATE_SCHEMA, V3_SCHEMA,
                         nest_values)
from scqo.v3_names import v3_address_map

COMPONENTS = "components.toml"
BACKUP_SUFFIX = ".v3.bak"
#: Device-folder children that are never a (cooldown, setup) context: a setup
#: snapshot also holds scqo/ store files, and it is immutable run data.
NOT_CONTEXTS = frozenset({"setup_snapshots", "campaigns"})
DAY_FOLDER = re.compile(r"\d{4}-\d{2}-\d{2}$")


@dataclass
class Step:
    """One file's conversion: what it is, what moves, what stays put."""

    path: Path
    status: str                      # convert | done | skip | fail
    note: str = ""
    #: old owner -> sorted "old.field -> new.field" lines (report only)
    moves: dict[str, list[str]] = field(default_factory=dict)
    #: (old entity, old field) kept under the old name - no 4.0.0 address
    kept: list[str] = field(default_factory=list)
    count: int = 0                   # values (a store) or rows (history)
    moved: int = 0


# ------------------------------------------------------------------ discovery

def devices(data_root: Path) -> list[Path]:
    """Every device folder: a direct child of the data root holding a
    components.toml or any store file."""
    out = []
    for d in sorted(p for p in data_root.iterdir() if p.is_dir()):
        if ((d / COMPONENTS).is_file() or (d / PHYSICAL_FILE).is_file()
                or any(d.glob(f"*/*/scqo/{HISTORY_FILE}"))
                or any(d.glob(f"*/*/scqo/{STATE_FILE}"))
                or any(d.glob(f"*/*/scqo/{PHYSICAL_FILE}"))):
            out.append(d)
    return out


def contexts(device_dir: Path) -> list[Path]:
    """The folders holding store files: each ``<cooldown>/<setup>/scqo/`` plus
    the device folder itself (the setup-less escape hatch). Setup snapshots
    (``setup_snapshots/<hash>/scqo/``) are immutable and never listed."""
    found = sorted(
        p for p in device_dir.glob("*/*/scqo")
        if p.is_dir() and p.parent.parent.name not in NOT_CONTEXTS
        and not DAY_FOLDER.match(p.parent.parent.name))
    if any((device_dir / name).is_file()
           for name in (PHYSICAL_FILE, STATE_FILE, HISTORY_FILE)):
        found.append(device_dir)
    return found


@dataclass(frozen=True)
class Pending:
    """One run or campaign still holding pending suggestions."""

    what: str          # "run" | "campaign"
    device: str
    ident: str         # run_id / campaign_id (timestamp-prefixed)
    count: int

    def __str__(self) -> str:
        return f"{self.what} {self.device}/{self.ident}: {self.count} pending"


def pending_suggestions(data_root: Path) -> list[Pending]:
    """Every run / campaign of the data root still holding a PENDING
    suggestion, oldest first (the run folders and campaign manifests are the
    truth; the index is only a cache)."""
    out: list[Pending] = []
    for device_dir in devices(data_root):
        for record in device_dir.glob("*/*/record.json"):
            if not DAY_FOLDER.match(record.parent.parent.name):
                continue
            n = _pending_in(record)
            if n:
                out.append(Pending("run", device_dir.name,
                                   record.parent.name, n))
        for manifest in device_dir.glob("campaigns/*/campaign.json"):
            n = _pending_in(manifest)
            if n:
                out.append(Pending("campaign", device_dir.name,
                                   manifest.parent.name, n))
    return sorted(out, key=lambda p: (p.ident, p.device))


REJECT_COMMENT = ("rejected by convert_store_v4: proposed with 3.x names, "
                  "superseded by the 4.0.0 store cutover")


def reject_pending(data_root: Path, pending: list[Pending]) -> None:
    """Reject every pending suggestion of ``pending`` through the datastore's
    locked editors (record/manifest first, then the index row)."""
    from scqo.datastore import DataStore
    from scqo.stores import _current_operator, _now

    now, who = _now(), _current_operator() or None

    def editor(rows: list) -> list:
        return [({**r, "status": "rejected", "decided_at": now,
                  "decided_by": who, "comment": REJECT_COMMENT}
                 if isinstance(r, dict) and r.get("status") == "pending"
                 else r) for r in rows]

    for item in pending:
        store = DataStore(data_root, device_name=item.device)
        try:
            if item.what == "run":
                store.edit_suggestions(item.ident, editor)
            else:
                store.edit_campaign_suggestions(item.ident, editor)
        except KeyError as err:
            raise SystemExit(
                f"{item}: {err} - rebuild the index (python -m scqo "
                f"{data_root}) and re-run") from None


def _pending_in(path: Path) -> int:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    rows = data.get("suggestions") if isinstance(data, dict) else None
    return sum(1 for s in rows or []
               if isinstance(s, dict) and s.get("status") == "pending")


# --------------------------------------------------------------- value files

def _address(new: tuple[str, str]) -> str:
    return f"{new[0]}.{new[1]}"


def convert_values(path: Path, amap: dict, *, apply: bool) -> Step:
    """One schema-3 store file -> schema 4 (see the module docstring)."""
    if not path.is_file():
        return Step(path, "skip", "absent")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        return Step(path, "fail", f"unreadable ({err}) - repair or remove it")
    schema = data.get("schema") if isinstance(data, dict) else None
    if schema == STATE_SCHEMA:
        return Step(path, "done", f"already schema {STATE_SCHEMA}")
    if schema != V3_SCHEMA:
        return Step(path, "skip",
                    f"schema {schema!r} is not a 3.x store - left to the "
                    f"store's own pre-greenfield policy")
    values = data.get("values")
    if not isinstance(values, dict):
        values = {}
    step = Step(path, "convert")
    flat: dict[str, dict] = {}
    kept: dict[str, dict] = {}
    for old_entity in sorted(values):
        fields = values[old_entity]
        if not isinstance(fields, dict):  # never a value: 3.x never read it
            step.kept.append(f"{old_entity} (not a table - IGNORED, as 3.x did)")
            continue
        for old_field in sorted(fields):
            value = fields[old_field]
            step.count += 1
            new = amap.get((old_entity, old_field))
            if new is not None and new[1] not in flat.get(new[0], {}):
                flat.setdefault(new[0], {})[new[1]] = value
                if new != (old_entity, old_field):
                    step.moved += 1
                    step.moves.setdefault(old_entity, []).append(
                        f"{old_entity}.{old_field} -> {_address(new)}")
                continue
            why = ("no 4.0.0 address" if new is None
                   else f"{_address(new)} is taken by another 3.x value")
            kept.setdefault(old_entity, {})[old_field] = value
            step.kept.append(f"{old_entity}.{old_field} ({why})")
    for old_entity, fields in kept.items():
        slot = flat.setdefault(old_entity, {})
        for f, v in fields.items():
            slot.setdefault(f, v)
    payload = {"schema": STATE_SCHEMA, "values": nest_values(flat)}
    if apply:
        _write_values(path, payload)
        _verify_values(path, values, amap, step)
    return step


def _backup(path: Path) -> None:
    bak = path.with_name(path.name + BACKUP_SUFFIX)
    if not bak.exists():  # keep-oldest: a re-run never overwrites the 3.x copy
        shutil.copy2(path, bak)


def _write_values(path: Path, payload: dict) -> None:
    with _file_lock(path):
        _backup(path)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n",
                       encoding="utf-8")
        os.replace(tmp, path)


def _live(value) -> bool:
    """A value a store actually loads (finite number, or a non-empty list of
    them); anything else was inert under 3.x and stays inert."""
    def finite(v) -> bool:
        return (isinstance(v, (int, float)) and not isinstance(v, bool)
                and math.isfinite(v))
    return finite(value) or (isinstance(value, list) and bool(value)
                             and all(finite(v) for v in value))


def _verify_values(path: Path, old_values: dict, amap: dict,
                   step: Step) -> None:
    """Every live 3.x value is in the written file, at its new address or
    (kept) under its old one, and equal."""
    from scqo.stores import flatten_values

    written = flatten_values(
        json.loads(path.read_text(encoding="utf-8"))["values"])
    for old_entity, fields in old_values.items():
        if not isinstance(fields, dict):
            continue
        for old_field, value in fields.items():
            if not _live(value):
                continue
            new = amap.get((old_entity, old_field))
            at_new = (written.get(new[0], {}).get(new[1])
                      if new is not None else None)
            at_old = written.get(old_entity, {}).get(old_field)
            if value not in (at_new, at_old):
                raise SystemExit(
                    f"{path}: verification failed for {old_entity}.{old_field} "
                    f"({value!r}); the 3.x file is at "
                    f"{path.name}{BACKUP_SUFFIX}")


# ----------------------------------------------------------------- history

def _history_version(db: sqlite3.Connection) -> int | None:
    has_meta = db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                          "AND name='meta'").fetchone()
    if not has_meta:
        return None
    row = db.execute("SELECT value FROM meta WHERE key = "
                     "'changes_schema_version'").fetchone()
    return int(row[0]) if row is not None else None


def convert_history(path: Path, roster: Roster, amap: dict, *,
                    apply: bool) -> Step:
    """One v1 history.sqlite -> v2 (see the module docstring)."""
    if not path.is_file():
        return Step(path, "skip", "absent")
    # A dry run opens read-only: even closing a read-write connection to a WAL
    # database checkpoints it, and a dry run must not touch a byte. Autocommit
    # mode: the one write transaction is BEGIN IMMEDIATE ... COMMIT below,
    # spelled out rather than left to the module's implicit BEGIN.
    db = (sqlite3.connect(path, timeout=10, isolation_level=None) if apply
          else sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True,
                               timeout=10, isolation_level=None))
    try:
        has_rows = db.execute("SELECT name FROM sqlite_master WHERE "
                              "type='table' AND name='changes'").fetchone()
        version = _history_version(db)
        if not has_rows:
            return Step(path, "skip", "no changes table (never written)")
        if version == CHANGES_SCHEMA_VERSION:
            return Step(path, "done", f"already v{CHANGES_SCHEMA_VERSION}")
        if version not in (V3_CHANGES_SCHEMA_VERSION, None):
            return Step(path, "fail",
                        f"history schema v{version} is neither 3.x "
                        f"(v{V3_CHANGES_SCHEMA_VERSION}) nor 4.0.0 "
                        f"(v{CHANGES_SCHEMA_VERSION}) - not converted")
        step = Step(path, "convert")
        rows = db.execute("SELECT seq, entity, field, coupled_to "
                          "FROM changes ORDER BY seq").fetchall()
        step.count = len(rows)
        updates = []
        kept: set[str] = set()
        for seq, entity, fld, coupled_to in rows:
            new = amap.get((entity, fld))
            if new is None:
                kept.add(f"{entity}.{fld} (no 4.0.0 address)")
                continue
            if new == (entity, fld):
                continue
            coupled = coupled_to
            if coupled_to:
                cause = amap.get((entity, coupled_to))
                if cause is not None:
                    coupled = (cause[1] if cause[0] == new[0]
                               else _address(cause))
            updates.append((new[0], new[1], roster.entities[new[0]].kind,
                            coupled, seq))
            step.moves.setdefault(entity, [])
            line = f"{entity}.{fld} -> {_address(new)}"
            if line not in step.moves[entity]:
                step.moves[entity].append(line)
        step.moved = len(updates)
        step.kept = sorted(kept)
        if apply:
            _backup_db(path, db)
            db.execute("BEGIN IMMEDIATE")
            before = db.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
            db.executemany("UPDATE changes SET entity = ?, field = ?, kind = ?, "
                           "coupled_to = ? WHERE seq = ?", updates)
            db.execute("CREATE TABLE IF NOT EXISTS meta "
                       "(key TEXT PRIMARY KEY, value TEXT)")
            db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES "
                       "('changes_schema_version', ?)",
                       (str(CHANGES_SCHEMA_VERSION),))
            after = db.execute("SELECT COUNT(*) FROM changes").fetchone()[0]
            if after != before or before != step.count:
                db.execute("ROLLBACK")
                raise SystemExit(f"{path}: rows changed while converting "
                                 f"({step.count} read, {before} -> {after}) - "
                                 f"rolled back; stop every scqo session on "
                                 f"this data root and re-run")
            db.execute("COMMIT")
        return step
    finally:
        db.close()


def _backup_db(path: Path, db: sqlite3.Connection) -> None:
    """A consistent copy through SQLite's backup API (a live ``-wal`` is
    folded in), keep-oldest like the value files."""
    bak = path.with_name(path.name + BACKUP_SUFFIX)
    if bak.exists():
        return
    dst = sqlite3.connect(bak)
    try:
        db.backup(dst)
    finally:
        dst.close()


# ------------------------------------------------------------------- driver

def convert(data_root: Path, *, apply: bool, reject: bool = False,
            verbose: bool = False, out=sys.stdout) -> int:
    """Plan (and with ``apply``, perform) the conversion. Returns the exit
    code: 0 done or nothing to do, 2 refused (nothing was written)."""
    def say(text: str = "") -> None:
        print(text, file=out)

    mode = "APPLY" if apply else "dry run - add --apply to write"
    say(f"convert_store_v4: {data_root} ({mode})")
    blockers = pending_suggestions(data_root)
    if blockers and reject:
        total = sum(p.count for p in blockers)
        if apply:
            reject_pending(data_root, blockers)
            say(f"rejected {total} pending suggestion(s) on {len(blockers)} "
                f"run(s)/campaign(s): \"{REJECT_COMMENT}\"")
            blockers = pending_suggestions(data_root)
        else:
            say(f"would reject {total} pending suggestion(s) on "
                f"{len(blockers)} run(s)/campaign(s) (--reject-pending)")
            blockers = []
    plans: list[tuple[Path, Roster | None, str]] = []
    for device_dir in devices(data_root):
        try:
            roster = load_components(device_dir)
            plans.append((device_dir, roster, ""))
        except RosterError as err:
            plans.append((device_dir, None, str(err)))
    unloadable = [(d, e) for d, r, e in plans if r is None
                  and _has_v3_files(d)]
    if blockers or unloadable:
        say()
        if blockers:
            total = sum(p.count for p in blockers)
            say(f"REFUSED: {total} PENDING suggestion(s) on {len(blockers)} "
                f"run(s)/campaign(s), oldest {blockers[0].ident[:8]}, newest "
                f"{blockers[-1].ident[:8]}. They carry 3.x names 4.0.0 cannot "
                f"apply: decide the ones that matter on the 3.x install "
                f"(scqo accept <run_id>), then re-run with --reject-pending "
                f"to reject the rest.")
            shown = blockers if verbose else blockers[-15:]
            if len(shown) < len(blockers):
                say(f"  ... {len(blockers) - len(shown)} older (-v lists all)")
            for item in shown:
                say(f"  {item}")
        for device_dir, err in unloadable:
            say(f"REFUSED: {device_dir.name}: its components.toml does not "
                f"load under 4.0.0 - fix it first:\n  {err}")
        if apply:
            say("\nnothing was written.")
            return 2
    failed = False
    totals = {"convert": 0, "done": 0}
    for device_dir, roster, _err in plans:
        if roster is None:
            continue
        amap = v3_address_map(roster)
        say(f"\ndevice {device_dir.name}: {len(roster.channels())} designed "
            f"channel(s), {len(roster.lines())} line(s), "
            f"{len(roster.operation_entities())} operation(s)")
        for ctx in contexts(device_dir):
            where = (ctx.relative_to(device_dir).parent.as_posix()
                     if ctx != device_dir else "(device level)")
            # (an apply with blockers already returned above)
            steps = [convert_values(ctx / PHYSICAL_FILE, amap, apply=apply),
                     convert_values(ctx / STATE_FILE, amap, apply=apply),
                     convert_history(ctx / HISTORY_FILE, roster, amap,
                                     apply=apply)]
            for step in steps:
                if step.status == "skip" and step.note == "absent":
                    continue
                totals[step.status] = totals.get(step.status, 0) + 1
                failed |= step.status == "fail"
                _report(say, where, step, verbose=verbose)
    say()
    if failed:
        say("FAILED: see the file(s) marked FAIL above; the others are "
            + ("converted." if apply else "unchanged."))
        return 2
    if blockers or unloadable:
        return 2
    if not totals["convert"]:
        say("nothing to convert - every store is already 4.0.0.")
        return 0
    say(f"{totals['convert']} file(s) "
        + ("converted; the 3.x copies are *.v3.bak beside them."
           if apply else "would be converted; re-run with --apply."))
    return 0


def _has_v3_files(device_dir: Path) -> bool:
    """Whether a device still holds anything this script would convert."""
    for ctx in contexts(device_dir):
        for name in (PHYSICAL_FILE, STATE_FILE):
            try:
                data = json.loads((ctx / name).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and data.get("schema") == V3_SCHEMA:
                return True
        path = ctx / HISTORY_FILE
        if path.is_file():
            db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
            try:
                if _history_version(db) != CHANGES_SCHEMA_VERSION:
                    return True
            finally:
                db.close()
    return False


def _report(say, where: str, step: Step, *, verbose: bool) -> None:
    name = step.path.name
    if step.status != "convert":
        say(f"  {where:<22} {name:<16} {step.status.upper()}: {step.note}")
        return
    what = "row(s)" if name == HISTORY_FILE else "value(s)"
    say(f"  {where:<22} {name:<16} {step.count} {what}, {step.moved} "
        f"re-addressed, {len(step.kept)} kept under the old name")
    for old in sorted(step.moves):
        lines = step.moves[old]
        if verbose:
            for line in lines:
                say(f"      {line}")
        else:
            owners = sorted({line.split(" -> ")[1].rpartition(".")[0]
                             for line in lines})
            say(f"      {old} -> {', '.join(owners)} "
                f"({len(lines)} field(s))")
    for line in step.kept:
        say(f"      KEPT {line}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert a data root's 3.x value stores to SCQO 4.0.0 "
                    "(docs/store-by-line-plan.md section 5).")
    parser.add_argument("data_root", type=Path)
    parser.add_argument("--apply", action="store_true",
                        help="write the conversion (default: dry run)")
    parser.add_argument("--reject-pending", action="store_true",
                        help="reject every pending suggestion first (they "
                             "carry 3.x names 4.0.0 cannot apply)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="list every re-addressed field and every "
                             "pending run")
    args = parser.parse_args(argv)
    if not args.data_root.is_dir():
        parser.error(f"{args.data_root} is not a directory")
    return convert(args.data_root, apply=args.apply,
                   reject=args.reject_pending, verbose=args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
