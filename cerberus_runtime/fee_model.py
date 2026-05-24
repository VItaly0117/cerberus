"""
Fee model for the Cerberus trading runtime.

All monetary arithmetic uses Decimal.  The only float→Decimal conversion
happens at the boundary where FeeParams fields (sourced from market data)
are ingested.
"""
from __future__ import annotations

import logging
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from cerberus_runtime.models import FeeParams

logger = logging.getLogger(__name__)

# Six-decimal-place precision used for all returned fee values.
_FEE_PRECISION = Decimal("0.000001")


class FeeModel:
    """Computes trading fees from live market fee parameters.

    The MVP treats every order as a taker order.  ``calculate_fee`` accepts
    the ``order_side`` argument for forward-compatibility but always applies
    the taker rate.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def calculate_fee(
        self,
        notional_usdc: Decimal,
        fee_params: Optional[FeeParams],
        order_side: str,
    ) -> Decimal:
        """Calculate the taker fee for an order.

        Args:
            notional_usdc: Trade notional in USDC as a ``Decimal``.
            fee_params:    Per-market fee config sourced from market data.
                           Passing ``None`` is treated the same as
                           ``fees_enabled=False``.
            order_side:    ``"taker"`` or ``"maker"`` (only taker used in MVP).

        Returns:
            Fee in USDC rounded to 6 decimal places.
            Returns ``Decimal("0")`` when ``fee_params`` is ``None`` or
            ``fees_enabled`` is ``False``.

        Raises:
            ValueError: When ``fee_params.taker_fee_rate <= 0`` — this
                        indicates corrupt or missing market data.
        """
        if fee_params is None or not fee_params.fees_enabled:
            return Decimal("0")

        # Convert float field from FeeParams (market-data boundary) to Decimal.
        # Using str() avoids floating-point representation artefacts.
        taker_rate: Decimal = Decimal(str(fee_params.taker_fee_rate))

        if taker_rate <= Decimal("0"):
            raise ValueError("invalid fee rate from market data")

        fee = notional_usdc * taker_rate
        return fee.quantize(_FEE_PRECISION, rounding=ROUND_HALF_UP)

    def conservative_fallback_fee(self, notional_usdc: Decimal) -> Decimal:
        """Conservative 1 % fallback fee.

        **Used only when fee_params unavailable.**  Logs a WARNING each time
        this path is taken so the issue can be investigated.

        Args:
            notional_usdc: Trade notional in USDC as a ``Decimal``.

        Returns:
            ``notional_usdc * Decimal("0.01")`` (1 % of notional).
        """
        logger.warning(
            "FeeModel.conservative_fallback_fee: using 1%% fallback "
            "(fee_params unavailable) for notional=%s USDC",
            notional_usdc,
        )
        return notional_usdc * Decimal("0.01")
