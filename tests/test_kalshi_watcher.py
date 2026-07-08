"""
Tests for cerberus_runtime/kalshi_watcher.py

All tests fully mocked: no real HTTP calls hit Kalshi's API.

Run with:
    pytest -v tests/test_kalshi_watcher.py
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, List
from unittest.mock import patch

import pytest

from cerberus_runtime.kalshi_watcher import KalshiWatcher, _asks_from_bids


# ── _asks_from_bids: pure inversion logic ───────────────────────────────────


class TestAsksFromBids:
    def test_basic_inversion_and_sort(self) -> None:
        """[[63, 10], [40, 5]] -> asks at (1-0.63)=0.37 and (1-0.40)=0.60,
        sorted cheapest (0.37) first."""
        levels = _asks_from_bids([[63, 10], [40, 5]])
        assert len(levels) == 2
        assert levels[0].price == Decimal("0.37")
        assert levels[0].size == Decimal("10")
        assert levels[1].price == Decimal("0.60")
        assert levels[1].size == Decimal("5")

    def test_empty_input_returns_empty(self) -> None:
        assert _asks_from_bids([]) == []
        assert _asks_from_bids(None) == []

    def test_non_list_input_returns_empty(self) -> None:
        assert _asks_from_bids("not a list") == []

    def test_skips_malformed_entry_wrong_shape(self) -> None:
        levels = _asks_from_bids([[50], [60, 5]])
        assert len(levels) == 1
        assert levels[0].size == Decimal("5")

    def test_skips_non_numeric_entry(self) -> None:
        levels = _asks_from_bids([["abc", "def"], [55, 3]])
        assert len(levels) == 1

    def test_skips_zero_or_negative_count(self) -> None:
        levels = _asks_from_bids([[50, 0], [50, -5], [60, 5]])
        assert len(levels) == 1
        assert levels[0].size == Decimal("5")

    def test_skips_degenerate_price_boundaries(self) -> None:
        """0 or 100 cent bids invert to 1.00/0.00 ask — degenerate, must skip."""
        levels = _asks_from_bids([[0, 10], [100, 10], [50, 10]])
        assert len(levels) == 1
        assert levels[0].price == Decimal("0.5")


# ── KalshiWatcher.fetch_orderbook_snapshot ──────────────────────────────────


class _FakeResponse:
    def __init__(self, status: int = 200, payload: Any = None) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no payload")
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("error", request=None, response=self)


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        pass

    async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        return self._response


@pytest.mark.asyncio
async def test_fetch_orderbook_snapshot_success() -> None:
    payload = {
        "orderbook": {
            "yes": [[63, 10]],   # -> no_ask = 1 - 0.63 = 0.37
            "no": [[40, 5]],     # -> yes_ask = 1 - 0.40 = 0.60
        }
    }
    watcher = KalshiWatcher()
    with patch(
        "cerberus_runtime.kalshi_watcher.httpx.AsyncClient",
        return_value=_FakeClient(_FakeResponse(200, payload)),
    ):
        snapshot = await watcher.fetch_orderbook_snapshot("TICKER-1")

    assert snapshot is not None
    assert snapshot.market_id == "TICKER-1"
    assert len(snapshot.yes_asks) == 1
    assert snapshot.yes_asks[0].price == Decimal("0.6")
    assert len(snapshot.no_asks) == 1
    assert snapshot.no_asks[0].price == Decimal("0.37")


@pytest.mark.asyncio
async def test_fetch_orderbook_snapshot_empty_book_returns_none() -> None:
    payload = {"orderbook": {"yes": [], "no": []}}
    watcher = KalshiWatcher()
    with patch(
        "cerberus_runtime.kalshi_watcher.httpx.AsyncClient",
        return_value=_FakeClient(_FakeResponse(200, payload)),
    ):
        snapshot = await watcher.fetch_orderbook_snapshot("TICKER-1")
    assert snapshot is None


@pytest.mark.asyncio
async def test_fetch_orderbook_snapshot_malformed_json_returns_none() -> None:
    watcher = KalshiWatcher()
    with patch(
        "cerberus_runtime.kalshi_watcher.httpx.AsyncClient",
        return_value=_FakeClient(_FakeResponse(200, None)),
    ):
        snapshot = await watcher.fetch_orderbook_snapshot("TICKER-1")
    assert snapshot is None


@pytest.mark.asyncio
async def test_fetch_orderbook_snapshot_non_dict_orderbook_returns_none() -> None:
    payload = {"orderbook": ["not", "a", "dict"]}
    watcher = KalshiWatcher()
    with patch(
        "cerberus_runtime.kalshi_watcher.httpx.AsyncClient",
        return_value=_FakeClient(_FakeResponse(200, payload)),
    ):
        snapshot = await watcher.fetch_orderbook_snapshot("TICKER-1")
    assert snapshot is None


@pytest.mark.asyncio
async def test_fetch_orderbook_snapshot_http_error_returns_none() -> None:
    watcher = KalshiWatcher()
    with patch(
        "cerberus_runtime.kalshi_watcher.httpx.AsyncClient",
        return_value=_FakeClient(_FakeResponse(500, {})),
    ):
        snapshot = await watcher.fetch_orderbook_snapshot("TICKER-1")
    assert snapshot is None


# ── KalshiWatcher.fetch_markets ──────────────────────────────────────────────


class _FakeMarketsClient:
    """Serves a fixed sequence of (status, payload) per call, in order."""

    def __init__(self, responses: List[tuple]) -> None:
        self._responses = list(responses)
        self._idx = 0

    async def __aenter__(self) -> "_FakeMarketsClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        pass

    async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        status, payload = self._responses[self._idx]
        self._idx += 1
        return _FakeResponse(status, payload)


@pytest.mark.asyncio
async def test_fetch_markets_paginates_via_cursor() -> None:
    page1 = (200, {"markets": [{"ticker": "A"}], "cursor": "next"})
    page2 = (200, {"markets": [{"ticker": "B"}], "cursor": ""})
    watcher = KalshiWatcher()
    with patch(
        "cerberus_runtime.kalshi_watcher.httpx.AsyncClient",
        return_value=_FakeMarketsClient([page1, page2]),
    ):
        markets = await watcher.fetch_markets()
    assert markets == [{"ticker": "A"}, {"ticker": "B"}]


@pytest.mark.asyncio
async def test_fetch_markets_backoff_on_429() -> None:
    responses = [
        (429, {}),
        (429, {}),
        (200, {"markets": [], "cursor": ""}),
    ]
    watcher = KalshiWatcher()
    sleep_calls = []

    async def _fake_sleep(secs: float) -> None:
        sleep_calls.append(secs)

    with (
        patch("cerberus_runtime.kalshi_watcher.KalshiWatcher._sleep", side_effect=_fake_sleep),
        patch(
            "cerberus_runtime.kalshi_watcher.httpx.AsyncClient",
            return_value=_FakeMarketsClient(responses),
        ),
    ):
        markets = await watcher.fetch_markets()

    assert markets == []
    assert sleep_calls == [2, 4]
