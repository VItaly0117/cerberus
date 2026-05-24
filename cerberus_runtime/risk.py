"""
Cerberus Risk Manager — Sprint 2.

RiskManager is the central gate-keeper that decides whether an arbitrage
signal may be forwarded to the executor.  It maintains per-session state
in memory and writes risk incidents to persistent storage.

Ownership: Agent A  [CERBERUS-STRATEGY-UPDATE]
Touches:   cerberus_runtime/risk.py only.
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal
from enum import Enum
from typing import Optional

# AppConfig is the single canonical class — defined in config.py.
# Imported here so existing callers of ``from cerberus_runtime.risk import AppConfig``
# continue to work without modification.
from cerberus_runtime.config import AppConfig  # noqa: F401
from cerberus_runtime.models import OrderBookSnapshot

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ArbitrageResult enum
# ---------------------------------------------------------------------------


class ArbitrageResult(Enum):
    """Outcome codes for a completed arbitrage attempt round-trip.

    Used by callers to inform RiskManager of what actually happened so it
    can update cooldowns, loss accumulators, and the kill switch correctly.
    """

    SUCCESS = "SUCCESS"
    """Both legs filled; position closed profitably."""

    CLEAN_MISS = "CLEAN_MISS"
    """Signal evaporated before execution; no position taken."""

    LEGGED_RISK = "LEGGED_RISK"
    """One leg filled, other failed; net loss incurred."""

    REPAIRED = "REPAIRED"
    """Legged position partially recovered; smaller loss realised."""

    BLOCKED_BY_RISK = "BLOCKED_BY_RISK"
    """RiskManager refused the attempt before any order was placed."""

    STALE_BOOK = "STALE_BOOK"
    """Execution aborted because the order-book snapshot was stale."""


# ---------------------------------------------------------------------------
# CerberusStorage Protocol
# ---------------------------------------------------------------------------


class CerberusStorage:
    """Minimal interface required by RiskManager for risk-event persistence.

    Concrete implementations must supply an async ``insert_risk_event``
    method; everything else is optional from RiskManager's perspective.

    In tests, a lightweight in-memory mock that satisfies this interface
    can be substituted freely.
    """

    async def insert_risk_event(
        self,
        *,
        event_type: str,
        market_id: str,
        reason: str,
        detail: str,
    ) -> None:
        """Persist one risk-event row to backing storage.

        Args:
            event_type: Broad category — ``"legged_incident"`` or
                        ``"kill_switch"``.
            market_id:  ``condition_id`` of the affected market, or ``""``
                        for session-wide events.
            reason:     Machine-readable cause string (e.g. result enum name).
            detail:     Human-readable detail or numeric annotation.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# RiskManager
# ---------------------------------------------------------------------------


