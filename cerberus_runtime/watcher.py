"""
Watcher — Agent C owned module.

Subscribes to the Polymarket CLOB WebSocket, maintains per-market
LocalOrderBook instances, resyncs stale books via the CLOB REST API,
and emits OrderBookSnapshot objects into an opportunity queue whenever
a fresh, deep book is available.

Constraints
-----------
- Never import from market_discovery.py, fee_model.py, core.py,
  risk.py, or executor.py.
- WebSocket via the ``websockets`` library (async).
- HTTP resyncs via ``httpx`` (AsyncClient, timeout=10 s).
- Reconnects with exponential back-off: 1 s → 2 s → 4 s … 32 s max.
- At most config.max_open_markets (default 1) markets watched at once.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx
import ssl as _ssl

import certifi
import websockets

from cerberus_runtime.models import Market
from cerberus_runtime.orderbook import LocalOrderBook

logger = logging.getLogger(__name__)

# ── Defaults for fields that may not exist on the Config dataclass yet ────────
_DEFAULT_MAX_OPEN_MARKETS: int = 1
_DEFAULT_BOOK_MAX_AGE_MS: int = 5_000        # 5 seconds
_DEFAULT_CLOB_REST_URL: str = "https://clob.polymarket.com"
_DEFAULT_WS_URL: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

# Minimum ask levels required on each leg before emitting a snapshot.
_MIN_ASK_LEVELS: int = 5

# Reconnect timing
_RECONNECT_BASE_S: int = 1
_RECONNECT_MAX_S: int = 32

# Resync back-off timing (per-market)
_RESYNC_BASE_S: int = 1
_RESYNC_MAX_S: int = 30


class Watcher:
    """
    WebSocket consumer that maintains live L2 order books for binary markets.

    Parameters
    ----------
    config:
        Runtime configuration object.  The Watcher reads the following
        attributes via *getattr* with safe defaults so it remains compatible
        with any future extension of the Config dataclass:

        - max_open_markets  (int, default 1)
        - book_max_age_ms   (int, default 5 000)
        - clob_rest_url     (str, default "https://clob.polymarket.com")
        - ws_url            (str, default Polymarket WS URL)

    candidate_queue:
        ``asyncio.Queue`` of ``Market`` objects produced by MarketDiscovery.
        The Watcher drains up to ``max_open_markets`` entries on each cycle.

    opportunity_queue:
        ``asyncio.Queue`` into which ``OrderBookSnapshot`` objects are put
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

        # condition_id → Market
        self._active_markets: dict[str, Market] = {}
        # condition_id → LocalOrderBook
        self._books: dict[str, LocalOrderBook] = {}
        # token_id → condition_id  (reverse-lookup for WS event routing)
        self._token_to_cid: dict[str, str] = {}
        # current back-off delay in seconds; doubles on each failure, resets on success
        self._backoff: int = _RECONNECT_BASE_S

        # Per-market resync back-off state (bug #9 — prevents tight retry loop)
        self._resync_backoff_s: dict[str, float] = {}
        self._resync_next_attempt_ms: dict[str, int] = {}

    # ── Configuration helpers ─────────────────────────────────────────────────

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

    # ── Public entry point ────────────────────────────────────────────────────

    async def run(self) -> None:
        """
        Main loop — call once via ``asyncio.create_task(watcher.run())``.

        On every iteration:
        1. Drain candidate_queue up to max_open_markets.
        2. Run the WebSocket loop until it exits (clean finish → else branch)
           or raises an exception (disconnect / error → except branch).
        3. On exception: double the back-off, sleep, then retry.
        4. On clean exit: reset the back-off to 1 s.
        """
        while True:
            try:
                await self._drain_candidate_queue()
                await self._ws_loop()
            except Exception as exc:
                logger.warning(
                    "Watcher: error — %s. Reconnecting in %d s.", exc, self._backoff
                )
                self._backoff = min(self._backoff * 2, _RECONNECT_MAX_S)
                await asyncio.sleep(self._backoff)
            else:
                self._backoff = _RECONNECT_BASE_S
                # No markets to watch — yield to the event loop so other tasks
                # (market_discovery) can run and populate candidate_queue.
                await asyncio.sleep(1)

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _drain_candidate_queue(self) -> None:
        """
        Non-blocking drain of candidate_queue up to max_open_markets.

        For each new market accepted:
        - Adds it to ``_active_markets`` (condition_id → Market).
        - Creates a ``LocalOrderBook`` in ``_books`` (condition_id → book).
        - Registers both token IDs in the reverse-lookup map ``_token_to_cid``.
        """
        while len(self._active_markets) < self._max_open_markets:
            try:
                market = self.candidate_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            condition_id = market.condition_id
            self._active_markets[condition_id] = market
            self._books[condition_id] = LocalOrderBook(
                yes_token_id=market.yes_token_id,
                no_token_id=market.no_token_id,
            )
            self._token_to_cid[market.yes_token_id] = condition_id
            self._token_to_cid[market.no_token_id] = condition_id
            logger.info("Watcher: watching market %s", condition_id)

    async def _ws_loop(self) -> None:
        """
        Connect to the CLOB WebSocket, subscribe, and dispatch events.

        Raises immediately (propagates to ``run()``) on any WebSocket error
        so the outer loop can apply the back-off and reconnect.
        """
        market_ids = list(self._active_markets.keys())
        if not market_ids:
            return  # nothing to watch — clean exit, back-off resets

        logger.info("Watcher: connecting to %s", self._ws_url)
        _ssl_ctx = _ssl.create_default_context(cafile=certifi.where())
        async with websockets.connect(self._ws_url, ssl=_ssl_ctx, open_timeout=15) as ws:
            # ── Subscribe ────────────────────────────────────────────────────
            sub_msg = json.dumps(
                {
                    "type": "subscribe",
                    "channel": "market",
                    "market_ids": market_ids,
                }
            )
            await ws.send(sub_msg)
            logger.info("Watcher: subscribed to %s markets", len(market_ids))

            # ── Initial REST sync: populate books without waiting for WS events ──
            for cid in list(self._active_markets.keys()):
                await self._resync_from_rest(cid)
                await self._emit_snapshot_if_fresh(cid)

            # ── Dispatch loop ─────────────────────────────────────────────────
            # Use timeout-based recv so we can REST-poll even when WS is quiet.
            _REST_POLL_INTERVAL = 60  # seconds
            _last_rest_poll = time.time()

            while True:
                try:
                    raw_message = await asyncio.wait_for(ws.recv(), timeout=_REST_POLL_INTERVAL)
                except asyncio.TimeoutError:
                    # No WS event for 60s — refresh via REST
                    logger.debug("Watcher: no WS events for %ds, REST polling", _REST_POLL_INTERVAL)
                    for cid in list(self._active_markets.keys()):
                        await self._resync_from_rest(cid)
                        await self._emit_snapshot_if_fresh(cid)
                    continue
                # bug #3 fix: malformed JSON should not crash the watcher loop
                try:
                    event = json.loads(raw_message)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "Watcher: malformed JSON from WS, skipping (%s): %.200r",
                        exc, raw_message,
                    )
                    continue
                if not isinstance(event, dict):
                    logger.warning("Watcher: non-dict event from WS — skipping: %r", event)
                    continue
                event_type = event.get("type") or event.get("event_type", "")

                # ── Market-resolved event (no asset_id) ─────────────────────
                if event_type == "market_resolved":
                    await self._handle_market_resolved(event)
                    continue

                # ── Route to the correct book via token_id ──────────────────
                asset_id = event.get("asset_id", "")
                condition_id = self._token_to_cid.get(asset_id, "")
                book = self._books.get(condition_id)

                if book is None:
                    logger.debug(
                        "Watcher: ignoring event type=%s for unknown asset_id=%s",
                        event_type,
                        asset_id,
                    )
                    continue

                # ── Dispatch ────────────────────────────────────────────────
                if event_type == "book":
                    book.apply_book_event(event)
                elif event_type in ("price_change", "tick_size_change"):
                    book.apply_price_change(event)
                else:
                    logger.debug("Watcher: unknown event type '%s'", event_type)
                    continue

                # ── Resync if flagged ────────────────────────────────────────
                if book.needs_resync:
                    await self._resync_from_rest(condition_id)

                # ── Emit snapshot if book is deep and fresh ─────────────────
                if condition_id not in self._active_markets:
                    continue  # market was just resolved

                if book.is_fresh(self._book_max_age_ms):
                    market = self._active_markets[condition_id]
                    snapshot = book.get_snapshot(
                        market_id=condition_id,
                        condition_id=condition_id,
                        yes_token_id=market.yes_token_id,
                        no_token_id=market.no_token_id,
                        fee_params=getattr(market, "fee_params", None),
                    )
                    yes_levels = len(snapshot.yes_asks)
                    no_levels = len(snapshot.no_asks)
                    if yes_levels >= _MIN_ASK_LEVELS and no_levels >= _MIN_ASK_LEVELS:
                        await self.opportunity_queue.put(snapshot)

    async def _emit_snapshot_if_fresh(self, condition_id: str) -> None:
        """Emit a snapshot to opportunity_queue if the book is populated and fresh."""
        book = self._books.get(condition_id)
        market = self._active_markets.get(condition_id)
        if book is None or market is None:
            return
        if book.is_fresh(self._book_max_age_ms):
            snapshot = book.get_snapshot(
                market_id=condition_id,
                condition_id=condition_id,
                yes_token_id=market.yes_token_id,
                no_token_id=market.no_token_id,
                fee_params=getattr(market, "fee_params", None),
            )
            if snapshot is not None:
                await self.opportunity_queue.put(snapshot)
                logger.debug("Watcher: emitted REST-synced snapshot for %s", condition_id)

    async def _resync_from_rest(self, market_id: str) -> None:
        """
        Atomically resync both legs from the CLOB REST API.

        Sprint 5 fixes:
          - bug #5: two-phase fetch — validate BOTH legs before applying either.
            Prevents lopsided snapshots (YES fresh, NO stale).
          - bug #6: reject empty ``asks`` lists; treat as failure.
          - bug #9: per-market exponential back-off (1s → 30s) to prevent
            hammering the REST API when it consistently fails.
        """
        market = self._active_markets.get(market_id)
        book = self._books.get(market_id)
        if market is None or book is None:
            return

        # ── Back-off gate (bug #9) ────────────────────────────────────────────
        now_ms_int = int(time.time() * 1000)
        next_attempt = self._resync_next_attempt_ms.get(market_id, 0)
        if now_ms_int < next_attempt:
            return  # back-off active; skip this attempt

        yes_tid = market.yes_token_id
        no_tid = market.no_token_id
        logger.info("Watcher: resyncing book for market %s", market_id)
        now_ms = str(now_ms_int)

        # ── Phase 1: fetch & validate BOTH legs (bug #5, #6) ──────────────────
        fetched: dict[str, dict] = {}
        async with httpx.AsyncClient(timeout=10.0) as client:
            for token_id in (yes_tid, no_tid):
                url = f"{self._clob_rest_url}/book"
                try:
                    resp = await client.get(url, params={"token_id": token_id})
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as exc:
                    logger.warning(
                        "Watcher: resync REST call failed for token %s: %s — backing off.",
                        token_id, exc,
                    )
                    self._bump_resync_backoff(market_id)
                    return

                if not isinstance(data, dict):
                    logger.warning(
                        "Watcher: resync got non-dict payload for token %s — backing off.",
                        token_id,
                    )
                    self._bump_resync_backoff(market_id)
                    return

                asks = data.get("asks", [])
                if not isinstance(asks, list) or len(asks) == 0:
                    logger.warning(
                        "Watcher: resync got empty/invalid asks for token %s — backing off.",
                        token_id,
                    )
                    self._bump_resync_backoff(market_id)
                    return

                fetched[token_id] = data

        # ── Phase 2: atomic apply (both legs validated) ───────────────────────
        for token_id, data in fetched.items():
            synthetic_event = {
                "event_type": "book",
                "asset_id": token_id,
                "asks": data.get("asks", []),
                "hash": data.get("hash", ""),
                "timestamp": now_ms,
            }
            book.apply_book_event(synthetic_event)

        # Reset back-off and clear resync flag on success
        self._resync_backoff_s.pop(market_id, None)
        self._resync_next_attempt_ms.pop(market_id, None)
        book.needs_resync = False
        logger.info("Watcher: resync complete for market %s", market_id)

    def _bump_resync_backoff(self, market_id: str) -> None:
        """Double the resync back-off for *market_id* and update next-attempt time."""
        current = self._resync_backoff_s.get(market_id, 0.0)
        new_backoff = min(max(current * 2, _RESYNC_BASE_S), _RESYNC_MAX_S)
        self._resync_backoff_s[market_id] = new_backoff
        self._resync_next_attempt_ms[market_id] = int(time.time() * 1000) + int(new_backoff * 1000)
        logger.info(
            "Watcher: resync back-off for %s = %.1fs", market_id, new_backoff
        )

    async def _handle_market_resolved(self, event: dict) -> None:
        """
        Remove a resolved market and its books from active state.

        Reads ``market_id`` or ``market`` from the event dict.
        Silently ignores events for markets not currently tracked.
        """
        market_id = event.get("market_id") or event.get("market", "")
        market = self._active_markets.pop(market_id, None)
        if market is None:
            return

        self._books.pop(market_id, None)
        # Clean up the reverse-lookup map (safe for both dataclass and dict markets)
        self._token_to_cid.pop(getattr(market, "yes_token_id", ""), None)
        self._token_to_cid.pop(getattr(market, "no_token_id", ""), None)
        logger.info(
            "Watcher: market resolved, removing from active set — %s", market_id
        )
