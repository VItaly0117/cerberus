"""
Core opportunity-evaluation engine for the Cerberus trading runtime.

All arithmetic uses ``Decimal``.  There are deliberately **no** ``float``
values in this module — any float that enters via external types is rejected
by the type system before it can corrupt a calculation.

Domain types live in ``cerberus_runtime.models``.
``AppConfig`` is the unified config from ``cerberus_runtime.config``.
"""
from __future__ import annotations

import logging
from decimal import ROUND_DOWN, Decimal
from typing import List, Optional

# AppConfig is the single canonical class — defined in config.py.
# Imported here so existing callers of ``from cerberus_runtime.core import AppConfig``
# continue to work without modification.
from cerberus_runtime.config import AppConfig  # noqa: F401
from cerberus_runtime.fee_model import FeeModel
from cerberus_runtime.models import (
    ArbitrageResult,
    ArbitrageSignal,
    FeeParams,
    LegQuote,
    OrderBookSnapshot,
    PriceLevel,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Depth walk
# ---------------------------------------------------------------------------


def calculate_effective_leg(
    asks: List[PriceLevel],
    notional_usdc: Decimal,
    fee_model: FeeModel,
    fee_params: Optional[FeeParams],
    min_order_size: Decimal,
    tick_size: Decimal,
) -> Optional[LegQuote]:
    """Walk the ask side of the book and compute an effective fill quote.

    Algorithm
    ---------
    1. Iterate ask levels from cheapest to most expensive.
    2. For each level::

           remaining        = notional_usdc − accumulated_cost
           size_at_level    = min(level.size × level.price, remaining)

       Skip the level if ``size_at_level < min_order_size`` (order too small
       to place) **or** if the rounded amount collapses to zero.

       Round down to ``tick_size`` granularity::

           size_at_level = floor(size_at_level / tick_size) × tick_size

       Accumulate::

           accumulated_cost   += size_at_level
           accumulated_tokens += size_at_level / level.price

    3. ``coverage_pct = accumulated_cost / notional_usdc``
    4. Return ``None`` if ``coverage_pct < 0.95`` (< 95 % of notional filled).
    5. ``avg_price = accumulated_cost / accumulated_tokens``
    6. Compute fee and wrap result in a ``LegQuote``.

    Args:
        asks:           Ask price levels sorted cheapest-first.
        notional_usdc:  Maximum USDC to spend on this leg.
        fee_model:      :class:`FeeModel` instance for fee calculation.
        fee_params:     Per-market fee parameters (may be ``None``).
        min_order_size: Minimum USDC order value; levels below this are skipped.
        tick_size:      Minimum price increment; USDC amounts rounded down to this.

    Returns:
        :class:`LegQuote` on success; ``None`` when < 95 % of notional can be
        filled.
    """
    accumulated_cost: Decimal = Decimal("0")
    accumulated_tokens: Decimal = Decimal("0")
    levels_consumed: int = 0  # Sprint 6 — count non-skipped price levels

    for level in asks:  # cheapest → most expensive
        remaining: Decimal = notional_usdc - accumulated_cost
        if remaining <= Decimal("0"):
            break

        # USDC value available at this price level
        level_usdc_value: Decimal = level.size * level.price
        size_at_level: Decimal = min(level_usdc_value, remaining)

        # Skip levels that would produce orders below the exchange minimum
        if size_at_level < min_order_size:
            continue

        # Round down to tick_size precision to avoid over-committing USDC
        size_at_level = (
            (size_at_level / tick_size).to_integral_value(rounding=ROUND_DOWN)
            * tick_size
        )
        if size_at_level <= Decimal("0"):
            continue

        accumulated_cost += size_at_level
        accumulated_tokens += size_at_level / level.price
        levels_consumed += 1

    coverage_pct: Decimal = accumulated_cost / notional_usdc

    if coverage_pct < Decimal("0.95"):
        logger.debug(
            "calculate_effective_leg: insufficient depth — "
            "coverage_pct=%.6f < 0.95 for notional=%s",
            coverage_pct,
            notional_usdc,
        )
        return None

    # bug #2 fix: guard against division by zero when accumulated_tokens
    # is 0 (can happen if all walked levels have zero size after filtering).
    if accumulated_tokens <= Decimal("0"):
        logger.debug(
            "calculate_effective_leg: accumulated_tokens=0 — cannot compute avg_price"
        )
        return None

    avg_price: Decimal = accumulated_cost / accumulated_tokens
    fee_usdc: Decimal = fee_model.calculate_fee(accumulated_cost, fee_params, "taker")

    return LegQuote(
        avg_price=avg_price,
        coverage_pct=coverage_pct,
        fee_usdc=fee_usdc,
        accumulated_cost=accumulated_cost,
        accumulated_tokens=accumulated_tokens,
    )


# ---------------------------------------------------------------------------
# Signal evaluation
# ---------------------------------------------------------------------------


def evaluate_opportunity(
    snapshot: OrderBookSnapshot,
    config: AppConfig,
    fee_model: FeeModel,
) -> Optional[ArbitrageSignal]:
    """Evaluate whether a binary market snapshot presents a net-positive arb.

    Binary-market invariant: YES + NO payouts always sum to exactly **$1.00**,
    so buying both sides below $1.00 is profitable before fees and risk.

    Algorithm
    ---------
    ::

        payout_per_pair = Decimal("1")          ← YES + NO = $1 on binary market
        yes_quote = calculate_effective_leg(yes_asks, ...)
        no_quote  = calculate_effective_leg(no_asks,  ...)

        if yes_quote is None or no_quote is None: return None

        total_cost  = (yes_quote.avg_price + no_quote.avg_price) × notional
        edge_gross  = payout_per_pair × notional − total_cost
        fees_total  = yes_quote.fee_usdc + no_quote.fee_usdc
        risk_haircut = notional × (slippage_buffer_pct + legged_risk_buffer_pct)
        edge_net    = edge_gross − fees_total − risk_haircut
        edge_net_pct = edge_net / (notional × 2)

        if edge_net < min_net_edge_usd:  return None
        if edge_net_pct < min_net_edge_pct: return None

    Args:
        snapshot:  Current order book snapshot.
        config:    :class:`AppConfig` with notional, thresholds, fee params.
        fee_model: :class:`FeeModel` instance.

    Returns:
        :class:`ArbitrageSignal` when edge exceeds all thresholds; ``None``
        otherwise.
    """
    # YES + NO = $1.00 is the fundamental binary-market payout identity
    payout_per_pair: Decimal = Decimal("1")

    yes_quote: Optional[LegQuote] = calculate_effective_leg(
        asks=snapshot.yes_asks,
        notional_usdc=config.trade_notional_usdc,
        fee_model=fee_model,
        fee_params=config.fee_params,
        min_order_size=config.min_order_size,
        tick_size=config.tick_size,
    )
    no_quote: Optional[LegQuote] = calculate_effective_leg(
        asks=snapshot.no_asks,
        notional_usdc=config.trade_notional_usdc,
        fee_model=fee_model,
        fee_params=config.fee_params,
        min_order_size=config.min_order_size,
        tick_size=config.tick_size,
    )

    if yes_quote is None or no_quote is None:
        return None

    # ── Sprint 6 — depth gate (min_levels_consumed) ──────────────────────────
    # Reject single-tick books to avoid high realised slippage on illiquid
    # markets. We count distinct non-zero levels in both asks lists.
    min_levels = getattr(config, "min_levels_consumed", 1)
    if min_levels > 1:
        yes_count = sum(1 for lvl in snapshot.yes_asks if lvl.size >= config.min_order_size)
        no_count = sum(1 for lvl in snapshot.no_asks if lvl.size >= config.min_order_size)
        if yes_count < min_levels or no_count < min_levels:
            logger.debug(
                "evaluate_opportunity[%s]: depth gate failed (yes_levels=%d, "
                "no_levels=%d, required=%d)",
                snapshot.market_id, yes_count, no_count, min_levels,
            )
            return None

    total_cost: Decimal = (
        yes_quote.avg_price + no_quote.avg_price
    ) * config.trade_notional_usdc

    edge_gross: Decimal = payout_per_pair * config.trade_notional_usdc - total_cost
    fees_total: Decimal = yes_quote.fee_usdc + no_quote.fee_usdc
    risk_haircut: Decimal = config.trade_notional_usdc * (
        config.slippage_buffer_pct + config.legged_risk_buffer_pct
    )
    edge_net: Decimal = edge_gross - fees_total - risk_haircut
    edge_net_pct: Decimal = edge_net / (config.trade_notional_usdc * Decimal("2"))

    if edge_net < config.min_net_edge_usd:
        logger.debug(
            "evaluate_opportunity: edge_net=%s < min_net_edge_usd=%s — skip",
            edge_net,
            config.min_net_edge_usd,
        )
        return None

    if edge_net_pct < config.min_net_edge_pct:
        logger.debug(
            "evaluate_opportunity: edge_net_pct=%s < min_net_edge_pct=%s — skip",
            edge_net_pct,
            config.min_net_edge_pct,
        )
        return None

    return ArbitrageSignal(
        market_id=snapshot.market_id,
        yes_quote=yes_quote,
        no_quote=no_quote,
        edge_gross=edge_gross,
        fees_total=fees_total,
        risk_haircut=risk_haircut,
        edge_net=edge_net,
        edge_net_pct=edge_net_pct,
        trade_notional_usdc=config.trade_notional_usdc,
        order_strategy=config.order_strategy,
    )


def evaluate_opportunity_maker(
    snapshot: OrderBookSnapshot,
    config,
    fee_model: FeeModel,
) -> Optional[ArbitrageSignal]:
    """Evaluate a binary market snapshot for maker-order arbitrage.

    Posts limit orders at (best_ask - maker_offset_ticks) on both legs.
    Fees are zero for maker orders; edge requirement is lower as a result.

    A maker signal is only emitted when the bid-ask spread is wide enough
    to post a limit order with positive net edge.  The signal carries
    ``order_strategy="MAKER"`` so the executor chooses the maker path.

    Args:
        snapshot: Live L2 order book snapshot for a binary market.
        config:   :class:`~cerberus_runtime.config.AppConfig` instance.
        fee_model: :class:`~cerberus_runtime.fee_model.FeeModel` instance.

    Returns:
        :class:`~cerberus_runtime.models.ArbitrageSignal` with
        ``order_strategy="MAKER"`` if an opportunity exists, else ``None``.
    """
    if not snapshot.yes_asks or not snapshot.no_asks:
        return None

    tick = config.tick_size
    offset = tick * config.maker_offset_ticks

    # Post limit one tick below best ask — we become the new best bid.
    yes_limit_price = snapshot.yes_asks[0].price - offset
    no_limit_price = snapshot.no_asks[0].price - offset

    if yes_limit_price <= Decimal("0") or no_limit_price <= Decimal("0"):
        return None

    # Depth check: require at least min_levels_consumed ask levels on each side.
    if (
        len(snapshot.yes_asks) < config.min_levels_consumed
        or len(snapshot.no_asks) < config.min_levels_consumed
    ):
        return None

    # For maker orders fees are zero — pass order_side="maker".
    yes_fee = fee_model.calculate_fee(
        config.trade_notional_usdc,
        config.fee_params,
        order_side="maker",
    )
    no_fee = fee_model.calculate_fee(
        config.trade_notional_usdc,
        config.fee_params,
        order_side="maker",
    )

    # Compute gross edge: payout is always $1.00 per pair, cost is limit prices.
    payout_per_pair = Decimal("1")
    total_cost = (yes_limit_price + no_limit_price) * config.trade_notional_usdc
    edge_gross = payout_per_pair * config.trade_notional_usdc - total_cost

    # Maker orders have no fee drag — use slippage buffer only (no legged risk
    # buffer since limit orders don't have timing risk between legs).
    risk_haircut = config.trade_notional_usdc * config.slippage_buffer_pct
    fees_total = yes_fee + no_fee
    edge_net = edge_gross - fees_total - risk_haircut
    edge_net_pct = edge_net / (config.trade_notional_usdc * Decimal("2"))

    # Use a lower threshold for maker signals (no fee drag makes smaller spreads viable).
    maker_min_edge_usd = config.min_net_edge_usd * Decimal("0.5")
    maker_min_edge_pct = config.min_net_edge_pct * Decimal("0.5")

    if edge_net < maker_min_edge_usd:
        return None
    if edge_net_pct < maker_min_edge_pct:
        return None

    # Build synthetic LegQuote objects representing the limit order intentions.
    from cerberus_runtime.models import LegQuote  # avoid circular import at top level
    yes_quote = LegQuote(
        avg_price=yes_limit_price,
        coverage_pct=Decimal("1"),
        fee_usdc=yes_fee,
        accumulated_cost=yes_limit_price * config.trade_notional_usdc,
        accumulated_tokens=config.trade_notional_usdc,
        levels_consumed=len(snapshot.yes_asks),
    )
    no_quote = LegQuote(
        avg_price=no_limit_price,
        coverage_pct=Decimal("1"),
        fee_usdc=no_fee,
        accumulated_cost=no_limit_price * config.trade_notional_usdc,
        accumulated_tokens=config.trade_notional_usdc,
        levels_consumed=len(snapshot.no_asks),
    )

    return ArbitrageSignal(
        market_id=snapshot.market_id,
        yes_quote=yes_quote,
        no_quote=no_quote,
        edge_gross=edge_gross,
        fees_total=fees_total,
        risk_haircut=risk_haircut,
        edge_net=edge_net,
        edge_net_pct=edge_net_pct,
        trade_notional_usdc=config.trade_notional_usdc,
        order_strategy="MAKER",
    )
