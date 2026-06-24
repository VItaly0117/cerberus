"""Tests for cerberus_runtime/resolver.py — Sprint 4."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cerberus_runtime.config import Config
from cerberus_runtime.models import ResolutionSignal
from cerberus_runtime.resolver import (
    MAX_ASK_PRICE,
    MIN_EDGE_PCT,
    ResolutionScanner,
)
from cerberus_runtime.storage import Storage


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def config() -> Config:
    return Config(gamma_host="https://gamma-api.polymarket.com")


@pytest.fixture
def signal_queue() -> asyncio.Queue:
    return asyncio.Queue()


@pytest.fixture
async def storage(tmp_path):
    s = Storage(db_path=str(tmp_path / "test.db"))
    await s.connect()
    yield s
    await s.close()


@pytest.fixture
def scanner(config, signal_queue, storage) -> ResolutionScanner:
    return ResolutionScanner(config, storage, signal_queue)


# ── _evaluate: confirmed YES signal ───────────────────────────────────────────

def test_evaluate_confirmed_yes_signal(scanner):
    """Returns ResolutionSignal when outcome=YES and ask is low enough."""
    now = datetime.now(tz=timezone.utc)
    market_data = {
        "id": "market-1",
        "conditionId": "cond-1",
        "outcome": "yes",
        "outcomePrices": ["0.85", "0.15"],
        "tokens": [
            {"outcome": "Yes", "tokenId": "tok-yes-1"},
            {"outcome": "No", "tokenId": "tok-no-1"},
        ],
        "endDate": (now - timedelta(minutes=5)).isoformat(),
    }
    signal = scanner._evaluate(market_data, now)

    assert signal is not None
    assert signal.outcome == "YES"
    assert signal.current_ask == Decimal("0.85")
    assert signal.confidence == "confirmed"
    assert signal.edge_net_pct > Decimal("0")
    assert signal.market_id == "market-1"
    assert signal.condition_id == "cond-1"


def test_evaluate_confirmed_no_signal(scanner):
    """Returns ResolutionSignal when outcome=NO and ask is low enough."""
    now = datetime.now(tz=timezone.utc)
    market_data = {
        "id": "market-2",
        "conditionId": "cond-2",
        "outcome": "no",
        "outcomePrices": ["0.15", "0.82"],
        "tokens": [],
        "endDate": (now - timedelta(hours=1)).isoformat(),
    }
    signal = scanner._evaluate(market_data, now)

    assert signal is not None
    assert signal.outcome == "NO"
    assert signal.current_ask == Decimal("0.82")
    assert signal.confidence == "confirmed"


# ── _evaluate: edge below threshold ───────────────────────────────────────────

def test_evaluate_no_signal_when_edge_too_small(scanner):
    """Returns None when ask is too close to 1.00 (edge < MIN_EDGE_PCT)."""
    now = datetime.now(tz=timezone.utc)
    market_data = {
        "id": "market-3",
        "outcome": "yes",
        "outcomePrices": ["0.99", "0.01"],
        "tokens": [],
        "endDate": (now - timedelta(minutes=1)).isoformat(),
    }
    signal = scanner._evaluate(market_data, now)
    assert signal is None


# ── _evaluate: price already at threshold ─────────────────────────────────────

def test_evaluate_no_signal_when_ask_above_max(scanner):
    """Returns None when ask >= MAX_ASK_PRICE."""
    now = datetime.now(tz=timezone.utc)
    market_data = {
        "id": "market-4",
        "outcome": "yes",
        "outcomePrices": [str(float(MAX_ASK_PRICE) + 0.01), "0.01"],
        "tokens": [],
        "endDate": (now - timedelta(minutes=1)).isoformat(),
    }
    signal = scanner._evaluate(market_data, now)
    assert signal is None


# ── _evaluate: no outcome field ───────────────────────────────────────────────

def test_evaluate_returns_none_without_outcome(scanner):
    """Returns None when Gamma has not yet published an outcome."""
    now = datetime.now(tz=timezone.utc)
    market_data = {
        "id": "market-5",
        "outcomePrices": ["0.80", "0.20"],
        "tokens": [],
        "endDate": (now + timedelta(hours=2)).isoformat(),
    }
    signal = scanner._evaluate(market_data, now)
    assert signal is None


# ── _evaluate: numeric outcome aliases ────────────────────────────────────────

def test_evaluate_numeric_outcome_1_maps_to_yes(scanner):
    """Numeric outcome '1' is treated as YES."""
    now = datetime.now(tz=timezone.utc)
    market_data = {
        "id": "market-6",
        "outcome": "1",
        "outcomePrices": ["0.80", "0.20"],
        "tokens": [],
        "endDate": (now - timedelta(minutes=10)).isoformat(),
    }
    signal = scanner._evaluate(market_data, now)
    assert signal is not None
    assert signal.outcome == "YES"


def test_evaluate_numeric_outcome_0_maps_to_no(scanner):
    """Numeric outcome '0' is treated as NO."""
    now = datetime.now(tz=timezone.utc)
    market_data = {
        "id": "market-7",
        "outcome": "0",
        "outcomePrices": ["0.20", "0.80"],
        "tokens": [],
        "endDate": (now - timedelta(minutes=10)).isoformat(),
    }
    signal = scanner._evaluate(market_data, now)
    assert signal is not None
    assert signal.outcome == "NO"


# ── Storage round-trip ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_storage_insert_and_summary(storage):
    """insert_resolution_signal + get_resolution_summary round-trip."""
    signal = ResolutionSignal(
        market_id="m1",
        condition_id="c1",
        outcome="YES",
        token_id="tok-1",
        current_ask=Decimal("0.85"),
        edge_net_pct=Decimal("0.176"),
        fee_usdc=Decimal("0"),
        confidence="confirmed",
        source="gamma_api",
        ts_ms=int(time.time() * 1000),
    )
    pnl = Decimal("1") - signal.current_ask - signal.fee_usdc
    await storage.insert_resolution_signal(signal, simulated_pnl=pnl)

    summary = await storage.get_resolution_summary()
    assert summary["total_signals"] == 1
    assert summary["confirmed_signals"] == 1
    assert summary["total_simulated_pnl"] == pnl


# ── Deduplication ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scanner_deduplicates_signals(scanner, signal_queue):
    """Same market is not emitted twice into the queue."""
    now = datetime.now(tz=timezone.utc)
    market_data = {
        "id": "market-dup",
        "conditionId": "cond-dup",
        "outcome": "yes",
        "outcomePrices": ["0.80", "0.20"],
        "tokens": [],
        "endDate": (now - timedelta(minutes=5)).isoformat(),
    }

    mock_markets = [market_data]

    with patch.object(
        scanner, "_fetch_resolving_markets", new=AsyncMock(return_value=mock_markets)
    ):
        await scanner._scan()
        await scanner._scan()

    assert signal_queue.qsize() == 1
