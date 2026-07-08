"""
Tests for cerberus_runtime/odds_watcher.py

All tests fully mocked/synthetic: no real HTTP calls hit The Odds API (no
ODDS_API_KEY was available while writing this module — see its docstring).

Run with:
    pytest -v tests/test_odds_watcher.py
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest

from cerberus_runtime.odds_watcher import OddsApiWatcher, american_odds_to_prob


# ── american_odds_to_prob: pure conversion logic ────────────────────────────


class TestAmericanOddsToProb:
    def test_negative_odds_favorite(self) -> None:
        assert american_odds_to_prob(-150) == Decimal("0.6")

    def test_positive_odds_underdog(self) -> None:
        assert american_odds_to_prob(150) == Decimal("0.4")

    def test_even_money(self) -> None:
        assert american_odds_to_prob(100) == Decimal("0.5")
        assert american_odds_to_prob(-100) == Decimal("0.5")

    def test_zero_is_invalid(self) -> None:
        assert american_odds_to_prob(0) is None

    def test_non_numeric_returns_none(self) -> None:
        assert american_odds_to_prob("not a number") is None
        assert american_odds_to_prob(None) is None


# ── OddsApiWatcher.candidates_and_snapshots ─────────────────────────────────


class TestCandidatesAndSnapshots:
    def test_two_way_market_produces_both_sides(self) -> None:
        events = [{
            "id": "evt1",
            "commence_time": "2026-07-10T18:00:00Z",
            "bookmakers": [{"markets": [{"key": "h2h", "outcomes": [
                {"name": "Argentina", "price": -150},
                {"name": "Brazil", "price": 130},
            ]}]}],
        }]
        watcher = OddsApiWatcher(api_key="dummy")
        result = watcher.candidates_and_snapshots(events, Decimal("50"), 1_720_000_000_000)

        assert set(result.keys()) == {"evt1:Argentina", "evt1:Brazil"}
        arg_question, arg_snapshot, arg_tokens = result["evt1:Argentina"]
        assert arg_question == "Will Argentina beat Brazil?"
        assert arg_snapshot.yes_asks[0].price == Decimal("0.6")
        assert arg_tokens[0] == "Argentina"

    def test_best_price_wins_across_bookmakers(self) -> None:
        """Two books quote Brazil differently (+130 vs +140) — the better
        (higher implied prob... no, LOWER risk / better payout) price for
        the bettor must win, not whichever book happened to be listed last."""
        events = [{
            "id": "evt1",
            "commence_time": "2026-07-10T18:00:00Z",
            "bookmakers": [
                {"markets": [{"key": "h2h", "outcomes": [
                    {"name": "Argentina", "price": -150},
                    {"name": "Brazil", "price": 130},
                ]}]},
                {"markets": [{"key": "h2h", "outcomes": [
                    {"name": "Argentina", "price": -140},
                    {"name": "Brazil", "price": 140},
                ]}]},
            ],
        }]
        watcher = OddsApiWatcher(api_key="dummy")
        result = watcher.candidates_and_snapshots(events, Decimal("50"), 1_720_000_000_000)

        _, brazil_snapshot, _ = result["evt1:Brazil"]
        # best (highest) implied prob for Brazil comes from the +140 quote,
        # not the worse +130 quote from the other book.
        expected = american_odds_to_prob(140)
        assert brazil_snapshot.yes_asks[0].price == expected

    def test_three_way_market_is_skipped(self) -> None:
        """Soccer home/away/draw doesn't reduce to a clean Yes/No question
        — must be skipped entirely, not silently mis-mapped."""
        events = [{
            "id": "evt2",
            "commence_time": "2026-07-10T20:00:00Z",
            "bookmakers": [{"markets": [{"key": "h2h", "outcomes": [
                {"name": "France", "price": -110},
                {"name": "Morocco", "price": 250},
                {"name": "Draw", "price": 300},
            ]}]}],
        }]
        watcher = OddsApiWatcher(api_key="dummy")
        result = watcher.candidates_and_snapshots(events, Decimal("50"), 1_720_000_000_000)
        assert result == {}

    def test_malformed_event_skipped_not_crashed(self) -> None:
        events = ["not a dict", {"id": None, "bookmakers": []}, {}]
        watcher = OddsApiWatcher(api_key="dummy")
        result = watcher.candidates_and_snapshots(events, Decimal("50"), 1_720_000_000_000)
        assert result == {}


# ── OddsApiWatcher.fetch_odds ────────────────────────────────────────────────


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
async def test_fetch_odds_no_api_key_returns_none_without_calling_http() -> None:
    watcher = OddsApiWatcher(api_key="")
    result = await watcher.fetch_odds("basketball_nba")
    assert result is None


@pytest.mark.asyncio
async def test_fetch_odds_success() -> None:
    payload = [{"id": "evt1", "bookmakers": []}]
    watcher = OddsApiWatcher(api_key="dummy-key")
    with patch(
        "cerberus_runtime.odds_watcher.httpx.AsyncClient",
        return_value=_FakeClient(_FakeResponse(200, payload)),
    ):
        result = await watcher.fetch_odds("basketball_nba")
    assert result == payload


@pytest.mark.asyncio
async def test_fetch_odds_non_list_payload_returns_none() -> None:
    watcher = OddsApiWatcher(api_key="dummy-key")
    with patch(
        "cerberus_runtime.odds_watcher.httpx.AsyncClient",
        return_value=_FakeClient(_FakeResponse(200, {"message": "invalid key"})),
    ):
        result = await watcher.fetch_odds("basketball_nba")
    assert result is None


@pytest.mark.asyncio
async def test_fetch_odds_http_error_returns_none() -> None:
    watcher = OddsApiWatcher(api_key="dummy-key")
    with patch(
        "cerberus_runtime.odds_watcher.httpx.AsyncClient",
        return_value=_FakeClient(_FakeResponse(401, None)),
    ):
        result = await watcher.fetch_odds("basketball_nba")
    assert result is None
