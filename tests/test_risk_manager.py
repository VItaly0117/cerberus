"""
Tests for cerberus_runtime/risk.py — RiskManager.

All 10 tests must pass.  Async tests rely on ``asyncio_mode = auto`` in
setup.cfg so no explicit ``@pytest.mark.asyncio`` decorator is needed.

Ownership: Agent A  [CERBERUS-STRATEGY-UPDATE]
"""
from __future__ import annotations

import time
from decimal import Decimal

import pytest

from cerberus_runtime.models import OrderBookSnapshot, PriceLevel
from cerberus_runtime.risk import AppConfig, ArbitrageResult, CerberusStorage, RiskManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_snapshot(condition_id: str = "market_A") -> OrderBookSnapshot:
    """Return an OrderBookSnapshot whose ts_ms is the current wall-clock ms."""
    now_ms = int(time.time() * 1000)
    return OrderBookSnapshot(
        market_id=condition_id,
        yes_asks=[PriceLevel(price=Decimal("0.50"), size=Decimal("100"))],
        no_asks=[PriceLevel(price=Decimal("0.49"), size=Decimal("100"))],
        timestamp=time.time(),
        condition_id=condition_id,
        ts_ms=now_ms,
    )


def _stale_snapshot(condition_id: str = "market_A") -> OrderBookSnapshot:
    """Return an OrderBookSnapshot with ts_ms = 0 (always stale)."""
    return OrderBookSnapshot(
        market_id=condition_id,
        yes_asks=[PriceLevel(price=Decimal("0.50"), size=Decimal("100"))],
        no_asks=[PriceLevel(price=Decimal("0.49"), size=Decimal("100"))],
        timestamp=0.0,
        condition_id=condition_id,
        ts_ms=0,
    )


