"""The two per-context value stores — schema 3, one shape, two role sets.

docs/greenfield-schema.md section 1: per (cooldown, setup) the device has
``physical.json`` (facts — measured sample physics) and ``scqo_state.json``
(knobs + monitors — the operating state), both machine-written JSON::

    {"schema": 3, "values": {entity: {field: float | [float, ...]}}}

plus the append-only ``*.history.jsonl`` provenance sidecars (the
:mod:`scqo._state_io` plumbing survives the cutover verbatim — lock file,
merge-on-save, torn-line tolerance).

ONE :class:`Store` class serves both files; the difference is the ROLE SET it
accepts (``fact`` vs ``knob``+``monitor``) — routing is
:meth:`scqo.model.roster.Roster.spec` plus a role-membership check, so a fact
written at the state store is refused with a pointer to physical.json and
vice versa. Write-time validation: finite numbers only, ``float[]`` shapes
checked, paired arrays equal-length (both directions, re-checked against the
MERGED values before a save commits), a ``*_waveform`` write requires its
``*_waveform_dt_s`` companion already set.

FRESH-START POLICY (doc section 10): files without the ``"schema": 3`` stamp
are pre-cutover — archived aside (``*.v2.bak``, values and sidecar both,
keep-oldest) on first contact and never read, at BOTH the load and the
save-merge site (the v1->v2 precedent). A sidecar whose rows are not v3
ChangeRecords (v2 leftovers after a values-only reset) is archived the same
way. An UNPARSEABLE values file is different: it is quarantined alone as
``*.corrupt.bak`` and the intact sidecar is kept — a torn write must never
destroy provenance. Knobs reseed from the vendor via ``state_sync="pull"``;
monitors are re-measured; facts re-accumulate.

Crash consistency in ``save()`` (ported from the proven store): everything
re-read and validated under the lock BEFORE any write; history sidecar
written FIRST (a failed sidecar write commits nothing); the merge then
commits to memory, so a failed values write cannot re-append rows on retry;
the values file lands by unique-temp + ``os.replace``. For a key several
sessions wrote, the LATEST-timestamp record wins — the persisted value
always matches its newest crediting row.

Vendor push is NOT here — the store is pure persistence; the recording
device wrapper (phase 5) pushes knobs in catalog declaration order.
"""

from __future__ import annotations

import getpass
import json
import math
import os
from dataclasses import asdict, dataclass, fields as _dc_fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .._state_io import _file_lock, history_path, read_history, write_history
from .catalog import FieldSpec
from .roster import Roster

STATE_SCHEMA = 3
PHYSICAL_FILE = "physical.json"
STATE_FILE = "scqo_state.json"


class StoreError(ValueError):
    """A store write that cannot be recorded correctly must fail loudly."""


def _now() -> str:
    """ISO-8601 timestamp in local time WITH the UTC offset (e.g. ``+08:00``).

    Local (not UTC) so the date prefix matches the datastore's run folders and
    what a human types into ``find_runs(since=...)``; the explicit offset keeps
    it machine-unambiguous. Lexicographic order == chronological order as long
    as the lab's UTC offset is fixed (no DST in the lab's timezone).
    """
    return datetime.now(timezone.utc).astimezone().isoformat()


def _current_operator() -> str:
    """OS login name of whoever runs this process ("" if undeterminable)."""
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover - no login name in exotic setups
        return ""


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
    #: OS login of whoever made the change (None only when undeterminable).
    operator: str | None = None
    #: Set when this change is the echo of writing another field (one vendor
    #: knob feeds several neutral fields): names the causing field.
    coupled_to: str | None = None
    #: NAMED setup the writing session was bound to.
    setup: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_ROW_KEYS = frozenset(f.name for f in _dc_fields(ChangeRecord))
_ROW_REQUIRED = frozenset({"timestamp", "entity", "field", "new"})


