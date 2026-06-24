#!/usr/bin/env python3
"""
resolvertest.py — Resolution Arbitrage CLI (Sprint 4)

Usage:
    python3 resolvertest.py --scan              # one-shot scan, print signals
    python3 resolvertest.py --paper             # run loop, save to DB
    python3 resolvertest.py --paper --hours 24  # run for 24 hours
    python3 resolvertest.py --summary           # print DB summary
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

_IMPORTS_OK = True
_IMPORT_ERROR = ""
try:
    from cerberus_runtime.config import Config, get_config
    from cerberus_runtime.resolver import ResolutionScanner
    from cerberus_runtime.storage import Storage
except Exception as exc:
    _IMPORTS_OK = False
    _IMPORT_ERROR = str(exc)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)-30s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("resolvertest")


# ─────────────────────────────────────────────────────────────────────────────
# One-shot scan
# ─────────────────────────────────────────────────────────────────────────────

async def _run_scan() -> int:
    """Run one scan cycle and print results to stdout."""
    config = get_config()
    storage = Storage(db_path=":memory:")
    await storage.connect()

    signal_queue: asyncio.Queue = asyncio.Queue()
    scanner = ResolutionScanner(config, storage, signal_queue)

    logger.info("Running one-shot resolution scan…")
    await scanner._scan()

    signals = []
    while not signal_queue.empty():
        signals.append(signal_queue.get_nowait())

    print("\n" + "=" * 60)
    print(f"  Resolution Scan — {len(signals)} signal(s) found")
    print("=" * 60)

    if not signals:
        print("  No resolution opportunities found this cycle.")
    else:
        for s in signals:
            print(
                f"  market={s.market_id}  outcome={s.outcome}  "
                f"ask={float(s.current_ask):.4f}  "
                f"edge={float(s.edge_net_pct)*100:.1f}%  "
                f"confidence={s.confidence}"
            )

    print("=" * 60 + "\n")
    await storage.close()
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Paper loop
# ─────────────────────────────────────────────────────────────────────────────

async def _run_paper(hours: float) -> int:
    """Run the resolution scanner in paper mode for *hours* hours."""
    db_path = os.getenv("DB_PATH", "cerberus.db")
    config = get_config()
    storage = Storage(db_path=db_path)
    await storage.connect()

    signal_queue: asyncio.Queue = asyncio.Queue()
    scanner = ResolutionScanner(config, storage, signal_queue)

    stop_event = asyncio.Event()
    deadline = time.monotonic() + hours * 3600

    logger.info("Starting resolution paper run — duration=%.1fh", hours)

    async def _consume() -> None:
        """Log signals as they arrive."""
        while not stop_event.is_set():
            try:
                signal = await asyncio.wait_for(signal_queue.get(), timeout=1.0)
                logger.info(
                    "SIGNAL market=%s outcome=%s ask=%.4f edge=%.2f%% pnl≈%.4f USDC",
                    signal.market_id,
                    signal.outcome,
                    float(signal.current_ask),
                    float(signal.edge_net_pct) * 100,
                    float(Decimal("1") - signal.current_ask - signal.fee_usdc),
                )
            except asyncio.TimeoutError:
                continue

    async def _timer() -> None:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)
        stop_event.set()

    scan_task = asyncio.create_task(scanner.run())
    consume_task = asyncio.create_task(_consume())
    timer_task = asyncio.create_task(_timer())

    try:
        await asyncio.gather(timer_task)
    except asyncio.CancelledError:
        pass
    finally:
        stop_event.set()
        scan_task.cancel()
        consume_task.cancel()
        await asyncio.gather(scan_task, consume_task, return_exceptions=True)

    # ── Final report ──────────────────────────────────────────────────────────
    summary = await storage.get_resolution_summary()
    await storage.close()

    report = {
        "duration_hours": hours,
        "total_signals": summary["total_signals"],
        "confirmed_signals": summary["confirmed_signals"],
        "total_simulated_pnl_usdc": float(summary["total_simulated_pnl"]),
        "median_edge_net_pct": float(summary["median_edge_net_pct"]),
    }

    out_dir = Path("artifacts/resolver")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    report_path = out_dir / f"report_{ts}.json"
    report_path.write_text(json.dumps(report, indent=2))

    print("\n" + "=" * 60)
    print("  CERBERUS RESOLUTION PAPER RUN — FINAL REPORT")
    print("=" * 60)
    for k, v in report.items():
        print(f"  {k}: {v}")
    print("=" * 60 + "\n")
    logger.info("Report saved to %s", report_path)

    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────

async def _run_summary() -> int:
    db_path = os.getenv("DB_PATH", "cerberus.db")
    storage = Storage(db_path=db_path)
    await storage.connect()
    summary = await storage.get_resolution_summary()
    await storage.close()

    print("\n" + "=" * 60)
    print("  Resolution Arbitrage DB Summary")
    print("=" * 60)
    print(f"  Total signals:      {summary['total_signals']}")
    print(f"  Confirmed:          {summary['confirmed_signals']}")
    print(f"  Total P&L (sim):    {float(summary['total_simulated_pnl']):.4f} USDC")
    print(f"  Median edge:        {float(summary['median_edge_net_pct'])*100:.2f}%")
    print("=" * 60 + "\n")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    if not _IMPORTS_OK:
        print(f"[FAIL] Import error: {_IMPORT_ERROR}", file=sys.stderr)
        return 1

    parser = argparse.ArgumentParser(description="Cerberus Resolution Arbitrage CLI")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--scan", action="store_true", help="One-shot scan")
    mode.add_argument("--paper", action="store_true", help="Paper run loop")
    mode.add_argument("--summary", action="store_true", help="Print DB summary")
    parser.add_argument("--hours", type=float, default=72.0, metavar="N",
                        help="Duration for --paper mode (default 72)")
    args = parser.parse_args()

    if args.scan:
        return asyncio.run(_run_scan())
    elif args.paper:
        return asyncio.run(_run_paper(args.hours))
    elif args.summary:
        return asyncio.run(_run_summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
