"""Tests for cerberus_runtime/metrics.py — Sprint 7."""
from __future__ import annotations

import json
import threading

import pytest

from cerberus_runtime import metrics


@pytest.fixture(autouse=True)
def _clean_state():
    metrics.reset()
    yield
    metrics.reset()


# ── Counters ──────────────────────────────────────────────────────────────────

def test_incr_creates_and_increments_counter():
    metrics.incr("signals_emitted")
    metrics.incr("signals_emitted")
    metrics.incr("signals_emitted", by=3)
    snap = metrics.snapshot()
    assert snap["counters"]["signals_emitted"] == 5


def test_incr_with_labels_separates_buckets():
    metrics.incr("signals_rejected_by_reason", labels={"reason": "EDGE_BELOW_THRESHOLD"})
    metrics.incr("signals_rejected_by_reason", labels={"reason": "EDGE_BELOW_THRESHOLD"})
    metrics.incr("signals_rejected_by_reason", labels={"reason": "BOOK_STALE"})
    snap = metrics.snapshot()
    entries = snap["labelled"]["signals_rejected_by_reason"]
    by_reason = {tuple(sorted(e["labels"].items())): e["value"] for e in entries}
    assert by_reason[(("reason", "EDGE_BELOW_THRESHOLD"),)] == 2
    assert by_reason[(("reason", "BOOK_STALE"),)] == 1


# ── Gauges ────────────────────────────────────────────────────────────────────

def test_set_gauge_overwrites_value():
    metrics.set_gauge("book_staleness_ms", 100)
    metrics.set_gauge("book_staleness_ms", 250)
    snap = metrics.snapshot()
    assert snap["gauges"]["book_staleness_ms"] == 250


# ── Recent rejects (for /why) ─────────────────────────────────────────────────

def test_record_reject_appends_and_increments_counter():
    metrics.record_reject("EDGE_BELOW_THRESHOLD", market_id="m1")
    metrics.record_reject("BOOK_STALE", market_id="m2", detail="ts=0")
    rejects = metrics.get_recent_rejects(limit=10)
    assert len(rejects) == 2
    assert rejects[0]["reason"] == "EDGE_BELOW_THRESHOLD"
    assert rejects[0]["market_id"] == "m1"
    assert rejects[1]["detail"] == "ts=0"

    # The reason counter should reflect both
    snap = metrics.snapshot()
    entries = snap["labelled"]["signals_rejected_by_reason"]
    assert len(entries) == 2


def test_record_reject_keeps_only_last_n():
    for i in range(60):
        metrics.record_reject(f"REASON_{i % 3}", market_id=f"m{i}")
    rejects = metrics.get_recent_rejects(limit=100)
    # _RECENT_REJECTS_MAX = 50
    assert len(rejects) == 50


# ── Thread safety ─────────────────────────────────────────────────────────────

def test_counter_atomicity_under_concurrent_increments():
    """Increments from N threads must sum exactly to N*M (no lost updates)."""
    threads_count = 10
    per_thread = 1000

    def worker():
        for _ in range(per_thread):
            metrics.incr("hits")

    threads = [threading.Thread(target=worker) for _ in range(threads_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snap = metrics.snapshot()
    assert snap["counters"]["hits"] == threads_count * per_thread


# ── JSONL persistence ─────────────────────────────────────────────────────────

def test_write_jsonl_appends_valid_lines(tmp_path):
    metrics.incr("signals_emitted")
    metrics.set_gauge("book_staleness_ms", 123)
    metrics.record_reject("EDGE_BELOW_THRESHOLD")

    path = tmp_path / "metrics.log"
    metrics.write_jsonl(path)
    metrics.write_jsonl(path)

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    for line in lines:
        obj = json.loads(line)
        assert "ts_ms" in obj
        assert "counters" in obj
        assert "gauges" in obj


# ── Storage flush ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_flush_to_storage_writes_all_keys(tmp_path):
    from cerberus_runtime.storage import Storage

    storage = Storage(db_path=str(tmp_path / "test.db"))
    await storage.connect()

    metrics.incr("signals_emitted", by=5)
    metrics.set_gauge("book_staleness_ms", 200)
    metrics.record_reject("EDGE_BELOW_THRESHOLD")

    await metrics.flush_to_storage(storage)

    health = await storage.get_runtime_health()
    assert health["signals_emitted"] == "5"
    assert health["book_staleness_ms"] == "200"
    assert "snapshot_ts_ms" in health
    # Labelled metric should appear with label suffix
    label_keys = [k for k in health if k.startswith("signals_rejected_by_reason")]
    assert label_keys, "Labelled metrics should appear in runtime_health"

    await storage.close()
