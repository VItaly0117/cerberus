"""
KalshiWatcher — cross-venue measurement module (read-only).

Polls the *public*, unauthenticated Kalshi REST API for market metadata and
order-book depth. Kalshi's public REST surface only exposes resting BIDS
(``yes`` / ``no`` arrays of ``[price_cents, count]``) — there is no ask side
and no anonymous WebSocket. To get an ask-equivalent price usable by
``core.calculate_effective_leg`` (which expects cheapest-first ask levels),
each side is synthesised from the *opposite* side's bids:

    yes_ask = 1 - best_no_bid
    no_ask  = 1 - best_yes_bid

This is not an approximation — it is the exact no-arbitrage identity for a
binary contract that settles at $1: a resting bid to buy NO at price ``p``
is economically identical to a resting offer to sell YES at ``1 - p``.
Mixing up which side feeds which is the single easiest way to fabricate a
fake edge, so this module keeps the inversion in one place
(:func:`_asks_from_bids`) and is exercised directly by tests.

Constraints
-----------
- Never import market_discovery.py, watcher.py, orderbook.py, core.py,
  risk.py, executor.py — this module mirrors their patterns but is a
  distinct, independent data source (cross_venue.py wires the two together).
- HTTP via httpx (async), no API keys.
- Read-only: GET /markets and GET /markets/{ticker}/orderbook only.
"""
from __future__ import annotations

import hashlib
import logging
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

import httpx

from cerberus_runtime.models import OrderBookSnapshot, PriceLevel

logger = logging.getLogger(__name__)

# ── Tunable constants ────────────────────────────────────────────────────────
_DEFAULT_BASE_URL: str = "https://api.elections.kalshi.com/trade-api/v2"
_REQUEST_TIMEOUT: float = 10.0
_BACKOFF_BASE: int = 2
_BACKOFF_MAX: int = 64
_MAX_MARKETS_PAGES: int = 20  # 20 * 200 = 4 000 markets/scan ceiling

# Kalshi's published general fee schedule is a per-contract taker fee of
# 7% * price * (1 - price). We feed the existing FeeModel.calculate_fee(...,
# avg_price=...) path (rate * min(price, 1-price) * size) with this rate —
# same mechanism already used for the Polymarket leg, per the note in
# fee_model.py that this is a conservative approximation of a curve peaking
# at price=0.5. Only the two endpoints below are used, so this rate cannot
# be read live from the API; verify against Kalshi's fee schedule docs if
# it changes.
KALSHI_TAKER_FEE_RATE: float = 0.07


