"""
Cerberus runtime configuration — single source of truth for all config.

Two classes:
  Config    — infrastructure (API URLs, DB path, timeouts)
  AppConfig — trading + risk parameters (Decimal-based)

Both core.py and risk.py import AppConfig from here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from cerberus_runtime.models import FeeParams


# ---------------------------------------------------------------------------
# Infrastructure / environment config
# ---------------------------------------------------------------------------


@dataclass
class Config:
    """Central infrastructure configuration for all Cerberus runtime components."""

    gamma_host: str = "https://gamma-api.polymarket.com"
    """Base URL for the Gamma REST API."""

    rescan_interval: int = 300
    """Seconds between market-discovery scan cycles (default 5 min)."""

    request_timeout: float = 10.0
    """HTTP request timeout in seconds."""

    db_path: str = ":memory:"
    """SQLite database path; ':memory:' for in-process testing."""

    clob_rest_url: str = "https://clob.polymarket.com"
    """Base URL for the CLOB REST API (order placement, REST resync)."""

    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    """Polymarket CLOB WebSocket endpoint."""

    max_open_markets: int = 1
    """Maximum number of markets watched simultaneously by the Watcher."""

    book_max_age_ms: int = 5_000
    """Maximum acceptable order-book age in milliseconds (Watcher freshness gate)."""


def get_config() -> Config:
    """Build a Config from environment variables with sensible defaults."""
    return Config(
        gamma_host=os.getenv("GAMMA_HOST", "https://gamma-api.polymarket.com"),
        rescan_interval=int(os.getenv("RESCAN_INTERVAL", "300")),
        request_timeout=float(os.getenv("REQUEST_TIMEOUT", "10")),
        db_path=os.getenv("DB_PATH", "cerberus.db"),
        clob_rest_url=os.getenv("CLOB_REST_URL", "https://clob.polymarket.com"),
        ws_url=os.getenv(
            "WS_URL",
            "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        ),
        max_open_markets=int(os.getenv("MAX_OPEN_MARKETS", "1")),
        book_max_age_ms=int(os.getenv("BOOK_MAX_AGE_MS", "5000")),
    )


# ---------------------------------------------------------------------------
# Unified trading + risk AppConfig — canonical home for all strategy params
# ---------------------------------------------------------------------------


@dataclass
class AppConfig:
    """Unified trading + risk configuration consumed by core, risk, and executor.

    All monetary / percentage fields are ``Decimal``.  Fields that previously
    lived in ``core.AppConfig`` and ``risk.AppConfig`` are merged here with
    sensible paper-trading defaults so callers only need to override what they
    care about.

    Trading parameters (core.py / executor.py):
        trade_notional_usdc:    Maximum USDC to deploy per leg.
        slippage_buffer_pct:    Fraction reserved for slippage uncertainty.
        legged_risk_buffer_pct: Fraction reserved for leg-execution timing risk.
        min_net_edge_usd:       Minimum acceptable net edge in USDC.
        min_net_edge_pct:       Minimum acceptable net edge as a fraction of
                                total deployed capital (2 × notional).
        min_order_size:         Minimum USDC order value; levels below this are
                                skipped during the depth walk.
        tick_size:              Minimum price increment; USDC amounts rounded
                                down to this granularity.
        fee_params:             Per-market fee configuration (may be ``None``).

    Risk parameters (risk.py / RiskManager):
        dry_run_mode:            When True, no live CLOB API calls are made.
        allow_live_mode:         Second-factor guard: must be True to permit
                                 live order placement (independent of dry_run).
        max_book_age_ms:         Maximum order-book age before RiskManager
                                 considers it stale (milliseconds).
        max_open_markets:        Maximum simultaneous in-flight positions.
        max_attempts_per_hour:   Rolling hourly execution-attempt cap.
        daily_loss_limit_usd:    Kill-switch threshold — cumulative realised
                                 loss that permanently blocks further attempts.
        market_cooldown_seconds: Quiet period after each result before the same
                                 market may be attempted again.
    """

    # ── Trading parameters ────────────────────────────────────────────────────
    trade_notional_usdc: Decimal = field(
        default_factory=lambda: Decimal("25")
    )
    slippage_buffer_pct: Decimal = field(
        default_factory=lambda: Decimal("0.001")
    )
    legged_risk_buffer_pct: Decimal = field(
        default_factory=lambda: Decimal("0.001")
    )
    min_net_edge_usd: Decimal = field(
        default_factory=lambda: Decimal("0.10")
    )
    min_net_edge_pct: Decimal = field(
        default_factory=lambda: Decimal("0.0125")
    )
    min_order_size: Decimal = field(
        default_factory=lambda: Decimal("1")
    )
    tick_size: Decimal = field(
        default_factory=lambda: Decimal("0.01")
    )
    fee_params: Optional[FeeParams] = None

    # ── Risk parameters ───────────────────────────────────────────────────────
    dry_run_mode: bool = True
    allow_live_mode: bool = False
    max_book_age_ms: int = 5_000
    max_open_markets: int = 1
    max_attempts_per_hour: int = 60
    daily_loss_limit_usd: Decimal = field(
        default_factory=lambda: Decimal("50.00")
    )
    market_cooldown_seconds: float = 10.0


def get_app_config() -> AppConfig:
    """Build an AppConfig from environment variables with paper-trading defaults."""
    return AppConfig(
        trade_notional_usdc=Decimal(os.getenv("TRADE_NOTIONAL_USDC", "25")),
        slippage_buffer_pct=Decimal(os.getenv("SLIPPAGE_BUFFER_PCT", "0.001")),
        legged_risk_buffer_pct=Decimal(
            os.getenv("LEGGED_RISK_BUFFER_PCT", "0.001")
        ),
        min_net_edge_usd=Decimal(os.getenv("MIN_NET_EDGE_USD", "0.10")),
        min_net_edge_pct=Decimal(os.getenv("MIN_NET_EDGE_PCT", "0.0125")),
        min_order_size=Decimal(os.getenv("MIN_ORDER_SIZE", "1")),
        tick_size=Decimal(os.getenv("TICK_SIZE", "0.01")),
        dry_run_mode=os.getenv("DRY_RUN_MODE", "true").lower() != "false",
        allow_live_mode=os.getenv("ALLOW_LIVE_MODE", "false").lower() == "true",
        max_book_age_ms=int(os.getenv("MAX_BOOK_AGE_MS", "5000")),
        max_open_markets=int(os.getenv("MAX_OPEN_MARKETS", "1")),
        max_attempts_per_hour=int(os.getenv("MAX_ATTEMPTS_PER_HOUR", "60")),
        daily_loss_limit_usd=Decimal(
            os.getenv("DAILY_LOSS_LIMIT_USD", "50.00")
        ),
        market_cooldown_seconds=float(
            os.getenv("MARKET_COOLDOWN_SECONDS", "10.0")
        ),
    )
