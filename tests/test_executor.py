"""
Tests for cerberus_runtime/executor.py.

All 10 required tests:
  test_dry_run_success_both_legs_filled
  test_dry_run_clean_miss_leg1_not_filled
  test_dry_run_legged_risk_leg2_partial
  test_dry_run_edge_degradation_blocks
  test_legs_are_sequential_not_parallel
  test_no_api_call_in_dry_run
  test_simulate_fill_fok_fails_on_high_ask
  test_simulate_fill_fok_succeeds_exact_price
  test_emergency_repair_logs_legged_event
  test_all_arithmetic_is_decimal

Tag: [CERBERUS-STRATEGY-UPDATE]
"""
from __future__ import annotations

import time
import uuid
from decimal import Decimal
from typing import List
from unittest.mock import AsyncMock

import pytest

from cerberus_runtime.core import AppConfig
from cerberus_runtime.executor import (
    ArbitrageResult,
    CerberusStorage,
    Executor,
    OrderResult,
)
from cerberus_runtime.models import (
    ArbitrageSignal,
    LegQuote,
    OrderBookSnapshot,
    PriceLevel,
)


# ---------------------------------------------------------------------------
# Test-fixture helpers  (module-level functions, not pytest fixtures, to keep
# control explicit and avoid accidental conftest shadowing)
# ---------------------------------------------------------------------------


def _make_config(**overrides) -> AppConfig:
    """AppConfig with safe test defaults; override per-test as needed."""
    defaults: dict = dict(
        trade_notional_usdc=Decimal("100"),
        slippage_buffer_pct=Decimal("0.005"),
        legged_risk_buffer_pct=Decimal("0.005"),
        min_net_edge_usd=Decimal("1.0"),
        min_net_edge_pct=Decimal("0.005"),
        min_order_size=Decimal("1.0"),
        tick_size=Decimal("0.01"),
        fee_params=None,
    )
    defaults.update(overrides)
    return AppConfig(**defaults)


def _make_signal() -> ArbitrageSignal:
    """Arbitrage signal: YES@0.45, NO@0.50, notional=100 USDC, edge_net=4 USDC."""
    yes_quote = LegQuote(
        avg_price=Decimal("0.45"),
        coverage_pct=Decimal("1.0"),
        fee_usdc=Decimal("0"),
        accumulated_cost=Decimal("100"),
        accumulated_tokens=Decimal("100") / Decimal("0.45"),
    )
    no_quote = LegQuote(
        avg_price=Decimal("0.50"),
        coverage_pct=Decimal("1.0"),
        fee_usdc=Decimal("0"),
        accumulated_cost=Decimal("100"),
        accumulated_tokens=Decimal("200"),
    )
    return ArbitrageSignal(
        market_id="test_market",
        yes_quote=yes_quote,
        no_quote=no_quote,
        edge_gross=Decimal("5"),
        fees_total=Decimal("0"),
        risk_haircut=Decimal("1"),
        edge_net=Decimal("4"),
        edge_net_pct=Decimal("0.02"),
        trade_notional_usdc=Decimal("100"),
    )


def _make_good_snapshot() -> OrderBookSnapshot:
    """Fresh snapshot where the edge check passes and both legs can fully fill.

    Edge re-quote math (verify this stays consistent with _make_config):
        YES avg_price ≈ 0.45, NO avg_price = 0.50
        total_cost = (0.45 + 0.50) * 100 = 95
        edge_gross  = 100 − 95 = 5
        risk_haircut = 100 * (0.005 + 0.005) = 1
        edge_net    = 5 − 0 − 1 = 4  >  min_net_edge * 0.8 = 0.8  ✓
    """
    return OrderBookSnapshot(
        market_id="test_market",
        yes_asks=[PriceLevel(price=Decimal("0.45"), size=Decimal("300"))],
        no_asks=[PriceLevel(price=Decimal("0.50"), size=Decimal("300"))],
        timestamp=1_000.0,
        yes_token_id="yes_tok",
        no_token_id="no_tok",
    )


def _make_mock_storage() -> AsyncMock:
    """AsyncMock that satisfies the CerberusStorage interface."""
    storage = AsyncMock(spec=CerberusStorage)
    storage.insert_order = AsyncMock(return_value="order_001")
    storage.insert_fill = AsyncMock(return_value=None)
    storage.insert_legged_event = AsyncMock(return_value=None)
    return storage


def _make_executor(dry_run: bool = True, **config_overrides) -> Executor:
    return Executor(
        config=_make_config(**config_overrides),
        storage=_make_mock_storage(),
        dry_run_mode=dry_run,
    )


# ---------------------------------------------------------------------------
# Test 1 — happy path: both legs fill, return SUCCESS
# ---------------------------------------------------------------------------


