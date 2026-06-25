#!/usr/bin/env python3
"""
cerberustest.py — Cerberus paper-trading CLI.

Usage
-----
  python3 cerberustest.py --preflight
      Run preflight checks, print summary.  Exit 0 on pass, 1 on fail.

  python3 cerberustest.py --paper [--duration-hours N] [--max-signals N]
      Run paper-trading loop for N hours (default 72) or max-signals snapshots.
      DRY_RUN_MODE is always True in this mode.
      Saves report to artifacts/paper/report_{timestamp}.json on exit.
      Exit 0 if median_edge_net_pct > 0, else exit 2.

  python3 cerberustest.py
      Print usage.

Safety invariants
-----------------
- allow_live_mode must be False in --paper mode. If True in env, abort immediately.
- DRY_RUN_MODE is forced True regardless of env.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cerberustest")

# ---------------------------------------------------------------------------
# Runtime imports (deferred so --preflight can report import failures)
# ---------------------------------------------------------------------------

try:
    from cerberus_runtime.config import AppConfig, Config, get_app_config, get_config
    from cerberus_runtime.storage import Storage
    from cerberus_runtime.fee_model import FeeModel
    from cerberus_runtime import core as _core
    from cerberus_runtime.market_discovery import MarketDiscovery
    from cerberus_runtime.watcher import Watcher
    from cerberus_runtime.risk import RiskManager, ArbitrageResult as RiskResult
    from cerberus_runtime.executor import Executor, ArbitrageResult as ExecResult
    from cerberus_runtime import metrics as _metrics
    _IMPORTS_OK = True
    _IMPORT_ERROR: Optional[str] = None
except Exception as exc:  # pragma: no cover
    _IMPORTS_OK = False
    _IMPORT_ERROR = str(exc)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ARTIFACTS_DIR = Path("artifacts/paper")
LIVE_STATS_INTERVAL_S = 15 * 60  # 15 minutes


# ===========================================================================
# Preflight
# ===========================================================================


def run_preflight() -> int:
    """Run configuration and import sanity checks.

    Returns:
        0 on pass, 1 on any failure.
    """
    print("=" * 60)
    print("  Cerberus preflight checks")
    print("=" * 60)

    failures: list[str] = []

    # 1. Import check
    if not _IMPORTS_OK:
        failures.append(f"Import error: {_IMPORT_ERROR}")
        print(f"  [FAIL] Imports: {_IMPORT_ERROR}")
    else:
        print("  [PASS] All modules imported OK")

    if not _IMPORTS_OK:
        _print_preflight_result(failures)
        return 1

    # 2. AppConfig construction
    try:
        cfg = get_app_config()
        print(f"  [PASS] AppConfig loaded — notional={cfg.trade_notional_usdc} USDC")
    except Exception as exc:
        failures.append(f"AppConfig: {exc}")
        print(f"  [FAIL] AppConfig: {exc}")

    # 3. DRY_RUN_MODE check
    allow_live = os.getenv("ALLOW_LIVE_MODE", "false").lower() == "true"
    if allow_live:
        msg = "ALLOW_LIVE_MODE=true in environment — disable for paper run"
        failures.append(msg)
        print(f"  [WARN] {msg}")
    else:
        print("  [PASS] allow_live_mode=False (paper mode safe)")

    # 4. Storage connectivity
    try:
        import aiosqlite  # noqa: F401
        print("  [PASS] aiosqlite available")
    except ImportError:
        failures.append("aiosqlite not installed")
        print("  [FAIL] aiosqlite not installed")

    # 5. WebSocket library
    try:
        import websockets  # noqa: F401
        print("  [PASS] websockets available")
    except ImportError:
        failures.append("websockets not installed")
        print("  [FAIL] websockets not installed")

    # 6. httpx
    try:
        import httpx  # noqa: F401
        print("  [PASS] httpx available")
    except ImportError:
        failures.append("httpx not installed")
        print("  [FAIL] httpx not installed")

    # 7. Env var summary (informational — missing vars use defaults)
    env_vars = [
        "GAMMA_HOST", "CLOB_REST_URL", "WS_URL",
        "TRADE_NOTIONAL_USDC", "MIN_NET_EDGE_PCT",
        "DB_PATH", "ALLOW_LIVE_MODE",
    ]
    missing = [v for v in env_vars if not os.getenv(v)]
    if missing:
        print(f"  [INFO] Missing env vars (defaults used): {', '.join(missing)}")
    else:
        print(f"  [PASS] All {len(env_vars)} env vars set")

    _print_preflight_result(failures)
    return 1 if failures else 0


def _print_preflight_result(failures: list[str]) -> None:
    print("-" * 60)
    if failures:
        print(f"  RESULT: {len(failures)} failure(s) — NOT ready")
        for f in failures:
            print(f"    ✗ {f}")
    else:
        print("  RESULT: All checks passed — ready for --paper run")
    print("=" * 60)


# ===========================================================================
# Paper trading loop
# ===========================================================================


async def _core_loop(
    opportunity_queue: asyncio.Queue,
    storage: Storage,
    app_config: AppConfig,
    fee_model: FeeModel,
    risk_manager: RiskManager,
    executor: Executor,
    stop_event: asyncio.Event,
    max_signals: Optional[int],
    counters: dict,
) -> None:
    """Consume snapshots from the opportunity queue and evaluate each one."""
    evaluated = 0

    while not stop_event.is_set():
        try:
            snapshot = await asyncio.wait_for(
                opportunity_queue.get(), timeout=1.0
            )
        except asyncio.TimeoutError:
            continue

        evaluated += 1
        counters["evaluated"] = evaluated
        _metrics.incr("snapshots_evaluated")
        market_id = snapshot.market_id
        condition_id = snapshot.condition_id or market_id
        ts_ms = snapshot.ts_ms or int(time.time() * 1000)
        _metrics.set_gauge("last_snapshot_ts_ms", ts_ms)
        _metrics.set_gauge(
            "book_staleness_ms", max(0, int(time.time() * 1000) - ts_ms)
        )

        # ── Risk gate ────────────────────────────────────────────────────
        allowed, reason = risk_manager.allows(snapshot)
        if not allowed:
            logger.info(
                "BLOCKED [%s] reason=%s", market_id, reason
            )
            _metrics.record_reject(reason or "BLOCKED_BY_RISK", market_id=market_id)
            await storage.insert_paper_signal_from_snapshot(
                market_id=market_id,
                condition_id=condition_id,
                ts_ms=ts_ms,
                signal=None,
                result="BLOCKED_BY_RISK",
                rejection_reason=reason,
                simulated_pnl=Decimal("0"),
            )
            counters["blocked"] += 1
            _check_max_signals(evaluated, max_signals, stop_event)
            continue

        # ── Opportunity evaluation ────────────────────────────────────────
        signal = _core.evaluate_opportunity(snapshot, app_config, fee_model)
        if signal is None:
            logger.debug("FILTERED [%s] edge below threshold", market_id)
            _metrics.record_reject("EDGE_BELOW_THRESHOLD", market_id=market_id)
            await storage.insert_paper_signal_from_snapshot(
                market_id=market_id,
                condition_id=condition_id,
                ts_ms=ts_ms,
                signal=None,
                result="FILTERED",
                rejection_reason="edge_below_threshold",
                simulated_pnl=Decimal("0"),
            )
            _check_max_signals(evaluated, max_signals, stop_event)
            continue

        # ── Execute (dry-run) ─────────────────────────────────────────────
        _metrics.incr("signals_emitted")
        _metrics.set_gauge("last_signal_ts_ms", ts_ms)
        exec_result = await executor.execute_pair(signal, snapshot)

        if exec_result == ExecResult.SUCCESS:
            simulated_pnl = signal.edge_net
            counters["successes"] += 1
            _metrics.incr("executions_filled")
        elif exec_result in (ExecResult.LEGGED_RISK, ExecResult.REPAIRED):
            simulated_pnl = Decimal("-0.10")
            counters["legged"] += 1
            _metrics.incr("executions_legged")
        else:
            simulated_pnl = Decimal("0")
            counters["clean_misses"] += 1
            _metrics.incr("executions_clean_miss")

        logger.info(
            "SIGNAL [%s] result=%s edge_net=%s pnl=%s",
            market_id,
            exec_result.name,
            signal.edge_net,
            simulated_pnl,
        )

        await storage.insert_paper_signal_from_snapshot(
            market_id=market_id,
            condition_id=condition_id,
            ts_ms=ts_ms,
            signal=signal,
            result=exec_result.name,
            simulated_pnl=simulated_pnl,
        )

        # Inform risk manager of the outcome
        try:
            risk_result = RiskResult[exec_result.name]
        except KeyError:
            risk_result = RiskResult.CLEAN_MISS
        loss = abs(simulated_pnl) if exec_result == ExecResult.LEGGED_RISK else Decimal("0")
        await risk_manager.record_result(condition_id, risk_result, loss_usd=loss)

        _check_max_signals(evaluated, max_signals, stop_event)


def _check_max_signals(
    evaluated: int,
    max_signals: Optional[int],
    stop_event: asyncio.Event,
) -> None:
    if max_signals is not None and evaluated >= max_signals:
        logger.info("Max signals (%d) reached — stopping.", max_signals)
        stop_event.set()


async def _stats_printer(
    storage: Storage,
    stop_event: asyncio.Event,
    interval_s: int = LIVE_STATS_INTERVAL_S,
) -> None:
    """Print live stats every *interval_s* seconds."""
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(
                asyncio.shield(stop_event.wait()), timeout=interval_s
            )
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        summary = await storage.get_paper_summary(since_ts_ms=0)
        _print_summary(summary, prefix="[LIVE STATS] ")


async def _metrics_flusher(
    storage: Storage,
    stop_event: asyncio.Event,
    interval_s: int = 30,
) -> None:
    """Sprint 7: Periodically flush in-memory metrics to runtime_health table
    and append a JSONL snapshot for offline analysis."""
    artifacts_dir = "artifacts/paper"
    jsonl_path = f"{artifacts_dir}/metrics.log"
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(
                asyncio.shield(stop_event.wait()), timeout=interval_s
            )
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        try:
            await _metrics.flush_to_storage(storage)
            _metrics.write_jsonl(jsonl_path)
        except Exception as exc:
            logger.warning("Metrics flush failed: %s", exc)


def _print_summary(summary: dict, prefix: str = "") -> None:
    print(
        f"{prefix}"
        f"evaluated={summary['total_snapshots_evaluated']}  "
        f"viable={summary['viable_signals']}  "
        f"success={summary['successes']}  "
        f"legged={summary['legged_incidents']}  "
        f"blocked={summary['blocked_by_risk']}  "
        f"stale={summary['stale_book_rejections']}  "
        f"pnl={summary['total_simulated_pnl']:.4f}  "
        f"median_edge_pct={summary['median_edge_net_pct']:.4f}"
    )


def _build_report(
    app_config: AppConfig,
    summary: dict,
    run_duration_hours: float,
    risk_manager: RiskManager,
) -> dict:
    total = summary["total_snapshots_evaluated"]
    stale = summary["stale_book_rejections"]
    viable = summary["viable_signals"]
    legged = summary["legged_incidents"]

    stale_rate = (stale / total * 100) if total > 0 else 0.0
    legged_rate = (legged / viable * 100) if viable > 0 else 0.0
    median_positive = float(summary["median_edge_net_pct"]) > 0

    stop_conditions: list[str] = []
    if stale_rate > 5:
        stop_conditions.append("stale_book_rate_above_5pct")
    if legged_rate > 10:
        stop_conditions.append("legged_incident_rate_above_10pct")
    if not median_positive:
        stop_conditions.append("median_edge_not_positive")
    if risk_manager.is_killed():
        stop_conditions.append("kill_switch_triggered")

    return {
        "run_duration_hours": run_duration_hours,
        "config": {
            "trade_notional_usdc": str(app_config.trade_notional_usdc),
            "min_net_edge_pct": str(app_config.min_net_edge_pct),
            "max_book_age_ms": app_config.max_book_age_ms,
        },
        "results": {
            "total_snapshots_evaluated": total,
            "viable_signals": viable,
            "successes": summary["successes"],
            "clean_misses": summary["clean_misses"],
            "legged_incidents": legged,
            "blocked_by_risk": summary["blocked_by_risk"],
            "stale_book_rejections": stale,
            "total_simulated_pnl": str(summary["total_simulated_pnl"]),
            "median_edge_net_pct": str(summary["median_edge_net_pct"]),
        },
        "gate_status": {
            "stale_book_rate_pct": round(stale_rate, 2),
            "legged_incident_rate_pct": round(legged_rate, 2),
            "median_edge_positive": median_positive,
        },
        "stop_conditions_triggered": stop_conditions,
    }


async def run_paper(
    duration_hours: float = 72.0,
    max_signals: Optional[int] = None,
) -> int:
    """Run the full paper-trading loop.

    Returns:
        0 if median_edge_net_pct > 0, else 2.
    """
    # ── Safety gate: abort if live mode is enabled ────────────────────────
    allow_live = os.getenv("ALLOW_LIVE_MODE", "false").lower() == "true"
    if allow_live:
        logger.critical(
            "ABORT: ALLOW_LIVE_MODE=true detected in paper mode. "
            "Paper mode requires allow_live_mode=False."
        )
        return 1

    logger.info(
        "Starting paper run — duration=%.1fh  max_signals=%s",
        duration_hours,
        max_signals or "unlimited",
    )

    # ── Build components ──────────────────────────────────────────────────
    infra_cfg = get_config()
    app_config = get_app_config()
    # Force paper-trading safety settings
    app_config.dry_run_mode = True
    app_config.allow_live_mode = False

    storage = Storage(infra_cfg.db_path)
    await storage.connect()

    fee_model = FeeModel()
    risk_manager = RiskManager(config=app_config, storage=storage)
    executor = Executor(
        config=app_config,
        storage=storage,
        dry_run_mode=True,
    )

    candidate_queue: asyncio.Queue = asyncio.Queue()
    opportunity_queue: asyncio.Queue = asyncio.Queue()

    market_discovery = MarketDiscovery(
        config=infra_cfg,
        storage=storage,
        candidate_queue=candidate_queue,
    )
    watcher = Watcher(
        config=infra_cfg,
        candidate_queue=candidate_queue,
        opportunity_queue=opportunity_queue,
    )

    stop_event = asyncio.Event()
    start_ts = time.monotonic()
    counters: dict = {
        "evaluated": 0,
        "successes": 0,
        "legged": 0,
        "clean_misses": 0,
        "blocked": 0,
    }

    # Duration deadline
    duration_s = duration_hours * 3600

    async def _duration_guard() -> None:
        await asyncio.sleep(duration_s)
        logger.info("Duration %.1fh reached — stopping.", duration_hours)
        stop_event.set()

    # ── Run concurrently ──────────────────────────────────────────────────
    tasks = [
        asyncio.create_task(market_discovery.run(), name="discovery"),
        asyncio.create_task(watcher.run(), name="watcher"),
        asyncio.create_task(
            _core_loop(
                opportunity_queue=opportunity_queue,
                storage=storage,
                app_config=app_config,
                fee_model=fee_model,
                risk_manager=risk_manager,
                executor=executor,
                stop_event=stop_event,
                max_signals=max_signals,
                counters=counters,
            ),
            name="core",
        ),
        asyncio.create_task(
            _stats_printer(storage, stop_event, interval_s=LIVE_STATS_INTERVAL_S),
            name="stats",
        ),
        asyncio.create_task(
            _metrics_flusher(storage, stop_event, interval_s=30),
            name="metrics_flusher",
        ),
        asyncio.create_task(_duration_guard(), name="duration"),
    ]

    try:
        # Wait until stop_event is set (by duration guard, max_signals, or Ctrl+C)
        await stop_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        # Cancel all background tasks gracefully
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # ── Final report ──────────────────────────────────────────────────────
    run_duration_hours = (time.monotonic() - start_ts) / 3600
    summary = await storage.get_paper_summary(since_ts_ms=0)
    report = _build_report(app_config, summary, run_duration_hours, risk_manager)

    print("\n" + "=" * 60)
    print("  CERBERUS PAPER RUN — FINAL REPORT")
    print("=" * 60)
    _print_summary(summary)
    if report["stop_conditions_triggered"]:
        print(f"  STOP CONDITIONS: {report['stop_conditions_triggered']}")
    else:
        print("  STOP CONDITIONS: none triggered ✓")
    print("=" * 60)

    # Save JSON report
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    ts_str = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = ARTIFACTS_DIR / f"report_{ts_str}.json"
    report_path.write_text(json.dumps(report, indent=2))
    logger.info("Report saved to %s", report_path)

    await storage.close()

    median_positive = float(summary["median_edge_net_pct"]) > 0
    return 0 if median_positive else 2


# ===========================================================================
# Entry point
# ===========================================================================


def _print_usage() -> None:
    print(__doc__)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Cerberus paper-trading CLI",
        add_help=True,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--preflight",
        action="store_true",
        help="Run preflight checks and exit.",
    )
    group.add_argument(
        "--paper",
        action="store_true",
        help="Run paper-trading loop.",
    )
    parser.add_argument(
        "--duration-hours",
        type=float,
        default=72.0,
        metavar="N",
        help="Paper-run duration in hours (default: 72).",
    )
    parser.add_argument(
        "--max-signals",
        type=int,
        default=None,
        metavar="N",
        help="Stop after evaluating N snapshots.",
    )

    args = parser.parse_args()

    if args.preflight:
        return run_preflight()

    if args.paper:
        try:
            return asyncio.run(
                run_paper(
                    duration_hours=args.duration_hours,
                    max_signals=args.max_signals,
                )
            )
        except KeyboardInterrupt:
            logger.info("Interrupted by user.")
            return 0

    _print_usage()
    return 0


if __name__ == "__main__":
    sys.exit(main())