class MockStorage(CerberusStorage):
    """In-memory CerberusStorage implementation for unit tests."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    async def insert_risk_event(
        self,
        *,
        event_type: str,
        market_id: str,
        reason: str,
        detail: str,
    ) -> None:
        self.events.append(
            {
                "event_type": event_type,
                "market_id": market_id,
                "reason": reason,
                "detail": detail,
            }
        )


def _make_rm(
    *,
    dry_run_mode: bool = True,
    allow_live_mode: bool = False,
    max_open_markets: int = 1,
    max_attempts_per_hour: int = 60,
    daily_loss_limit_usd: Decimal = Decimal("50.00"),
    market_cooldown_seconds: float = 10.0,
    max_book_age_ms: int = 5_000,
) -> tuple[RiskManager, MockStorage]:
    """Construct a (RiskManager, MockStorage) pair with the given config."""
    cfg = AppConfig(
        dry_run_mode=dry_run_mode,
        allow_live_mode=allow_live_mode,
        max_open_markets=max_open_markets,
        max_attempts_per_hour=max_attempts_per_hour,
        daily_loss_limit_usd=daily_loss_limit_usd,
        market_cooldown_seconds=market_cooldown_seconds,
        max_book_age_ms=max_book_age_ms,
    )
    storage = MockStorage()
    return RiskManager(config=cfg, storage=storage), storage


# ---------------------------------------------------------------------------
# 1. Happy path — all gates open
# ---------------------------------------------------------------------------


def test_allows_clean_path():
    """Fresh snapshot + default config → (True, "") with no gates firing."""
    rm, _ = _make_rm()
    snapshot = _fresh_snapshot("market_A")
    allowed, reason = rm.allows(snapshot)
    assert allowed is True
    assert reason == ""


# ---------------------------------------------------------------------------
# 2. Kill switch blocks everything
# ---------------------------------------------------------------------------


def test_blocks_on_kill_switch():
    """Once _kill_switch is latched, allows() always returns False."""
    rm, _ = _make_rm()
    rm._kill_switch = True
    allowed, reason = rm.allows(_fresh_snapshot("market_A"))
    assert allowed is False
    assert reason == "kill_switch_active"


# ---------------------------------------------------------------------------
# 3. Stale book
# ---------------------------------------------------------------------------


def test_blocks_stale_book():
    """A snapshot with ts_ms == 0 is always stale regardless of wall-clock."""
    rm, _ = _make_rm()
    allowed, reason = rm.allows(_stale_snapshot("market_A"))
    assert allowed is False
    assert reason == "stale_book"


# ---------------------------------------------------------------------------
# 4. Cooldown after SUCCESS
# ---------------------------------------------------------------------------


async def test_blocks_market_in_cooldown():
    """After recording SUCCESS, the same market is in cooldown immediately."""
    rm, _ = _make_rm(market_cooldown_seconds=30.0)
    snap = _fresh_snapshot("market_A")

    # First call succeeds and opens the market.
    allowed, _ = rm.allows(snap)
    assert allowed is True

    # Record success — cooldown is applied (30 s).
    await rm.record_result("market_A", ArbitrageResult.SUCCESS)

    # Immediate re-attempt must be blocked by cooldown.
    allowed2, reason2 = rm.allows(snap)
    assert allowed2 is False
    assert reason2 == "market_in_cooldown"


# ---------------------------------------------------------------------------
# 5. Max open markets cap
# ---------------------------------------------------------------------------


def test_blocks_max_open_markets():
    """With max_open_markets=1, a second distinct market is rejected."""
    rm, _ = _make_rm(max_open_markets=1)

    snap_a = _fresh_snapshot("market_A")
    snap_b = _fresh_snapshot("market_B")

    # Open market_A — should succeed.
    ok_a, _ = rm.allows(snap_a)
    assert ok_a is True
    assert "market_A" in rm._open_markets

    # Attempt market_B — cap is reached (1 open already).
    ok_b, reason_b = rm.allows(snap_b)
    assert ok_b is False
    assert reason_b == "max_open_markets_reached"


# ---------------------------------------------------------------------------
# 6. Hourly attempt cap
# ---------------------------------------------------------------------------


def test_blocks_hourly_cap():
    """When _attempts_this_hour == max, the next attempt is rejected."""
    rm, _ = _make_rm(max_attempts_per_hour=5)
    # Force the counter to the limit without going through allows().
    rm._attempts_this_hour = 5
    # Ensure _last_hour is set so _refresh_hour_counter() won't reset.
    rm._last_hour = int(time.monotonic() // 3600)

    allowed, reason = rm.allows(_fresh_snapshot("market_A"))
    assert allowed is False
    assert reason == "hourly_attempt_cap"


# ---------------------------------------------------------------------------
# 7. Daily loss limit latches kill switch
# ---------------------------------------------------------------------------


async def test_kill_switch_on_daily_loss_limit():
    """Cumulative LEGGED_RISK losses >= limit latch the kill switch."""
    rm, storage = _make_rm(daily_loss_limit_usd=Decimal("10.00"))

    snap = _fresh_snapshot("market_A")

    # Allow the first attempt.
    ok, _ = rm.allows(snap)
    assert ok is True

    # Record a LEGGED_RISK that exactly hits the limit.
    await rm.record_result("market_A", ArbitrageResult.LEGGED_RISK, Decimal("10.00"))

    assert rm.is_killed() is True
    assert rm._daily_loss_usd == Decimal("10.00")

    # Subsequent attempt must be blocked by the kill switch.
    ok2, reason2 = rm.allows(_fresh_snapshot("market_B"))
    assert ok2 is False
    assert reason2 == "kill_switch_active"

    # Risk event must have been persisted.
    assert len(storage.events) == 1
    assert storage.events[0]["event_type"] == "legged_incident"


# ---------------------------------------------------------------------------
# 8. LEGGED_RISK applies triple cooldown
# ---------------------------------------------------------------------------


async def test_legged_risk_applies_triple_cooldown():
    """Cooldown duration after LEGGED_RISK must be exactly 3× normal cooldown."""
    cooldown_s = 10.0
    rm, _ = _make_rm(
        market_cooldown_seconds=cooldown_s,
        daily_loss_limit_usd=Decimal("999.00"),  # high limit — don't latch kill switch
    )

    # Open the market.
    rm.allows(_fresh_snapshot("market_A"))

    before = time.monotonic()
    await rm.record_result("market_A", ArbitrageResult.LEGGED_RISK, Decimal("1.00"))
    after = time.monotonic()

    cooldown_until = rm._market_cooldowns.get("market_A")
    assert cooldown_until is not None

    # The cooldown deadline must be before + 3×cooldown_s ± 1 s tolerance.
    expected = before + cooldown_s * 3
    assert abs(cooldown_until - expected) < 1.0, (
        f"Expected cooldown_until ≈ {expected:.2f}, got {cooldown_until:.2f}"
    )


# ---------------------------------------------------------------------------
# 9. record_result removes condition_id from open markets
# ---------------------------------------------------------------------------


async def test_record_result_removes_open_market():
    """Any result (SUCCESS here) causes condition_id to leave _open_markets."""
    rm, _ = _make_rm()
    snap = _fresh_snapshot("market_A")

    # Open the market.
    allowed, _ = rm.allows(snap)
    assert allowed is True
    assert "market_A" in rm._open_markets

    # Record result.
    await rm.record_result("market_A", ArbitrageResult.SUCCESS)
    assert "market_A" not in rm._open_markets


# ---------------------------------------------------------------------------
# 10. Dry-run off + live mode off → blocked
# ---------------------------------------------------------------------------


def test_dry_run_blocks_when_live_not_allowed():
    """dry_run_mode=False and allow_live_mode=False must block the attempt."""
    rm, _ = _make_rm(dry_run_mode=False, allow_live_mode=False)
    allowed, reason = rm.allows(_fresh_snapshot("market_A"))
    assert allowed is False
    assert reason == "live_mode_not_allowed"
