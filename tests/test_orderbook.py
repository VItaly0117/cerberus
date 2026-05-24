"""
Unit tests for cerberus_runtime.orderbook.LocalOrderBook.

All tests use synthetic event dicts — no real WebSocket connections.
Run with:  pytest -v tests/test_orderbook.py
"""
from __future__ import annotations

import hashlib
import time
from decimal import Decimal

import pytest

from cerberus_runtime.models import FeeParams, OrderBookSnapshot, PriceLevel
from cerberus_runtime.orderbook import LocalOrderBook


# ──────────────────────────────────────────────────────────────────────────────
# Helpers — mirror LocalOrderBook._token_hash so tests can build valid hashes
# ──────────────────────────────────────────────────────────────────────────────

def _token_hash(price_levels: list[PriceLevel]) -> str:
    """Canonical SHA-256 of a sorted ask list — must match _token_hash() exactly."""
    parts = [f"{pl.price:.6f}:{pl.size:.6f}" for pl in price_levels]
    return hashlib.sha256(",".join(parts).encode()).hexdigest()


def _make_book_event(
    asset_id: str,
    asks: list[dict],
    timestamp: str = "1000",
    corrupt_hash: bool = False,
) -> dict:
    """
    Build a synthetic 'book' event dict with a correctly computed hash.

    PriceLevel fields use Decimal (via Decimal(str(...))) to match the
    canonical representation used by LocalOrderBook._parse_levels.

    Set *corrupt_hash=True* to deliberately provide the wrong hash so
    tests can verify that needs_resync is triggered.
    """
    levels = sorted(
        [
            PriceLevel(price=Decimal(str(a["price"])), size=Decimal(str(a["size"])))
            for a in asks
            if Decimal(str(a["size"])) > Decimal("0")
        ],
        key=lambda pl: pl.price,
    )
    good_hash = _token_hash(levels)
    return {
        "event_type": "book",
        "asset_id": asset_id,
        "asks": asks,
        "hash": "deadbeef_invalid_hash" if corrupt_hash else good_hash,
        "timestamp": timestamp,
    }