async def test_dry_run_success_both_legs_filled():
    """Both legs simulate full fill in dry-run → SUCCESS."""
    executor = Executor(
        config=_make_config(),
        storage=_make_mock_storage(),
        dry_run_mode=True,
    )
    result = await executor.execute_pair(_make_signal(), _make_good_snapshot())
    assert result == ArbitrageResult.SUCCESS


# ---------------------------------------------------------------------------
# Test 2 — Leg 1 fails → CLEAN_MISS, Leg 2 never attempted
# ---------------------------------------------------------------------------


async def test_dry_run_clean_miss_leg1_not_filled():
    """YES ask above signal limit → Leg 1 NOT_FILLED → CLEAN_MISS.

    Snapshot has YES ask at 0.46 (> signal limit 0.45) but low enough for the
    edge check to pass (fresh edge ≈ 3 > 0.8).
    """
    snapshot = OrderBookSnapshot(
        market_id="test_market",
        yes_asks=[PriceLevel(price=Decimal("0.46"), size=Decimal("300"))],
        no_asks=[PriceLevel(price=Decimal("0.50"), size=Decimal("300"))],
        timestamp=1_000.0,
        yes_token_id="yes_tok",
        no_token_id="no_tok",
    )
    storage = _make_mock_storage()
    executor = Executor(config=_make_config(), storage=storage, dry_run_mode=True)

    result = await executor.execute_pair(_make_signal(), snapshot)

    assert result == ArbitrageResult.CLEAN_MISS

    # Verify Leg 2 insert_order was never called (only YES leg is recorded)
    no_calls = [
        c
        for c in storage.insert_order.call_args_list
        if c.args[1] == "NO"
    ]
    assert len(no_calls) == 0, "Leg 2 insert_order must not be called after CLEAN_MISS"


# ---------------------------------------------------------------------------
# Test 3 — Leg 1 fills, Leg 2 partial → LEGGED_RISK or REPAIRED
# ---------------------------------------------------------------------------


async def test_dry_run_legged_risk_leg2_partial():
    """Leg 1 fills; Leg 2 gets only 80 tokens (need 200) → emergency repair.

    Snapshot design:
    - YES: 300 tokens @0.45 → fully fills Leg 1 (222.2… tokens needed).
    - NO:  80 tokens @0.50 (signal limit), 300 more @0.55 (above limit).
      * Edge check: calculates_effective_leg walks both levels → coverage=1.0 ✓
        avg_no_price ≈ 0.529 → edge_net ≈ 1.1 > 0.8 → passes the gate.
      * FOK simulation: only 80 tokens ≤ limit price 0.50 → PARTIAL (need 200).
    - Emergency repair sells Leg 1 back at best_ask − 0.5 % slippage → loss → LEGGED_RISK.
    """
    snapshot = OrderBookSnapshot(
        market_id="test_market",
        yes_asks=[PriceLevel(price=Decimal("0.45"), size=Decimal("300"))],
        no_asks=[
            PriceLevel(price=Decimal("0.50"), size=Decimal("80")),   # 80 available at limit
            PriceLevel(price=Decimal("0.55"), size=Decimal("300")),  # above signal limit
        ],
        timestamp=1_000.0,
        yes_token_id="yes_tok",
        no_token_id="no_tok",
    )
    storage = _make_mock_storage()
    executor = Executor(config=_make_config(), storage=storage, dry_run_mode=True)

    result = await executor.execute_pair(_make_signal(), snapshot)

    assert result in (ArbitrageResult.LEGGED_RISK, ArbitrageResult.REPAIRED)
    storage.insert_legged_event.assert_called_once()


# ---------------------------------------------------------------------------
# Test 4 — fresh edge below 80 % of min threshold → BLOCKED_BY_RISK
# ---------------------------------------------------------------------------


async def test_dry_run_edge_degradation_blocks():
    """Snapshot too thin (< 95 % coverage) → fresh edge = None → BLOCKED_BY_RISK.

    With asks of only 5 tokens @0.45, the USDC value is 5×0.45 = 2.25 USDC,
    which is 2.25 % of the 100 USDC notional — well below the 95 % coverage
    required by calculate_effective_leg.  _evaluate_fresh_edge returns None,
    which triggers BLOCKED_BY_RISK before any orders are sent.
    """
    snapshot = OrderBookSnapshot(
        market_id="test_market",
        yes_asks=[PriceLevel(price=Decimal("0.45"), size=Decimal("5"))],  # too thin
        no_asks=[PriceLevel(price=Decimal("0.50"), size=Decimal("5"))],
        timestamp=1_000.0,
        yes_token_id="yes_tok",
        no_token_id="no_tok",
    )
    executor = Executor(config=_make_config(), storage=_make_mock_storage(), dry_run_mode=True)

    result = await executor.execute_pair(_make_signal(), snapshot)

    assert result == ArbitrageResult.BLOCKED_BY_RISK


