"""Tests for CerberusStorage and database operations."""

from decimal import Decimal

import pytest
from pathlib import Path
from tests.conftest import CerberusStorage

# Real async Storage — used by the paper_signals tests below.
from cerberus_runtime.storage import Storage
from cerberus_runtime.models import ArbitrageSignal, LegQuote


class TestCerberusStorage:
    """Test suite for CerberusStorage."""

    def test_storage_initialization(self, tmp_db: CerberusStorage):
        """Test that tmp_db fixture initializes storage correctly."""
        assert tmp_db.db_path is not None
        assert tmp_db._initialized is True

    def test_storage_with_memory_db(self):
        """Test CerberusStorage with in-memory SQLite database."""
        storage = CerberusStorage(":memory:")
        storage.initialize()
        assert storage._initialized is True
        storage.close()

    def test_storage_with_file_path(self, tmp_path: Path):
        """Test CerberusStorage with file-based database."""
        db_file = tmp_path / "test.db"
        storage = CerberusStorage(str(db_file))
        storage.initialize()
        assert storage._initialized is True
        assert storage.db_path == str(db_file)
        storage.close()

    def test_storage_context_cleanup(self, tmp_path: Path):
        """Test that storage properly cleans up resources."""
        db_file = tmp_path / "test_cleanup.db"
        storage = CerberusStorage(str(db_file))
        storage.initialize()
        storage.close()
        # Verify storage was closed without error
        assert storage._initialized is True


# ---------------------------------------------------------------------------
# paper_signals — real async Storage tests
# ---------------------------------------------------------------------------


def _make_signal(market_id: str = "mkt1", edge_net_pct: str = "0.02") -> ArbitrageSignal:
    """Build a minimal ArbitrageSignal for testing."""
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
        edge_net=Decimal("4.7"),
        edge_net_pct=Decimal(edge_net_pct),
        trade_notional_usdc=Decimal("100"),
    )


async def test_paper_signal_insert_and_summary():
    """insert_paper_signal + get_paper_summary return consistent stats."""
    storage = Storage(":memory:")
    await storage.connect()

    signal = _make_signal()

    # Insert a mix of outcomes
    await storage.insert_paper_signal(signal, "SUCCESS", simulated_pnl=Decimal("4.7"))
    await storage.insert_paper_signal(None, "BLOCKED_BY_RISK", rejection_reason="stale_book")
    await storage.insert_paper_signal(None, "FILTERED", rejection_reason="edge_below_threshold")
    await storage.insert_paper_signal(signal, "CLEAN_MISS", simulated_pnl=Decimal("0"))
    await storage.insert_paper_signal(signal, "LEGGED_RISK", simulated_pnl=Decimal("-0.10"))

    summary = await storage.get_paper_summary(since_ts_ms=0)

    assert summary["total_snapshots_evaluated"] == 5
    assert summary["successes"] == 1
    assert summary["clean_misses"] == 1
    assert summary["legged_incidents"] == 1
    assert summary["blocked_by_risk"] == 1
    assert summary["stale_book_rejections"] == 1
    # total_simulated_pnl: 4.7 + 0 + 0 + 0 + (-0.10) = 4.6
    assert summary["total_simulated_pnl"] == Decimal("4.6")
    # median_edge_net_pct: only SUCCESS row → 0.02
    assert summary["median_edge_net_pct"] == Decimal("0.02")

    await storage.close()
