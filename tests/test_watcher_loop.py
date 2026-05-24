"""
Unit tests for cerberus_runtime.watcher.Watcher — WebSocket loop.

All tests are fully mocked: no real WebSocket connections, no real HTTP calls.
Run with:  pytest tests/test_watcher_loop.py -v

asyncio_mode = auto  (set in setup.cfg) — no @pytest.mark.asyncio needed.
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import websockets.exceptions

from cerberus_runtime.models import FeeParams, Market, PriceLevel
from cerberus_runtime.orderbook import LocalOrderBook
from cerberus_runtime.watcher import Watcher

# ── Helpers ───────────────────────────────────────────────────────────────────


def make_market(
    condition_id: str = "mkt-1",
    yes_token_id: str = "yes-1",
    no_token_id: str = "no-1",
) -> Market:
    """Return a minimal Market dataclass suitable for watcher tests."""
    return Market(
        condition_id=condition_id,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        category="test",
        fee_params=FeeParams(fees_enabled=False, maker_fee_rate=0.0, taker_fee_rate=0.0),
        min_order_size=1.0,
        tick_size=0.01,
        end_date=datetime(2026, 12, 31, tzinfo=timezone.utc),
        volume_24h=10_000.0,
        active=True,
        closed=False,
    )


class FakeConfig:
    """Minimal config object with sensible test defaults."""

    max_open_markets: int = 1
    book_max_age_ms: int = 5_000
    clob_rest_url: str = "https://clob.test"
    ws_url: str = "wss://ws.test"


def make_watcher(max_open_markets: int = 1) -> tuple[Watcher, asyncio.Queue, asyncio.Queue]:
    """Create a Watcher with fresh queues and a FakeConfig."""
    cfg = FakeConfig()
    cfg.max_open_markets = max_open_markets
    candidate_q: asyncio.Queue = asyncio.Queue()
    opp_q: asyncio.Queue = asyncio.Queue()
    return Watcher(cfg, candidate_q, opp_q), candidate_q, opp_q


def populate_watcher(
    watcher: Watcher,
    market: Market,
) -> LocalOrderBook:
    """
    Directly register *market* in the watcher's internal state and return
    the newly created LocalOrderBook.
    """
    cid = market.condition_id
    book = LocalOrderBook(yes_token_id=market.yes_token_id, no_token_id=market.no_token_id)
    watcher._active_markets[cid] = market
    watcher._books[cid] = book
    watcher._token_to_cid[market.yes_token_id] = cid
    watcher._token_to_cid[market.no_token_id] = cid
    return book


# ── FakeWS — async-iterable WebSocket mock ────────────────────────────────────


class FakeWS:
    """
    Minimal WebSocket mock that delivers a predetermined sequence of raw JSON
    strings and records outgoing ``send()`` calls.
    """

    def __init__(self, messages: list[str]) -> None:
        self._messages = list(messages)
        self._idx = 0
        self.sent: list[str] = []

    async def send(self, msg: str) -> None:
        self.sent.append(msg)

    def __aiter__(self) -> "FakeWS":
        return self

    async def __anext__(self) -> str:
        if self._idx >= len(self._messages):
            raise StopAsyncIteration
        msg = self._messages[self._idx]
        self._idx += 1
        return msg


def ws_connect_factory(fake_ws: FakeWS):
    """Return a drop-in replacement for ``websockets.connect`` that yields *fake_ws*."""

    @asynccontextmanager
    async def _connect(url: str, **kwargs):
        yield fake_ws

    return _connect


def raising_connect_factory(exc: Exception):
    """Return a ``websockets.connect`` mock that raises *exc* on entry."""

    @asynccontextmanager
    async def _connect(url: str, **kwargs):
        raise exc
        yield  # pragma: no cover — makes this function a generator

    return _connect


# ── Event builders ────────────────────────────────────────────────────────────


def book_event(
    asset_id: str,
    n_levels: int = 5,
    ts_ms: int | None = None,
    hash_: str = "",
) -> str:
    """
    Build a 'book' WS event JSON string with *n_levels* ask levels at
    prices 0.01, 0.02, …, each with size 100.
    """
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    asks = [
        {"price": f"{(i + 1) * 0.01:.2f}", "size": "100"}
        for i in range(n_levels)
    ]
    return json.dumps({
        "event_type": "book",
        "asset_id": asset_id,
        "asks": asks,
        "hash": hash_,
        "timestamp": str(ts_ms),
    })


def price_change_event(asset_id: str, price: str = "0.50", size: str = "200") -> str:
    """Build a 'price_change' WS event JSON string."""
    return json.dumps({
        "event_type": "price_change",
        "asset_id": asset_id,
        "changes": [{"price": price, "size": size, "side": "SELL"}],
        "hash": "",
        "timestamp": str(int(time.time() * 1000)),
    })


def market_resolved_event(market_id: str) -> str:
    """Build a 'market_resolved' WS event JSON string."""
    return json.dumps({
        "event_type": "market_resolved",
        "market_id": market_id,
    })


# ═══════════════════════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestSubscribesOnConnect:
    """test_subscribes_to_active_markets — subscription sent with correct market_ids."""

    async def test_subscribes_to_active_markets(self):
        watcher, _, _ = make_watcher()
        market = make_market("mkt-1", "yes-1", "no-1")
        populate_watcher(watcher, market)

        fake_ws = FakeWS([])  # deliver no messages — loop exits immediately
        with patch("cerberus_runtime.watcher.websockets.connect", ws_connect_factory(fake_ws)):
            await watcher._ws_loop()

        assert len(fake_ws.sent) == 1, "Expected exactly one subscription message"
        sub = json.loads(fake_ws.sent[0])
        assert sub["type"] == "subscribe"
        assert sub["channel"] == "market"
        assert sub["market_ids"] == ["mkt-1"]


class TestEventDispatch:
    """Tests that events are routed to the correct LocalOrderBook method."""

    async def test_dispatches_book_event_to_correct_book(self):
        watcher, _, _ = make_watcher()
        market = make_market("mkt-1", "yes-1", "no-1")
        book = populate_watcher(watcher, market)

        # Deliver a book event for the YES token with 6 ask levels
        msg = book_event("yes-1", n_levels=6)
        fake_ws = FakeWS([msg])
        with patch("cerberus_runtime.watcher.websockets.connect", ws_connect_factory(fake_ws)):
            await watcher._ws_loop()

        assert len(book.yes_asks) == 6, "YES ask levels should be updated"
        assert book.no_asks == [], "NO ask levels should remain empty"

    async def test_dispatches_price_change(self):
        watcher, _, _ = make_watcher()
        market = make_market("mkt-1", "yes-1", "no-1")
        book = populate_watcher(watcher, market)

        # Seed the yes side with one level first
        seed = book_event("yes-1", n_levels=1)
        # Then deliver a price_change that adds another level
        change = price_change_event("yes-1", price="0.50", size="999")
        fake_ws = FakeWS([seed, change])
        with patch("cerberus_runtime.watcher.websockets.connect", ws_connect_factory(fake_ws)):
            await watcher._ws_loop()

        prices = [float(pl.price) for pl in book.yes_asks]
        assert pytest.approx(0.50, abs=1e-6) in prices, (
            "price_change should add the 0.50 level to yes_asks"
        )


class TestResync:
    """test_triggers_resync_on_needs_resync_flag — _resync_from_rest called when flagged."""

    async def test_triggers_resync_on_needs_resync_flag(self):
        watcher, _, _ = make_watcher()
        market = make_market("mkt-1", "yes-1", "no-1")
        book = populate_watcher(watcher, market)

        # A book event with a bad hash will set needs_resync=True
        msg = book_event("yes-1", n_levels=3, hash_="bad-hash-will-not-match")
        fake_ws = FakeWS([msg])

        resync_mock = AsyncMock()
        with patch("cerberus_runtime.watcher.websockets.connect", ws_connect_factory(fake_ws)):
            with patch.object(watcher, "_resync_from_rest", resync_mock):
                await watcher._ws_loop()

        resync_mock.assert_awaited_once_with("mkt-1")


class TestSnapshotEmission:
    """Tests for opportunity snapshot emission thresholds."""

    async def test_emits_snapshot_when_5_levels_present(self):
        watcher, _, opp_q = make_watcher()
        market = make_market("mkt-1", "yes-1", "no-1")
        populate_watcher(watcher, market)

        # Build a snapshot-quality book: 5 levels on each leg with a recent timestamp
        now = int(time.time() * 1000)
        yes_msg = book_event("yes-1", n_levels=5, ts_ms=now)
        no_msg = book_event("no-1", n_levels=5, ts_ms=now)
        fake_ws = FakeWS([yes_msg, no_msg])

        with patch("cerberus_runtime.watcher.websockets.connect", ws_connect_factory(fake_ws)):
            await watcher._ws_loop()

        assert opp_q.qsize() == 1, "Expected exactly one snapshot in the opportunity queue"

    async def test_does_not_emit_when_less_than_5_levels(self):
        watcher, _, opp_q = make_watcher()
        market = make_market("mkt-1", "yes-1", "no-1")
        populate_watcher(watcher, market)

        # Only 3 levels on the YES side — below the 5-level threshold
        now = int(time.time() * 1000)
        yes_msg = book_event("yes-1", n_levels=3, ts_ms=now)
        no_msg = book_event("no-1", n_levels=3, ts_ms=now)
        fake_ws = FakeWS([yes_msg, no_msg])

        with patch("cerberus_runtime.watcher.websockets.connect", ws_connect_factory(fake_ws)):
            await watcher._ws_loop()

        assert opp_q.empty(), "Snapshot must NOT be emitted when fewer than 5 levels"


class TestReconnectAndBackoff:
    """Tests for reconnect behaviour and exponential back-off."""

    async def test_reconnects_on_ws_disconnect(self):
        """
        ConnectionClosed on the first attempt → sleep → second connect attempt made.
        """
        watcher, _, _ = make_watcher()
        market = make_market()
        populate_watcher(watcher, market)

        connect_count = 0

        @asynccontextmanager
        async def mock_connect(url, **kwargs):
            nonlocal connect_count
            connect_count += 1
            if connect_count == 1:
                raise websockets.exceptions.ConnectionClosed(None, None)
            # Second attempt: stop the loop via CancelledError (not caught by run())
            raise asyncio.CancelledError()
            yield  # pragma: no cover

        sleep_called = False

        async def mock_sleep(delay):
            nonlocal sleep_called
            sleep_called = True

        with patch("cerberus_runtime.watcher.websockets.connect", mock_connect):
            with patch("asyncio.sleep", mock_sleep):
                with pytest.raises(asyncio.CancelledError):
                    await watcher.run()

        assert connect_count == 2, "Watcher should retry the connection after disconnect"
        assert sleep_called, "Watcher should sleep before reconnecting"

    async def test_backoff_doubles_on_repeated_failure(self):
        """
        Two successive failures → sleep(2) then sleep(4); _backoff == 4.
        """
        watcher, _, _ = make_watcher()
        market = make_market()
        populate_watcher(watcher, market)

        sleep_calls: list[int] = []

        async def mock_sleep(delay):
            sleep_calls.append(delay)
            if len(sleep_calls) >= 2:
                raise asyncio.CancelledError()

        @asynccontextmanager
        async def failing_connect(url, **kwargs):
            raise websockets.exceptions.ConnectionClosed(None, None)
            yield  # pragma: no cover

        with patch("cerberus_runtime.watcher.websockets.connect", failing_connect):
            with patch("asyncio.sleep", mock_sleep):
                with pytest.raises(asyncio.CancelledError):
                    await watcher.run()

        assert sleep_calls == [2, 4], (
            f"Back-off should be [2, 4] after two failures; got {sleep_calls}"
        )
        assert watcher._backoff == 4, "Stored back-off should be 4 after two failures"


class TestMarketResolved:
    """test_market_resolved_removes_from_active — resolved market removed from state."""

    async def test_market_resolved_removes_from_active(self):
        watcher, _, _ = make_watcher()
        market = make_market("mkt-1", "yes-1", "no-1")
        populate_watcher(watcher, market)

        msg = market_resolved_event("mkt-1")
        fake_ws = FakeWS([msg])

        with patch("cerberus_runtime.watcher.websockets.connect", ws_connect_factory(fake_ws)):
            await watcher._ws_loop()

        assert "mkt-1" not in watcher._active_markets, (
            "Resolved market must be removed from _active_markets"
        )
        assert "mkt-1" not in watcher._books, (
            "Resolved market's book must be removed from _books"
        )
        assert "yes-1" not in watcher._token_to_cid
        assert "no-1" not in watcher._token_to_cid


class TestDrainQueue:
    """test_drain_respects_max_open_markets — queue drained only up to the limit."""

    async def test_drain_respects_max_open_markets(self):
        watcher, candidate_q, _ = make_watcher(max_open_markets=1)

        # Enqueue three markets; only one should be accepted
        for i in range(3):
            await candidate_q.put(make_market(f"mkt-{i}", f"yes-{i}", f"no-{i}"))

        await watcher._drain_candidate_queue()

        assert len(watcher._active_markets) == 1, (
            "With max_open_markets=1, only one market should be drained from the queue"
        )
        # The remaining two markets must still be in the queue
        assert candidate_q.qsize() == 2
