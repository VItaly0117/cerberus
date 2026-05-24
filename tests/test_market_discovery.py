"""
Tests for cerberus_runtime/market_discovery.py

Run with:
    pytest -v tests/test_market_discovery.py

Required names (per sprint1-assignments.md):
  test_filter_rejects_neg_risk
  test_filter_rejects_expired_markets
  test_filter_rejects_low_volume
  test_filter_accepts_valid_market
  test_backoff_on_429
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cerberus_runtime.config import Config
from cerberus_runtime.market_discovery import MarketDiscovery
from cerberus_runtime.models import FeeParams, Market
from cerberus_runtime.storage import Storage

# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _valid_market(**overrides: Any) -> Dict[str, Any]:
    """
    Return a market dict that passes ALL filters by default.
    Override individual fields via keyword arguments.
    """
    base: Dict[str, Any] = {
        "condition_id": "0xabc123",
        "active": True,
        "closed": False,
        "neg_risk": False,
        # end_date 10 days from now → inside [3d, 30d] window
        "end_date_iso": (_now_utc() + timedelta(days=10)).isoformat(),
        # volume inside [1000, 15000]
        "volume_24hr": 5_000.0,
        "tokens": [
            {"outcome": "Yes", "token_id": "111"},
            {"outcome": "No", "token_id": "222"},
        ],
        "category": "Sports",
        "fees": {"maker_fee_rate": "0.01", "taker_fee_rate": "0.02"},
        "minimum_order_size": 5.0,
        "minimum_tick_size": 0.01,
    }
    base.update(overrides)
    return base


def _make_discovery(queue: asyncio.Queue | None = None) -> MarketDiscovery:
    """Construct a MarketDiscovery with a mock Storage and default Config."""
    config = Config()
    storage = MagicMock(spec=Storage)
    storage.upsert_market = AsyncMock()
    storage.mark_market_closed = AsyncMock()
    if queue is None:
        queue = asyncio.Queue()
    return MarketDiscovery(config=config, storage=storage, candidate_queue=queue)


# ── Filter unit tests ─────────────────────────────────────────────────────────

class TestFilterRejects:
    """_passes_filter must return False for markets that violate any criterion."""

    def test_filter_rejects_neg_risk(self) -> None:
        """Markets with neg_risk=True must be excluded."""
        md = _make_discovery()
        market = _valid_market(neg_risk=True)
        assert md._passes_filter(market) is False

    def test_filter_rejects_expired_markets(self) -> None:
        """end_date < now + 3 days must be excluded."""
        md = _make_discovery()
        # 1 day from now — too soon
        market = _valid_market(
            end_date_iso=(_now_utc() + timedelta(days=1)).isoformat()
        )
        assert md._passes_filter(market) is False

    def test_filter_rejects_markets_too_far_in_future(self) -> None:
        """end_date > now + 30 days must also be excluded."""
        md = _make_discovery()
        market = _valid_market(
            end_date_iso=(_now_utc() + timedelta(days=31)).isoformat()
        )
        assert md._passes_filter(market) is False

    def test_filter_rejects_low_volume(self) -> None:
        """volume_24hr < 1000 must be excluded."""
        md = _make_discovery()
        market = _valid_market(volume_24hr=500.0)
        assert md._passes_filter(market) is False

    def test_filter_rejects_high_volume(self) -> None:
        """volume_24hr > 15 000 must be excluded."""
        md = _make_discovery()
        market = _valid_market(volume_24hr=20_000.0)
        assert md._passes_filter(market) is False

    def test_filter_rejects_inactive(self) -> None:
        md = _make_discovery()
        assert md._passes_filter(_valid_market(active=False)) is False

    def test_filter_rejects_closed(self) -> None:
        md = _make_discovery()
        assert md._passes_filter(_valid_market(closed=True)) is False

    def test_filter_rejects_non_binary_token_count(self) -> None:
        """Three outcome tokens → not binary → rejected."""
        md = _make_discovery()
        market = _valid_market(
            tokens=[
                {"outcome": "Yes", "token_id": "1"},
                {"outcome": "No", "token_id": "2"},
                {"outcome": "Maybe", "token_id": "3"},
            ]
        )
        assert md._passes_filter(market) is False

    def test_filter_rejects_missing_yes_token(self) -> None:
        """Both YES and NO token outcomes are required."""
        md = _make_discovery()
        market = _valid_market(
            tokens=[
                {"outcome": "A", "token_id": "1"},
                {"outcome": "B", "token_id": "2"},
            ]
        )
        assert md._passes_filter(market) is False

    def test_filter_rejects_empty_token_ids(self) -> None:
        """token_id must be non-empty for both outcomes."""
        md = _make_discovery()
        market = _valid_market(
            tokens=[
                {"outcome": "Yes", "token_id": ""},
                {"outcome": "No", "token_id": "222"},
            ]
        )
        assert md._passes_filter(market) is False


class TestFilterAccepts:
    """_passes_filter must return True for fully compliant markets."""

    def test_filter_accepts_valid_market(self) -> None:
        """A market satisfying all criteria must be accepted."""
        md = _make_discovery()
        assert md._passes_filter(_valid_market()) is True

    def test_filter_accepts_boundary_volume_min(self) -> None:
        """volume == 1 000 USDC is at the lower boundary — accepted."""
        md = _make_discovery()
        assert md._passes_filter(_valid_market(volume_24hr=1_000.0)) is True

    def test_filter_accepts_boundary_volume_max(self) -> None:
        """volume == 15 000 USDC is at the upper boundary — accepted."""
        md = _make_discovery()
        assert md._passes_filter(_valid_market(volume_24hr=15_000.0)) is True

    def test_filter_accepts_boundary_end_date_min(self) -> None:
        """end_date exactly 3 days from now — accepted (inclusive)."""
        md = _make_discovery()
        # Add a small buffer to avoid flakiness on slow machines
        market = _valid_market(
            end_date_iso=(_now_utc() + timedelta(days=3, seconds=5)).isoformat()
        )
        assert md._passes_filter(market) is True

    def test_filter_accepts_volume_field_alias(self) -> None:
        """API may return volume_24h instead of volume_24hr — both accepted."""
        md = _make_discovery()
        raw = _valid_market()
        del raw["volume_24hr"]
        raw["volume_24h"] = 7_500.0
        assert md._passes_filter(raw) is True


# ── Back-off test ─────────────────────────────────────────────────────────────

class TestBackoffOn429:
    """_fetch_markets must apply exponential back-off on HTTP 429."""

    @pytest.mark.asyncio
    async def test_backoff_on_429(self) -> None:
        """
        Scenario: first two Gamma API calls return 429, third returns 200 [].
        Expected:
          - asyncio.sleep called with 2 then 4 (exponential: 2→4)
          - final result is an empty list
        """
        md = _make_discovery()

        sleep_calls: List[float] = []
        response_payloads = [429, 429, 200]
        call_state = {"idx": 0}

        class _FakeResponse:
            def __init__(self, status: int) -> None:
                self.status_code = status

            def json(self) -> List[Any]:
                return []

            def raise_for_status(self) -> None:
                # Only called for non-429 responses in our implementation
                pass

        class _FakeClient:
            async def __aenter__(self) -> "_FakeClient":
                return self

            async def __aexit__(self, *_: Any) -> None:
                pass

            async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
                status = response_payloads[call_state["idx"]]
                call_state["idx"] += 1
                return _FakeResponse(status)

        async def _fake_sleep(secs: float) -> None:
            sleep_calls.append(secs)

        with (
            patch("asyncio.sleep", side_effect=_fake_sleep),
            patch(
                "cerberus_runtime.market_discovery.httpx.AsyncClient",
                return_value=_FakeClient(),
            ),
        ):
            result = await md._fetch_markets()

        assert result == [], "Expected empty list from 200 response"
        assert len(sleep_calls) == 2, (
            f"Expected 2 back-off sleeps, got {len(sleep_calls)}: {sleep_calls}"
        )
        assert sleep_calls[0] == 2, f"First back-off should be 2s, got {sleep_calls[0]}"
        assert sleep_calls[1] == 4, f"Second back-off should be 4s, got {sleep_calls[1]}"

    @pytest.mark.asyncio
    async def test_backoff_caps_at_64s(self) -> None:
        """
        Back-off must not exceed 64 seconds regardless of how many 429s occur.
        """
        md = _make_discovery()
        sleep_calls: List[float] = []

        # 7 × 429 → back-offs: 2, 4, 8, 16, 32, 64, 64 (capped); then 200
        num_429 = 7
        response_payloads = [429] * num_429 + [200]
        call_state = {"idx": 0}

        class _FakeResponse:
            def __init__(self, status: int) -> None:
                self.status_code = status

            def json(self) -> List[Any]:
                return []

            def raise_for_status(self) -> None:
                pass

        class _FakeClient:
            async def __aenter__(self) -> "_FakeClient":
                return self

            async def __aexit__(self, *_: Any) -> None:
                pass

            async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
                status = response_payloads[call_state["idx"]]
                call_state["idx"] += 1
                return _FakeResponse(status)

        async def _fake_sleep(secs: float) -> None:
            sleep_calls.append(secs)

        with (
            patch("asyncio.sleep", side_effect=_fake_sleep),
            patch(
                "cerberus_runtime.market_discovery.httpx.AsyncClient",
                return_value=_FakeClient(),
            ),
        ):
            await md._fetch_markets()

        assert max(sleep_calls) == 64, (
            f"Back-off ceiling is 64s; got max={max(sleep_calls)}"
        )

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self) -> None:
        """Network timeout → _fetch_markets returns None (skip cycle)."""
        import httpx as _httpx

        md = _make_discovery()

        class _FakeClient:
            async def __aenter__(self) -> "_FakeClient":
                return self

            async def __aexit__(self, *_: Any) -> None:
                pass

            async def get(self, *args: Any, **kwargs: Any) -> None:
                raise _httpx.TimeoutException("timed out")

        with patch(
            "cerberus_runtime.market_discovery.httpx.AsyncClient",
            return_value=_FakeClient(),
        ):
            result = await md._fetch_markets()

        assert result is None

    @pytest.mark.asyncio
    async def test_connect_error_sleeps_60s_and_returns_none(self) -> None:
        """Unreachable host → logs critical, sleeps 60s, returns None."""
        import httpx as _httpx

        md = _make_discovery()
        sleep_calls: List[float] = []

        class _FakeClient:
            async def __aenter__(self) -> "_FakeClient":
                return self

            async def __aexit__(self, *_: Any) -> None:
                pass

            async def get(self, *args: Any, **kwargs: Any) -> None:
                raise _httpx.ConnectError("unreachable")

        async def _fake_sleep(secs: float) -> None:
            sleep_calls.append(secs)

        with (
            patch("asyncio.sleep", side_effect=_fake_sleep),
            patch(
                "cerberus_runtime.market_discovery.httpx.AsyncClient",
                return_value=_FakeClient(),
            ),
        ):
            result = await md._fetch_markets()

        assert result is None
        assert 60 in sleep_calls, f"Expected 60s sleep on ConnectError, got {sleep_calls}"


# ── Scan-level integration tests ──────────────────────────────────────────────

class TestScanBehavior:
    """Higher-level tests for _scan() and run() behaviour."""

    @pytest.mark.asyncio
    async def test_scan_emits_new_market_to_queue(self) -> None:
        """A valid market fetched for the first time must appear in the queue."""
        queue: asyncio.Queue = asyncio.Queue()
        md = _make_discovery(queue=queue)

        raw = [_valid_market(condition_id="0xNEW")]

        with patch.object(md, "_fetch_markets", new=AsyncMock(return_value=raw)):
            await md._scan()

        assert not queue.empty(), "Queue should contain the new market"
        emitted: Market = queue.get_nowait()
        assert emitted.condition_id == "0xNEW"

    @pytest.mark.asyncio
    async def test_scan_does_not_re_emit_known_market(self) -> None:
        """A market seen in a previous cycle must NOT be re-emitted."""
        queue: asyncio.Queue = asyncio.Queue()
        md = _make_discovery(queue=queue)
        raw = [_valid_market(condition_id="0xKNOWN")]

        with patch.object(md, "_fetch_markets", new=AsyncMock(return_value=raw)):
            await md._scan()   # first scan — emits
            await md._scan()   # second scan — must not emit again

        # Queue should have exactly one item
        items: List[Market] = []
        while not queue.empty():
            items.append(queue.get_nowait())
        assert len(items) == 1, f"Expected 1 emission total, got {len(items)}"

    @pytest.mark.asyncio
    async def test_scan_marks_disappeared_market_closed(self) -> None:
        """
        A market present in cycle N but absent in cycle N+1 must be marked
        closed in storage and removed from the active set.
        """
        queue: asyncio.Queue = asyncio.Queue()
        md = _make_discovery(queue=queue)
        storage_mock: MagicMock = md.storage  # type: ignore[assignment]

        raw_first = [_valid_market(condition_id="0xVANISH")]
        raw_second: List[Dict[str, Any]] = []  # market vanished

        with patch.object(
            md,
            "_fetch_markets",
            new=AsyncMock(side_effect=[raw_first, raw_second]),
        ):
            await md._scan()   # market appears
            await md._scan()   # market disappears

        storage_mock.mark_market_closed.assert_awaited_once_with("0xVANISH")
        assert "0xVANISH" not in md._active_candidates

    @pytest.mark.asyncio
    async def test_scan_skips_malformed_market(self) -> None:
        """A malformed raw dict must be skipped without crashing the scan."""
        queue: asyncio.Queue = asyncio.Queue()
        md = _make_discovery(queue=queue)

        # Missing 'condition_id' → _parse_market raises KeyError
        malformed = {
            "active": True,
            "closed": False,
            "neg_risk": False,
            "end_date_iso": (_now_utc() + timedelta(days=10)).isoformat(),
            "volume_24hr": 5_000.0,
            "tokens": [
                {"outcome": "Yes", "token_id": "1"},
                {"outcome": "No", "token_id": "2"},
            ],
        }
        good = _valid_market(condition_id="0xGOOD")

        with patch.object(
            md, "_fetch_markets", new=AsyncMock(return_value=[malformed, good])
        ):
            await md._scan()   # must not raise

        emitted_ids = []
        while not queue.empty():
            emitted_ids.append(queue.get_nowait().condition_id)

        assert "0xGOOD" in emitted_ids, "Valid market after malformed one must still be emitted"

    @pytest.mark.asyncio
    async def test_run_rescans_after_interval(self) -> None:
        """
        run() must call _scan() repeatedly, separated by asyncio.sleep().
        We cancel after 2 scan cycles via a side-effect on sleep.
        """
        md = _make_discovery()
        scan_count = {"n": 0}

        async def _fake_scan() -> None:
            scan_count["n"] += 1

        async def _fake_sleep(_secs: float) -> None:
            if scan_count["n"] >= 2:
                raise asyncio.CancelledError()

        with (
            patch.object(md, "_scan", side_effect=_fake_scan),
            patch("asyncio.sleep", side_effect=_fake_sleep),
        ):
            try:
                await md.run()
            except asyncio.CancelledError:
                pass

        assert scan_count["n"] >= 2, "run() should invoke _scan() at least twice"
