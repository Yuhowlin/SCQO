"""The production cut — ``components.lock`` freeze and drift check.

docs/greenfield-schema.md section 7: at the production cut the device's
EXPANDED name set is frozen. Afterwards every load must produce a SUPERSET
by signature — (entity class, name, kind, target(s) for channels) — so
post-cut evolution is always an append:

* appending a rider to a frozen line MINTS a new name (legal);
* declaring a new mode/composite/line/channel (legal);
* declaring a new operation on a frozen composite (legal — operations are
  not part of the signature);
* moving a rider to another line, re-mediating a readout (legal — line and
  via are wiring, not identity; the doctor's vendor witness covers them);
* REMOVING a name, or changing its kind or targets (REFUSED — store keys,
  history rows, and trends key on those).

Retirement is ``retired = true`` in the roster, never deletion: the name
keeps resolving, so its stored values and history stay readable.

The lock file lives beside components.toml and is written once by
``scqo device freeze``; it is data, not code — a hand-edit is a policy
decision the lab records in git.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .roster import Roster

LOCK_FILE = "components.lock"
LOCK_SCHEMA = 1


class LockError(ValueError):
    """A lock file that cannot be read correctly must fail loudly."""


@dataclass(frozen=True)
class Drift:
    """One post-cut violation of the append-only rule."""

    name: str
    problem: str  # "missing" | "changed"
    detail: str

    def __str__(self) -> str:
        return f"{self.name}: {self.detail}"


def _canonical(signature) -> list:
    """Signatures are tuples of str/tuple; JSON round-trips them as lists."""
    return [list(part) if isinstance(part, (list, tuple)) else part
            for part in signature]


def freeze(roster: Roster, device_dir: str | Path, *,
           note: str = "") -> Path:
    """Write ``components.lock`` for the device's CURRENT expanded roster.

    Idempotent in content but never silently re-cut: freezing an already
    frozen device raises, because a second cut would bless whatever drifted
    since the first (use git to inspect, and a deliberate file removal to
    re-cut)."""
    path = Path(device_dir) / LOCK_FILE
    if path.exists():
        raise LockError(
            f"{path} already exists — the production cut happens ONCE; "
            f"post-cut evolution is append-only (remove the file "
            f"deliberately, in git, to re-cut)")
    payload = {
        "schema": LOCK_SCHEMA,
        "frozen_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "note": note,
        "entities": {name: _canonical(sig)
                     for name, sig in sorted(roster.signatures().items())},
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def load(device_dir: str | Path) -> dict[str, list] | None:
    """The frozen name -> signature map, or None when the device is still in
    the trial phase (no lock file)."""
    path = Path(device_dir) / LOCK_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise LockError(f"{path}: {err}") from None
    if not isinstance(data, dict) or data.get("schema") != LOCK_SCHEMA:
        raise LockError(
            f"{path}: schema = {LOCK_SCHEMA} required (found "
            f"{data.get('schema') if isinstance(data, dict) else '?'})")
    entities = data.get("entities")
    if not isinstance(entities, dict):
        raise LockError(f"{path}: 'entities' must be a table")
    return entities


def verify(roster: Roster, device_dir: str | Path) -> list[Drift]:
    """Check the roster against the lock: [] when the device is unfrozen or
    the current expansion is a superset by signature."""
    frozen = load(device_dir)
    if frozen is None:
        return []
    current = {name: _canonical(sig)
               for name, sig in roster.signatures().items()}
    drift: list[Drift] = []
    for name, sig in sorted(frozen.items()):
        now = current.get(name)
        if now is None:
            drift.append(Drift(
                name, "missing",
                "frozen name is gone from the expanded roster — its stored "
                "values and history would stop resolving; restore it (mark "
                "it retired = true instead of deleting)"))
        elif now != sig:
            drift.append(Drift(
                name, "changed",
                f"frozen identity {sig} != current {now} — kind and "
                f"target(s) are the lock's identity and may never change"))
    return drift


def additions(roster: Roster, device_dir: str | Path) -> list[str]:
    """Names present now but not in the lock — the legal appends, listed so
    the doctor can show what grew since the cut."""
    frozen = load(device_dir)
    if frozen is None:
        return []
    return sorted(set(roster.signatures()) - set(frozen))
