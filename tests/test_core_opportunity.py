"""
Tests for cerberus_runtime.core: depth walk and opportunity evaluation.

Covers:
- test_depth_walk_returns_none_when_coverage_below_95
- test_depth_walk_skips_levels_below_min_order_size
- test_edge_net_positive_triggers_signal
- test_edge_net_negative_returns_none
- test_no_float_in_calculations
"""
from __future__ import annotations

from decimal import Decimal
from typing import List

import pytest

from cerberus_runtime.core import (
    AppConfig,
    ArbitrageSignal,
    LegQuote,
    OrderBookSnapshot,
    PriceLevel,
    calculate_effective_leg,
    evaluate_opportunity,
)
from cerberus_runtime.fee_model import FeeModel
from cerberus_runtime.models import FeeParams


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides) -> AppConfig:
    """Return an AppConfig with safe test defaults, overridable per-test."""
    defaults: dict = dict(
        trade_notional_usdc=Decimal("100"),
        slippage_buffer_pct=Decimal("0.001"),
        legged_risk_buffer_pct=Decimal("0.001"),
        min_net_edge_usd=Decimal("0.10"),
        min_net_edge_pct=Decimal("0.001"),
        min_order_size=Decimal("1"),
        tick_size=Decimal("0.01"),
        fee_params=FeeParams(
            fees_enabled=True, taker_fee_rate=0.005, maker_fee_rate=0.001
        ),
    )
    defaults.update(overrides)
    return AppConfig(**defaults)


def _levels(*price_size_pairs) -> List[PriceLevel]:
    """Build a list of Decimal PriceLevels from (price, size) tuples."""
    return [
        PriceLevel(price=Decimal(str(p)), size=Decimal(str(s)))
        for p, s in price_size_pairs
    ]


def _snapshot(
    yes_asks: List[PriceLevel],
    no_asks: List[PriceLevel],
    market_id: str = "test_market",
) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        market_id=market_id,
        yes_asks=yes_asks,
        no_asks=no_asks,
        timestamp=0.0,
    )


# ---------------------------------------------------------------------------
# calculate_effective_leg — depth walk
# ---------------------------------------------------------------------------


