"""Index at scale + under concurrency: the two questions a shared index must answer.

1. Speed: 100k runs on one sample must not slow that sample's (or any neighbor's)
   queries — device-scoped pages walk the composite index and read only LIMIT rows.
2. Safety: two same-PC sessions (two students, two samples) writing simultaneously
   must never fail or lose rows — WAL + busy retry + short-lived connections.

Rows are inserted directly into the real schema (no run folders) so 100k rows cost
seconds, not hours; the query path exercised is exactly production ``find_runs``.
"""

from __future__ import annotations

import threading
import time

from scqo import DataStore

_COLS = (
    "run_id, started_at, ended_at, experiment, device, backend, targets, outcome, "
    "outcomes, fit, tags, note, error, parameters, updated_device, path, schema_version"
)
_SQL = f"INSERT INTO runs ({_COLS}) VALUES ({','.join('?' * 17)})"


def _row(device: str, i: int) -> tuple:
    # fixed-width fractional seconds => lexicographic order == insertion order
    ts = f"2026-01-01T00:00:00.{i:09d}"
    return (
        f"r{i:07d}", ts, ts, "resonator_spectroscopy", device, "qblox",
        '["q1"]', "successful", '{"q1": "successful"}',
        '{"q1": {"readout_freq": 6.0e9}}', '["cooldown1"]', "", None, "{}",
        1, f"{device}/2026-01-01/r{i:07d}", 8,
    )


def _bulk_insert(store: DataStore, device: str, n: int) -> None:
    with store._connect() as db:
        db.executemany(_SQL, (_row(device, i) for i in range(n)))


def test_scoped_queries_stay_fast_at_100k(tmp_path):
    store = DataStore(tmp_path)
    _bulk_insert(store, "bigchip", 100_000)
    _bulk_insert(store, "smallchip", 1_000)

    t = time.perf_counter()
    big = store.find_runs(device="bigchip", limit=50)
    dt_big = time.perf_counter() - t
    t = time.perf_counter()
    small = store.find_runs(device="smallchip", limit=50)
    dt_small = time.perf_counter() - t
    print(f"\nfind_runs @100k rows: bigchip {dt_big * 1e3:.1f} ms, smallchip {dt_small * 1e3:.1f} ms")

    assert [r["run_id"] for r in big[:2]] == ["r0099999", "r0099998"]  # newest first
    assert len(big) == 50 and len(small) == 50
    # generous CI bounds — locally these are single-digit milliseconds
    assert dt_big < 1.0 and dt_small < 1.0

    # not luck but the composite index: the query plan must walk it
    with store._connect() as db:
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM runs WHERE device = ? "
            "ORDER BY started_at DESC, run_id DESC LIMIT 50",
            ("bigchip",),
        ).fetchall()
    assert any("idx_runs_device_started" in r["detail"] for r in plan)


def test_two_sessions_write_simultaneously(tmp_path):
    """Two students, two samples, one PC: interleaved writes + a browsing reader."""
    errors: list[Exception] = []

    def writer(device: str) -> None:
        try:
            # own DataStore = own connections, the same file-lock path two processes take
            store = DataStore(tmp_path, device_name=device)
            for i in range(200):
                with store._connect() as db:  # one short-lived connection per run, as in production
                    db.execute(_SQL, _row(device, i))
        except Exception as err:  # pragma: no cover - the assertion below reports it
            errors.append(err)

    threads = [threading.Thread(target=writer, args=(dev,)) for dev in ("chipA", "chipB")]
    for t in threads:
        t.start()
    reader = DataStore(tmp_path)  # the viewer, browsing while both write
    for _ in range(20):
        reader.find_runs(limit=10)
    for t in threads:
        t.join()

    assert errors == []
    assert len(reader.find_runs(device="chipA", limit=1000)) == 200
    assert len(reader.find_runs(device="chipB", limit=1000)) == 200
