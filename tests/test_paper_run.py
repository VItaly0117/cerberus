"""
Tests for cerberustest.py paper-trading pipeline.

Seven required tests:
  test_paper_loop_runs_one_cycle          — mock all, assert insert_paper_signal called
  test_blocked_signal_recorded            — risk blocks → BLOCKED_BY_RISK stored
  test_filtered_signal_recorded           — core returns None → FILTERED stored
  test_success_recorded_with_pnl          — executor SUCCESS → positive simulated_pnl
  test_report_json_written_to_artifacts   — JSON report file written after run
  test_stop_conditions_stale_book         — >5% stale → flag in report dict
  test_dry_run_enforced_in_paper_mode     — ALLOW_LIVE_MODE=true → abort (rc=1)
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from decimal import Decimal
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module under test
# ---------------------------------------------------------------------------
import cerberustest
from cerberustest import _build_report, _core_loop, run_paper

# ---------------------------------------------------------------------------
# Runtime imports
# ---------------------------------------------------------------------------
from cerberus_runtime.config import AppConfig
from cerberus_runtime.executor import ArbitrageResult as ExecResult
from cerberus_runtime.fee_model import FeeModel
from cerberus_runtime.models import (
    ArbitrageSignal,
    LegQuote,
    OrderBookSnapshot,
    PriceLevel,
)
from cerberus_runtime.risk import ArbitrageResult as RiskResult, RiskManager
from cerberus_runtime.storage import Storage


# ===========================================================================
# Shared helpers
# ===========================================================================


def _make_app_config(**overrides) -> AppConfig:
    """AppConfig with paper-safe defaults."""
    defaults = dict(
        trade_notional_usdc=Decimal("100"),
        slippage_buffer_pct=Decimal("0.001"),
        legged_risk_buffer_pct=Decimal("0.001"),
        min_net_edge_usd=Decimal("0.10"),
        min_net_edge_pct=Decimal("0.0125"),
        min_order_size=Decimal("1"),
        tick_size=Decimal("0.01"),
        fee_params=None,
        dry_run_mode=True,
        allow_live_mode=False,
        max_book_age_ms=5_000,
        max_open_markets=1,
        max_attempts_per_hour=60,
        daily_loss_limit_usd=Decimal("50"),
        market_cooldown_seconds=10.0,
    )
    defaults.update(overrides)
    return AppConfig(**defaults)


def _make_snapshot(market_id: str = "mkt1") -> OrderBookSnapshot:
    """A fresh snapshot with good depth on both sides."""
    now_ms = int(time.time() * 1000)
    level = PriceLevel(price=Decimal("0.45"), size=Decimal("500"))
    return OrderBookSnapshot(
        market_id=market_id,
        yes_asks=[level],
        no_asks=[level],
        timestamp=time.time(),
        condition_id=market_id,
        ts_ms=now_ms,
    )


def _make_signal(market_id: str = "mkt1", edge_net: str = "4.00") -> ArbitrageSignal:
    """Minimal ArbitrageSignal that passes risk thresholds."""
    leg = LegQuote(
        avg_price=Decimal("0.45"),
        coverage_pct=Decimal("1.0"),
        fee_usdc=Decimal("0.05"),
        accumulated_cost=Decimal("100"),
        accumulated_tokens=Decimal("222"),
    )
    return ArbitrageSignal(
        market_id=market_id,
        yes_quote=leg,
        no_quote=leg,
        edge_gross=Decimal("5"),
        fees_total=Decimal("0.1"),
        risk_haircut=Decimal("0.2"),
        edge_net=Decimal(edge_net),
        edge_net_pct=Decimal("0.02"),
        trade_notional_usdc=Decimal("100"),
    )


def _make_mock_storage() -> MagicMock:
    """Return a mock that satisfies the Storage protocol expected by _core_loop."""
    storage = MagicMock()
    storage.insert_paper_signal_from_snapshot = AsyncMock()
    storage.get_paper_summary = AsyncMock(return_value={
        "total_snapshots_evaluated": 0,
        "viable_signals": 0,
        "successes": 0,
        "clean_misses": 0,
        "legged_incidents": 0,
        "blocked_by_risk": 0,
        "stale_book_rejections": 0,
        "total_simulated_pnl": Decimal("0"),
        "median_edge_net_pct": Decimal("0"),
    })
    return storage


def _make_mock_risk(allows_result: tuple = (True, "")) -> MagicMock:
    """Return a RiskManager mock with configurable allows()."""
    rm = MagicMock()
    rm.allows = MagicMock(return_value=allows_result)
    rm.record_result = AsyncMock()
    rm.is_killed = MagicMock(return_value=False)
    return rm


def _make_mock_executor(result: ExecResult = ExecResult.SUCCESS) -> MagicMock:
    """Return an Executor mock that always returns *result*."""
    ex = MagicMock()
    ex.execute_pair = AsyncMock(return_value=result)
    return ex


# ===========================================================================
# 1 — paper_loop_runs_one_cycle
# ===========================================================================


async def test_paper_loop_runs_one_cycle():
    """_core_loop processes exactly 1 snapshot and calls insert_paper_signal_from_snapshot."""
    storage = _make_mock_storage()
    risk_manager = _make_mock_risk(allows_result=(True, ""))
    executor = _make_mock_executor(ExecResult.SUCCESS)
    fee_model = FeeModel()
    app_config = _make_app_config()
    stop_event = asyncio.Event()
    counters = {"evaluated": 0, "successes": 0, "legged": 0, "clean_misses": 0, "blocked": 0}

    snapshot = _make_snapshot()
    q: asyncio.Queue = asyncio.Queue()
    await q.put(snapshot)

    signal = _make_signal()

    with patch("cerberus_runtime.core.evaluate_opportunity", return_value=signal):
        await _core_loop(
            opportunity_queue=q,
            storage=storage,
            app_config=app_config,
            fee_model=fee_model,
            risk_manager=risk_manager,
            executor=executor,
            stop_event=stop_event,
            max_signals=1,
            counters=counters,
        )

    storage.insert_paper_signal_from_snapshot.assert_called_once()
    assert stop_event.is_set()
    assert counters["evaluated"] == 1


# ===========================================================================
# 2 — blocked_signal_recorded
# ===========================================================================


async def test_blocked_signal_recorded():
    """When risk_manager.allows() returns False, BLOCKED_BY_RISK is stored."""
    storage = _make_mock_storage()
    risk_manager = _make_mock_risk(allows_result=(False, "stale_book"))
    executor = _make_mock_executor()
    fee_model = FeeModel()
    app_config = _make_app_config()
    stop_event = asyncio.Event()
    counters = {"evaluated": 0, "successes": 0, "legged": 0, "clean_misses": 0, "blocked": 0}

    q: asyncio.Queue = asyncio.Queue()
    await q.put(_make_snapshot())

    with patch("cerberus_runtime.core.evaluate_opportunity") as mock_eval:
        await _core_loop(
            opportunity_queue=q,
            storage=storage,
            app_config=app_config,
            fee_model=fee_model,
            risk_manager=risk_manager,
            executor=executor,
            stop_event=stop_event,
            max_signals=1,
            counters=counters,
        )
        # evaluate_opportunity should NOT be called when blocked
        mock_eval.assert_not_called()

    call_kwargs = storage.insert_paper_signal_from_snapshot.call_args.kwargs
    assert call_kwargs["result"] == "BLOCKED_BY_RISK"
    assert call_kwargs["rejection_reason"] == "stale_book"
    assert call_kwargs["signal"] is None
    assert counters["blocked"] == 1


# ===========================================================================
# 3 — filtered_signal_recorded
# ===========================================================================


async def test_filtered_signal_recorded():
    """When evaluate_opportunity returns None, FILTERED is stored."""
    storage = _make_mock_storage()
    risk_manager = _make_mock_risk(allows_result=(True, ""))
    executor = _make_mock_executor()
    fee_model = FeeModel()
    app_config = _make_app_config()
    stop_event = asyncio.Event()
    counters = {"evaluated": 0, "successes": 0, "legged": 0, "clean_misses": 0, "blocked": 0}

    q: asyncio.Queue = asyncio.Queue()
    await q.put(_make_snapshot())

    with patch("cerberus_runtime.core.evaluate_opportunity", return_value=None):
        await _core_loop(
            opportunity_queue=q,
            storage=storage,
            app_config=app_config,
            fee_model=fee_model,
            risk_manager=risk_manager,
            executor=executor,
            stop_event=stop_event,
            max_signals=1,
            counters=counters,
        )

    call_kwargs = storage.insert_paper_signal_from_snapshot.call_args.kwargs
    assert call_kwargs["result"] == "FILTERED"
    assert call_kwargs["rejection_reason"] == "edge_below_threshold"
    assert call_kwargs["signal"] is None
    # executor.execute_pair must not have been called
    executor.execute_pair.assert_not_called()


# ===========================================================================
# 4 — success_recorded_with_pnl
# ===========================================================================


async def test_success_recorded_with_pnl():
    """On executor SUCCESS, stored simulated_pnl equals signal.edge_net."""
    storage = _make_mock_storage()
    risk_manager = _make_mock_risk(allows_result=(True, ""))
    executor = _make_mock_executor(ExecResult.SUCCESS)
    fee_model = FeeModel()
    app_config = _make_app_config()
    stop_event = asyncio.Event()
    counters = {"evaluated": 0, "successes": 0, "legged": 0, "clean_misses": 0, "blocked": 0}

    q: asyncio.Queue = asyncio.Queue()
    await q.put(_make_snapshot())

    signal = _make_signal(edge_net="3.75")

    with patch("cerberus_runtime.core.evaluate_opportunity", return_value=signal):
        await _core_loop(
            opportunity_queue=q,
            storage=storage,
            app_config=app_config,
            fee_model=fee_model,
            risk_manager=risk_manager,
            executor=executor,
            stop_event=stop_event,
            max_signals=1,
            counters=counters,
        )

    call_kwargs = storage.insert_paper_signal_from_snapshot.call_args.kwargs
    assert call_kwargs["result"] == ExecResult.SUCCESS.name  # "SUCCESS"
    # simulated_pnl is set to signal.edge_net on success
    assert call_kwargs["simulated_pnl"] == Decimal("3.75")
    assert counters["successes"] == 1


# ===========================================================================
# 5 — report_json_written_to_artifacts
# ===========================================================================


async def test_report_json_written_to_artifacts(tmp_path: Path, monkeypatch):
    """run_paper() writes a JSON report to ARTIFACTS_DIR after completion."""
    # Point ARTIFACTS_DIR at a temp location so we don't pollute the real dir.
    monkeypatch.setattr(cerberustest, "ARTIFACTS_DIR", tmp_path / "paper")
    monkeypatch.delenv("ALLOW_LIVE_MODE", raising=False)

    # Patch out all components that make network calls or need real env.
    fake_infra_cfg = MagicMock()
    fake_infra_cfg.db_path = ":memory:"

    with (
        patch("cerberustest.get_config", return_value=fake_infra_cfg),
        patch("cerberustest.get_app_config", return_value=_make_app_config()),
        patch("cerberustest.MarketDiscovery") as MockDiscovery,
        patch("cerberustest.Watcher") as MockWatcher,
    ):
        # Use AsyncMock so .run() returns a coroutine and never blocks
        MockDiscovery.return_value.run = AsyncMock()
        MockWatcher.return_value.run = AsyncMock()

        # Run with max_signals=0 so the loop exits immediately after startup.
        rc = await run_paper(duration_hours=0.0001, max_signals=0)

    # A report JSON file should have been created.
    report_files = list((tmp_path / "paper").glob("report_*.json"))
    assert len(report_files) == 1, f"Expected 1 report file, got {report_files}"

    report = json.loads(report_files[0].read_text())
    assert "results" in report
    assert "stop_conditions_triggered" in report
    # Return code: 0 when median_edge_net_pct > 0, or 2 when ≤ 0. Either is valid here.
    assert rc in (0, 2)


# ===========================================================================
# 6 — stop_conditions_stale_book
# ===========================================================================


def test_stop_conditions_stale_book():
    """_build_report flags stale_book_rate_above_5pct when stale > 5% of total."""
    app_config = _make_app_config()
    risk_manager = MagicMock()
    risk_manager.is_killed.return_value = False

    # 6 stale out of 100 total → 6 % → flag triggered
    summary = {
        "total_snapshots_evaluated": 100,
        "viable_signals": 20,
        "successes": 10,
        "clean_misses": 5,
        "legged_incidents": 2,
        "blocked_by_risk": 10,
        "stale_book_rejections": 6,
        "total_simulated_pnl": Decimal("8.50"),
        "median_edge_net_pct": Decimal("0.02"),
    }

    report = _build_report(
        app_config=app_config,
        summary=summary,
        run_duration_hours=1.0,
        risk_manager=risk_manager,
    )

    assert "stale_book_rate_above_5pct" in report["stop_conditions_triggered"]
    assert report["gate_status"]["stale_book_rate_pct"] == 6.0

    # Sanity: with <5% stale the flag should NOT appear
    summary_ok = {**summary, "stale_book_rejections": 4}
    report_ok = _build_report(app_config, summary_ok, 1.0, risk_manager)
    assert "stale_book_rate_above_5pct" not in report_ok["stop_conditions_triggered"]


# ===========================================================================
# 7 — dry_run_enforced_in_paper_mode
# ===========================================================================


async def test_dry_run_enforced_in_paper_mode(monkeypatch):
    """run_paper() must abort with exit code 1 when ALLOW_LIVE_MODE=true."""
    monkeypatch.setenv("ALLOW_LIVE_MODE", "true")

    rc = await run_paper(duration_hours=1.0, max_signals=1)

    assert rc == 1, (
        "run_paper() must return 1 (abort) when ALLOW_LIVE_MODE=true; "
        f"got {rc} instead."
    )