def _rehydrate(rows: list[dict]) -> list[ChangeRecord] | None:
    """Sidecar rows -> ChangeRecords, tolerantly: unknown keys are IGNORED
    (a future 3.x writer's rows must load), missing optionals default. None
    means the sidecar is foreign (pre-v3 component/category rows) — the
    fresh-start policy applies to it."""
    out: list[ChangeRecord] = []
    for r in rows:
        if not (isinstance(r, dict) and _ROW_REQUIRED <= set(r)):
            return None
        out.append(ChangeRecord(**{k: v for k, v in r.items()
                                   if k in _ROW_KEYS}))
    return out


# ------------------------------------------------------------ file plumbing

def _archive_pre_v3(path: Path, *, sidecar_only: bool = False) -> None:
    """Move a pre-cutover values file and/or its sidecar aside (``*.v2.bak``,
    keep-oldest — the v1 precedent). Race-tolerant: a concurrent first
    contact archiving the same files is fine, the loser just moves on."""
    targets = ((history_path(path),) if sidecar_only
               else (path, history_path(path)))
    for p in targets:
        try:
            if p.is_file():
                bak = p.with_name(p.name + ".v2.bak")
                if bak.exists():
                    p.unlink()
                else:
                    os.replace(p, bak)
        except OSError:  # the other session archived it first
            pass


def _load_v3(path: Path) -> dict | None:
    """The values file's JSON if (and only if) it carries the v3 stamp.

    A parseable file WITHOUT the stamp is pre-cutover: archived aside with
    its sidecar. An UNPARSEABLE file (torn write, disk fault) is NOT
    pre-cutover: it is quarantined alone as ``*.corrupt.bak`` and the intact
    sidecar survives — provenance is never collateral damage.
    """
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        try:
            os.replace(path, path.with_name(path.name + ".corrupt.bak"))
        except OSError:  # pragma: no cover - raced another quarantine
            pass
        return None
    if isinstance(data, dict) and data.get("schema") == STATE_SCHEMA:
        return data
    _archive_pre_v3(path)
    return None


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _finite_number(v: Any) -> bool:
    return _is_number(v) and math.isfinite(v)


