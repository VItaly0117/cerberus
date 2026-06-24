"""
ResolutionScanner — Sprint 4.

Scans Polymarket markets whose end_date has passed (or is within the
look-ahead window) and checks whether the Gamma API has already published
a resolution outcome.  When the confirmed outcome token is still trading
below the resolution threshold, a ResolutionSignal is emitted.

Signal flow:
    ResolutionScanner.run()
        │
        ├─ every SCAN_INTERVAL seconds:
        │       _scan() → fetches markets closing within LOOKAHEAD_HOURS
        │
        └─ for each market with outcome != None:
                _evaluate(market_data) → ResolutionSignal | None
                    → signal_queue.put(signal)

Constraints:
  - No imports from core.py, risk.py, executor.py, watcher.py, orderbook.py
  - HTTP via httpx (async)
  - Decimal-only arithmetic (no float in signal fields)
  - Exponential back-off on HTTP 429
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import httpx

from cerberus_runtime.config import Config
from cerberus_runtime.models import ResolutionSignal
from cerberus_runtime.storage import Storage

logger = logging.getLogger(__name__)

# ── Tunable constants ──────────────────────────────────────────────────────────
SCAN_INTERVAL: int = 60               # seconds between scans
LOOKAHEAD_HOURS: int = 4              # scan markets closing within this window
MIN_EDGE_PCT: Decimal = Decimal("0.02")  # min net edge after fee (2%)
TAKER_FEE_RATE: Decimal = Decimal("0.0")  # Polymarket taker fee (0% currently)
MIN_ASK_PRICE: Decimal = Decimal("0.01")  # ignore dust prices
MAX_ASK_PRICE: Decimal = Decimal("0.97")  # signal only when price < this
_REQUEST_TIMEOUT: float = 10.0
_BACKOFF_BASE: int = 2
_BACKOFF_MAX: int = 64
_PAGE_LIMIT: int = 100
# ──────────────────────────────────────────────────────────────────────────────


class ResolutionScanner:
    """
    Async service that finds resolution-arbitrage opportunities on Polymarket.

    Parameters
    ----------
    config:
        Runtime configuration (gamma_host, timeouts).
    storage:
        Async storage backend — must already be connected.
    signal_queue:
        asyncio.Queue into which ResolutionSignal objects are put.
        Downstream consumers read from this queue.
    """

    def __init__(
        self,
        config: Config,
        storage: Storage,
        signal_queue: asyncio.Queue,
    ) -> None:
        self.config = config
        self.storage = storage
        self.signal_queue = signal_queue
        self._seen_market_ids: set[str] = set()

    # ── Public API ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Infinite scan loop — call once via asyncio.create_task()."""
        logger.info(
            "ResolutionScanner starting (scan every %ds, lookahead %dh).",
            SCAN_INTERVAL,
            LOOKAHEAD_HOURS,
        )
        while True:
            try:
                await self._scan()
            except Exception:
                logger.exception("Unhandled error in ResolutionScanner._scan(); continuing.")
            await asyncio.sleep(SCAN_INTERVAL)

    # ── Private helpers ────────────────────────────────────────────────────────

    async def _scan(self) -> None:
        """One complete fetch-evaluate cycle."""
        now = datetime.now(tz=timezone.utc)
        cutoff = now + timedelta(hours=LOOKAHEAD_HOURS)

        markets = await self._fetch_resolving_markets(cutoff)
        if not markets:
            logger.debug("ResolutionScanner: no resolving markets found this cycle.")
            return

        logger.info("ResolutionScanner: checking %d candidate markets.", len(markets))
        new_signals = 0

        for market_data in markets:
            signal = self._evaluate(market_data, now)
            if signal is None:
                continue

            key = f"{signal.market_id}:{signal.outcome}"
            if key in self._seen_market_ids:
                continue
            self._seen_market_ids.add(key)

            simulated_pnl = (Decimal("1") - signal.current_ask - signal.fee_usdc)
            await self.storage.insert_resolution_signal(signal, simulated_pnl)
            await self.signal_queue.put(signal)
            new_signals += 1
            logger.info(
                "RESOLUTION_SIGNAL market=%s outcome=%s ask=%.4f edge=%.2f%% confidence=%s",
                signal.market_id,
                signal.outcome,
                float(signal.current_ask),
                float(signal.edge_net_pct) * 100,
                signal.confidence,
            )

        if new_signals:
            logger.info("ResolutionScanner: emitted %d new signals this cycle.", new_signals)

    async def _fetch_resolving_markets(
        self,
        cutoff: datetime,
    ) -> List[Dict[str, Any]]:
        """Fetch markets that end before *cutoff* and may already be resolved."""
        url = f"{self.config.gamma_host}/markets"
        params = {
            "active": "true",
            "closed": "false",
            "limit": str(_PAGE_LIMIT),
            "order": "end_date_min",
            "ascending": "true",
        }

        backoff = _BACKOFF_BASE
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            while True:
                try:
                    resp = await client.get(url, params=params)
                except httpx.RequestError as exc:
                    logger.warning("ResolutionScanner HTTP error: %s — retrying in %ds.", exc, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, _BACKOFF_MAX)
                    continue

                if resp.status_code == 429:
                    logger.warning("ResolutionScanner rate-limited — sleeping %ds.", backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, _BACKOFF_MAX)
                    continue

                if resp.status_code != 200:
                    logger.warning(
                        "ResolutionScanner unexpected HTTP %d — skipping cycle.",
                        resp.status_code,
                    )
                    return []

                data = resp.json()
                markets = data if isinstance(data, list) else data.get("markets", [])

                # Also check markets whose end_date already passed (not yet closed)
                also_check = await self._fetch_ended_markets(client)
                markets = markets + also_check

                return markets

    async def _fetch_ended_markets(
        self,
        client: httpx.AsyncClient,
    ) -> List[Dict[str, Any]]:
        """Fetch markets whose end_date has passed but are not yet marked closed."""
        url = f"{self.config.gamma_host}/markets"
        params = {
            "active": "true",
            "closed": "false",
            "limit": str(_PAGE_LIMIT),
        }
        try:
            resp = await client.get(url, params=params)
            if resp.status_code != 200:
                return []
            data = resp.json()
            markets = data if isinstance(data, list) else data.get("markets", [])
            now = datetime.now(tz=timezone.utc)
            ended = []
            for m in markets:
                end_str = m.get("endDate") or m.get("end_date_min") or ""
                if not end_str:
                    continue
                try:
                    end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    if end_dt < now:
                        ended.append(m)
                except ValueError:
                    continue
            return ended
        except Exception:
            return []

    def _evaluate(
        self,
        market_data: Dict[str, Any],
        now: datetime,
    ) -> Optional[ResolutionSignal]:
        """
        Evaluate one market dict from Gamma API.

        Returns a ResolutionSignal if:
          - The outcome is confirmed (outcome field present and non-empty), OR
          - The market end_date has passed (probable resolution pending)
          AND the winning-token ask is below MAX_ASK_PRICE with enough edge.
        """
        market_id = market_data.get("id") or market_data.get("condition_id") or ""
        condition_id = market_data.get("conditionId") or market_data.get("condition_id") or market_id

        if not market_id:
            return None

        # ── Determine confirmed outcome ────────────────────────────────────────
        outcome_raw = (
            market_data.get("outcome")
            or market_data.get("resolutionOutcome")
            or ""
        )
        outcome_raw = str(outcome_raw).strip().upper()

        confidence = "probable"
        if outcome_raw in ("YES", "NO", "1", "0", "TRUE", "FALSE"):
            confidence = "confirmed"
            if outcome_raw in ("1", "TRUE"):
                outcome_raw = "YES"
            elif outcome_raw in ("0", "FALSE"):
                outcome_raw = "NO"
        else:
            # Not confirmed — check if end_date passed
            end_str = market_data.get("endDate") or market_data.get("end_date_min") or ""
            if not end_str:
                return None
            try:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            except ValueError:
                return None
            if end_dt > now:
                return None
            # End date passed but no confirmed outcome yet — skip (too risky)
            return None

        # ── Get token IDs ─────────────────────────────────────────────────────
        tokens = market_data.get("tokens") or market_data.get("clobTokenIds") or []
        yes_token_id = ""
        no_token_id = ""

        if isinstance(tokens, list):
            for t in tokens:
                if isinstance(t, dict):
                    outcome_label = str(t.get("outcome", "")).upper()
                    if outcome_label == "YES":
                        yes_token_id = str(t.get("token_id") or t.get("tokenId") or "")
                    elif outcome_label == "NO":
                        no_token_id = str(t.get("token_id") or t.get("tokenId") or "")

        winning_token_id = yes_token_id if outcome_raw == "YES" else no_token_id

        # ── Get current best ask for winning token ────────────────────────────
        best_ask = self._extract_best_ask(market_data, outcome_raw)
        if best_ask is None:
            return None

        if best_ask < MIN_ASK_PRICE or best_ask >= MAX_ASK_PRICE:
            return None

        # ── Compute edge ──────────────────────────────────────────────────────
        fee = best_ask * TAKER_FEE_RATE
        edge_net_pct = (Decimal("1") - best_ask - fee) / best_ask

        if edge_net_pct < MIN_EDGE_PCT:
            return None

        ts_ms = int(time.time() * 1000)

        return ResolutionSignal(
            market_id=market_id,
            condition_id=condition_id,
            outcome=outcome_raw,
            token_id=winning_token_id,
            current_ask=best_ask,
            edge_net_pct=edge_net_pct,
            fee_usdc=fee,
            confidence=confidence,
            source="gamma_api",
            ts_ms=ts_ms,
        )

    def _extract_best_ask(
        self,
        market_data: Dict[str, Any],
        outcome: str,
    ) -> Optional[Decimal]:
        """Extract best ask price for the winning-outcome token from market data."""
        # Gamma API may return outcomePrices: ["0.85", "0.15"]
        # index 0 = YES price, index 1 = NO price
        prices = market_data.get("outcomePrices") or []
        if isinstance(prices, list) and len(prices) >= 2:
            try:
                idx = 0 if outcome == "YES" else 1
                price_str = prices[idx]
                return Decimal(str(price_str))
            except Exception:
                pass

        # Fallback: bestAsk / lastTradePriceYes fields
        field = "bestAskYes" if outcome == "YES" else "bestAskNo"
        raw = market_data.get(field)
        if raw is not None:
            try:
                return Decimal(str(raw))
            except Exception:
                pass

        return None
