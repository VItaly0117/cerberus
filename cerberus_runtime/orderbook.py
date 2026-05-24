"""
LocalOrderBook — Agent B owned module.

Maintains the L2 ask-side state for one binary prediction market
(YES leg and NO leg) using events received from the Polymarket CLOB
WebSocket.

Constraints
-----------
- Never import from market_discovery.py, fee_model.py, core.py,
  risk.py, or executor.py.
- All state mutations are synchronous (no I/O).
- Hash verification is done per-token after every state update.
"""
from __future__ import annotations

import hashlib
import time
from typing import Optional

from cerberus_runtime.models import FeeParams, OrderBookSnapshot, PriceLevel


class LocalOrderBook:
    """
    Maintains L2 ask-side state for one binary market (YES + NO legs).

    Parameters
    ----------
    yes_token_id:
        ERC-1155 token ID for the YES outcome.
    no_token_id:
        ERC-1155 token ID for the NO outcome.
    """

    def __init__(self, yes_token_id: str, no_token_id: str) -> None:
        self.yes_token_id: str = yes_token_id
        self.no_token_id: str = no_token_id

        # Ask-side price levels, sorted by price ascending.
        self.yes_asks: list[PriceLevel] = []
        self.no_asks: list[PriceLevel] = []

        # Set to True when a resync from the REST API is needed.
        self.needs_resync: bool = False

        # Epoch-ms of the last event that touched this book.
        self.ts_ms: int = 0

    # ------------------------------------------------------------------ #
    # Private helpers                                                      #
    # ------------------------------------------------------------------ #

    def _update_ts(self, event: dict) -> None:
        """Update ts_ms from the event's timestamp field (ms string)."""
        ts_str = event.get("timestamp", "")
        if ts_str:
            try:
                self.ts_ms = int(ts_str)
            except (ValueError, TypeError):
                pass

    @staticmethod
    def _parse_levels(raw: list[dict]) -> list[PriceLevel]:
        """
        Convert raw price-level dicts to a sorted list of PriceLevel.

        Levels with size == 0 are discarded (they represent removals in
        a delta, or are simply absent from a snapshot).
        """
        levels: list[PriceLevel] = []
        for entry in raw:
            size = float(entry.get("size", 0))
            if size > 0:
                levels.append(PriceLevel(price=float(entry["price"]), size=size))
        levels.sort(key=lambda pl: pl.price)
        return levels

    @staticmethod
    def _apply_changes(
        current: list[PriceLevel],
        changes: list[dict],
    ) -> list[PriceLevel]:
        """
        Apply delta changes to an ask list and return the updated list.

        A change with size == 0 removes the level; any other size adds
        or replaces the level at that price.
        """
        book: dict[float, float] = {pl.price: pl.size for pl in current}
        for ch in changes:
            price = float(ch["price"])
            size = float(ch["size"])
            if size == 0.0:
                book.pop(price, None)
            else:
                book[price] = size
        return sorted(
            [PriceLevel(price=p, size=s) for p, s in book.items()],
            key=lambda pl: pl.price,
        )

    def _token_hash(self, asks: list[PriceLevel]) -> str:
        """
        SHA-256 of a canonical, deterministic serialisation of an ask list.

        Format: "price:size,price:size,…" with 6 decimal places, sorted
        by price ascending (list is assumed already sorted).
        """
        parts = [f"{pl.price:.6f}:{pl.size:.6f}" for pl in asks]
        return hashlib.sha256(",".join(parts).encode()).hexdigest()

    def _verify_hash(self, asks: list[PriceLevel], event_hash: str) -> None:
        """
        Compare the locally computed hash against the hash in the event.

        Sets needs_resync=True on mismatch.  Empty or missing event_hash
        is treated as "no verification" and is silently skipped.
        """
        if not event_hash:
            return
        if self._token_hash(asks) != event_hash:
            self.needs_resync = True

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def apply_book_event(self, event: dict) -> None:
        """
        Apply a full L2 book snapshot received from the CLOB WebSocket.

        Expected event keys
        -------------------
        event_type : str   — "book"
        asset_id   : str   — token ID (yes or no)
        asks       : list  — [{"price": "0.60", "size": "100"}, …]
        hash       : str   — expected SHA-256 of the book state
        timestamp  : str   — epoch-ms string
        """
        self._update_ts(event)

        asset_id = event.get("asset_id", "")
        raw_asks = event.get("asks", [])
        levels = self._parse_levels(raw_asks)
        event_hash = event.get("hash", "")

        if asset_id == self.yes_token_id:
            self.yes_asks = levels
            self._verify_hash(self.yes_asks, event_hash)
        elif asset_id == self.no_token_id:
            self.no_asks = levels
            self._verify_hash(self.no_asks, event_hash)

    def apply_price_change(self, event: dict) -> None:
        """
        Apply a delta price-level update from the CLOB WebSocket.

        Handles two event types:
        - "price_change"  — update one or more price levels on one leg.
        - "tick_size_change" — do NOT apply any delta; mark needs_resync.

        Expected event keys (price_change)
        -----------------------------------
        event_type : str   — "price_change" | "tick_size_change"
        asset_id   : str   — token ID
        changes    : list  — [{"price": "0.62", "size": "50", "side": "SELL"}, …]
        hash       : str   — expected hash after applying the changes
        timestamp  : str   — epoch-ms string
        """
        # ts_ms is updated for every event, including tick_size_change.
        self._update_ts(event)

        event_type = event.get("event_type", "price_change")
        if event_type == "tick_size_change":
            # Tick size changed — our level spacing is now invalid; resync needed.
            self.needs_resync = True
            return

        asset_id = event.get("asset_id", "")
        changes = event.get("changes", [])
        # Only the ask (SELL) side is tracked; skip bid-side changes.
        sell_changes = [
            c for c in changes
            if c.get("side", "SELL").upper() in ("SELL", "ASK")
        ]
        event_hash = event.get("hash", "")

        if asset_id == self.yes_token_id:
            self.yes_asks = self._apply_changes(self.yes_asks, sell_changes)
            self._verify_hash(self.yes_asks, event_hash)
        elif asset_id == self.no_token_id:
            self.no_asks = self._apply_changes(self.no_asks, sell_changes)
            self._verify_hash(self.no_asks, event_hash)

    def get_snapshot(
        self,
        market_id: str,
        condition_id: str,
        yes_token_id: str,
        no_token_id: str,
        fee_params: Optional[FeeParams],
    ) -> OrderBookSnapshot:
        """
        Return a point-in-time snapshot of this order book.

        The caller is responsible for verifying freshness before calling
        this method (use is_fresh()).
        """
        return OrderBookSnapshot(
            market_id=market_id,
            condition_id=condition_id,
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            fee_params=fee_params,
            yes_asks=list(self.yes_asks),   # defensive copy
            no_asks=list(self.no_asks),
            ts_ms=self.ts_ms,
            book_hash=self.book_hash(),
        )

    def is_fresh(self, max_age_ms: int) -> bool:
        """
        Return True if the book received an update within *max_age_ms* ms.

        Returns False if the book has never been updated (ts_ms == 0).
        """
        if self.ts_ms == 0:
            return False
        age_ms = int(time.time() * 1000) - self.ts_ms
        return age_ms <= max_age_ms

    def book_hash(self) -> str:
        """
        SHA-256 digest covering both legs' sorted ask lists.

        Computed as: sha256( sha256(yes_asks) + ":" + sha256(no_asks) ).
        Used for cross-component integrity checks.
        """
        yes_h = self._token_hash(self.yes_asks)
        no_h = self._token_hash(self.no_asks)
        combined = f"{yes_h}:{no_h}"
        return hashlib.sha256(combined.encode()).hexdigest()