class TestDepthWalk:

    def test_depth_walk_returns_none_when_coverage_below_95(self):
        """Return None when less than 95 % of notional can be filled.

        100 tokens at 0.50 → 50 USDC available; notional = 100 USDC.
        coverage = 50 / 100 = 0.50 < 0.95 → must return None.
        """
        fee_model = FeeModel()
        config = _make_config()
        asks = _levels((0.50, 100))  # 100 × 0.50 = 50 USDC — only 50 % coverage

        result = calculate_effective_leg(
            asks=asks,
            notional_usdc=config.trade_notional_usdc,
            fee_model=fee_model,
            fee_params=config.fee_params,
            min_order_size=config.min_order_size,
            tick_size=config.tick_size,
        )

        assert result is None

    def test_depth_walk_exactly_95_percent_coverage_passes(self):
        """Coverage of exactly 95 % should *not* be filtered out."""
        fee_model = FeeModel()
        config = _make_config()
        # 95 tokens × 1.00 = 95 USDC on a 100 USDC notional → coverage = 0.95
        asks = _levels((1.00, 95))

        result = calculate_effective_leg(
            asks=asks,
            notional_usdc=config.trade_notional_usdc,
            fee_model=fee_model,
            fee_params=config.fee_params,
            min_order_size=config.min_order_size,
            tick_size=config.tick_size,
        )

        assert result is not None
        assert result.coverage_pct >= Decimal("0.95")

    def test_depth_walk_skips_levels_below_min_order_size(self):
        """Levels whose USDC value is below min_order_size must be skipped.

        Level 1: 1 token × 0.50 = 0.50 USDC  →  below min_order_size=10, SKIP.
        Level 2: 300 tokens × 0.51 = 153 USDC →  fill the full 100 USDC notional.

        If Level 1 were incorrectly included, its tiny contribution would nudge
        avg_price toward 0.50; using only Level 2, avg_price ≈ 0.51.
        """
        fee_model = FeeModel()
        config = _make_config(min_order_size=Decimal("10"))
        asks = [
            PriceLevel(price=Decimal("0.50"), size=Decimal("1")),    # 0.50 USDC → SKIP
            PriceLevel(price=Decimal("0.51"), size=Decimal("300")),  # 153 USDC  → USE
        ]

        result = calculate_effective_leg(
            asks=asks,
            notional_usdc=Decimal("100"),
            fee_model=fee_model,
            fee_params=config.fee_params,
            min_order_size=config.min_order_size,
            tick_size=config.tick_size,
        )

        # Level 2 alone achieves 100 % coverage
        assert result is not None
        assert result.coverage_pct >= Decimal("0.95")
        # avg_price is driven entirely by Level 2 (≈ 0.51) — not influenced by Level 1
        assert result.avg_price > Decimal("0.505")

    def test_depth_walk_aggregates_multiple_levels(self):
        """Coverage from multiple levels must be accumulated correctly."""
        fee_model = FeeModel()
        config = _make_config()
        # Three levels; each contributes roughly 40 USDC
        asks = _levels(
            (0.40, 100),  # 100 × 0.40 = 40 USDC
            (0.41, 100),  # 100 × 0.41 = 41 USDC
            (0.42, 100),  # 100 × 0.42 = 42 USDC  → total = 123 USDC > 100 USDC needed
        )

        result = calculate_effective_leg(
            asks=asks,
            notional_usdc=Decimal("100"),
            fee_model=fee_model,
            fee_params=config.fee_params,
            min_order_size=config.min_order_size,
            tick_size=config.tick_size,
        )

        assert result is not None
        assert result.accumulated_cost >= Decimal("95")
        assert result.accumulated_cost <= Decimal("100")

    def test_depth_walk_returns_leg_quote_on_full_fill(self):
        """A well-stocked single level must produce a valid LegQuote."""
        fee_model = FeeModel()
        config = _make_config()
        asks = _levels((0.45, 300))  # 300 × 0.45 = 135 USDC — more than enough

        result = calculate_effective_leg(
            asks=asks,
            notional_usdc=config.trade_notional_usdc,
            fee_model=fee_model,
            fee_params=config.fee_params,
            min_order_size=config.min_order_size,
            tick_size=config.tick_size,
        )

        assert result is not None
        assert isinstance(result, LegQuote)
        assert result.coverage_pct >= Decimal("0.95")
        assert isinstance(result.avg_price, Decimal)
        assert isinstance(result.fee_usdc, Decimal)
        assert isinstance(result.accumulated_cost, Decimal)
        assert isinstance(result.accumulated_tokens, Decimal)

    def test_depth_walk_empty_asks_returns_none(self):
        """Empty ask list must return None (zero coverage)."""
        fee_model = FeeModel()
        config = _make_config()

        result = calculate_effective_leg(
            asks=[],
            notional_usdc=config.trade_notional_usdc,
            fee_model=fee_model,
            fee_params=config.fee_params,
            min_order_size=config.min_order_size,
            tick_size=config.tick_size,
        )

        assert result is None


# ---------------------------------------------------------------------------
# evaluate_opportunity
# ---------------------------------------------------------------------------


