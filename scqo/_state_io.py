"""The values-file lock shared by the two per-context state stores.

Both stores — ``scqo_state.json`` (knobs + monitors) and ``physical.json``
(facts) — keep their CURRENT values in a small human-readable JSON; change
provenance lives in the context's ``history.sqlite`` (:mod:`scqo.changes`).
The lock file here (``<values file>.lock``) guards a whole save — the
values read-merge-write plus the history transaction it wraps — so two
same-context sessions cannot erase each other's rows. Lock order is fixed:
this file lock strictly OUTSIDE, the database transaction strictly INSIDE.

:func:`history_path` names the RETIRED ``*.history.jsonl`` sidecar of the
pre-database era — kept only so the v2 fresh-start gate
(:func:`scqo.stores._archive_pre_v3`) can archive a legacy sidecar
alongside its values file. Nothing reads sidecars anymore.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

#: Lock acquisition gives up after this many seconds (another writer is stuck).
_LOCK_TIMEOUT_S = 10.0
#: A lock file older than this is a crashed writer's leftover and is taken over.
_LOCK_STALE_S = 10.0


@contextmanager
def _file_lock(target: Path):
    """`O_CREAT|O_EXCL` lock file next to ``target`` — cross-platform, no deps.

    Retries for :data:`_LOCK_TIMEOUT_S`; a lock older than :data:`_LOCK_STALE_S`
    is a crashed writer's leftover. State saves are rare and take milliseconds,
    so contention is the exception, not the rule — but the file is shared by
    every same-context session, so two subtle races are closed:

    * **Stale takeover is atomic.** Two waiters must not both "break" one stale
      lock and then both enter the section. The stale lock is claimed by
      ``os.replace``-renaming it to a per-waiter unique name: exactly one waiter's
      rename succeeds (the OS guarantees it), the losers' raise and simply retry.
    * **A lock is only ever released by its owner.** Each acquisition writes a
      unique token into the lock file; release unlinks ONLY if the token still
      matches. So if our lock were ever deemed stale and taken over while we
      paused, we do not delete the new holder's lock out from under it.
    """
    lock = target.with_name(target.name + ".lock")
    token = f"{os.getpid()}.{os.urandom(6).hex()}".encode()
    deadline = time.monotonic() + _LOCK_TIMEOUT_S
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, token)
            finally:
                os.close(fd)
            break
        except FileExistsError:
            try:
                stale = time.time() - lock.stat().st_mtime > _LOCK_STALE_S
            except OSError:
                stale = False  # raced with the holder's release — just retry
            if stale:
                # Atomic claim: only ONE waiter's rename of the stale lock can
                # succeed; the winner removes it and retries the O_EXCL create,
                # the losers' os.replace raises (already gone) and they retry too.
                claim = lock.with_name(f"{lock.name}.stale.{os.getpid()}.{os.urandom(4).hex()}")
                try:
                    os.replace(lock, claim)
                    claim.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"could not lock {target} within {_LOCK_TIMEOUT_S:.0f}s — if no other "
                    f"scqo process is saving state, delete the stale {lock}")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            if lock.read_bytes() == token:  # still OURS — never free a takeover's lock
                lock.unlink(missing_ok=True)
        except OSError:
            pass


def history_path(values_path: str | Path) -> Path:
    """The RETIRED history sidecar name of a values file
    (``scqo_state.json`` -> ``scqo_state.history.jsonl``) — used only by the
    v2 fresh-start gate to archive a legacy sidecar with its values file."""
    return Path(values_path).with_suffix(".history.jsonl")
