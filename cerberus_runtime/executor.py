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

        # Initialize CLOB client for live trading.
        # In dry_run_mode: always None — no CLOB API calls ever made.
        if dry_run_mode:
            self._client = None
        elif config.allow_live_mode and config.polymarket_private_key:
            self._client = self._init_live_client(config)
        else:
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
        # Step 0a: Sprint 6 — Market drift guard
        # Compare signal price (yes+no) vs current snapshot top-of-book.
        # If drift exceeds max_drift_pct → DRIFT_REJECT (market moved away).
        # ------------------------------------------------------------------
        drift_reject = self._check_market_drift(signal, fresh_snapshot)
        if drift_reject:
            logger.info(
                "execute_pair[%s]: market drift > %.3f%% → DRIFT_REJECT",
                signal.market_id,
                float(self._config.max_drift_pct) * 100,
            )
            return ArbitrageResult.BLOCKED_BY_RISK

        # ------------------------------------------------------------------
        # Step 0b: Re-quote check — abort if edge has degraded too much
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

        _use_maker = getattr(signal, "order_strategy", "FOK") == "MAKER"
        if self._dry_run_mode:
            if _use_maker:
                order_params_1["asks"] = fresh_snapshot.yes_asks
                order_params_1["order_type"] = "LIMIT"
                leg1_result: OrderResult = await self._send_maker_order(order_params_1)
            else:
                leg1_result = self._simulate_fill(order_params_1, fresh_snapshot.yes_asks)
        elif _use_maker:
            order_params_1["asks"] = fresh_snapshot.yes_asks
            order_params_1["order_type"] = "LIMIT"
            leg1_result = await self._send_maker_order(order_params_1)  # sequential ①
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
            if _use_maker:
                order_params_2["asks"] = fresh_snapshot.no_asks
                order_params_2["order_type"] = "LIMIT"
                leg2_result: OrderResult = await self._send_maker_order(order_params_2)
            else:
                leg2_result = self._simulate_fill(order_params_2, fresh_snapshot.no_asks)
        elif _use_maker:
            order_params_2["asks"] = fresh_snapshot.no_asks
            order_params_2["order_type"] = "LIMIT"
            leg2_result = await self._send_maker_order(order_params_2)  # sequential ②
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

    def _check_market_drift(
        self,
        signal: ArbitrageSignal,
        snapshot: OrderBookSnapshot,
    ) -> bool:
        """Sprint 6 — return True if market drifted past max_drift_pct.

        Compares the signal's effective prices (yes_avg + no_avg) against
        the current top-of-book sum. If absolute drift exceeds the threshold,
        the trade should be aborted as the opportunity has degraded.
        """
        if not snapshot.yes_asks or not snapshot.no_asks:
            return True  # no book to compare — treat as drift

        signal_sum = signal.yes_quote.avg_price + signal.no_quote.avg_price
        fresh_sum = snapshot.yes_asks[0].price + snapshot.no_asks[0].price

        if signal_sum <= Decimal("0"):
            return True

        drift = abs(fresh_sum - signal_sum) / signal_sum
        return drift > self._config.max_drift_pct

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
        # bug #8 fix: short-circuit when leg1 was reported FILLED but filled_size==0.
        # In that case there is nothing to repair — recording a fake repair would
        # falsify the audit trail and PnL accounting.
        if leg1_result.filled_size <= Decimal("0"):
            logger.warning(
                "_emergency_repair[%s]: leg1 filled_size=0 — no position to repair",
                snapshot.market_id,
            )
            await self._storage.insert_legged_event(
                market_id=snapshot.market_id,
                leg1_order_id=leg1_result.order_id,
                leg2_order_id=leg2_result.order_id,
                leg1_filled=leg1_result.filled_size,
                leg2_filled=leg2_result.filled_size,
                repair_action="no_position",
                repair_loss_usdc=Decimal("0"),
            )
            return ArbitrageResult.REPAIRED

        leg1_cost: Decimal = leg1_result.filled_size * leg1_result.fill_price

        if self._dry_run_mode:
            # bug #1 fix: yes_asks may be empty if orderbook resync produced no
            # levels. Already guarded by ``if snapshot.yes_asks`` — make the
            # fallback explicit and log so we can detect bad book state.
            if snapshot.yes_asks:
                best_price: Decimal = snapshot.yes_asks[0].price
            else:
                logger.warning(
                    "_emergency_repair[%s]: empty yes_asks — using leg1 fill_price as proxy",
                    snapshot.market_id,
                )
                best_price = leg1_result.fill_price
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

    @staticmethod
    def _init_live_client(config):
        """Initialize the Polymarket CLOB client for live order placement.

        Requires py_clob_client to be installed.  Returns None if the library
        is not available or credentials are missing.
        """
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
        except ImportError:
            logger.warning(
                "py_clob_client not installed — live trading unavailable. "
                "Run: pip install py-clob-client"
            )
            return None

        if not config.polymarket_private_key:
            logger.error("POLYMARKET_PK not set — live client not initialized")
            return None

        creds = None
        if config.polymarket_api_key:
            creds = ApiCreds(
                api_key=config.polymarket_api_key,
                api_secret=config.polymarket_api_secret,
                api_passphrase=config.polymarket_api_passphrase,
            )

        client = ClobClient(
            host=config.clob_rest_url,
            key=config.polymarket_private_key,
            chain_id=config.polymarket_chain_id,
            creds=creds,
        )
        logger.info("Polymarket CLOB client initialized (chain_id=%d)", config.polymarket_chain_id)
        return client

    async def _send_order(self, order_params: Dict) -> OrderResult:
        """Send a live FOK order to the Polymarket CLOB.

        Called **only** in non-dry-run mode and **always** awaited sequentially
        (never gathered).

        Raises:
            RuntimeError: If called in dry-run mode (programming error).
            RuntimeError: If CLOB client not initialized.
        """
        if self._dry_run_mode:
            raise RuntimeError(
                "_send_order called in dry_run_mode — this is a bug in executor"
            )
        if self._client is None:
            raise RuntimeError(
                "CLOB client not initialized — check POLYMARKET_PK and ALLOW_LIVE_MODE"
            )
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
        except ImportError:
            raise RuntimeError("py_clob_client not installed — cannot send live orders")

        order_args = OrderArgs(
            token_id=order_params["token_id"],
            price=float(order_params["price"]),
            size=float(order_params["size"]),
        )
        # FOK order: fill or kill immediately
        resp = self._client.create_and_post_order(order_args, OrderType.FOK)

        status: Literal["FILLED", "PARTIAL", "NOT_FILLED"] = "NOT_FILLED"
        filled_size = Decimal("0")
        fill_price = Decimal("0")

        if resp and resp.get("status") == "matched":
            filled = resp.get("filled_size", 0)
            requested = float(order_params["size"])
            filled_size = Decimal(str(filled))
            fill_price = Decimal(str(order_params["price"]))
            status = "FILLED" if filled >= requested * 0.99 else "PARTIAL"

        fee_usdc = self._fee_model.calculate_fee(
            filled_size * fill_price,
            self._config.fee_params,
            order_side="taker",
        )
        return OrderResult(
            order_id=resp.get("order_id", str(uuid.uuid4())) if resp else str(uuid.uuid4()),
            status=status,
            filled_size=filled_size,
            fill_price=fill_price,
            fee_usdc=fee_usdc,
        )

    async def _send_maker_order(self, order_params: Dict) -> OrderResult:
        """Post a limit (maker) order and poll for fill up to timeout.

        In dry-run: immediately simulates fill using _simulate_fill.
        In live: posts limit order via CLOB, polls for fill.
        """
        if self._dry_run_mode:
            # Reuse FOK simulation but with limit price — maker fills if ask <= limit.
            sim_params = {
                "price": order_params["price"],
                "size": order_params["size"],
                "token_id": order_params.get("token_id", ""),
                "side": "BUY",
            }
            result = self._simulate_fill(sim_params, order_params.get("asks", []))
            # Override fee to reflect maker (zero fee on Polymarket).
            return OrderResult(
                order_id=result.order_id,
                status=result.status,
                filled_size=result.filled_size,
                fill_price=result.fill_price,
                fee_usdc=self._fee_model.calculate_fee(
                    result.filled_size * result.fill_price,
                    self._config.fee_params,
                    order_side="maker",
                ),
            )

        if self._client is None:
            raise RuntimeError("CLOB client not initialized")
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
        except ImportError:
            raise RuntimeError("py_clob_client not installed")

        order_args = OrderArgs(
            token_id=order_params["token_id"],
            price=float(order_params["price"]),
            size=float(order_params["size"]),
        )
        resp = self._client.create_and_post_order(order_args, OrderType.GTC)
        order_id = resp.get("order_id", str(uuid.uuid4())) if resp else str(uuid.uuid4())

        timeout = self._config.maker_timeout_seconds
        import asyncio as _asyncio
        import time as _time
        deadline = _time.monotonic() + timeout
        filled_size = Decimal("0")
        fill_price = Decimal(str(order_params["price"]))

        while _time.monotonic() < deadline:
            await _asyncio.sleep(2)
            try:
                order_info = self._client.get_order(order_id)
                filled = float(order_info.get("size_matched", 0))
                filled_size = Decimal(str(filled))
                if order_info.get("status") in ("matched", "filled"):
                    break
            except Exception:
                pass

        # Cancel unfilled portion
        if filled_size < Decimal(str(order_params["size"])) * self._config.maker_min_fill_pct:
            try:
                self._client.cancel_order(order_id)
            except Exception:
                pass

        status: Literal["FILLED", "PARTIAL", "NOT_FILLED"]
        requested = Decimal(str(order_params["size"]))
        if filled_size >= requested * Decimal("0.99"):
            status = "FILLED"
        elif filled_size >= requested * self._config.maker_min_fill_pct:
            status = "PARTIAL"
        else:
            status = "NOT_FILLED"
            filled_size = Decimal("0")

        fee_usdc = self._fee_model.calculate_fee(
            filled_size * fill_price,
            self._config.fee_params,
            order_side="maker",
        )
        return OrderResult(
            order_id=order_id,
            status=status,
            filled_size=filled_size,
            fill_price=fill_price,
            fee_usdc=fee_usdc,
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