class TestEvaluateOpportunity:

    def test_edge_net_positive_triggers_signal(self):
        """Large spread (combined ask < $1) must produce an ArbitrageSignal.

        YES at 0.40, NO at 0.40 → combined = $0.80 < $1.00.
        Gross edge per $100 notional = $20; after fees & risk still very positive.
        """
        fee_model = FeeModel()
        config = _make_config(
            fee_params=FeeParams(
                fees_enabled=True, taker_fee_rate=0.001, maker_fee_rate=0.001
            ),
            min_net_edge_usd=Decimal("0.01"),
            min_net_edge_pct=Decimal("0.0001"),
        )
        snap = _snapshot(
            yes_asks=_levels((0.40, 300)),
            no_asks=_levels((0.40, 300)),
        )

        result = evaluate_opportunity(snap, config, fee_model)

        assert result is not None
        assert isinstance(result, ArbitrageSignal)
        assert result.edge_net > Decimal("0")
        assert result.market_id == "test_market"

    def test_edge_net_negative_returns_none(self):
        """Combined price > $1 (no gross profit) must return None.

        YES at 0.55, NO at 0.55 → combined = $1.10 → edge_gross < 0.
        """
        fee_model = FeeModel()
        config = _make_config(min_net_edge_usd=Decimal("0.01"))
        snap = _snapshot(
            yes_asks=_levels((0.55, 300)),
            no_asks=_levels((0.55, 300)),
        )

        result = evaluate_opportunity(snap, config, fee_model)

        assert result is None

    def test_signal_below_min_edge_usd_returns_none(self):
        """Edge that clears gross but fails min_net_edge_usd must be filtered."""
        fee_model = FeeModel()
        config = _make_config(
            fee_params=FeeParams(
                fees_enabled=True, taker_fee_rate=0.01, maker_fee_rate=0.001
            ),
            min_net_edge_usd=Decimal("100"),  # unreachably high threshold
        )
        snap = _snapshot(
            yes_asks=_levels((0.499, 300)),
            no_asks=_levels((0.499, 300)),
        )

        result = evaluate_opportunity(snap, config, fee_model)

        assert result is None

    def test_returns_none_when_yes_leg_has_insufficient_liquidity(self):
        """Must return None when the YES leg cannot achieve 95 % fill."""
        fee_model = FeeModel()
        config = _make_config()
        snap = _snapshot(
            yes_asks=_levels((0.40, 10)),  # 10 × 0.40 = 4 USDC — only 4 % fill
            no_asks=_levels((0.40, 300)),  # plenty of NO liquidity
        )

        result = evaluate_opportunity(snap, config, fee_model)

        assert result is None

    def test_returns_none_when_no_leg_has_insufficient_liquidity(self):
        """Must return None when the NO leg cannot achieve 95 % fill."""
        fee_model = FeeModel()
        config = _make_config()
        snap = _snapshot(
            yes_asks=_levels((0.40, 300)),  # plenty of YES liquidity
            no_asks=_levels((0.40, 10)),   # 10 × 0.40 = 4 USDC — only 4 % fill
        )

        result = evaluate_opportunity(snap, config, fee_model)

        assert result is None

    def test_no_float_in_calculations(self):
        """All monetary fields in ArbitrageSignal must be Decimal — no floats leaked.

        Verifies the Decimal-only contract described in the module docstring.
        """
        fee_model = FeeModel()
        config = _make_config(
            fee_params=FeeParams(
                fees_enabled=True, taker_fee_rate=0.005, maker_fee_rate=0.001
            ),
        )
        # YES + NO at 0.45 → $0.90 combined → $10 gross edge on $100 notional
        snap = _snapshot(
            yes_asks=_levels((0.45, 300)),
            no_asks=_levels((0.45, 300)),
            market_id="decimal_check",
        )

        result = evaluate_opportunity(snap, config, fee_model)

        assert result is not None, (
            "Expected a positive ArbitrageSignal — check edge thresholds in config"
        )

        # Top-level signal fields
        assert isinstance(result.edge_gross, Decimal), "edge_gross must be Decimal"
        assert isinstance(result.fees_total, Decimal), "fees_total must be Decimal"
        assert isinstance(result.risk_haircut, Decimal), "risk_haircut must be Decimal"
        assert isinstance(result.edge_net, Decimal), "edge_net must be Decimal"
        assert isinstance(result.edge_net_pct, Decimal), "edge_net_pct must be Decimal"
        assert isinstance(result.trade_notional_usdc, Decimal), (
            "trade_notional_usdc must be Decimal"
        )

        # YES leg fields
        assert isinstance(result.yes_quote.avg_price, Decimal), (
            "yes_quote.avg_price must be Decimal"
        )
        assert isinstance(result.yes_quote.fee_usdc, Decimal), (
            "yes_quote.fee_usdc must be Decimal"
        )
        assert isinstance(result.yes_quote.accumulated_cost, Decimal), (
            "yes_quote.accumulated_cost must be Decimal"
        )
        assert isinstance(result.yes_quote.accumulated_tokens, Decimal), (
            "yes_quote.accumulated_tokens must be Decimal"
        )
        assert isinstance(result.yes_quote.coverage_pct, Decimal), (
            "yes_quote.coverage_pct must be Decimal"
        )

        # NO leg fields
        assert isinstance(result.no_quote.avg_price, Decimal), (
            "no_quote.avg_price must be Decimal"
        )
        assert isinstance(result.no_quote.fee_usdc, Decimal), (
            "no_quote.fee_usdc must be Decimal"
        )
        assert isinstance(result.no_quote.accumulated_cost, Decimal), (
            "no_quote.accumulated_cost must be Decimal"
        )
        assert isinstance(result.no_quote.accumulated_tokens, Decimal), (
            "no_quote.accumulated_tokens must be Decimal"
        )
        assert isinstance(result.no_quote.coverage_pct, Decimal), (
            "no_quote.coverage_pct must be Decimal"
        )