# ---------------------------------------------------------------------------
# Test 5 — legs are sequential, never concurrent
# ---------------------------------------------------------------------------


async def test_legs_are_sequential_not_parallel():
    """_send_order is called for YES, then for NO — never concurrently.

    Strategy: replace _send_order with a tracking async function.  After the
    call, assert the two entries in ``call_log`` are [YES_token_id, NO_token_id]
    in that order (never reversed, never called via asyncio.gather).
    """
    call_log: List[str] = []

    async def _tracked_send(order_params: dict) -> OrderResult:
        call_log.append(order_params["token_id"])
        return OrderResult(
            order_id=str(uuid.uuid4()),
            status="FILLED",
            filled_size=order_params["size"],
            fill_price=order_params["price"],
            fee_usdc=Decimal("0"),
        )

    executor = Executor(
        config=_make_config(),
        storage=_make_mock_storage(),
        dry_run_mode=False,
    )
    # Replace the method on the instance (not the class) so self is not passed.
    executor._send_order = _tracked_send  # type: ignore[method-assign]

    await executor.execute_pair(_make_signal(), _make_good_snapshot())

    assert len(call_log) == 2, "Expected exactly two _send_order calls"
    assert call_log[0] == "yes_tok", "Leg 1 (YES) must be sent first"
    assert call_log[1] == "no_tok",  "Leg 2 (NO) must be sent second"


# ---------------------------------------------------------------------------
# Test 6 — _client must be None in dry-run
# ---------------------------------------------------------------------------


def test_no_api_call_in_dry_run():
    """Executor._client is always None when dry_run_mode=True."""
    executor = Executor(
        config=_make_config(),
        storage=_make_mock_storage(),
        dry_run_mode=True,
    )
    assert executor._client is None, "_client must be None in dry_run_mode"


# ---------------------------------------------------------------------------
# Test 7 — _simulate_fill: ask above limit → NOT_FILLED
# ---------------------------------------------------------------------------


def test_simulate_fill_fok_fails_on_high_ask():
    """Best ask (0.60) > limit price (0.50) → NOT_FILLED, filled_size = 0."""
    executor = _make_executor()
    asks = [PriceLevel(price=Decimal("0.60"), size=Decimal("500"))]
    order_params = {
        "token_id": "tok",
        "price": Decimal("0.50"),
        "size": Decimal("200"),
        "side": "BUY",
        "order_type": "FOK",
    }

    result = executor._simulate_fill(order_params, asks)

    assert result.status == "NOT_FILLED"
    assert result.filled_size == Decimal("0")
    assert isinstance(result.filled_size, Decimal)
    assert isinstance(result.fill_price, Decimal)
    assert isinstance(result.fee_usdc, Decimal)


# ---------------------------------------------------------------------------
# Test 8 — _simulate_fill: ask == limit price, enough size → FILLED
# ---------------------------------------------------------------------------


def test_simulate_fill_fok_succeeds_exact_price():
    """Ask price exactly equals limit price, size > order size → FILLED."""
    executor = _make_executor()
    asks = [PriceLevel(price=Decimal("0.45"), size=Decimal("500"))]
    order_params = {
        "token_id": "tok",
        "price": Decimal("0.45"),
        "size": Decimal("200"),
        "side": "BUY",
        "order_type": "FOK",
    }

    result = executor._simulate_fill(order_params, asks)

    assert result.status == "FILLED"
    assert result.filled_size == Decimal("200")
    assert isinstance(result.fill_price, Decimal)
    assert result.fill_price == Decimal("0.45")


# ---------------------------------------------------------------------------
# Test 9 — emergency repair records a legged event in storage
# ---------------------------------------------------------------------------


async def test_emergency_repair_logs_legged_event():
    """After a Leg 2 partial fill, storage.insert_legged_event must be called once.

    Reuses the legged-risk snapshot from test 3.
    """
    snapshot = OrderBookSnapshot(
        market_id="test_market",
        yes_asks=[PriceLevel(price=Decimal("0.45"), size=Decimal("300"))],
        no_asks=[
            PriceLevel(price=Decimal("0.50"), size=Decimal("80")),
            PriceLevel(price=Decimal("0.55"), size=Decimal("300")),
        ],
        timestamp=1_000.0,
        yes_token_id="yes_tok",
        no_token_id="no_tok",
    )
    storage = _make_mock_storage()
    executor = Executor(config=_make_config(), storage=storage, dry_run_mode=True)

    result = await executor.execute_pair(_make_signal(), snapshot)

    assert result in (ArbitrageResult.LEGGED_RISK, ArbitrageResult.REPAIRED)
    storage.insert_legged_event.assert_called_once()

    # Confirm the keyword arguments are present and correctly typed
    kwargs = storage.insert_legged_event.call_args.kwargs
    assert "repair_action" in kwargs
    assert kwargs["repair_action"] == "sell_leg1"
    assert "repair_loss_usdc" in kwargs
    assert isinstance(kwargs["repair_loss_usdc"], Decimal), (
        "repair_loss_usdc must be Decimal"
    )