def _make_price_change_event(
    asset_id: str,
    current_yes_asks: list[PriceLevel],
    changes: list[dict],
    timestamp: str = "2000",
    corrupt_hash: bool = False,
) -> dict:
    """
    Build a synthetic 'price_change' event dict.

    Applies *changes* to *current_yes_asks* locally so it can embed the
    correct expected hash.  Uses Decimal keys to match _apply_changes exactly.
    Set *corrupt_hash=True* to force mismatch.
    """
    # Replicate _apply_changes logic using Decimal keys for hash consistency.
    book: dict[str, Decimal] = {str(pl.price): pl.size for pl in current_yes_asks}
    sell_changes = [c for c in changes if c.get("side", "SELL").upper() in ("SELL", "ASK")]
    for ch in sell_changes:
        price_str = str(Decimal(str(ch["price"])))
        size = Decimal(str(ch["size"]))
        if size == Decimal("0"):
            book.pop(price_str, None)
        else:
            book[price_str] = size
    updated = sorted(
        [PriceLevel(Decimal(p), s) for p, s in book.items()],
        key=lambda pl: pl.price,
    )
    good_hash = _token_hash(updated)
    return {
        "event_type": "price_change",
        "asset_id": asset_id,
        "changes": changes,
        "hash": "deadbeef_invalid_hash" if corrupt_hash else good_hash,
        "timestamp": timestamp,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def book() -> LocalOrderBook:
    """Fresh LocalOrderBook with well-known token IDs."""
    return LocalOrderBook(yes_token_id="YES_TOKEN", no_token_id="NO_TOKEN")


@pytest.fixture
def seeded_book(book: LocalOrderBook) -> LocalOrderBook:
    """
    LocalOrderBook with both legs populated with two ask levels each.
    Timestamp set to *now* so the book is fresh.
    """
    now_ms = str(int(time.time() * 1000))
    book.apply_book_event(
        _make_book_event(
            "YES_TOKEN",
            [{"price": "0.60", "size": "100"}, {"price": "0.65", "size": "200"}],
            timestamp=now_ms,
        )
    )
    book.apply_book_event(
        _make_book_event(
            "NO_TOKEN",
            [{"price": "0.35", "size": "150"}, {"price": "0.40", "size": "50"}],
            timestamp=now_ms,
        )
    )
    return book


# ──────────────────────────────────────────────────────────────────────────────
# 1. test_apply_book_snapshot_sets_asks
# ──────────────────────────────────────────────────────────────────────────────

def test_apply_book_snapshot_sets_asks(book: LocalOrderBook) -> None:
    """
    Applying a full book snapshot must populate yes_asks and no_asks,
    sort them by price ascending, and leave needs_resync=False when the
    hash is correct.
    """
    yes_raw = [{"price": "0.65", "size": "200"}, {"price": "0.60", "size": "100"}]
    no_raw  = [{"price": "0.40", "size": "50"},  {"price": "0.35", "size": "150"}]

    book.apply_book_event(_make_book_event("YES_TOKEN", yes_raw))
    book.apply_book_event(_make_book_event("NO_TOKEN",  no_raw))

    # Correct number of levels populated.
    assert len(book.yes_asks) == 2
    assert len(book.no_asks)  == 2

    # Sorted by price ascending (Decimal comparisons).
    assert book.yes_asks[0].price == Decimal("0.60")
    assert book.yes_asks[1].price == Decimal("0.65")
    assert book.no_asks[0].price  == Decimal("0.35")
    assert book.no_asks[1].price  == Decimal("0.40")

    # Sizes preserved.
    assert book.yes_asks[0].size == Decimal("100")
    assert book.yes_asks[1].size == Decimal("200")

    # Hash was correct → no resync needed.
    assert not book.needs_resync


# ──────────────────────────────────────────────────────────────────────────────
# 2. test_is_fresh_returns_false_after_max_age
# ──────────────────────────────────────────────────────────────────────────────

def test_is_fresh_returns_false_after_max_age(book: LocalOrderBook) -> None:
    """
    A book whose last event is older than max_age_ms must report stale.
    """
    # Simulate an event that happened 10 seconds ago.
    old_ts_ms = int(time.time() * 1000) - 10_000
    event = {
        "event_type": "book",
        "asset_id": "YES_TOKEN",
        "asks": [{"price": "0.60", "size": "100"}],
        "hash": "",          # skip hash verification for this test
        "timestamp": str(old_ts_ms),
    }
    book.apply_book_event(event)

    # 5-second window should be exceeded.
    assert not book.is_fresh(max_age_ms=5_000)


def test_is_fresh_returns_true_for_recent_event(book: LocalOrderBook) -> None:
    """
    A book whose last event is within max_age_ms must report fresh.
    (This is a sanity guard for the timestamp logic.)
    """
    now_ms = int(time.time() * 1000)
    event = {
        "event_type": "book",
        "asset_id": "YES_TOKEN",
        "asks": [{"price": "0.60", "size": "100"}],
        "hash": "",
        "timestamp": str(now_ms),
    }
    book.apply_book_event(event)
    assert book.is_fresh(max_age_ms=5_000)


def test_is_fresh_returns_false_before_any_event(book: LocalOrderBook) -> None:
    """A book with ts_ms == 0 (never updated) must not be considered fresh."""
    assert not book.is_fresh(max_age_ms=60_000)


# ──────────────────────────────────────────────────────────────────────────────
# 3. test_hash_mismatch_sets_resync_flag
# ──────────────────────────────────────────────────────────────────────────────

def test_hash_mismatch_sets_resync_flag(seeded_book: LocalOrderBook) -> None:
    """
    A price_change event with an incorrect hash must set needs_resync=True.
    """
    assert not seeded_book.needs_resync   # baseline

    bad_event = _make_price_change_event(
        asset_id="YES_TOKEN",
        current_yes_asks=seeded_book.yes_asks,
        changes=[{"price": "0.62", "size": "50", "side": "SELL"}],
        corrupt_hash=True,          # deliberately wrong hash
    )
    seeded_book.apply_price_change(bad_event)

    assert seeded_book.needs_resync


def test_hash_match_does_not_set_resync_flag(seeded_book: LocalOrderBook) -> None:
    """
    A price_change event with a correct hash must NOT set needs_resync.
    (Complement to test_hash_mismatch_sets_resync_flag.)
    """
    good_event = _make_price_change_event(
        asset_id="YES_TOKEN",
        current_yes_asks=seeded_book.yes_asks,
        changes=[{"price": "0.62", "size": "50", "side": "SELL"}],
        corrupt_hash=False,
    )
    seeded_book.apply_price_change(good_event)

    assert not seeded_book.needs_resync


# ──────────────────────────────────────────────────────────────────────────────
# 4. test_tick_size_change_sets_resync_flag
# ──────────────────────────────────────────────────────────────────────────────

def test_tick_size_change_sets_resync_flag(seeded_book: LocalOrderBook) -> None:
    """
    A tick_size_change event must set needs_resync=True without
    modifying yes_asks or no_asks.
    """
    snapshot_yes = list(seeded_book.yes_asks)
    snapshot_no  = list(seeded_book.no_asks)
    assert not seeded_book.needs_resync   # baseline

    tick_event = {
        "event_type": "tick_size_change",
        "asset_id": "YES_TOKEN",
        "timestamp": "9999",
    }
    seeded_book.apply_price_change(tick_event)

    assert seeded_book.needs_resync
    # Book state must be completely unchanged.
    assert seeded_book.yes_asks == snapshot_yes
    assert seeded_book.no_asks  == snapshot_no


# ──────────────────────────────────────────────────────────────────────────────
# 5. test_get_snapshot_returns_correct_dataclass
# ──────────────────────────────────────────────────────────────────────────────

def test_get_snapshot_returns_correct_dataclass(seeded_book: LocalOrderBook) -> None:
    """
    get_snapshot() must return an OrderBookSnapshot with all fields
    populated correctly.
    """
    fee = FeeParams(fees_enabled=True, maker_fee_rate=0.001, taker_fee_rate=0.002)

    snap = seeded_book.get_snapshot(
        market_id="COND_ABC",
        condition_id="COND_ABC",
        yes_token_id="YES_TOKEN",
        no_token_id="NO_TOKEN",
        fee_params=fee,
    )

    assert isinstance(snap, OrderBookSnapshot)
    assert snap.market_id == "COND_ABC"
    assert snap.condition_id == "COND_ABC"
    assert snap.yes_token_id == "YES_TOKEN"
    assert snap.no_token_id  == "NO_TOKEN"
    assert snap.fee_params is fee

    # Both legs are present.
    assert len(snap.yes_asks) == 2
    assert len(snap.no_asks)  == 2

    # ts_ms came from the fixture's events.
    assert snap.ts_ms > 0

    # book_hash is a 64-char hex SHA-256 string.
    assert isinstance(snap.book_hash, str)
    assert len(snap.book_hash) == 64
    assert all(c in "0123456789abcdef" for c in snap.book_hash)

    # Snapshot is a defensive copy — mutating the snapshot must not affect the book.
    snap.yes_asks.clear()
    assert len(seeded_book.yes_asks) == 2


def test_get_snapshot_with_no_fee_params(seeded_book: LocalOrderBook) -> None:
    """get_snapshot() must accept fee_params=None without raising."""
    snap = seeded_book.get_snapshot(
        market_id="MKT",
        condition_id="MKT",
        yes_token_id="YES_TOKEN",
        no_token_id="NO_TOKEN",
        fee_params=None,
    )
    assert snap.fee_params is None


# ──────────────────────────────────────────────────────────────────────────────
# 6. test_apply_price_change_updates_level
# ──────────────────────────────────────────────────────────────────────────────

def test_apply_price_change_updates_level(book: LocalOrderBook) -> None:
    """
    A price_change delta must:
    - Add a new price level (0.62 / 50).
    - Remove an existing level (0.65 → size 0).
    - Leave unchanged levels intact (0.60 / 100).
    - Not set needs_resync when the hash is correct.
    """
    now_ms = str(int(time.time() * 1000))
    # Seed YES leg with two levels.
    book.apply_book_event(
        _make_book_event(
            "YES_TOKEN",
            [{"price": "0.60", "size": "100"}, {"price": "0.65", "size": "200"}],
            timestamp=now_ms,
        )
    )
    assert len(book.yes_asks) == 2

    # Build the delta.
    delta_event = _make_price_change_event(
        asset_id="YES_TOKEN",
        current_yes_asks=book.yes_asks,
        changes=[
            {"price": "0.62", "size": "50",  "side": "SELL"},   # add new level
            {"price": "0.65", "size": "0",   "side": "SELL"},   # remove existing
        ],
        timestamp=str(int(time.time() * 1000)),
    )
    book.apply_price_change(delta_event)

    # Hash matched → no resync.
    assert not book.needs_resync

    # Expected post-delta state: [0.60/100, 0.62/50].
    assert len(book.yes_asks) == 2
    prices = {pl.price for pl in book.yes_asks}
    assert Decimal("0.60") in prices
    assert Decimal("0.62") in prices
    assert Decimal("0.65") not in prices

    # Size of the unchanged level is intact.
    level_060 = next(pl for pl in book.yes_asks if pl.price == Decimal("0.60"))
    assert level_060.size == Decimal("100")

    # Size of the new level is correct.
    level_062 = next(pl for pl in book.yes_asks if pl.price == Decimal("0.62"))
    assert level_062.size == Decimal("50")


def test_apply_price_change_unknown_asset_is_noop(book: LocalOrderBook) -> None:
    """price_change for an asset_id we don't track must be silently ignored."""
    book.apply_book_event(
        _make_book_event("YES_TOKEN", [{"price": "0.60", "size": "100"}])
    )
    before = list(book.yes_asks)

    unknown_event = {
        "event_type": "price_change",
        "asset_id": "UNKNOWN_TOKEN",
        "changes": [{"price": "0.62", "size": "50", "side": "SELL"}],
        "hash": "ignored",
        "timestamp": "5000",
    }
    book.apply_price_change(unknown_event)

    assert book.yes_asks == before
    assert not book.needs_resync


def test_apply_price_change_updates_existing_size(book: LocalOrderBook) -> None:
    """price_change with a non-zero size for an existing price must update the size."""
    now_ms = str(int(time.time() * 1000))
    book.apply_book_event(
        _make_book_event(
            "YES_TOKEN",
            [{"price": "0.60", "size": "100"}],
            timestamp=now_ms,
        )
    )

    delta_event = _make_price_change_event(
        asset_id="YES_TOKEN",
        current_yes_asks=book.yes_asks,
        changes=[{"price": "0.60", "size": "250", "side": "SELL"}],
        timestamp=str(int(time.time() * 1000)),
    )
    book.apply_price_change(delta_event)

    assert len(book.yes_asks) == 1
    assert book.yes_asks[0].price == Decimal("0.60")
    assert book.yes_asks[0].size  == Decimal("250")
    assert not book.needs_resync
