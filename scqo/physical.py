"""Instrument-independent physical parameters of the SAMPLE — the measured-physics ledger.

The instrument config (:mod:`scqo.config`) answers "how do I drive this qubit *on this
setup*"; this store answers "what IS this qubit" — coherence times, transmon arch
parameters, dispersive-fit quantities. Every value is a measurement of the sample
**through a setup, in a cooldown** (a noisy drive line shortens the measured T2
through no fault of the sample; ``dv_phi0_v`` is volts at the DAC, wiring-dependent),
so the file is PER (cooldown, setup) — one context, flat values::

    <data_root>/<device>/<cooldown>/<setup>/scqo/physical.json
        {"values":  {"q0": {"t1_s": 2.5e-05}},
         "history": [ChangeRecord, ...]}          # rows also carry setup=

A setup's estimate never overwrites another's — they are different files. Compare
across setups/cooldowns by querying the run index (every run stamps cooldown + setup)
or the trends page. The setup-INDEPENDENT sample truth is Phase-3 *inference* over
these measurements (``sample.json``, a separate future per-sample output).

Values are written through the same suggest -> review -> accept flow as calibration
knobs (see :mod:`scqo.suggestions`): an experiment's ``update()`` *proposes*, a human
(or an explicitly auto-applying caller) accepts, and every accepted value lands here
with full provenance (experiment / run_id / operator / setup ChangeRecords, exactly
like the instrument-config history). Nothing in this module ever touches a vendor
object. ``save()`` merges under a lock file rather than blindly rewriting — two
same-context sessions cannot erase each other's rows.

Instrument-DEPENDENT measured values (readout_fidelity, thermal population) do NOT
belong here — they stay in the instrument state / run records with ``backend``
provenance, compared across instruments by query.
"""

from __future__ import annotations

import json
import math
import os
import time
from contextlib import contextmanager
from pathlib import Path

from . import config as _config
from .config import FIELDS, ChangeRecord, FieldSpec, _now

#: File name inside a context's ``.../<cooldown>/<setup>/scqo/`` folder (per context).
PHYSICAL_FILE = "physical.json"

#: Lock acquisition gives up after this many seconds (another writer is stuck).
_LOCK_TIMEOUT_S = 10.0
#: A lock file older than this is a crashed writer's leftover and is taken over.
_LOCK_STALE_S = 10.0

#: The physical (sample-owned, instrument-independent) fields SCQO tracks.
#: Adding one requires an entry here and nothing else — no ABC, no driver code.
#: ``push`` is always False: there is no vendor knob for sample physics.
PHYSICAL_FIELDS: dict[str, FieldSpec] = {
    "t1_s": FieldSpec("s", "Energy-relaxation time T1.", push=False),
    "t2_star_s": FieldSpec("s", "Ramsey dephasing time T2*.", push=False),
    "t2_echo_s": FieldSpec("s", "Hahn-echo coherence time T2_echo.", push=False),
    "sweet_spot_flux_v": FieldSpec("V", "Flux bias of the qubit's sweet spot on its own flux line.", push=False),
    "dv_phi0_v": FieldSpec("V", "Flux period: volts per flux quantum on the qubit's flux line.", push=False),
    "ej_sum_ghz": FieldSpec("GHz", "Total Josephson energy EJ1+EJ2 (transmon arch fit).", push=False),
    "f_q_max_hz": FieldSpec("Hz", "Qubit 0-1 frequency at the sweet spot (arch top).", push=False),
    "f_r0_hz": FieldSpec("Hz", "Bare resonator frequency (dispersive fit).", push=False),
    "g_hz": FieldSpec("Hz", "Qubit-resonator coupling (dispersive fit).", push=False),
}

# A field name must resolve to exactly ONE store (suggestion routing + the shared
# QubitView surface in scqo.suggestions depend on it).
_overlap = set(PHYSICAL_FIELDS) & set(FIELDS)
assert not _overlap, f"PHYSICAL_FIELDS must not overlap config.FIELDS: {sorted(_overlap)}"
del _overlap


