"""
Watcher — Agent B owned module.

Subscribes to the Polymarket CLOB WebSocket, maintains per-market
LocalOrderBook instances, resyncs stale books via the CLOB REST API,
and emits OrderBookSnapshot objects into an opportunity queue whenever
a fresh, deep book is available.

Constraints
-----------
- Never import from market_discovery.py, fee_model.py, core.py,
  risk.py, or executor.py.
- WebSocket via the ``websockets`` library (async).
- HTTP resyncs via ``aiohttp``.
- Reconnects with exponential back-off: 1 s → 2 s → 4 s … 32 s max.
- At most config.max_open_markets (default 1) markets watched at once.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

import aiohttp
import websockets

from cerberus_runtime.models import Market
from cerberus_runtime.orderbook import LocalOrderBook

logger = logging.getLogger(__name__)

# ── Defaults for fields that may not exist on the Config dataclass yet ──────
_DEFAULT_MAX_OPEN_MARKETS: int = 1
_DEFAULT_BOOK_MAX_AGE_MS: int = 5_000        # 5 seconds
_DEFAULT_CLOB_REST_URL: str = "https://clob.polymarket.com"
_DEFAULT_WS_URL: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Minimum ask levels required on each leg before emitting a snapshot.
_MIN_ASK_LEVELS: int = 5

# Reconnect timing
_RECONNECT_BASE_S: int = 1
_RECONNECT_MAX_S: int = 32


class Watcher:
    """
    WebSocket consumer that maintains live L2 order books for binary markets.

    Parameters
    ----------
    config:
        Runtime configuration object. The Watcher accesses the following
        attributes via *getattr* with safe defaults so it remains
        compatible with the current Config dataclass and any future
        extension of it:

        - max_open_markets  (int, default 1)
        - book_max_age_ms   (int, default 5 000)
        - clob_rest_url     (str, default "https://clob.polymarket.com")
        - ws_url            (str, default Polymarket WS URL)

    candidate_queue:
        asyncio.Queue of Market objects produced by MarketDiscovery.
        The Watcher drains up to ``max_open_markets`` entries on startup.

    opportunity_queue:
        asyncio.Queue into which OrderBookSnapshot objects are put
        whenever a fresh, sufficiently deep book is available.
    """

    def __init__(
        self,
        config,
        candidate_queue: asyncio.Queue,
        opportunity_queue: asyncio.Queue,
    ) -> None:
        self.config = config
        self.candidate_queue = candidate_queue
        self.opportunity_queue = opportunity_queue

        # market condition_id → Market object
        self._markets: dict[str, Market] = {}
        # token_id (yes OR no) → LocalOrderBook for that market
        self._books: dict[str, LocalOrderBook] = {}

    # ------------------------------------------------------------------ #
    # Configuration helpers                                                #
    # ------------------------------------------------------------------ #

    @property
    def _max_open_markets(self) -> int:
        return getattr(self.config, "max_open_markets", _DEFAULT_MAX_OPEN_MARKETS)

    @property
    def _book_max_age_ms(self) -> int:
        return getattr(self.config, "book_max_age_ms", _DEFAULT_BOOK_MAX_AGE_MS)

    @property
    def _clob_rest_url(self) -> str:
        return getattr(self.config, "clob_rest_url", _DEFAULT_CLOB_REST_URL)

    @property
    def _ws_url(self) -> str:
        return getattr(self.config, "ws_url", _DEFAULT_WS_URL)

    # ------------------------------------------------------------------ #
    # Public entry point                                                   #
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        """
        Main loop — call once via ``asyncio.create_task(watcher.run())``.

        Steps:
        1. Drain candidate_queue up to max_open_markets.
        2. Subscribe to the CLOB WebSocket.
        3. Dispatch incoming events to LocalOrderBooks.
        4. After each WS message batch: resync stale books, emit snapshots.
        5. On disconnect: sleep with exponential back-off, then reconnect.
        """
        await self._load_markets()

        if not self._markets:
            logger.warning("Watcher: candidate queue empty; no markets to watch.")
            return

        market_ids = list(self._markets.keys())
        delay_s: int = _RECONNECT_BASE_S

        while True:
            try:
                logger.info("Watcher: connecting to %s", self._ws_url)
                async with websockets.connect(self._ws_url) as ws:
                    await self._subscribe(ws, market_ids)
                    delay_s = _RECONNECT_BASE_S  # reset on successful connection

                    async for raw_msg in ws:
                        await self._handle_raw(raw_msg)

            except (
                websockets.ConnectionClosed,
                websockets.WebSocketException,
                OSError,
            ) as exc:
                logger.warning(
                    "Watcher: WS error — %s. Reconnecting in %ds.", exc, delay_s
                )
                await asyncio.sleep(delay_s)
                delay_s = min(delay_s * 2, _RECONNECT_MAX_S)

    # ------------------------------------------------------------------ #
    # Internals                                                            #
    # ------------------------------------------------------------------ #

    async def _load_markets(self) -> None:
        """
        Non-blocking drain of candidate_queue up to max_open_markets.

        Accepts Market objects (as produced by MarketDiscovery) or plain
        dicts for testing convenience.
        """
        while len(self._markets) < self._max_open_markets:
            try:
                item = self.candidate_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            # Support both Market dataclass and plain dict (test convenience).
            if isinstance(item, dict):
                condition_id = item.get("condition_id", item.get("market_id", ""))
                yes_tid = item["yes_token_id"]
                no_tid = item["no_token_id"]
                # Wrap in a minimal Market-like object (use the dict directly).
                market = item  # type: ignore[assignment]
                market["condition_id"] = condition_id
            else:
                condition_id = item.condition_id
                yes_tid = item.yes_token_id
                no_tid = item.no_token_id
                market = item

            book = LocalOrderBook(yes_token_id=yes_tid, no_token_id=no_tid)
            self._markets[condition_id] = market
            self._books[yes_tid] = book
            self._books[no_tid] = book
            logger.info("Watcher: watching market %s", condition_id)

    @staticmethod
    async def _subscribe(ws, market_ids: list[str]) -> None:
        """Send the CLOB WebSocket subscription message."""
        msg = json.dumps(
            {
                "type": "subscribe",
                "channel": "market",
                "market_ids": market_ids,
            }
        )
        await ws.send(msg)
        logger.debug("Watcher: subscribed to %s", market_ids)

    async def _handle_raw(self, raw_msg: str) -> None:
        """Parse a raw WS frame and dispatch all events it contains."""
        try:
            payload = json.loads(raw_msg)
        except json.JSONDecodeError:
            logger.warning("Watcher: received non-JSON frame: %.200s", raw_msg)
            return

        events = payload if isinstance(payload, list) else [payload]
        for event in events:
            await self._dispatch_event(event)

        # After processing the full batch: resync then emit.
        await self._check_resyncs()
        await self._emit_snapshots()

    async def _dispatch_event(self, event: dict) -> None:
        """Route a single WS event to the correct LocalOrderBook method."""
        event_type = event.get("event_type", "")

        if event_type == "market_resolved":
            await self._handle_market_resolved(event)
            return

        asset_id = event.get("asset_id", "")
        book = self._books.get(asset_id)
        if book is None:
            return  # event for a market we're not tracking

        if event_type == "book":
            book.apply_book_event(event)
        elif event_type in ("price_change", "tick_size_change"):
            book.apply_price_change(event)

    async def _handle_market_resolved(self, event: dict) -> None:
        """Remove a resolved market and its books from active state."""
        # Polymarket may use "market_id" or "market" in the event.
        market_id = event.get("market_id") or event.get("market", "")
        market = self._markets.pop(market_id, None)
        if market is None:
            return

        if isinstance(market, dict):
            yes_tid = market.get("yes_token_id", "")
            no_tid = market.get("no_token_id", "")
        else:
            yes_tid = market.yes_token_id
            no_tid = market.no_token_id

        self._books.pop(yes_tid, None)
        self._books.pop(no_tid, None)
        logger.info("Watcher: market resolved and removed — %s", market_id)

    async def _check_resyncs(self) -> None:
        """
        For every book marked needs_resync, fetch a REST snapshot and
        apply it, then clear the flag.
        """
        visited: set[int] = set()
        for market in list(self._markets.values()):
            if isinstance(market, dict):
                yes_tid = market.get("yes_token_id", "")
                no_tid = market.get("no_token_id", "")
                condition_id = market.get("condition_id", "")
            else:
                yes_tid = market.yes_token_id
                no_tid = market.no_token_id
                condition_id = market.condition_id

            book = self._books.get(yes_tid)
            if book is None or id(book) in visited:
                continue
            visited.add(id(book))

            if book.needs_resync:
                await self._resync_book(book, condition_id, yes_tid, no_tid)

    async def _resync_book(
        self,
        book: LocalOrderBook,
        condition_id: str,
        yes_tid: str,
        no_tid: str,
    ) -> None:
        """
        Fetch full L2 snapshots from the CLOB REST API for both legs and
        apply them to *book*, then clear needs_resync.
        """
        logger.info("Watcher: resyncing book for market %s", condition_id)
        now_ms = str(int(time.time() * 1000))

        async with aiohttp.ClientSession() as session:
            for token_id in (yes_tid, no_tid):
                url = f"{self._clob_rest_url}/book"
                try:
                    async with session.get(url, params={"token_id": token_id}) as resp:
                        resp.raise_for_status()
                        data = await resp.json()
                except Exception as exc:
                    logger.error(
                        "Watcher: resync REST call failed for token %s: %s",
                        token_id,
                        exc,
                    )
                    return  # leave needs_resync=True; will retry next cycle

                # Normalise the REST response into our book-event format.
                synthetic_event = {
                    "event_type": "book",
                    "asset_id": token_id,
                    "asks": data.get("asks", []),
                    "hash": data.get("hash", ""),
                    "timestamp": now_ms,
                }
                book.apply_book_event(synthetic_event)

        book.needs_resync = False
        logger.info("Watcher: resync complete for market %s", condition_id)

    async def _emit_snapshots(self) -> None:
        """
        For each ready book, build an OrderBookSnapshot and put it on
        opportunity_queue.
        """
        visited: set[int] = set()
        for market in self._markets.values():
            if isinstance(market, dict):
                yes_tid = market.get("yes_token_id", "")
                condition_id = market.get("condition_id", "")
                no_tid = market.get("no_token_id", "")
                fee_params = market.get("fee_params")
            else:
                yes_tid = market.yes_token_id
                condition_id = market.condition_id
                no_tid = market.no_token_id
                fee_params = market.fee_params

            book = self._books.get(yes_tid)
            if book is None or id(book) in visited:
                continue
            visited.add(id(book))

            if not self._book_is_ready(book):
                continue

            snapshot = book.get_snapshot(
                market_id=condition_id,
                condition_id=condition_id,
                yes_token_id=yes_tid,
                no_token_id=no_tid,
                fee_params=fee_params,
            )
            await self.opportunity_queue.put(snapshot)

    def _book_is_ready(self, book: LocalOrderBook) -> bool:
        """
        Return True only when the book is fresh AND has enough depth on
        both legs to be actionable.
        """
        if not book.is_fresh(self._book_max_age_ms):
            return False
        if len(book.yes_asks) < _MIN_ASK_LEVELS:
            return False
        if len(book.no_asks) < _MIN_ASK_LEVELS:
            return False
        return True
