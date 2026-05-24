"""
Shared domain models for the Cerberus trading runtime.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass
class PriceLevel:
    """A single price level on one side of an order book."""

    price: float
    """Decimal price (e.g. 0.62 = 62 ¢)."""

    size: float
    """Quantity available at this price in USDC."""


@dataclass
class OrderBookSnapshot:
    """Point-in-time L2 snapshot for one binary market's ask side."""

    market_id: str
    """Condition ID — used as the canonical market identifier."""

    condition_id: str
    """Polymarket condition ID (hex string)."""

    yes_token_id: str
    """ERC-1155 token ID for the YES outcome."""

    no_token_id: str
    """ERC-1155 token ID for the NO outcome."""

    fee_params: Optional["FeeParams"]
    """Maker/taker fee configuration, or None if not yet known."""

    yes_asks: List[PriceLevel]
    """Ask (sell) side for YES token, sorted by price ascending."""

    no_asks: List[PriceLevel]
    """Ask (sell) side for NO token, sorted by price ascending."""

    ts_ms: int
    """Epoch milliseconds of the most-recent event that updated this book."""

    book_hash: str
    """SHA-256 integrity digest of the combined ask lists."""


@dataclass
class FeeParams:
    """Per-market fee configuration reported by Gamma API."""

    fees_enabled: bool
    maker_fee_rate: float
    taker_fee_rate: float


@dataclass
class Market:
    """A single binary prediction market eligible for Cerberus activity."""

    condition_id: str
    """Unique identifier (hex string) for the market condition."""

    yes_token_id: str
    """ERC-1155 token ID representing the YES outcome."""

    no_token_id: str
    """ERC-1155 token ID representing the NO outcome."""

    category: str
    """Market category string (e.g. 'Politics', 'Sports')."""

    fee_params: FeeParams
    """Maker/taker fee configuration for this market."""

    min_order_size: float
    """Minimum order size in USDC."""

    tick_size: float
    """Minimum price increment (e.g. 0.01)."""

    end_date: datetime
    """Scheduled close/resolution timestamp (timezone-aware UTC)."""

    volume_24h: float
    """Rolling 24-hour traded volume in USDC."""

    active: bool = True
    """Whether Gamma still considers this market active."""

    closed: bool = False
    """Whether the market has been resolved/closed."""
