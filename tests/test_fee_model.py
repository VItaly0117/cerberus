"""
Tests for cerberus_runtime.fee_model.FeeModel.

Covers:
- test_fee_uses_taker_rate_from_params
- test_fee_zero_when_fees_disabled
- test_fallback_fee_is_one_percent
- test_invalid_fee_rate_raises
"""
from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from cerberus_runtime.fee_model import FeeModel
from cerberus_runtime.models import FeeParams


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fp(*, enabled: bool = True, taker: float = 0.01, maker: float = 0.001) -> FeeParams:
    """Construct a FeeParams with sane defaults for brevity."""
    return FeeParams(fees_enabled=enabled, taker_fee_rate=taker, maker_fee_rate=maker)


# ---------------------------------------------------------------------------
# calculate_fee
# ---------------------------------------------------------------------------


class TestCalculateFee:

    def test_fee_uses_taker_rate_from_params(self):
        """Fee = notional × taker_fee_rate sourced from fee_params (never hardcoded)."""
        model = FeeModel()
        fp = _fp(enabled=True, taker=0.02)
        fee = model.calculate_fee(Decimal("100"), fp, "taker")
        # 100 × 0.02 = 2.000000
        assert fee == Decimal("2.000000")
        assert isinstance(fee, Decimal)

    def test_fee_uses_exact_rate_not_any_constant(self):
        """Fee must scale linearly with whatever rate is in fee_params."""
        model = FeeModel()
        # Use a rate that would differ from any plausible hardcoded value
        fp = _fp(enabled=True, taker=0.0073)
        fee = model.calculate_fee(Decimal("1000"), fp, "taker")
        # 1000 × 0.0073 = 7.300000
        assert fee == Decimal("7.300000")

    def test_fee_zero_when_fees_disabled(self):
        """Fee must be Decimal('0') when fees_enabled=False, regardless of rate."""
        model = FeeModel()
        fp = _fp(enabled=False, taker=0.02)
        fee = model.calculate_fee(Decimal("100"), fp, "taker")
        assert fee == Decimal("0")
        assert isinstance(fee, Decimal)

    def test_fee_zero_when_fee_params_none(self):
        """Fee must be Decimal('0') when fee_params is None."""
        model = FeeModel()
        fee = model.calculate_fee(Decimal("500"), None, "taker")
        assert fee == Decimal("0")
        assert isinstance(fee, Decimal)

    def test_invalid_fee_rate_raises(self):
        """Zero taker_fee_rate (fees enabled) must raise ValueError."""
        model = FeeModel()
        fp = _fp(enabled=True, taker=0.0)
        with pytest.raises(ValueError, match="invalid fee rate from market data"):
            model.calculate_fee(Decimal("100"), fp, "taker")

    def test_negative_fee_rate_raises(self):
        """Negative taker_fee_rate must raise ValueError."""
        model = FeeModel()
        fp = _fp(enabled=True, taker=-0.005)
        with pytest.raises(ValueError, match="invalid fee rate from market data"):
            model.calculate_fee(Decimal("100"), fp, "taker")

    def test_fee_rounded_to_6_decimal_places(self):
        """Returned Decimal must be quantized to exactly 6 decimal places."""
        model = FeeModel()
        fp = _fp(enabled=True, taker=0.003)
        fee = model.calculate_fee(Decimal("100"), fp, "taker")
        # 100 × 0.003 = 0.300000
        assert fee == Decimal("0.300000")
        # Confirm precision via the internal exponent
        assert fee.as_tuple().exponent == -6

    def test_fee_result_is_decimal_not_float(self):
        """Return type must be Decimal, not float."""
        model = FeeModel()
        fp = _fp(enabled=True, taker=0.005)
        fee = model.calculate_fee(Decimal("250"), fp, "taker")
        assert isinstance(fee, Decimal)
        assert not isinstance(fee, float)


# ---------------------------------------------------------------------------
# conservative_fallback_fee
# ---------------------------------------------------------------------------


class TestConservativeFallbackFee:

    def test_fallback_fee_is_one_percent(self):
        """Fallback must return exactly 1 % of notional."""
        model = FeeModel()
        fee = model.conservative_fallback_fee(Decimal("200"))
        assert fee == Decimal("2")  # 200 × 0.01

    def test_fallback_fee_scales_with_notional(self):
        """Fallback must scale linearly with any notional."""
        model = FeeModel()
        assert model.conservative_fallback_fee(Decimal("0")) == Decimal("0")
        assert model.conservative_fallback_fee(Decimal("1000")) == Decimal("10")
        assert model.conservative_fallback_fee(Decimal("50")) == Decimal("0.5")

    def test_fallback_emits_warning(self, caplog):
        """Fallback path must log a WARNING so operators are alerted."""
        model = FeeModel()
        with caplog.at_level(logging.WARNING, logger="cerberus_runtime.fee_model"):
            model.conservative_fallback_fee(Decimal("100"))
        assert any("fallback" in record.message.lower() for record in caplog.records)

    def test_fallback_result_is_decimal(self):
        """Return type must be Decimal."""
        model = FeeModel()
        fee = model.conservative_fallback_fee(Decimal("75"))
        assert isinstance(fee, Decimal)
