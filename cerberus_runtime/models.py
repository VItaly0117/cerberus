"""
Shared domain models for the Cerberus trading runtime.

Canonical home for ALL dataclasses.  Modules may import freely from here;
no runtime module may define its own competing versions of these types.

Decimal policy
--------------
All types used in arithmetic (PriceLevel, LegQuote, ArbitrageSignal) carry
``Decimal`` fields.  Fee-rate fields on ``FeeParams`` remain ``float`` because
they originate at the external-API boundary and are converted to ``Decimal``
exactly once, inside ``FeeModel.calculate_fee``, via ``Decimal(str(rate))``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import List, Optional


# ---------------------------------------------------------------------------
# Low-level order-book primitives  (Decimal — safe for arithmetic in core.py)
# ---------------------------------------------------------------------------


@dataclass
class PriceLevel:
    """A single resting level in a Polymarket CLOB order book.

    Attributes:
        price: Price in USDC per prediction-market token (0 < price < 1).
        size:  Available token quantity at this price level.

    Both fields are ``Decimal`` so they can participate directly in the
    depth-walk arithmetic without silent float-contamination.
    """

    price: Decimal
    size: Decimal


# ---------------------------------------------------------------------------
# Fee configuration  (float rates — external-API boundary)
# ---------------------------------------------------------------------------


@dataclass
class FeeParams:
    """Per-market fee configuration reported by the Gamma API.

    ``maker_fee_rate`` and ``taker_fee_rate`` are kept as ``float`` because
    they arrive from JSON.  Callers that do arithmetic must convert via
    ``Decimal(str(rate))`` to avoid floating-point contamination.
    """

    fees_enabled: bool
    maker_fee_rate: float
    taker_fee_rate: float


# ---------------------------------------------------------------------------
# Order-book snapshot  (extended merge of Agent B + Agent C)
# ---------------------------------------------------------------------------


@dataclass
class OrderBookSnapshot:
    """Point-in-time order book snapshot for a single binary-outcome market.

    Core fields (required by the opportunity evaluator in ``core.py``):
        market_id:  Exchange-assigned market identifier.
        yes_asks:   Ask levels for the YES token, sorted cheapest-first.
        no_asks:    Ask levels for the NO token, sorted cheapest-first.
        timestamp:  Unix timestamp of the snapshot (seconds, wall-clock only).

    Extended fields (maintained by ``LocalOrderBook`` in ``orderbook.py``):
        condition_id, yes_token_id, no_token_id, fee_params, ts_ms, book_hash
    """

    market_id: str
    yes_asks: List[PriceLevel]
    no_asks: List[PriceLevel]
    timestamp: float

    # Extended fields — optional so the core evaluator can construct
    # lightweight snapshots without knowing watcher internals.
    condition_id: str = ""
    yes_token_id: str = ""
    no_token_id: str = ""
    fee_params: Optional[FeeParams] = None
    ts_ms: int = 0
    book_hash: str = ""


# ---------------------------------------------------------------------------
# Depth-walk result  (Decimal — internal trading math)
# ---------------------------------------------------------------------------


@dataclass
class LegQuote:
    """Aggregate fill result produced by a depth walk over one order-book side.

    Attributes:
        avg_price:          Volume-weighted average fill price (USDC / token).
        coverage_pct:       Fraction of the requested notional that was filled
                            (0 ≤ coverage_pct ≤ 1).
        fee_usdc:           Fee in USDC for this leg.
        accumulated_cost:   Total USDC spent walking the book.
        accumulated_tokens: Total tokens acquired walking the book.
    """

    avg_price: Decimal
    coverage_pct: Decimal
    fee_usdc: Decimal
    accumulated_cost: Decimal
    accumulated_tokens: Decimal


# ---------------------------------------------------------------------------
# Arbitrage signal  (Decimal — result of opportunity evaluation)
# ---------------------------------------------------------------------------


@dataclass
class ArbitrageSignal:
    """A confirmed arbitrage opportunity that cleared all risk thresholds.

    Attributes:
        market_id:           Exchange-assigned market identifier.
        yes_quote:           Depth-walk result for the YES leg.
        no_quote:            Depth-walk result for the NO leg.
        edge_gross:          Raw profit before fees and risk reserve (USDC).
        fees_total:          Combined fee for YES + NO legs (USDC).
        risk_haircut:        Slippage + legged-risk reserve (USDC).
        edge_net:            Profit after all deductions (USDC).
        edge_net_pct:        Net edge as a fraction of total deployed capital.
        trade_notional_usdc: Per-leg notional used to compute this signal (USDC).
    """

    market_id: str
    yes_quote: LegQuote
    no_quote: LegQuote
    edge_gross: Decimal
    fees_total: Decimal
    risk_haircut: Decimal
    edge_net: Decimal
    edge_net_pct: Decimal
    trade_notional_usdc: Decimal


# ``ArbitrageResult`` is a forward-compatible alias — Sprint 2 will extend this.
ArbitrageResult = ArbitrageSignal


# ---------------------------------------------------------------------------
# Market discovery  (Agent B — float fields from Gamma API)
# ---------------------------------------------------------------------------


@dataclass
class Market:
    """A single binary prediction market eligible for Cerberus activity.

    Attributes:
        condition_id:   Unique identifier (hex string) for the market condition.
        yes_token_id:   ERC-1155 token ID representing the YES outcome.
        no_token_id:    ERC-1155 token ID representing the NO outcome.
        category:       Market category string (e.g. ``'Politics'``).
        fee_params:     Maker/taker fee configuration for this market.
        min_order_size: Minimum order size in USDC.
        tick_size:      Minimum price increment (e.g. 0.01).
        end_date:       Scheduled close/resolution timestamp (timezone-aware UTC).
        volume_24h:     Rolling 24-hour traded volume in USDC.
        active:         Whether Gamma still considers this market active.
        closed:         Whether the market has been resolved/closed.
    """

    condition_id: str
    yes_token_id: str
    no_token_id: str
    category: str
    fee_params: FeeParams
    min_order_size: float
    tick_size: float
    end_date: datetime
    volume_24h: float
    active: bool = True
    closed: bool = False