# ---------------------------------------------------------------------------
# Test 10 — all OrderResult monetary fields are Decimal, never float
# ---------------------------------------------------------------------------


def test_all_arithmetic_is_decimal():
    """_simulate_fill must not produce any float fields in OrderResult."""
    executor = _make_executor()
    asks = [PriceLevel(price=Decimal("0.45"), size=Decimal("300"))]
    order_params = {
        "token_id": "tok",
        "price": Decimal("0.45"),
        "size": Decimal("222"),
        "side": "BUY",
        "order_type": "FOK",
    }

    result = executor._simulate_fill(order_params, asks)

    # All monetary fields must be Decimal
    for field_name, value in [
        ("filled_size", result.filled_size),
        ("fill_price",  result.fill_price),
        ("fee_usdc",    result.fee_usdc),
    ]:
        assert isinstance(value, Decimal), (
            f"OrderResult.{field_name} must be Decimal, got {type(value).__name__}"
        )
        assert not isinstance(value, float), (
            f"OrderResult.{field_name} must not be float"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Sprint 5 — emergency_repair guards (bugs #1, #8)
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_emergency_repair_zero_fill_no_false_repaired():
    """Bug #8: when leg1 status=FILLED but filled_size==0, repair must record
    'no_position' with zero loss instead of falsely claiming a sale was made."""
    storage = _make_mock_storage()
    config = _make_config()
    executor = Executor(storage=storage, config=config, dry_run_mode=True)

    signal = _make_signal()
    snapshot = OrderBookSnapshot(
        market_id=signal.market_id,
        yes_asks=[PriceLevel(price=Decimal("0.50"), size=Decimal("300"))],
        no_asks=[PriceLevel(price=Decimal("0.45"), size=Decimal("300"))],
        timestamp=float(time.time() * 1000),
        yes_token_id="yes_tok",
        no_token_id="no_tok",
    )

    # Pathological state: status=FILLED but no actual fill — emergency repair
    # must short-circuit without computing fake loss.
    leg1_result = OrderResult(
        order_id="leg1", status="FILLED",
        filled_size=Decimal("0"), fill_price=Decimal("0.50"),
        fee_usdc=Decimal("0"),
    )
    leg2_result = OrderResult(
        order_id="leg2", status="NOT_FILLED",
        filled_size=Decimal("0"), fill_price=Decimal("0"),
        fee_usdc=Decimal("0"),
    )

    result = await executor._emergency_repair(leg1_result, leg2_result, snapshot)

    assert result == ArbitrageResult.REPAIRED
    storage.insert_legged_event.assert_called_once()
    call_kwargs = storage.insert_legged_event.call_args.kwargs
    assert call_kwargs["repair_action"] == "no_position", (
        "Zero-fill leg1 must record 'no_position', not a fake sale"
    )
    assert call_kwargs["repair_loss_usdc"] == Decimal("0")


@pytest.mark.asyncio
async def test_emergency_repair_empty_orderbook_no_crash():
    """Bug #1: empty yes_asks must use leg1.fill_price as fallback, not crash with IndexError."""
    storage = _make_mock_storage()
    config = _make_config()
    executor = Executor(storage=storage, config=config, dry_run_mode=True)

    signal = _make_signal()
    snapshot = OrderBookSnapshot(
        market_id=signal.market_id,
        yes_asks=[],                          # ← empty book (post-resync corruption)
        no_asks=[PriceLevel(price=Decimal("0.45"), size=Decimal("300"))],
        timestamp=float(time.time() * 1000),
        yes_token_id="yes_tok",
        no_token_id="no_tok",
    )

    leg1_result = OrderResult(
        order_id="leg1", status="FILLED",
        filled_size=Decimal("50"), fill_price=Decimal("0.50"),
        fee_usdc=Decimal("0"),
    )
    leg2_result = OrderResult(
        order_id="leg2", status="NOT_FILLED",
        filled_size=Decimal("0"), fill_price=Decimal("0"),
        fee_usdc=Decimal("0"),
    )

    # Must NOT raise IndexError; result depends on repair_loss but either is fine
    result = await executor._emergency_repair(leg1_result, leg2_result, snapshot)
    assert result in (ArbitrageResult.REPAIRED, ArbitrageResult.LEGGED_RISK)
    storage.insert_legged_event.assert_called_once()
