"""Physical-side component values — the measured-physics ledger of the SAMPLE.

The instrument store (:mod:`scqo.config`) answers "how do I drive this component
*on this setup*"; this store answers "what IS it" — the fields of each
component's PHYSICAL category (:mod:`scqo.categories`): transmon coherence and
spectrum, resonator linewidths, interaction-term couplings and flux transfer
functions. Every value is a measurement of the sample **through a setup, in a
cooldown** (a noisy drive line shortens the measured T2 through no fault of the
sample; ZControl volts are DAC-plane, wiring-dependent), so the store is PER
(cooldown, setup)::

    <data_root>/<device>/<cooldown>/<setup>/scqo/physical.json
        {"schema": 2, "values": {"q1": {"t1_s": 2.5e-05},
                                 "q1_res": {"f_r_hz": 5.9e9}}}
    <data_root>/<device>/<cooldown>/<setup>/scqo/physical.history.jsonl
        one ChangeRecord JSON object per line   # rows carry component/category/setup

DESIGN values (declared targets from the chip layout) are NOT here — they live
in the device-level roster (``components.toml``), one copy above all setups.
Files without the v2 schema stamp are pre-cutover: archived aside (``*.v1.bak``,
values and sidecar both) on first contact and never read — the fresh-start
policy, applied at BOTH the load and the save-merge site so old-home values can
never resurrect into a v2 file.

Values are written through the same suggest -> review -> accept flow as
calibration knobs (:mod:`scqo.suggestions`); nothing here ever touches a vendor
object. ``save()`` merges under a lock file — two same-context sessions cannot
erase each other's rows.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

from . import config as _config
from ._state_io import _file_lock, read_history, write_history
from .config import STATE_SCHEMA, ChangeRecord, _load_v2, _now

#: File name inside a context's ``.../<cooldown>/<setup>/scqo/`` folder (per context).
PHYSICAL_FILE = "physical.json"


def _clean_values(raw: dict) -> dict[str, dict[str, float]]:
    """Sanitize a file's ``values`` block to ``{component: {field: float}}`` —
    keep only numeric leaves."""
    return {
        name: {field: v for field, v in fields.items() if isinstance(v, (int, float))}
        for name, fields in raw.items() if isinstance(fields, dict)
    }


class PhysicalStore:
    """One (cooldown, setup) context's measured physics + change history.

    The ROSTER validates writes: a field must belong to the component's PHYSICAL
    category (``roster.resolve``). ``roster=None`` (bare unit tests) skips
    validation. Mirrors the RecordingDevice contract — finite-value guard,
    ChangeRecord provenance, ``None`` until first measured — minus everything
    vendor-related.
    """

    def __init__(self, path: str | Path | None = None, *, roster=None,
                 setup: str | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._roster = roster
        self._setup = setup or ""
        self._values: dict[str, dict[str, float]] = {}
        self._history: list[ChangeRecord] = []
        #: merge-on-save baseline: rows [:_saved] were loaded/merged from the file,
        #: rows [_saved:] are ours and not yet persisted.
        self._saved = 0
        #: (component, field) pairs we wrote since the last load/save — the only
        #: value keys `save()` may overwrite.
        self._dirty: set[tuple[str, str]] = set()
        if self._path is not None:
            data = _load_v2(self._path)  # archives pre-cutover files aside
            if data is not None:
                self._values = _clean_values(data.get("values", {}))
            self._history = [ChangeRecord(**r) for r in read_history(self._path)]
            self._saved = len(self._history)

    def _validate(self, component: str, field: str) -> str | None:
        """Roster check (returns the owning category); typos must not become truth."""
        if self._roster is None:
            return None
        side, _spec = self._roster.resolve(component, field)  # KeyError on unknown
        if side != "physical":
            raise ValueError(
                f"{component}.{field} is an INSTRUMENT field — it belongs in the "
                f"calibration store, not the physical ledger")
        phys, _instr = self._roster.category(component)
        return phys

    def get(self, component: str, field: str) -> float | None:
        """The current value (None until first measured in this context)."""
        return self._values.get(component, {}).get(field)

    def record(
        self,
        component: str,
        field: str,
        value: float,
        *,
        experiment: str | None = None,
        run_id: str | None = None,
    ) -> None:
        """Record a measured physical value: history + values, never any vendor."""
        category = self._validate(component, field)
        value = float(value)
        if not math.isfinite(value):  # the JSON must stay strictly parseable
            raise ValueError(f"refusing to record non-finite {field}={value!r} for {component}")
        self._history.append(
            ChangeRecord(
                timestamp=_now(), component=component, field=field,
                old=self.get(component, field), new=value,
                category=category,
                experiment=experiment, run_id=run_id,
                # via the module (not a name import) so tests/tools can monkeypatch
                # scqo.config._current_operator once for every stamping site
                operator=_config._current_operator() or None,
                setup=self._setup or None,
            )
        )
        self._values.setdefault(component, {})[field] = value
        self._dirty.add((component, field))

    def snapshot(self) -> dict:
        """This context's values, flat ``{component: {field: value}}`` (deep-copied)."""
        return {q: dict(fields) for q, fields in self._values.items()}

    def history(self) -> list[ChangeRecord]:
        """Every recorded change in this context (rows carry component/category/setup)."""
        return list(self._history)

    def save(self) -> None:
        """Merge-persist under a lock (no-op in-memory).

        Under the lock the on-disk state is re-read THROUGH THE V2 GATE (a
        pre-cutover file is archived aside, never merged — the resurrection
        bug); its history plus OUR unsaved rows are unioned (ordered by
        timestamp) and only the value keys WE wrote overwrite the file's.

        Two writes, history sidecar FIRST: a failed sidecar write commits
        nothing; once the sidecar lands the merge is committed to memory, so a
        failure of the values write cannot re-append the rows on retry.
        """
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(self._path):
            data = _load_v2(self._path)  # archive side effect under the lock
            file_values: dict = _clean_values(data.get("values", {})) if data else {}
            file_history = [ChangeRecord(**r) for r in read_history(self._path)]
            ours = self._history[self._saved:]
            merged = sorted(file_history + ours, key=lambda r: r.timestamp)  # stable
            write_history(self._path, [r.as_dict() for r in merged])
            self._history = merged
            self._saved = len(merged)
            # For each key WE wrote, the persisted value is the LATEST-timestamp
            # record for it across the merged history — a concurrent same-context
            # session's newer value wins and the persisted value always matches
            # its crediting record.
            latest: dict[tuple[str, str], float] = {}
            for r in merged:  # ascending timestamp -> last write per key wins
                latest[(r.component, r.field)] = r.new
            for component, field in self._dirty:
                file_values.setdefault(component, {})[field] = latest[(component, field)]
            payload = {"schema": STATE_SCHEMA, "values": file_values}
            tmp = self._path.with_suffix(f"{self._path.suffix}.{os.getpid()}.tmp")
            try:
                tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                os.replace(tmp, self._path)
            except OSError:
                tmp.unlink(missing_ok=True)  # no orphan temp; values self-heal next save
                raise
            self._values = file_values
            self._dirty.clear()
