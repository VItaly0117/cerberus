"""
Async SQLite-backed persistence layer for Cerberus runtime data.

Only market_discovery.py should write market rows; other components read-only.
"""
from __future__ import annotations

import logging
import statistics
from decimal import Decimal
from typing import Optional

import aiosqlite

from cerberus_runtime.models import ArbitrageSignal, FeeParams, Market, ResolutionSignal

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS markets (
    condition_id   TEXT PRIMARY KEY,
    yes_token_id   TEXT NOT NULL,
    no_token_id    TEXT NOT NULL,
    category       TEXT NOT NULL DEFAULT '',
    fees_enabled   INTEGER NOT NULL DEFAULT 0,
    maker_fee_rate REAL NOT NULL DEFAULT 0.0,
    taker_fee_rate REAL NOT NULL DEFAULT 0.0,
    min_order_size REAL NOT NULL DEFAULT 0.0,
    tick_size      REAL NOT NULL DEFAULT 0.0,
    end_date       TEXT NOT NULL,
    volume_24h     REAL NOT NULL DEFAULT 0.0,
    active         INTEGER NOT NULL DEFAULT 1,
    closed         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS paper_signals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id        TEXT NOT NULL,
    condition_id     TEXT NOT NULL DEFAULT '',
    ts_ms            INTEGER NOT NULL DEFAULT 0,
    yes_best_ask     REAL,
    no_best_ask      REAL,
    edge_gross       REAL,
    fees_total       REAL,
    risk_haircut     REAL,
    edge_net         REAL,
    edge_net_pct     REAL,
    result           TEXT NOT NULL,
    rejection_reason TEXT NOT NULL DEFAULT '',
    simulated_pnl    REAL NOT NULL DEFAULT 0.0,
    recorded_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_DDL_RUNTIME_HEALTH = """
CREATE TABLE IF NOT EXISTS runtime_health (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_DDL_RESOLUTION = """
CREATE TABLE IF NOT EXISTS resolution_signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id     TEXT NOT NULL,
    condition_id  TEXT NOT NULL DEFAULT '',
    outcome       TEXT NOT NULL,
    token_id      TEXT NOT NULL DEFAULT '',
    current_ask   REAL NOT NULL,
    edge_net_pct  REAL NOT NULL,
    fee_usdc      REAL NOT NULL DEFAULT 0.0,
    confidence    TEXT NOT NULL DEFAULT 'confirmed',
    source        TEXT NOT NULL DEFAULT 'gamma_api',
    simulated_pnl REAL NOT NULL DEFAULT 0.0,
    ts_ms         INTEGER NOT NULL DEFAULT 0,
    recorded_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

_INSERT_RESOLUTION_SIGNAL = """
INSERT INTO resolution_signals (
    market_id, condition_id, outcome, token_id,
    current_ask, edge_net_pct, fee_usdc,
    confidence, source, simulated_pnl, ts_ms
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_UPSERT = """
INSERT INTO markets (
    condition_id, yes_token_id, no_token_id, category,
    fees_enabled, maker_fee_rate, taker_fee_rate,
    min_order_size, tick_size, end_date, volume_24h,
    active, closed
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(condition_id) DO UPDATE SET
    yes_token_id   = excluded.yes_token_id,
    no_token_id    = excluded.no_token_id,
    category       = excluded.category,
    fees_enabled   = excluded.fees_enabled,
    maker_fee_rate = excluded.maker_fee_rate,
    taker_fee_rate = excluded.taker_fee_rate,
    min_order_size = excluded.min_order_size,
    tick_size      = excluded.tick_size,
    end_date       = excluded.end_date,
    volume_24h     = excluded.volume_24h,
    active         = excluded.active,
    closed         = excluded.closed;
"""

_INSERT_PAPER_SIGNAL = """
INSERT INTO paper_signals (
    market_id, condition_id, ts_ms,
    yes_best_ask, no_best_ask,
    edge_gross, fees_total, risk_haircut, edge_net, edge_net_pct,
    result, rejection_reason, simulated_pnl
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class Storage:
    """
    Async wrapper around a SQLite database.

    Usage::

        storage = Storage(db_path="cerberus.db")
        await storage.connect()
        await storage.upsert_market(market)
        await storage.close()
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the database connection and create schema if needed."""
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_DDL)
        await self._conn.executescript(_DDL_RESOLUTION)
        await self._conn.executescript(_DDL_RUNTIME_HEALTH)
        await self._conn.commit()
        logger.debug("Storage connected: %s", self.db_path)

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Markets table
    # ------------------------------------------------------------------

    async def upsert_market(self, market: Market) -> None:
        """Insert or update a market row (keyed on condition_id)."""
        assert self._conn, "Call connect() before upsert_market()"
        await self._conn.execute(
            _UPSERT,
            (
                market.condition_id,
                market.yes_token_id,
                market.no_token_id,
                market.category,
                int(market.fee_params.fees_enabled),
                market.fee_params.maker_fee_rate,
                market.fee_params.taker_fee_rate,
                market.min_order_size,
                market.tick_size,
                market.end_date.isoformat(),
                market.volume_24h,
                int(market.active),
                int(market.closed),
            ),
        )
        await self._conn.commit()

    async def mark_market_closed(self, condition_id: str) -> None:
        """Set active=0 and closed=1 for a market that has resolved."""
        assert self._conn, "Call connect() before mark_market_closed()"
        await self._conn.execute(
            "UPDATE markets SET closed = 1, active = 0 WHERE condition_id = ?",
            (condition_id,),
        )
        await self._conn.commit()
        logger.info("Storage: marked market %s as closed.", condition_id)

    # ------------------------------------------------------------------
    # paper_signals table
    # ------------------------------------------------------------------

    async def insert_paper_signal(
        self,
        signal: Optional[ArbitrageSignal],
        result: str,
        rejection_reason: str = "",
        simulated_pnl: Decimal = Decimal("0"),
    ) -> None:
        """Insert one paper-trading event row.

        Args:
            signal:           The ArbitrageSignal if one was computed; ``None``
                              when the snapshot was blocked or filtered before
                              reaching the evaluator.
            result:           Human-readable outcome string, e.g.
                              ``"SUCCESS"``, ``"FILTERED"``, ``"BLOCKED_BY_RISK"``.
            rejection_reason: Machine-readable reason for filtered/blocked rows
                              (e.g. ``"stale_book"``, ``"edge_below_threshold"``).
            simulated_pnl:    Simulated P&L in USDC (positive = profit,
                              negative = loss). Defaults to ``Decimal("0")``.
        """
        assert self._conn, "Call connect() before insert_paper_signal()"

        if signal is not None:
            yes_best = (
                float(signal.yes_quote.avg_price) if signal.yes_quote else None
            )
            no_best = (
                float(signal.no_quote.avg_price) if signal.no_quote else None
            )
            edge_gross   = float(signal.edge_gross)
            fees_total   = float(signal.fees_total)
            risk_haircut = float(signal.risk_haircut)
            edge_net     = float(signal.edge_net)
            edge_net_pct = float(signal.edge_net_pct)
            market_id    = signal.market_id
            condition_id = ""  # signal doesn't carry condition_id separately
            ts_ms        = 0
        else:
            yes_best = no_best = None
            edge_gross = fees_total = risk_haircut = edge_net = edge_net_pct = None
            market_id = rejection_reason or result
            condition_id = ""
            ts_ms = 0

        await self._conn.execute(
            _INSERT_PAPER_SIGNAL,
            (
                market_id,
                condition_id,
                ts_ms,
                yes_best,
                no_best,
                edge_gross,
                fees_total,
                risk_haircut,
                edge_net,
                edge_net_pct,
                result,
                rejection_reason,
                float(simulated_pnl),
            ),
        )
        await self._conn.commit()

    async def insert_paper_signal_from_snapshot(
        self,
        market_id: str,
        condition_id: str,
        ts_ms: int,
        signal: Optional[ArbitrageSignal],
        result: str,
        rejection_reason: str = "",
        simulated_pnl: Decimal = Decimal("0"),
    ) -> None:
        """Variant with explicit market/snapshot metadata (used by cerberustest.py)."""
        assert self._conn, "Call connect() before insert_paper_signal_from_snapshot()"

        if signal is not None:
            yes_best     = float(signal.yes_quote.avg_price)
            no_best      = float(signal.no_quote.avg_price)
            edge_gross   = float(signal.edge_gross)
            fees_total   = float(signal.fees_total)
            risk_haircut = float(signal.risk_haircut)
            edge_net     = float(signal.edge_net)
            edge_net_pct = float(signal.edge_net_pct)
        else:
            yes_best = no_best = None
            edge_gross = fees_total = risk_haircut = edge_net = edge_net_pct = None

        await self._conn.execute(
            _INSERT_PAPER_SIGNAL,
            (
                market_id,
                condition_id,
                ts_ms,
                yes_best,
                no_best,
                edge_gross,
                fees_total,
                risk_haircut,
                edge_net,
                edge_net_pct,
                result,
                rejection_reason,
                float(simulated_pnl),
            ),
        )
        await self._conn.commit()

    async def get_paper_summary(self, since_ts_ms: int = 0) -> dict:
        """Return aggregated paper-trading statistics since *since_ts_ms*.

        Args:
            since_ts_ms: Only include rows with ``ts_ms >= since_ts_ms``.
                         Pass ``0`` to include all rows.

        Returns:
            dict with keys:
              total_snapshots_evaluated  int
              viable_signals             int   — edge_net > 0
              blocked_by_risk            int
              clean_misses               int
              successes                  int
              legged_incidents           int
              total_simulated_pnl        Decimal
              median_edge_net_pct        Decimal  — median over SUCCESS rows
              stale_book_rejections      int
        """
        assert self._conn, "Call connect() before get_paper_summary()"

        async with self._conn.execute(
            "SELECT result, rejection_reason, edge_net, edge_net_pct, simulated_pnl "
            "FROM paper_signals WHERE ts_ms >= ?",
            (since_ts_ms,),
        ) as cur:
            rows = await cur.fetchall()

        total = len(rows)
        viable = blocked = clean_misses = successes = legged = stale = 0
        total_pnl = Decimal("0")
        success_edge_pcts: list[float] = []

        for row in rows:
            result = row["result"]
            reason = row["rejection_reason"] or ""
            edge_net = row["edge_net"]
            pnl = Decimal(str(row["simulated_pnl"]))
            total_pnl += pnl

            if result == "SUCCESS":
                successes += 1
                viable += 1
                if row["edge_net_pct"] is not None:
                    success_edge_pcts.append(float(row["edge_net_pct"]))
            elif result in ("CLEAN_MISS", "LEGGED_RISK", "REPAIRED"):
                viable += 1
                if result == "LEGGED_RISK":
                    legged += 1
                if result == "CLEAN_MISS":
                    clean_misses += 1
            elif result == "BLOCKED_BY_RISK":
                blocked += 1
                if reason == "stale_book":
                    stale += 1
            elif result == "FILTERED":
                pass  # counted in total but not viable
            # count edge > 0 in FILTERED rows as viable too
            if result == "FILTERED" and edge_net is not None and edge_net > 0:
                viable += 1

            # stale_book can also come as rejection_reason on FILTERED rows
            if reason == "stale_book" and result != "BLOCKED_BY_RISK":
                stale += 1

        median_pct = Decimal("0")
        if success_edge_pcts:
            median_pct = Decimal(str(statistics.median(success_edge_pcts)))

        return {
            "total_snapshots_evaluated": total,
            "viable_signals": viable,
            "blocked_by_risk": blocked,
            "clean_misses": clean_misses,
            "successes": successes,
            "legged_incidents": legged,
            "total_simulated_pnl": total_pnl,
            "median_edge_net_pct": median_pct,
            "stale_book_rejections": stale,
        }

    # ------------------------------------------------------------------
    # RiskManager storage interface
    # ------------------------------------------------------------------

    async def upsert_runtime_health(self, key: str, value: str) -> None:
        """Insert or update one runtime_health row (Sprint 7 metric flush)."""
        assert self._conn, "Call connect() before upsert_runtime_health()"
        await self._conn.execute(
            "INSERT INTO runtime_health (key, value, updated_at) "
            "VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value = excluded.value, updated_at = excluded.updated_at",
            (key, value),
        )
        await self._conn.commit()

    async def get_runtime_health(self) -> dict[str, str]:
        """Return all current runtime_health rows as a dict."""
        assert self._conn, "Call connect() before get_runtime_health()"
        async with self._conn.execute(
            "SELECT key, value FROM runtime_health"
        ) as cur:
            rows = await cur.fetchall()
        return {row["key"]: row["value"] for row in rows}

    async def insert_resolution_signal(
        self,
        signal: ResolutionSignal,
        simulated_pnl: Decimal = Decimal("0"),
    ) -> None:
        """Record one resolution-arbitrage signal."""
        assert self._conn, "Call connect() before insert_resolution_signal()"
        await self._conn.execute(
            _INSERT_RESOLUTION_SIGNAL,
            (
                signal.market_id,
                signal.condition_id,
                signal.outcome,
                signal.token_id,
                float(signal.current_ask),
                float(signal.edge_net_pct),
                float(signal.fee_usdc),
                signal.confidence,
                signal.source,
                float(simulated_pnl),
                signal.ts_ms,
            ),
        )
        await self._conn.commit()

    async def get_resolution_summary(self, since_ts_ms: int = 0) -> dict:
        """Return aggregated resolution-arbitrage statistics."""
        assert self._conn, "Call connect() before get_resolution_summary()"
        async with self._conn.execute(
            "SELECT outcome, edge_net_pct, simulated_pnl, confidence "
            "FROM resolution_signals WHERE ts_ms >= ?",
            (since_ts_ms,),
        ) as cur:
            rows = await cur.fetchall()

        total = len(rows)
        confirmed = sum(1 for r in rows if r["confidence"] == "confirmed")
        total_pnl = Decimal("0")
        edge_pcts: list[float] = []
        for row in rows:
            total_pnl += Decimal(str(row["simulated_pnl"]))
            if row["edge_net_pct"] is not None:
                edge_pcts.append(float(row["edge_net_pct"]))

        median_edge = Decimal("0")
        if edge_pcts:
            median_edge = Decimal(str(statistics.median(edge_pcts)))

        return {
            "total_signals": total,
            "confirmed_signals": confirmed,
            "total_simulated_pnl": total_pnl,
            "median_edge_net_pct": median_edge,
        }

    async def insert_risk_event(
        self,
        *,
        event_type: str,
        market_id: str,
        reason: str,
        detail: str,
    ) -> None:
        """Minimal stub so Storage satisfies the CerberusStorage protocol.

        Risk events are stored as paper_signals rows with result=event_type.
        """
        assert self._conn, "Call connect() before insert_risk_event()"
        await self._conn.execute(
            _INSERT_PAPER_SIGNAL,
            (
                market_id, "", 0,
                None, None, None, None, None, None, None,
                event_type, reason, 0.0,
            ),
        )
        await self._conn.commit()
