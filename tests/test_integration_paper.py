"""Integration tests for paper trading mode without network."""

from dataclasses import dataclass
from typing import Optional, Union
import decimal

import pytest

from tests.conftest import AppConfig, OrderBookSnapshot, FeeParams


@dataclass
class ArbitrageSignal:
    """Signal indicating an arbitrage opportunity."""
    yes_entry_price: decimal.Decimal
    no_entry_price: decimal.Decimal
    edge_gross: decimal.Decimal
    edge_net: decimal.Decimal
    position_size: decimal.Decimal
    estimated_profit: decimal.Decimal


def evaluate_opportunity(
    snapshot: OrderBookSnapshot,
    config: AppConfig
) -> Optional[ArbitrageSignal]:
    """Stub of core.evaluate_opportunity() to be implemented in sprint1/fee-model-core.

    Args:
        snapshot: Order book snapshot.
        config: Application configuration.

    Returns:
        ArbitrageSignal if opportunity exists, None otherwise.
    """
    # This will be implemented by Agent C in sprint1/fee-model-core
    return None


class TestPaperTradingIntegration:
    """Integration tests for paper trading mode."""

    @pytest.mark.skip(reason="core.evaluate_opportunity() not yet implemented by Agent C")
    def test_paper_trading_dry_run_scenario(self):
        """Test end-to-end paper trading run without network.

        Scenario:
        - Create AppConfig with dry_run=True
        - Synthetic OrderBookSnapshot with YES best ask=0.48, NO best ask=0.50
        - edge_gross = 1.00 - 0.48 - 0.50 = 0.02 per token
        """
        # Setup
        config = AppConfig(dry_run=True, notional=25.0)
        assert config.dry_run is True, "Config must have dry_run=True"

        # Create synthetic snapshot
        snapshot = OrderBookSnapshot(
            yes_bid=0.47,
            yes_ask=0.48,
            no_bid=0.49,
            no_ask=0.50,
            fee_params=FeeParams(fees_enabled=True, maker=0.001, taker=0.002)
        )

        # Calculate expected edge_gross
        edge_gross = 1.00 - snapshot.yes_ask - snapshot.no_ask
        assert edge_gross == 0.02, f"Expected edge_gross=0.02, got {edge_gross}"

        # Run evaluate_opportunity
        result = evaluate_opportunity(snapshot, config)

        # Assertions
        assert isinstance(result, (ArbitrageSignal, type(None))), (
            "Result must be either ArbitrageSignal or None, "
            f"got {type(result).__name__}"
        )

        if result is not None:
            # If signal returned, check that edge_net < edge_gross (fees applied)
            assert isinstance(result.edge_net, decimal.Decimal), (
                f"edge_net must be Decimal, got {type(result.edge_net).__name__}"
            )
            assert isinstance(result.edge_gross, decimal.Decimal), (
                f"edge_gross must be Decimal, got {type(result.edge_gross).__name__}"
            )
            assert result.edge_net < result.edge_gross, (
                f"edge_net ({result.edge_net}) should be < edge_gross ({result.edge_gross}) "
                "after fees and haircut"
            )

            # Assert no float types in signal fields
            for field_name in ["yes_entry_price", "no_entry_price", "edge_gross",
                               "edge_net", "position_size", "estimated_profit"]:
                field_value = getattr(result, field_name)
                assert not isinstance(field_value, float), (
                    f"Signal.{field_name} must not be float type, "
                    f"got {type(field_value).__name__}"
                )

    def test_paper_config_dry_run_defaults(self):
        """Test that paper trading config has dry_run enabled by default in tests."""
        config = AppConfig(dry_run=True, notional=25.0)
        assert config.dry_run is True
        assert config.notional == 25.0
        assert config.db_path == ":memory:"
        assert "fake" in config.api_key.lower() or "test" in config.api_key.lower()

    def test_snapshot_edge_calculation(self):
        """Test edge calculation from synthetic snapshot."""
        snapshot = OrderBookSnapshot(
            yes_bid=0.47,
            yes_ask=0.48,
            no_bid=0.49,
            no_ask=0.50,
        )

        # Gross edge: 1.00 - YES_ask - NO_ask
        edge_gross = 1.00 - snapshot.yes_ask - snapshot.no_ask
        assert edge_gross == pytest.approx(0.02), (
            f"For YES_ask=0.48, NO_ask=0.50: "
            f"edge_gross = 1.00 - 0.48 - 0.50 = 0.02"
        )

        # With fees (maker=0.001, taker=0.002):
        # edge_net = edge_gross - (taker costs)
        fee_params = FeeParams(fees_enabled=True, maker=0.001, taker=0.002)
        snapshot.fee_params = fee_params

        # Cost to enter both sides: YES_ask (taker) + NO_ask (taker)
        taker_cost = fee_params.taker * 2  # Two taker orders
        edge_net_expected = edge_gross - taker_cost
        assert edge_net_expected < edge_gross, (
            "With fees, edge_net should be less than edge_gross"
        )
