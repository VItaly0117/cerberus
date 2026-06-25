"""
Sprint 7 — Cerberus runtime metrics.

Thread-safe counters and gauges that record bot health and decision flow.
Periodically flushed to JSONL (`artifacts/paper/metrics.log`) and to the
``runtime_health`` table so the Control Plane bot can show live status.

Usage:
    from cerberus_runtime import metrics

    metrics.incr("signals_emitted")
    metrics.incr("signals_rejected_by_reason", labels={"reason": "EDGE_BELOW_THRESHOLD"})
    metrics.set_gauge("last_signal_ts", int(time.time() * 1000))
    metrics.set_gauge("book_staleness_ms", 1234)

    # Periodically flush — runtime starts a background task that calls this:
    await metrics.flush_to_storage(storage)
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Internal state ────────────────────────────────────────────────────────────
_lock = threading.RLock()

# Plain counters: name → int
_counters: dict[str, int] = {}

# Labelled counters: name → {label_tuple → int}
_labelled: dict[str, dict[tuple[tuple[str, str], ...], int]] = {}

# Gauges (current value only): name → value
_gauges: dict[str, Any] = {}

# Recent reject reasons (deque-like) — keeps last N entries for /why
_recent_rejects: list[dict[str, Any]] = []
_RECENT_REJECTS_MAX = 50


# ── Public API ────────────────────────────────────────────────────────────────


def incr(name: str, by: int = 1, labels: Optional[dict[str, str]] = None) -> None:
    """Increment a counter (optionally labelled).

    If ``labels`` is provided, the counter is tracked per unique label set.
    """
    with _lock:
        if labels:
            label_key = tuple(sorted(labels.items()))
            bucket = _labelled.setdefault(name, {})
            bucket[label_key] = bucket.get(label_key, 0) + by
        else:
            _counters[name] = _counters.get(name, 0) + by


def set_gauge(name: str, value: Any) -> None:
    """Set the current value of a gauge."""
    with _lock:
        _gauges[name] = value


def record_reject(reason: str, market_id: str = "", detail: str = "") -> None:
    """Record one reject decision for /why command.

    Also increments ``signals_rejected_by_reason{reason}`` counter.
    """
    incr("signals_rejected_by_reason", labels={"reason": reason})
    with _lock:
        entry = {
            "ts_ms": int(time.time() * 1000),
            "reason": reason,
            "market_id": market_id,
            "detail": detail,
        }
        _recent_rejects.append(entry)
        # Keep only last N
        if len(_recent_rejects) > _RECENT_REJECTS_MAX:
            del _recent_rejects[: len(_recent_rejects) - _RECENT_REJECTS_MAX]


def snapshot() -> dict[str, Any]:
    """Return a deep copy of the current metric state — safe to dump as JSON."""
    with _lock:
        return {
            "ts_ms": int(time.time() * 1000),
            "counters": dict(_counters),
            "labelled": {
                name: [
                    {"labels": dict(label_key), "value": value}
                    for label_key, value in bucket.items()
                ]
                for name, bucket in _labelled.items()
            },
            "gauges": dict(_gauges),
            "recent_rejects": list(_recent_rejects[-10:]),
        }


def get_recent_rejects(limit: int = 10) -> list[dict[str, Any]]:
    """Return the most recent reject entries."""
    with _lock:
        return list(_recent_rejects[-limit:])


def reset() -> None:
    """Clear all metric state — primarily for tests."""
    with _lock:
        _counters.clear()
        _labelled.clear()
        _gauges.clear()
        _recent_rejects.clear()


# ── Persistence ───────────────────────────────────────────────────────────────


def write_jsonl(path: str | Path) -> None:
    """Append one JSON line with current snapshot to *path*."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    snap = snapshot()
    with p.open("a") as fp:
        fp.write(json.dumps(snap) + "\n")


async def flush_to_storage(storage: Any) -> None:
    """Write counters + gauges to ``runtime_health`` table.

    Each (name, value) pair becomes one row, replacing the previous value
    for that key. Storage must implement ``upsert_runtime_health(key, value)``.
    """
    snap = snapshot()
    rows: list[tuple[str, str]] = []

    for k, v in snap["counters"].items():
        rows.append((k, str(v)))

    for name, entries in snap["labelled"].items():
        for entry in entries:
            label_str = ",".join(f"{k}={v}" for k, v in entry["labels"].items())
            rows.append((f"{name}{{{label_str}}}", str(entry["value"])))

    for k, v in snap["gauges"].items():
        rows.append((k, str(v)))

    rows.append(("snapshot_ts_ms", str(snap["ts_ms"])))

    for key, value in rows:
        try:
            await storage.upsert_runtime_health(key, value)
        except AttributeError:
            logger.debug("Storage has no upsert_runtime_health — skipping flush.")
            return
        except Exception as exc:
            logger.warning("Failed to flush metric %s: %s", key, exc)