@contextmanager
def _file_lock(target: Path):
    """`O_CREAT|O_EXCL` lock file next to ``target`` — cross-platform, no deps.

    Retries for :data:`_LOCK_TIMEOUT_S`; a lock older than :data:`_LOCK_STALE_S`
    is a crashed writer's leftover. Physical accepts are rare and saves are
    milliseconds, so contention is the exception, not the rule — but the file is
    shared by every setup's user, so two subtle races are closed:

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
                    f"scqo process is saving physical values, delete the stale {lock}")
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            if lock.read_bytes() == token:  # still OURS — never free a takeover's lock
                lock.unlink(missing_ok=True)
        except OSError:
            pass


def _clean_values(raw: dict) -> dict[str, dict[str, float]]:
    """Sanitize a file's ``values`` block to ``{qubit: {field: float}}`` — keep only
    numeric leaves (a stray dict from an older nested layout is dropped; fresh start,
    no migration — history rows are never rewritten)."""
    return {
        qubit: {field: v for field, v in fields.items() if isinstance(v, (int, float))}
        for qubit, fields in raw.items() if isinstance(fields, dict)
    }


class PhysicalStore:
    """One (cooldown, setup) context's measured physics + change history.

    The store's file (``<device>/<cooldown>/<setup>/scqo/physical.json``, or
    ``path=None`` for in-memory) holds a single context, so values are flat
    ``{qubit: {field: value}}``. Mirrors the RecordingDevice contract — finite-value
    guard, ChangeRecord provenance, ``None`` until first measured — minus everything
    vendor-related. ``setup`` is stamped onto each ChangeRecord (self-describing rows
    for a future ``sample.json`` roll-up); the context is otherwise implied by the
    file's path.
    """

    def __init__(self, path: str | Path | None = None, *, setup: str | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._setup = setup or ""
        self._values: dict[str, dict[str, float]] = {}
        self._history: list[ChangeRecord] = []
        #: merge-on-save baseline: rows [:_saved] were loaded/merged from the file,
        #: rows [_saved:] are ours and not yet persisted.
        self._saved = 0
        #: (qubit, field) pairs we wrote since the last load/save — the only value
        #: keys `save()` may overwrite (a co-running same-context writer keeps its own).
        self._dirty: set[tuple[str, str]] = set()
        if self._path is not None and self._path.is_file():
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._values = _clean_values(data.get("values", {}))
            self._history = [ChangeRecord(**r) for r in data.get("history", [])]
            self._saved = len(self._history)

    def get(self, qubit: str, field: str) -> float | None:
        """The current value (None until first measured in this context)."""
        return self._values.get(qubit, {}).get(field)

    def record(
        self,
        qubit: str,
        field: str,
        value: float,
        *,
        experiment: str | None = None,
        run_id: str | None = None,
    ) -> None:
        """Record a measured physical value: history + values, never any vendor."""
        value = float(value)
        if not math.isfinite(value):  # the JSON must stay strictly parseable
            raise ValueError(f"refusing to record non-finite {field}={value!r} for {qubit}")
        self._history.append(
            ChangeRecord(
                timestamp=_now(), qubit=qubit, field=field,
                old=self.get(qubit, field), new=value,
                experiment=experiment, run_id=run_id,
                # via the module (not a name import) so tests/tools can monkeypatch
                # scqo.config._current_operator once for every stamping site
                operator=_config._current_operator() or None,
                setup=self._setup or None,
            )
        )
        self._values.setdefault(qubit, {})[field] = value
        self._dirty.add((qubit, field))

    def snapshot(self) -> dict:
        """This context's values, flat ``{qubit: {field: value}}`` (deep-copied) —
        the shape staleness guards and ``Session.physical_state()`` reason about."""
        return {q: dict(fields) for q, fields in self._values.items()}

    def history(self) -> list[ChangeRecord]:
        """Every recorded change in this context (rows also carry ``setup=``)."""
        return list(self._history)

    def save(self) -> None:
        """Merge-persist under a lock (no-op in-memory).

        The file is one (cooldown, setup) context, but two same-context sessions
        (two terminals) could still race, so a blind rewrite could erase rows the
        other appended. Under the lock the file is re-read; its history plus OUR
        unsaved rows are unioned (ordered by timestamp — ISO strings with the lab's
        fixed UTC offset sort chronologically, the :func:`scqo.config._now`
        guarantee) and only the value keys WE wrote overwrite the file's. In-memory
        state becomes the merged truth ONLY after the write lands, so a failed
        replace (locked file, full disk) leaves our unsaved rows intact for the next
        save() to retry — never a silently dropped accept.
        """
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(self._path):
            file_values: dict = {}
            file_history: list[ChangeRecord] = []
            if self._path.is_file():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                file_values = _clean_values(data.get("values", {}))
                file_history = [ChangeRecord(**r) for r in data.get("history", [])]
            ours = self._history[self._saved:]
            merged = sorted(file_history + ours, key=lambda r: r.timestamp)  # stable
            # For each key WE wrote, the persisted value is the LATEST-timestamp
            # record for it across the merged history — not blindly our own. So if a
            # concurrent same-context session recorded a newer value for the same
            # (qubit, field), that newer value wins and the persisted value always
            # matches its crediting record (no "external"/older-save-wins). Every
            # dirty key has a record in `ours`, so it is present in `merged`.
            latest: dict[tuple[str, str], float] = {}
            for r in merged:  # ascending timestamp -> last write per key wins
                latest[(r.qubit, r.field)] = r.new
            for qubit, field in self._dirty:
                file_values.setdefault(qubit, {})[field] = latest[(qubit, field)]
            # Persist FIRST — commit the merge to memory only once it is durable.
            payload = {"values": file_values, "history": [r.as_dict() for r in merged]}
            tmp = self._path.with_suffix(f"{self._path.suffix}.{os.getpid()}.tmp")
            try:
                tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                os.replace(tmp, self._path)
            except OSError:
                tmp.unlink(missing_ok=True)  # no orphan temp; our rows stay pending
                raise
            self._values = file_values
            self._history = merged
            self._saved = len(merged)
            self._dirty.clear()