class KalshiWatcher:
    """Polls public Kalshi REST endpoints for markets and order-book depth.

    No persistent connection is held; each call is a discrete HTTP request.
    """

    def __init__(self, base_url: str = _DEFAULT_BASE_URL) -> None:
        self.base_url = base_url

    # ── Markets listing ──────────────────────────────────────────────────

    async def fetch_markets(self, status: str = "open") -> Optional[List[Dict[str, Any]]]:
        """GET {base_url}/markets?status=open, paginated via cursor.

        Handles per page:
        - HTTP 429 -> exponential back-off (2, 4, 8, ... 64s)
        - timeout 10s -> log error, return pages collected so far
        - ConnectError -> log critical, return pages collected so far
        - other HTTP errors -> log error, return pages collected so far
        """
        url = f"{self.base_url}/markets"
        all_markets: List[Dict[str, Any]] = []
        cursor: Optional[str] = None

        for _ in range(_MAX_MARKETS_PAGES):
            params: Dict[str, Any] = {"status": status, "limit": 200}
            if cursor:
                params["cursor"] = cursor

            backoff = _BACKOFF_BASE
            while True:
                try:
                    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                        response = await client.get(url, params=params)

                    if response.status_code == 429:
                        logger.warning(
                            "Kalshi API rate-limited (HTTP 429). Back-off %ds before retry.",
                            backoff,
                        )
                        await self._sleep(backoff)
                        backoff = min(backoff * 2, _BACKOFF_MAX)
                        continue

                    response.raise_for_status()
                    data = response.json()
                    break

                except httpx.TimeoutException:
                    logger.error(
                        "Kalshi API timeout (%ss). Using %d markets collected so far.",
                        _REQUEST_TIMEOUT,
                        len(all_markets),
                    )
                    return all_markets or None

                except httpx.ConnectError:
                    logger.critical(
                        "Kalshi API connection failed (%s). Using %d markets collected so far.",
                        self.base_url,
                        len(all_markets),
                    )
                    return all_markets or None

                except httpx.HTTPStatusError as exc:
                    logger.error(
                        "Kalshi API returned HTTP %s. Using %d markets collected so far. Detail: %s",
                        exc.response.status_code,
                        len(all_markets),
                        exc,
                    )
                    return all_markets or None

            if not isinstance(data, dict):
                logger.warning("Kalshi API: non-dict markets payload — stopping pagination.")
                break

            page_markets = data.get("markets") or []
            if not page_markets:
                break
            all_markets.extend(page_markets)

            cursor = data.get("cursor") or ""
            if not cursor:
                break

        return all_markets

    # ── Order book ───────────────────────────────────────────────────────

    async def fetch_orderbook_snapshot(self, ticker: str) -> Optional[OrderBookSnapshot]:
        """GET {base_url}/markets/{ticker}/orderbook, converted into an
        ``OrderBookSnapshot`` whose ``yes_asks``/``no_asks`` are synthesised
        asks (see module docstring for the inversion).

        Returns ``None`` on any HTTP failure, malformed payload, or if both
        synthesised ask sides come back empty (no resting liquidity to
        invert from — this is a real "no signal" state, not an error).
        """
        url = f"{self.base_url}/markets/{ticker}/orderbook"
        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                response = await client.get(url)
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException:
            logger.error("Kalshi orderbook timeout for %s.", ticker)
            return None
        except httpx.ConnectError:
            logger.critical("Kalshi orderbook connection failed for %s.", ticker)
            return None
        except httpx.HTTPStatusError as exc:
            logger.warning("Kalshi orderbook HTTP %s for %s.", exc.response.status_code, ticker)
            return None
        except ValueError:
            logger.warning("Kalshi orderbook: malformed JSON for %s.", ticker)
            return None

        if not isinstance(data, dict):
            logger.warning("Kalshi orderbook: non-dict payload for %s.", ticker)
            return None

        book = data.get("orderbook") or {}
        if not isinstance(book, dict):
            logger.warning("Kalshi orderbook: non-dict 'orderbook' field for %s.", ticker)
            return None

        yes_bids = book.get("yes") or []
        no_bids = book.get("no") or []

        yes_asks = _asks_from_bids(no_bids)   # yes_ask = 1 - best no bid
        no_asks = _asks_from_bids(yes_bids)   # no_ask  = 1 - best yes bid

        if not yes_asks and not no_asks:
            logger.debug("Kalshi orderbook: no invertible liquidity for %s.", ticker)
            return None

        ts_ms = int(time.time() * 1000)
        return OrderBookSnapshot(
            market_id=ticker,
            yes_asks=yes_asks,
            no_asks=no_asks,
            timestamp=ts_ms / 1000.0,
            condition_id=ticker,
            yes_token_id=f"{ticker}:yes",
            no_token_id=f"{ticker}:no",
            fee_params=None,
            ts_ms=ts_ms,
            book_hash=_book_hash(yes_asks, no_asks),
        )

    # ── internals ────────────────────────────────────────────────────────

    async def _sleep(self, seconds: int) -> None:
        import asyncio
        await asyncio.sleep(seconds)


def _asks_from_bids(bids: Any) -> List[PriceLevel]:
    """Convert Kalshi ``[price_cents, count]`` bid levels into synthesised
    ask ``PriceLevel``s (``price = 1 - price_cents/100``), sorted
    cheapest-first as required by ``calculate_effective_leg``.

    Malformed entries (wrong shape, non-numeric, out-of-range price,
    non-positive count) are skipped with a warning rather than raising —
    mirrors ``LocalOrderBook._parse_levels``'s tolerance for dirty feeds.
    """
    if not isinstance(bids, list):
        return []

    levels: List[PriceLevel] = []
    for entry in bids:
        try:
            if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                logger.warning("KalshiWatcher: malformed bid level — skipping: %r", entry)
                continue
            price_cents = Decimal(str(entry[0]))
            count = Decimal(str(entry[1]))
            if not price_cents.is_finite() or not count.is_finite():
                logger.warning("KalshiWatcher: non-finite bid level — skipping: %r", entry)
                continue
            if price_cents <= Decimal("0") or price_cents >= Decimal("100"):
                # 0 or 100 cent bids invert to a 1.00 or 0.00 ask — degenerate, skip.
                continue
            if count <= Decimal("0"):
                continue
            ask_price = (Decimal("100") - price_cents) / Decimal("100")
            levels.append(PriceLevel(price=ask_price, size=count))
        except (InvalidOperation, ValueError, TypeError, IndexError) as exc:
            logger.warning("KalshiWatcher: malformed bid level %r (%s) — skipping.", entry, exc)
            continue

    levels.sort(key=lambda pl: pl.price)
    return levels


def _book_hash(yes_asks: List[PriceLevel], no_asks: List[PriceLevel]) -> str:
    """SHA-256 over both synthesised ask sides, for cross-component diffing."""
    parts = [f"{pl.price:.6f}:{pl.size:.6f}" for pl in yes_asks]
    parts.append("|")
    parts.extend(f"{pl.price:.6f}:{pl.size:.6f}" for pl in no_asks)
    return hashlib.sha256(",".join(parts).encode()).hexdigest()