def _clean_values(raw: Any, roster: Roster
                  ) -> dict[str, dict[str, float | list[float]]]:
    """Sanitize a file's ``values`` block — a hand-mangled file must not
    poison the store: only finite numbers and lists of finite numbers
    survive (json.loads accepts Infinity/NaN tokens; we do not), and a value
    whose shape contradicts the roster's spec for a KNOWN field is dropped
    (a scalar sitting in a float[] slot would crash the relation checks)."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, float | list[float]]] = {}
    for entity, fields in raw.items():
        if not isinstance(fields, dict):
            continue
        specs = roster.fields_of(entity) if entity in roster else {}
        kept: dict[str, float | list[float]] = {}
        for field, v in fields.items():
            spec = specs.get(field)
            if _finite_number(v):
                if spec is None or spec.shape == "float":
                    kept[field] = float(v)
            elif (isinstance(v, list) and v
                  and all(_finite_number(x) for x in v)):
                if spec is None or spec.shape == "float[]":
                    kept[field] = [float(x) for x in v]
        out[entity] = kept
    return out


# ------------------------------------------------------------------ stores

class Store:
    """One (cooldown, setup) context's values of one ROLE SET + history.

    Mirrors the proven store contract — finite-value guard, ChangeRecord
    provenance, ``None`` until first written, merge-on-save under the lock
    file so two same-context sessions cannot erase each other's rows.
    """

    def __init__(self, path: str | Path | None, roster: Roster, *,
                 roles: frozenset[str], setup: str = "") -> None:
        #: None = in-memory store (demo/notebook sessions): full validation
        #: and history, no persistence — save() is a no-op.
        self._path = Path(path) if path is not None else None
        self._roster = roster
        self._roles = roles
        self._setup = setup
        self._values: dict[str, dict[str, float | list[float]]] = {}
        #: merge-on-save baseline: rows [:_saved] were loaded/merged from the
        #: file, rows [_saved:] are ours and not yet persisted.
        self._saved = 0
        #: (entity, field) keys we wrote since load/save — the only value
        #: keys ``save()`` may overwrite.
        self._dirty: set[tuple[str, str]] = set()
        self._history: list[ChangeRecord] = []
        if self._path is None:
            return
        data = _load_v3(self._path)
        if data is not None:
            self._values = _clean_values(data.get("values"), roster)
        rows = _rehydrate(read_history(self._path))
        if rows is None:  # foreign sidecar (v2 leftover / values-only reset)
            _archive_pre_v3(self._path, sidecar_only=data is not None)
            rows = []
        self._history = rows
        self._saved = len(rows)

    # ------------------------------------------------------------- reading

    def get(self, entity: str, field: str) -> float | list[float] | None:
        """The current value (None until first written in this context)."""
        value = self._values.get(entity, {}).get(field)
        return list(value) if isinstance(value, list) else value

    def values(self) -> dict[str, dict[str, float | list[float]]]:
        """Deep-copied snapshot of the ``values`` block (run records)."""
        return {e: {f: (list(v) if isinstance(v, list) else v)
                    for f, v in fields.items()}
                for e, fields in self._values.items()}

    def history(self) -> tuple[ChangeRecord, ...]:
        return tuple(self._history)

    # ------------------------------------------------------------- writing

    def _name(self) -> str:
        if self._path is not None:
            return self._path.name
        return PHYSICAL_FILE if "fact" in self._roles else STATE_FILE

    def _coerce(self, entity: str, field: str, spec: FieldSpec,
                value) -> "float | list[float]":
        where = f"{entity}.{field}"
        if spec.shape == "float[]":
            if not (isinstance(value, list) and value
                    and all(_is_number(v) for v in value)):
                raise StoreError(f"{where}: expected a non-empty list of "
                                 f"numbers (shape float[])")
            value = [float(v) for v in value]
            if not all(math.isfinite(v) for v in value):
                raise StoreError(f"{where}: refusing non-finite values")
        else:
            if not _is_number(value):
                raise StoreError(f"{where}: expected a number, got {value!r}"
                                 + (" (this field is float[])" if isinstance(
                                     value, list) else ""))
            value = float(value)
            if not math.isfinite(value):
                raise StoreError(f"{where}: refusing non-finite {value!r}")
        return value

    def _check_relations(self, entity: str, field: str, spec: FieldSpec,
                         value, values: dict) -> None:
        """Per-write relation: a waveform needs its time base first (the one
        prerequisite a well-ordered batch can always satisfy). Paired-array
        LENGTH equality is deliberately NOT per-write — a redone fit changes
        both partners' length in one batch, so it is a BATCH-END invariant
        (the session validates it) and a SAVE invariant (:meth:`_check_merged`
        — a direct store user meets it there at the latest)."""
        if (spec.shape == "float[]" and field.endswith("_waveform")
                and values.get(entity, {}).get(f"{field}_dt_s") is None):
            raise StoreError(
                f"{entity}.{field}: set {field}_dt_s first — a waveform "
                f"without its sample period is not physically interpretable")

    def _check_merged(self, merged: dict) -> None:
        """Re-run the paired-length invariant on the MERGED values for every
        entity we touched, before anything is written: two sessions'
        individually-valid writes must not union into an invalid file."""
        for entity in {e for e, _ in self._dirty}:
            for field, spec in self._roster.fields_of(entity).items():
                if not spec.paired_with:
                    continue
                a = merged.get(entity, {}).get(field)
                b = merged.get(entity, {}).get(spec.paired_with)
                if a is not None and b is not None and len(a) != len(b):
                    raise StoreError(
                        f"{entity}.{field}/{spec.paired_with}: merged store "
                        f"would hold unequal paired lengths ({len(a)} != "
                        f"{len(b)}) — another session wrote a conflicting "
                        f"pair; re-record both sides together")

    def check(self, entity: str, field: str, value, *,
              relations: bool = True) -> "float | list[float]":
        """Validate one prospective write WITHOUT recording it — the
        recording device calls this BEFORE pushing to the vendor, so a
        store-invalid value (wrong shape, NaN list element, waveform without
        its dt, paired-length break) can never reach the instrument.

        ``relations=False`` skips the CURRENT-values relation checks (paired
        lengths, waveform-dt-first) — suggestion capture uses it, because a
        proposal's partner may itself only be proposed; relations are
        re-checked when the accept actually records."""
        spec = self._roster.spec(entity, field)
        if spec.role not in self._roles:
            other = (PHYSICAL_FILE if spec.role == "fact" else STATE_FILE)
            raise StoreError(
                f"{entity}.{field} is a {spec.role} — it belongs in {other}, "
                f"not {self._name()}")
        value = self._coerce(entity, field, spec, value)
        if relations:
            self._check_relations(entity, field, spec, value, self._values)
        return value

    def record(self, entity: str, field: str, value, *,
               experiment: str | None = None, run_id: str | None = None,
               coupled_to: str | None = None) -> None:
        """Record one value: validate, append history, update values —
        never any vendor (push is the recording device's job)."""
        value = self.check(entity, field, value)
        self._history.append(ChangeRecord(
            timestamp=_now(), entity=entity, field=field,
            old=self.get(entity, field),
            # the history row owns its OWN copy — mutating a row must never
            # reach the live store
            new=list(value) if isinstance(value, list) else value,
            kind=self._roster.entities[entity].kind,
            experiment=experiment, run_id=run_id,
            operator=_current_operator() or None,
            coupled_to=coupled_to, setup=self._setup or None))
        self._values.setdefault(entity, {})[field] = value
        self._dirty.add((entity, field))

    # ------------------------------------------------------------- saving

    def save(self) -> None:
        """Merge-persist under the lock (ported crash discipline).

        Under the lock the on-disk state is re-read THROUGH THE V3 GATE; its
        history plus OUR unsaved rows are unioned (ordered by timestamp) and
        only the value keys WE wrote change in the file — each set to the
        LATEST-timestamp record across the merged history, so a concurrent
        session's newer value wins and the persisted value always matches
        its crediting row. Everything is validated BEFORE any write; the
        history sidecar goes FIRST (a failed sidecar write commits nothing;
        once it lands the merge is committed to memory, so a failed values
        write cannot re-append rows on retry); the values file lands by
        unique-temp + atomic replace.
        """
        if self._path is None:  # in-memory store: nothing to persist
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(self._path):
            data = _load_v3(self._path)  # archive side effects under the lock
            file_values = (_clean_values(data.get("values"), self._roster)
                           if data else {})
            file_history = _rehydrate(read_history(self._path))
            if file_history is None:  # foreign sidecar met at the save site
                _archive_pre_v3(self._path, sidecar_only=data is not None)
                file_history = []
            ours = self._history[self._saved:]
            merged = sorted(file_history + ours,
                            key=lambda r: r.timestamp)  # stable
            latest: dict[tuple[str, str], Any] = {}
            for r in merged:  # ascending -> last write per key wins
                latest[(r.entity, r.field)] = r.new
            candidate = file_values
            for entity, field in self._dirty:
                v = latest[(entity, field)]
                candidate.setdefault(entity, {})[field] = (
                    list(v) if isinstance(v, list) else v)
            self._check_merged(candidate)  # fail fast: nothing written yet
            write_history(self._path, [r.as_dict() for r in merged])
            self._history = merged
            self._saved = len(merged)
            payload = json.dumps({"schema": STATE_SCHEMA,
                                  "values": candidate},
                                 indent=2, allow_nan=False)
            tmp = self._path.with_name(
                f"{self._path.name}.{os.getpid()}.tmp")
            try:
                tmp.write_text(payload + "\n", encoding="utf-8")
                os.replace(tmp, self._path)
            except OSError:
                tmp.unlink(missing_ok=True)  # values self-heal next save
                raise
            self._values = candidate
            self._dirty.clear()


def physical_store(scqo_dir: str | Path | None, roster: Roster, *,
                   setup: str = "") -> Store:
    """The measured-facts store of one context directory (None = in-memory)."""
    path = Path(scqo_dir) / PHYSICAL_FILE if scqo_dir is not None else None
    return Store(path, roster, roles=frozenset({"fact"}), setup=setup)


def state_store(scqo_dir: str | Path | None, roster: Roster, *,
                setup: str = "") -> Store:
    """The operating store of one context directory (None = in-memory)."""
    path = Path(scqo_dir) / STATE_FILE if scqo_dir is not None else None
    return Store(path, roster, roles=frozenset({"knob", "monitor"}),
                 setup=setup)