class RiskManager:
    """Runtime gate-keeper for arbitrage signal execution.

    All ``allows()`` logic runs synchronously so it can be called from a
    hot path without an ``await``.  Storage writes (risk-event persistence)
    are async and live inside ``record_result`` and ``trigger_kill_switch``.

    The kill switch is a latch: once set it never resets without a process
    restart.

    State is entirely in-memory; the class makes no assumptions about the
    storage layer beyond the ``CerberusStorage`` interface.
    """

    def __init__(self, config: AppConfig, storage: CerberusStorage) -> None:
        self._config = config
        self._storage = storage

        # Accumulates over the trading day; never decremented.
        self._daily_loss_usd: Decimal = Decimal("0")

        # Resets at the start of each new wall-monotonic hour.
        self._attempts_this_hour: int = 0

        # condition_ids of markets with open / in-flight positions.
        self._open_markets: set[str] = set()

        # condition_id → monotonic timestamp after which attempts are re-allowed.
        self._market_cooldowns: dict[str, float] = {}

        # Permanently blocks all new attempts once latched.
        self._kill_switch: bool = False

        # Tracks which hour we last reset ``_attempts_this_hour`` for.
        self._last_hour: int = -1

    # ------------------------------------------------------------------
    # Public gate — synchronous hot path
    # ------------------------------------------------------------------

    def allows(self, snapshot: Optional[OrderBookSnapshot]) -> tuple[bool, str]:
        """Evaluate all risk gates for an incoming signal.

        Gates are checked in a strict priority order so that more severe
        conditions (kill switch, live-mode safety) take precedence.

        Args:
            snapshot: Current order-book snapshot for the candidate market.
                      May be ``None`` — the function handles it gracefully.

        Returns:
            ``(True, "")`` when all gates pass and the attempt is permitted.
            ``(False, reason)`` with a machine-readable reason string when any
            gate rejects the attempt.  The reason string is stable across
            versions and may be persisted by callers.
        """
        cfg = self._config

        # ── 1. Kill switch ────────────────────────────────────────────────
        if self._kill_switch:
            return False, "kill_switch_active"

        # ── 2. Dry-run / live-mode safety gate ───────────────────────────
        if not cfg.dry_run_mode and not cfg.allow_live_mode:
            return False, "live_mode_not_allowed"

        # ── 3. Book freshness ─────────────────────────────────────────────
        if snapshot is None:
            return False, "stale_book"
        now_ms = int(time.time() * 1000)
        if (now_ms - snapshot.ts_ms) > cfg.max_book_age_ms:
            return False, "stale_book"

        condition_id: str = snapshot.condition_id

        # ── 4. Per-market cooldown ────────────────────────────────────────
        cooldown_until = self._market_cooldowns.get(condition_id)
        if cooldown_until is not None and time.monotonic() < cooldown_until:
            return False, "market_in_cooldown"

        # ── 5. Open-markets cap ───────────────────────────────────────────
        if (
            condition_id not in self._open_markets
            and len(self._open_markets) >= cfg.max_open_markets
        ):
            return False, "max_open_markets_reached"

        # ── 6. Hourly attempt cap ─────────────────────────────────────────
        self._refresh_hour_counter()
        if self._attempts_this_hour >= cfg.max_attempts_per_hour:
            return False, "hourly_attempt_cap"

        # ── 7. Daily loss limit ───────────────────────────────────────────
        if self._daily_loss_usd >= cfg.daily_loss_limit_usd:
            self._kill_switch = True
            return False, "daily_loss_limit_reached"

        # ── All gates passed ──────────────────────────────────────────────
        self._attempts_this_hour += 1
        self._open_markets.add(condition_id)
        logger.debug(
            "RiskManager.allows: permitted condition_id=%s attempts_this_hour=%d",
            condition_id,
            self._attempts_this_hour,
        )
        return True, ""

    # ------------------------------------------------------------------
    # Result recording — async (storage write)
    # ------------------------------------------------------------------

    async def record_result(
        self,
        condition_id: str,
        result: ArbitrageResult,
        loss_usd: Decimal = Decimal("0"),
    ) -> None:
        """Update internal state after an execution attempt completes.

        Must be called exactly once per attempt that ``allows()`` permitted,
        and also for outcomes like STALE_BOOK that may arise after allows().

        Args:
            condition_id: The market this result belongs to.
            result:       Outcome code from :class:`ArbitrageResult`.
            loss_usd:     Realised loss in USDC (positive value, default 0).
                          Only meaningful for LEGGED_RISK / REPAIRED outcomes.
        """
        cfg = self._config

        # Always remove from in-flight set.
        self._open_markets.discard(condition_id)

        now = time.monotonic()

        if result in (ArbitrageResult.SUCCESS, ArbitrageResult.CLEAN_MISS):
            # Standard cooldown — market behaved normally.
            self._market_cooldowns[condition_id] = now + cfg.market_cooldown_seconds
            logger.info(
                "RiskManager: %s on %s — cooldown %.1fs",
                result.name,
                condition_id,
                cfg.market_cooldown_seconds,
            )

        elif result in (ArbitrageResult.LEGGED_RISK, ArbitrageResult.REPAIRED):
            # Triple cooldown + loss accounting + risk-event persistence.
            triple_cd = cfg.market_cooldown_seconds * 3
            self._market_cooldowns[condition_id] = now + triple_cd
            self._daily_loss_usd += loss_usd
            if self._daily_loss_usd >= cfg.daily_loss_limit_usd:
                self._kill_switch = True
            await self._storage.insert_risk_event(
                event_type="legged_incident",
                market_id=condition_id,
                reason=result.name,
                detail=str(loss_usd),
            )
            logger.warning(
                "RiskManager: %s on %s — loss_usd=%s daily_loss=%s kill=%s",
                result.name,
                condition_id,
                loss_usd,
                self._daily_loss_usd,
                self._kill_switch,
            )

        elif result in (ArbitrageResult.BLOCKED_BY_RISK, ArbitrageResult.STALE_BOOK):
            # No cooldown applied; no loss to record.
            logger.info(
                "RiskManager: %s on %s — no cooldown applied",
                result.name,
                condition_id,
            )

    # ------------------------------------------------------------------
    # Kill switch — async (storage write)
    # ------------------------------------------------------------------

    async def trigger_kill_switch(self, reason: str) -> None:
        """Manually latch the kill switch and persist the event.

        Args:
            reason: Human-readable explanation for the manual trigger.
        """
        self._kill_switch = True
        await self._storage.insert_risk_event(
            event_type="kill_switch",
            market_id="",
            reason=reason,
            detail="manual trigger",
        )
        logger.critical("RiskManager: kill switch triggered — reason=%s", reason)

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def is_killed(self) -> bool:
        """Return ``True`` if the kill switch is latched."""
        return self._kill_switch

    def daily_stats(self) -> dict:
        """Return a snapshot of current daily risk metrics.

        Returns:
            dict with keys:
              * ``daily_loss_usd``    – str(Decimal)
              * ``attempts_this_hour`` – int
              * ``open_markets``      – int (count)
              * ``kill_switch``       – bool
        """
        return {
            "daily_loss_usd": str(self._daily_loss_usd),
            "attempts_this_hour": self._attempts_this_hour,
            "open_markets": len(self._open_markets),
            "kill_switch": self._kill_switch,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _refresh_hour_counter(self) -> None:
        """Reset the hourly attempt counter when the wall-monotonic hour rolls over."""
        current_hour = int(time.monotonic() // 3600)
        if current_hour != self._last_hour:
            self._attempts_this_hour = 0
            self._last_hour = current_hour
