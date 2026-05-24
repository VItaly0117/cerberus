"""
Cerberus runtime executor — sequential FOK/FAK order execution with emergency repair.

Critical design rules
---------------------
1. Legs execute SEQUENTIALLY.  Never ``asyncio.gather`` on two order sends.
2. Leg 1 is always FOK.  If not fully filled → CLEAN_MISS, stop, do nothing else.
3. Leg 2 is FOK first; if partial/unfilled → emergency_repair.
4. ``_emergency_repair`` sells Leg 1 (YES) back at market to limit loss.
5. Re-quote both legs immediately before sending Leg 1 (fresh snapshot check).
6. In DRY_RUN_MODE: simulate everything, never call CLOB API.

Ownership (Agent B, Tag: [CERBERUS-STRATEGY-UPDATE])
-----------------------------------------------------
This file owns ``cerberus_runtime/executor.py`` only.
No imports from risk.py (RiskManager is injected, not imported directly).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Dict, List, Literal, Optional

from cerberus_runtime.config import AppConfig
from cerberus_runtime.core import calculate_effective_leg
from cerberus_runtime.fee_model import FeeModel
from cerberus_runtime.models import (
    ArbitrageSignal,
    OrderBookSnapshot,
    PriceLevel,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Execution result enum
# ---------------------------------------------------------------------------


class ArbitrageResult(Enum):
    """Outcome of a single :meth:`Executor.execute_pair` call."""

    SUCCESS = "SUCCESS"
    """Both legs filled; net profit locked in."""

    CLEAN_MISS = "CLEAN_MISS"
    """Leg 1 FOK failed; no position taken, no loss."""

    LEGGED_RISK = "LEGGED_RISK"
    """Leg 2 failed; emergency repair sold Leg 1 back at a loss."""

    REPAIRED = "REPAIRED"
    """Leg 2 failed; emergency repair closed Leg 1 at break-even or better."""

    BLOCKED_BY_RISK = "BLOCKED_BY_RISK"
    """Edge degraded below threshold on fresh re-quote; no orders sent."""

    DRY_RUN_SIMULATED = "DRY_RUN_SIMULATED"
    """Dry-run completed; returned from the simulation path."""


# ---------------------------------------------------------------------------
# Order result dataclass — all monetary fields are Decimal (no float)
# ---------------------------------------------------------------------------


@dataclass
class OrderResult:
    """Result of sending a single order leg (live or simulated).

    Attributes:
        order_id:    UUID string in dry-run; exchange order ID in live.
        status:      ``"FILLED"``, ``"PARTIAL"``, or ``"NOT_FILLED"``.
        filled_size: Tokens actually filled (``Decimal``).
        fill_price:  Volume-weighted average fill price in USDC (``Decimal``).
        fee_usdc:    Fee charged for this fill in USDC (``Decimal``).
    """

    order_id: str
    status: Literal["FILLED", "PARTIAL", "NOT_FILLED"]
    filled_size: Decimal
    fill_price: Decimal
    fee_usdc: Decimal


# ---------------------------------------------------------------------------
# Storage interface — concrete subclasses provide persistence
# ---------------------------------------------------------------------------


class CerberusStorage:
    """Abstract storage interface for executor persistence operations.

    Concrete implementations (SQLite, in-memory mock) must subclass this and
    implement all three async methods.
    """

    async def insert_order(
        self,
        market_id: str,
        leg: str,
        order_params: Dict,
        status: str,
    ) -> str:
        """Persist a sent order and return its order_id."""
        raise NotImplementedError  # pragma: no cover

    async def insert_fill(self, order_result: OrderResult) -> None:
        """Persist a completed fill record."""
        raise NotImplementedError  # pragma: no cover

    async def insert_legged_event(
        self,
        market_id: str,
        leg1_order_id: str,
        leg2_order_id: str,
        leg1_filled: Decimal,
        leg2_filled: Decimal,
        repair_action: str,
        repair_loss_usdc: Decimal,
    ) -> None:
        """Persist a legged-risk event produced by emergency repair."""
        raise NotImplementedError  # pragma: no cover


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class Executor:
    """Sequential FOK/FAK executor for Cerberus arbitrage signals.

    Legs **always** execute sequentially.  ``asyncio.gather`` is **never** used
    for order sends.

    Args:
        config:               :class:`~cerberus_runtime.core.AppConfig` with
                              notional, edge thresholds, and fee parameters.
        storage:              :class:`CerberusStorage` implementation for order
                              and fill persistence.
        dry_run_mode:         When ``True`` (default) — simulate all fills
                              without touching the CLOB API.
        aggressive_fill_cap:  Maximum fraction of the remaining Leg 2 quantity
                              to fill in FAK mode (live path only).
    """

    def __init__(
        self,
        config: AppConfig,
        storage: CerberusStorage,
        dry_run_mode: bool = True,
        aggressive_fill_cap: Decimal = Decimal("0.5"),
    ) -> None:
        self._config = config
        self._storage = storage
        self._dry_run_mode = dry_run_mode
        self._aggressive_fill_cap = aggressive_fill_cap
        self._fee_model = FeeModel()

        # _py_clob_client: initialized only when allow_live_mode=True.
        # In dry_run_mode: always None — no CLOB API calls ever made.
        if dry_run_mode:
            self._client = None
        else:
            # Live-mode stub: real py_clob_client initialization goes here.
            # MVP: remains None until CLOB integration is complete.
            self._client = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute_pair(
        self,
        signal: ArbitrageSignal,
        fresh_snapshot: OrderBookSnapshot,
    ) -> ArbitrageResult:
        """Execute a two-legged arbitrage: YES FOK → NO FOK (→ repair if needed).

        Legs execute **sequentially** — ``await _send_order`` is called for
        Leg 1, its result is checked, then (only if FILLED) ``await _send_order``
        is called for Leg 2.  No concurrent sends.

        Args:
            signal:          Confirmed arbitrage signal from the watcher.
            fresh_snapshot:  Live order-book snapshot captured just before
                             execution.

        Returns:
            :class:`ArbitrageResult` enum value indicating the execution outcome.
        """
        # ------------------------------------------------------------------
        # Step 0: Re-quote check — abort if edge has degraded too much
        # ------------------------------------------------------------------
        fresh_edge: Optional[Decimal] = self._evaluate_fresh_edge(fresh_snapshot)
        threshold: Decimal = self._config.min_net_edge_usd * Decimal("0.8")

        if fresh_edge is None or fresh_edge < threshold:
            logger.info(
                "execute_pair[%s]: edge degraded (fresh=%s, threshold=%s) "
                "→ BLOCKED_BY_RISK",
                signal.market_id,
                fresh_edge,
                threshold,
            )
            return ArbitrageResult.BLOCKED_BY_RISK

        market_id: str = signal.market_id

        # ------------------------------------------------------------------
        # Step 1: Leg 1 — YES token FOK
        #         Sequential: await result before proceeding to Leg 2.
        # ------------------------------------------------------------------
        yes_size: Decimal = (
            signal.yes_quote.accumulated_cost / signal.yes_quote.avg_price
        )
        order_params_1: Dict = {
            "token_id": fresh_snapshot.yes_token_id or (market_id + "_YES"),
            "price": signal.yes_quote.avg_price,
            "size": yes_size,
            "side": "BUY",
            "order_type": "FOK",
        }

        if self._dry_run_mode:
            leg1_result: OrderResult = self._simulate_fill(
                order_params_1, fresh_snapshot.yes_asks
            )
        else:
            leg1_result = await self._send_order(order_params_1)  # sequential ①

        await self._storage.insert_order(
            market_id, "YES", order_params_1, leg1_result.status
        )

        if leg1_result.status != "FILLED":
            logger.info(
                "execute_pair[%s]: Leg 1 %s → CLEAN_MISS (Leg 2 never attempted)",
                market_id,
                leg1_result.status,
            )
            return ArbitrageResult.CLEAN_MISS

        # ------------------------------------------------------------------
        # Step 2: Leg 2 — NO token FOK (then FAK in live path for remainder)
        #         Only reached after Leg 1 FILLED. Still sequential.
        # ------------------------------------------------------------------
        no_size: Decimal = (
            signal.no_quote.accumulated_cost / signal.no_quote.avg_price
        )
        order_params_2: Dict = {
            "token_id": fresh_snapshot.no_token_id or (market_id + "_NO"),
            "price": signal.no_quote.avg_price,
            "size": no_size,
            "side": "BUY",
            "order_type": "FOK",
        }

        if self._dry_run_mode:
            leg2_result: OrderResult = self._simulate_fill(
                order_params_2, fresh_snapshot.no_asks
            )
        else:
            leg2_result = await self._send_order(order_params_2)  # sequential ②

        await self._storage.insert_order(
            market_id, "NO", order_params_2, leg2_result.status
        )

        if leg2_result.status == "FILLED":
            await self._storage.insert_fill(leg1_result)
            await self._storage.insert_fill(leg2_result)
            logger.info("execute_pair[%s]: both legs filled → SUCCESS", market_id)
            return ArbitrageResult.SUCCESS

        # Leg 2 partial or not filled → emergency repair
        logger.warning(
            "execute_pair[%s]: Leg 2 %s → initiating emergency repair",
            market_id,
            leg2_result.status,
        )
        return await self._emergency_repair(leg1_result, leg2_result, fresh_snapshot)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _evaluate_fresh_edge(
        self, snapshot: OrderBookSnapshot
    ) -> Optional[Decimal]:
        """Re-compute net edge from a fresh snapshot using core depth-walk logic.

        Mirrors the arithmetic in :func:`~cerberus_runtime.core.evaluate_opportunity`
        without the threshold gates (the caller applies the 80 % gate).

        Returns:
            Net edge in USDC as a :class:`~decimal.Decimal`, or ``None`` when
            either leg lacks sufficient depth (< 95 % coverage).
        """
        yes_quote = calculate_effective_leg(
            asks=snapshot.yes_asks,
            notional_usdc=self._config.trade_notional_usdc,
            fee_model=self._fee_model,
            fee_params=self._config.fee_params,
            min_order_size=self._config.min_order_size,
            tick_size=self._config.tick_size,
        )
        no_quote = calculate_effective_leg(
            asks=snapshot.no_asks,
            notional_usdc=self._config.trade_notional_usdc,
            fee_model=self._fee_model,
            fee_params=self._config.fee_params,
            min_order_size=self._config.min_order_size,
            tick_size=self._config.tick_size,
        )

        if yes_quote is None or no_quote is None:
            return None

        total_cost: Decimal = (
            yes_quote.avg_price + no_quote.avg_price
        ) * self._config.trade_notional_usdc

        edge_gross: Decimal = (
            Decimal("1") * self._config.trade_notional_usdc - total_cost
        )
        fees_total: Decimal = yes_quote.fee_usdc + no_quote.fee_usdc
        risk_haircut: Decimal = self._config.trade_notional_usdc * (
            self._config.slippage_buffer_pct + self._config.legged_risk_buffer_pct
        )
        return edge_gross - fees_total - risk_haircut

    async def _emergency_repair(
        self,
        leg1_result: OrderResult,
        leg2_result: OrderResult,
        snapshot: OrderBookSnapshot,
    ) -> ArbitrageResult:
        """Attempt to sell Leg 1 YES tokens back at market to cap the loss.

        Strategy
        --------
        * Dry-run: simulate a sell at ``best_ask − 0.5 %`` slippage.
        * Live:    send an aggressive market sell for the full Leg 1 filled size.

        Records a legged-risk event in storage regardless of outcome.

        Returns:
            :class:`ArbitrageResult.LEGGED_RISK` when repair incurs a loss;
            :class:`ArbitrageResult.REPAIRED` when the position is closed at
            break-even or better.
        """
        leg1_cost: Decimal = leg1_result.filled_size * leg1_result.fill_price

        if self._dry_run_mode:
            # Use best YES ask as the market-price proxy; apply 0.5 % slippage.
            best_price: Decimal = (
                snapshot.yes_asks[0].price
                if snapshot.yes_asks
                else leg1_result.fill_price
            )
            sell_price: Decimal = best_price * Decimal("0.995")
        else:
            # Live: aggressive market sell — model slippage conservatively.
            sell_price = leg1_result.fill_price * Decimal("0.995")

        sell_proceeds: Decimal = leg1_result.filled_size * sell_price
        repair_loss: Decimal = leg1_cost - sell_proceeds

        await self._storage.insert_legged_event(
            market_id=snapshot.market_id,
            leg1_order_id=leg1_result.order_id,
            leg2_order_id=leg2_result.order_id,
            leg1_filled=leg1_result.filled_size,
            leg2_filled=leg2_result.filled_size,
            repair_action="sell_leg1",
            repair_loss_usdc=repair_loss,
        )

        if repair_loss > Decimal("0"):
            logger.warning(
                "_emergency_repair[%s]: repair_loss=%s USDC → LEGGED_RISK",
                snapshot.market_id,
                repair_loss,
            )
            return ArbitrageResult.LEGGED_RISK

        logger.info(
            "_emergency_repair[%s]: closed at no loss → REPAIRED",
            snapshot.market_id,
        )
        return ArbitrageResult.REPAIRED

    async def _send_order(self, order_params: Dict) -> OrderResult:
        """Send a live order to the CLOB.

        Called **only** in non-dry-run mode and **always** awaited sequentially
        (never gathered).  Raises :exc:`RuntimeError` if accidentally called in
        dry-run mode.

        Raises:
            RuntimeError:       If called in dry-run mode (programming error).
            NotImplementedError: Until py_clob_client integration is complete.
        """
        if self._dry_run_mode:
            raise RuntimeError(
                "_send_order called in dry_run_mode — this is a bug in executor"
            )
        raise NotImplementedError(
            "Live CLOB integration not yet implemented; use dry_run_mode=True"
        )

    def _simulate_fill(
        self,
        order_params: Dict,
        asks: List[PriceLevel],
    ) -> OrderResult:
        """Simulate a FOK fill by walking ask levels cheapest-first.

        All arithmetic uses :class:`~decimal.Decimal` — no floats.

        Algorithm
        ---------
        1. Iterate asks from cheapest to most expensive.
        2. Skip levels where ``ask.price > limit_price`` (FOK price constraint).
        3. Accumulate fills until ``order_size`` is reached or asks exhausted.
        4. Return ``FILLED`` when fully filled, ``PARTIAL`` when partially,
           ``NOT_FILLED`` when no fill was possible.

        Args:
            order_params: Dict with at least:
                ``"price"`` — limit price (``Decimal``);
                ``"size"``  — token quantity to buy (``Decimal``).
            asks:         Ask price levels, sorted cheapest-first.

        Returns:
            :class:`OrderResult` with all monetary fields as ``Decimal``.
        """
        limit_price: Decimal = order_params["price"]
        order_size: Decimal = order_params["size"]

        fill_size: Decimal = Decimal("0")
        fill_cost: Decimal = Decimal("0")  # USDC spent

        for level in asks:
            if level.price > limit_price:
                break  # FOK: no fill above the limit price

            remaining_tokens: Decimal = order_size - fill_size
            if remaining_tokens <= Decimal("0"):
                break

            qty: Decimal = min(level.size, remaining_tokens)
            fill_size += qty
            fill_cost += qty * level.price

            if fill_size >= order_size:
                break

        # No fill at all
        if fill_size <= Decimal("0"):
            return OrderResult(
                order_id=str(uuid.uuid4()),
                status="NOT_FILLED",
                filled_size=Decimal("0"),
                fill_price=Decimal("0"),
                fee_usdc=Decimal("0"),
            )

        avg_fill_price: Decimal = fill_cost / fill_size
        fee_usdc: Decimal = self._fee_model.calculate_fee(
            fill_cost, self._config.fee_params, "taker"
        )

        status: Literal["FILLED", "PARTIAL", "NOT_FILLED"] = (
            "FILLED" if fill_size >= order_size else "PARTIAL"
        )

        return OrderResult(
            order_id=str(uuid.uuid4()),
            status=status,
            filled_size=fill_size,
            fill_price=avg_fill_price,
            fee_usdc=fee_usdc,
        )
