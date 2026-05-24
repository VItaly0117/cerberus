"""Shared test fixtures for Cerberus test suite."""

import tempfile
from pathlib import Path
from dataclasses import dataclass, field
from typing import List

import pytest


# Type stubs for classes that will be implemented by other agents
@dataclass
class PriceLevel:
    """Represents a price level in an order book."""
    price: float
    size: float
    side: str  # "bid" or "ask"


@dataclass
class FeeParams:
    """Fee parameters for trading."""
    fees_enabled: bool = True
    maker: float = 0.001
    taker: float = 0.002


@dataclass
class OrderBookSnapshot:
    """Snapshot of an order book at a point in time."""
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    yes_depth: List[PriceLevel] = field(default_factory=list)
    no_depth: List[PriceLevel] = field(default_factory=list)
    fee_params: FeeParams = field(default_factory=FeeParams)
    timestamp: float = 0.0


@dataclass
class AppConfig:
    """Application configuration for Cerberus."""
    dry_run: bool = False
    notional: float = 25.0
    exchange_name: str = "test_exchange"
    api_key: str = "fake_key"
    api_secret: str = "fake_secret"
    db_path: str = ":memory:"
    log_level: str = "INFO"


class CerberusStorage:
    """Storage layer for Cerberus using SQLite."""

    def __init__(self, db_path: str):
        """Initialize storage with SQLite database at db_path."""
        self.db_path = db_path
        self._initialized = False

    def initialize(self):
        """Initialize the database schema."""
        self._initialized = True

    def close(self):
        """Close the database connection."""
        pass


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def tmp_db(tmp_path: Path) -> CerberusStorage:
    """Create a temporary SQLite database for testing.

    Returns:
        CerberusStorage instance pointing to a temporary database file.
    """
    db_file = tmp_path / "test.db"
    storage = CerberusStorage(str(db_file))
    storage.initialize()
    yield storage
    storage.close()


@pytest.fixture
def base_config() -> AppConfig:
    """Create a base AppConfig with safe test defaults.

    Returns:
        AppConfig with dry_run=True, notional=25, and fake credentials.
    """
    return AppConfig(
        dry_run=True,
        notional=25.0,
        exchange_name="test_exchange",
        api_key="test_key_12345",
        api_secret="test_secret_abcde",
        db_path=":memory:",
        log_level="DEBUG"
    )


@pytest.fixture
def sample_price_levels() -> List[PriceLevel]:
    """Create a sample list of 8 price levels for depth walk tests.

    Returns:
        List of 8 PriceLevel objects with varying prices and sizes.
    """
    return [
        PriceLevel(price=0.52, size=100.0, side="ask"),
        PriceLevel(price=0.51, size=150.0, side="ask"),
        PriceLevel(price=0.50, size=200.0, side="ask"),
        PriceLevel(price=0.49, size=250.0, side="ask"),
        PriceLevel(price=0.48, size=100.0, side="bid"),
        PriceLevel(price=0.47, size=150.0, side="bid"),
        PriceLevel(price=0.46, size=200.0, side="bid"),
        PriceLevel(price=0.45, size=250.0, side="bid"),
    ]


@pytest.fixture
def sample_fee_params() -> FeeParams:
    """Create sample fee parameters for testing.

    Returns:
        FeeParams with fees_enabled=True, maker=0.001, taker=0.002.
    """
    return FeeParams(
        fees_enabled=True,
        maker=0.001,
        taker=0.002
    )


@pytest.fixture
def sample_snapshot(
    sample_price_levels: List[PriceLevel],
    sample_fee_params: FeeParams
) -> OrderBookSnapshot:
    """Create a sample OrderBookSnapshot for testing.

    Args:
        sample_price_levels: Price levels for the snapshot.
        sample_fee_params: Fee parameters for the snapshot.

    Returns:
        OrderBookSnapshot with synthetic bid/ask prices and fee params.
    """
    return OrderBookSnapshot(
        yes_bid=0.48,
        yes_ask=0.49,
        no_bid=0.49,
        no_ask=0.50,
        yes_depth=sample_price_levels[:4],
        no_depth=sample_price_levels[4:],
        fee_params=sample_fee_params,
        timestamp=1234567890.0
    )
