"""
Tests for cerberus_runtime/cross_venue.py

Run with:
    pytest -v tests/test_cross_venue.py
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, List
from unittest.mock import AsyncMock

import pytest

from cerberus_runtime.config import AppConfig
from cerberus_runtime.cross_venue import (
    MatchedPair,
    _match_confidence,
    discover,
    discover_predictit,
    discover_sports,
    evaluate_cross_venue_opportunity,
    kalshi_fee_params,
    odds_fee_params,
    predictit_fee_params,
)
from cerberus_runtime.fee_model import FeeModel
from cerberus_runtime.models import FeeParams, Market, OrderBookSnapshot, PriceLevel


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _make_market(condition_id: str = "0xabc", end_date=None) -> Market:
    return Market(
        condition_id=condition_id,
        yes_token_id="yes-1",
        no_token_id="no-1",
        category="Politics",
        fee_params=FeeParams(fees_enabled=True, maker_fee_rate=0.0, taker_fee_rate=0.02),
        min_order_size=1.0,
        tick_size=0.01,
        end_date=end_date or (_now_utc() + timedelta(days=10)),
        volume_24h=10_000.0,
    )


def _make_snapshot(market_id: str, yes_ask: str, no_ask: str, size: str = "1000",
                    ts_ms: int = 0) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        market_id=market_id,
        yes_asks=[PriceLevel(price=Decimal(yes_ask), size=Decimal(size))],
        no_asks=[PriceLevel(price=Decimal(no_ask), size=Decimal(size))],
        timestamp=0.0,
        condition_id=market_id,
        fee_params=FeeParams(fees_enabled=True, maker_fee_rate=0.0, taker_fee_rate=0.02),
        ts_ms=ts_ms,
    )


def _config() -> AppConfig:
    return AppConfig(
        trade_notional_usdc=Decimal("50"),
        slippage_buffer_pct=Decimal("0.005"),
        legged_risk_buffer_pct=Decimal("0.003"),
        min_net_edge_usd=Decimal("0.40"),
        min_net_edge_pct=Decimal("0.02"),
        min_order_size=Decimal("1"),
        tick_size=Decimal("0.01"),
    )


# ── _match_confidence ────────────────────────────────────────────────────────


class TestMatchConfidence:
    def test_rejects_far_apart_close_dates(self) -> None:
        poly_end = _now_utc() + timedelta(days=10)
        kalshi_close = _now_utc() + timedelta(days=30)
        result = _match_confidence(
            "Will Trump win Pennsylvania?", poly_end,
            "Will Trump win Pennsylvania?", kalshi_close,
        )
        assert result is None

    def test_rejects_low_text_similarity(self) -> None:
        result = _match_confidence(
            "Will Trump win Pennsylvania?", None,
            "Will the Fed cut rates in March?", None,
        )
        assert result is None

    def test_rejects_different_events_similar_phrasing(self) -> None:
        """The canonical false-positive this heuristic exists to catch:
        same "wins X" template, different X."""
        result = _match_confidence(
            "Will Trump win Pennsylvania?", None,
            "Will Trump win the popular vote?", None,
        )
        assert result is None

    def test_accepts_close_match(self) -> None:
        end = _now_utc() + timedelta(days=10)
        result = _match_confidence(
            "Will Donald Trump win Pennsylvania in 2028?", end,
            "Will Donald Trump win Pennsylvania in 2028?", end,
        )
        assert result is not None
        assert Decimal("0") < result <= Decimal("1")


# ── discover() ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_discover_picks_best_candidate() -> None:
    market = _make_market(condition_id="0xabc", end_date=_now_utc() + timedelta(days=5))
    poly_questions = {"0xabc": "Will Donald Trump win Pennsylvania in 2028?"}

    fake_watcher = AsyncMock()
    fake_watcher.fetch_markets = AsyncMock(return_value=[
        {"ticker": "KALSHI-A", "title": "Will the Fed cut rates?",
         "close_time": (_now_utc() + timedelta(days=5)).isoformat()},
        {"ticker": "KALSHI-B", "title": "Will Donald Trump win Pennsylvania in 2028?",
         "close_time": (_now_utc() + timedelta(days=5)).isoformat()},
    ])

    pairs = await discover([market], poly_questions, fake_watcher)
    assert len(pairs) == 1
    assert pairs[0].kalshi_ticker == "KALSHI-B"


@pytest.mark.asyncio
async def test_discover_returns_empty_when_no_kalshi_markets() -> None:
    market = _make_market()
    fake_watcher = AsyncMock()
    fake_watcher.fetch_markets = AsyncMock(return_value=[])
    pairs = await discover([market], {"0xabc": "some question"}, fake_watcher)
    assert pairs == []


@pytest.mark.asyncio
async def test_discover_skips_market_without_question() -> None:
    market = _make_market(condition_id="0xnotfound")
    fake_watcher = AsyncMock()
    fake_watcher.fetch_markets = AsyncMock(return_value=[
        {"ticker": "KALSHI-A", "title": "anything", "close_time": None},
    ])
    pairs = await discover([market], {}, fake_watcher)
    assert pairs == []


# ── evaluate_cross_venue_opportunity() ──────────────────────────────────────


class TestEvaluateCrossVenueOpportunity:
    def test_viable_signal_when_combined_cost_below_dollar(self) -> None:
        # poly YES ask 0.40 + kalshi NO ask 0.40 = 0.80 total -> big edge
        poly = _make_snapshot("poly-1", yes_ask="0.40", no_ask="0.95", ts_ms=1000)
        kalshi = _make_snapshot("kalshi-1", yes_ask="0.95", no_ask="0.40", ts_ms=1000)
        config = _config()
        fee_model = FeeModel()

        signal = evaluate_cross_venue_opportunity(
            poly, kalshi, config, fee_model,
            match_confidence=Decimal("0.9"), question="test question",
        )
        assert signal is not None
        assert signal.combo == "poly_yes_kalshi_no"
        assert signal.edge_net > Decimal("0")

    def test_insufficient_depth_returns_none(self) -> None:
        poly = OrderBookSnapshot(
            market_id="poly-1", yes_asks=[], no_asks=[], timestamp=0.0,
            fee_params=None, ts_ms=1000,
        )
        kalshi = OrderBookSnapshot(
            market_id="kalshi-1", yes_asks=[], no_asks=[], timestamp=0.0,
            fee_params=None, ts_ms=1000,
        )
        config = _config()
        fee_model = FeeModel()
        reason_box: dict = {}

        signal = evaluate_cross_venue_opportunity(
            poly, kalshi, config, fee_model,
            match_confidence=Decimal("0.9"), question="q", _reason=reason_box,
        )
        assert signal is None
        assert reason_box["reason"] == "insufficient_depth"

    def test_edge_below_threshold_returns_none(self) -> None:
        # 0.50 + 0.50 = 1.00 -> zero gross edge, definitely below threshold
        poly = _make_snapshot("poly-1", yes_ask="0.50", no_ask="0.99", ts_ms=1000)
        kalshi = _make_snapshot("kalshi-1", yes_ask="0.99", no_ask="0.50", ts_ms=1000)
        config = _config()
        fee_model = FeeModel()
        reason_box: dict = {}

        signal = evaluate_cross_venue_opportunity(
            poly, kalshi, config, fee_model,
            match_confidence=Decimal("0.9"), question="q", _reason=reason_box,
        )
        assert signal is None
        assert reason_box["reason"] == "edge_below_threshold"

    def test_picks_higher_edge_combo(self) -> None:
        # combo A (poly yes + kalshi no): 0.45 + 0.45 = 0.90 -> smaller edge
        # combo B (poly no + kalshi yes): 0.30 + 0.30 = 0.60 -> bigger edge
        poly = _make_snapshot("poly-1", yes_ask="0.45", no_ask="0.30", ts_ms=1000)
        kalshi = _make_snapshot("kalshi-1", yes_ask="0.30", no_ask="0.45", ts_ms=1000)
        config = _config()
        fee_model = FeeModel()

        signal = evaluate_cross_venue_opportunity(
            poly, kalshi, config, fee_model,
            match_confidence=Decimal("0.9"), question="q",
        )
        assert signal is not None
        assert signal.combo == "poly_no_kalshi_yes"

    def test_window_ms_reflects_staler_snapshot(self) -> None:
        poly = _make_snapshot("poly-1", yes_ask="0.40", no_ask="0.95", ts_ms=1_000_000)
        kalshi = _make_snapshot("kalshi-1", yes_ask="0.95", no_ask="0.40", ts_ms=1_000_000 - 5000)
        config = _config()
        fee_model = FeeModel()

        signal = evaluate_cross_venue_opportunity(
            poly, kalshi, config, fee_model,
            match_confidence=Decimal("0.9"), question="q",
        )
        assert signal is not None
        # window_ms is measured against wall-clock "now", so it should be a
        # large positive number reflecting these fixed historical ts_ms values.
        assert signal.window_ms > 0


def test_kalshi_fee_params_shape() -> None:
    params = kalshi_fee_params()
    assert params.fees_enabled is True
    assert params.maker_fee_rate == 0.0
    assert params.taker_fee_rate > 0.0


class TestPredictitExpectedFeeUsdc:
    def test_basic_expected_value(self) -> None:
        from cerberus_runtime.cross_venue import predictit_expected_fee_usdc
        # E[fee] = avg_price * 0.10 * tokens * (1 - avg_price)
        #        = 0.5 * 0.10 * 100 * 0.5 = 2.5
        fee = predictit_expected_fee_usdc(Decimal("0.5"), Decimal("100"))
        assert fee == Decimal("2.500000")

    def test_zero_at_price_boundaries(self) -> None:
        from cerberus_runtime.cross_venue import predictit_expected_fee_usdc
        assert predictit_expected_fee_usdc(Decimal("0"), Decimal("100")) == Decimal("0")
        assert predictit_expected_fee_usdc(Decimal("1"), Decimal("100")) == Decimal("0")

    def test_zero_tokens_returns_zero(self) -> None:
        from cerberus_runtime.cross_venue import predictit_expected_fee_usdc
        assert predictit_expected_fee_usdc(Decimal("0.5"), Decimal("0")) == Decimal("0")


class TestEvaluateUsesOtherVenueFeeFnOverride:
    def test_fee_fn_overrides_rate_based_fee(self) -> None:
        from cerberus_runtime.cross_venue import predictit_expected_fee_usdc
        poly = _make_snapshot("poly-1", yes_ask="0.45", no_ask="0.30", ts_ms=1000)
        kalshi = _make_snapshot("kalshi-1", yes_ask="0.30", no_ask="0.45", ts_ms=1000)
        config = _config()
        fee_model = FeeModel()

        # With the old rate-based fee (predictit_fee_params has taker_fee_rate=0
        # now, by design — see its docstring), the other-venue leg fee is 0
        # unless fee_fn is supplied.
        signal_without_fn = evaluate_cross_venue_opportunity(
            poly, kalshi, config, fee_model,
            match_confidence=Decimal("0.9"), question="q",
            other_venue_fee_params=predictit_fee_params(),
        )
        signal_with_fn = evaluate_cross_venue_opportunity(
            poly, kalshi, config, fee_model,
            match_confidence=Decimal("0.9"), question="q",
            other_venue_fee_params=predictit_fee_params(),
            other_venue_fee_fn=predictit_expected_fee_usdc,
        )
        assert signal_without_fn is not None
        assert signal_with_fn is not None
        # The fee_fn path must charge MORE than zero, and the two paths must
        # differ — otherwise the override silently isn't being applied.
        assert signal_with_fn.fees_total > signal_without_fn.fees_total
        assert signal_without_fn.fees_total == Decimal("0")


def test_odds_fee_params_shape() -> None:
    # A sportsbook's vig is baked into the quoted price itself — there is
    # no separate per-trade fee to layer on top, unlike Kalshi/PredictIt.
    params = odds_fee_params()
    assert params.fees_enabled is False
    assert params.taker_fee_rate == 0.0


class TestMatchConfidenceTemplateCollisionRegressions:
    """Regression coverage for three real false-positive matches produced
    once the Polymarket candidate pool widened from 9 to 1700+ markets —
    all verified live against actual Gamma/PredictIt data before the fix."""

    def test_rejects_different_candidates_same_race(self) -> None:
        # "Tucker Carlson" and generic "AOC" contract both cleared the old
        # thresholds because the shared boilerplate ("presidential
        # election", "2028") alone was enough — two different people.
        conf = _match_confidence(
            "Will Tucker Carlson win the 2028 US Presidential Election?", None,
            "Who will win the 2028 US presidential election? - AOC", None,
        )
        assert conf is None

    def test_rejects_acronym_name_collapsing_to_shared_year_only(self) -> None:
        # "MTG" (Marjorie Taylor Greene) is too short to survive
        # tokenization, so the ONLY shared significant token was the bare
        # year "2028" — not evidence of the same entity.
        conf = _match_confidence(
            "Will Joe Kent win the 2028 Republican presidential nomination?", None,
            "Who will win the 2028 Republican presidential nomination? - MTG", None,
        )
        assert conf is None

    def test_rejects_same_person_different_race_stage(self) -> None:
        # Same person, but winning the NOMINATION and winning the general
        # ELECTION are different, non-identical real-world propositions.
        conf = _match_confidence(
            "Will Wes Moore win the 2028 US Presidential Election?", None,
            "Who will win the 2028 Democratic presidential nomination? - Moore", None,
        )
        assert conf is None

    def test_rejects_same_person_president_vs_vice_president(self) -> None:
        conf = _match_confidence(
            "Will James Talarico win the 2028 Democratic presidential nomination?", None,
            "Who will win the 2028 Democratic vice presidential nomination? - James Talarico", None,
        )
        assert conf is None

    def test_accepts_genuine_same_candidate_same_race(self) -> None:
        conf = _match_confidence(
            "Will Pete Buttigieg win the 2028 US Presidential Election?", None,
            "Who will win the 2028 US presidential election? - Buttigieg", None,
        )
        assert conf is not None
        assert conf > Decimal("0.6")

    def test_accepts_genuine_state_senate_race(self) -> None:
        conf = _match_confidence(
            "Which party will win the 2026 US Senate election in Georgia?", None,
            "Which party will win the 2026 US Senate election in Georgia? - Democratic", None,
        )
        assert conf is not None

    def test_rejects_shared_first_name_different_surname(self) -> None:
        # "Pete" Hegseth and "Pete" Buttigieg are different people — a
        # shared YEAR token used to pad jaccard enough to let this through.
        conf = _match_confidence(
            "Will Pete Hegseth win the 2028 US Presidential Election?", None,
            "Who will win the 2028 US presidential election? - Pete Buttigieg", None,
        )
        assert conf is None

        conf2 = _match_confidence(
            "Will Mark Cuban win the 2028 Democratic presidential nomination?", None,
            "Who will win the 2028 Democratic presidential nomination? - Mark Kelly", None,
        )
        assert conf2 is None

    def test_rejects_different_family_members_same_surname(self) -> None:
        # Verified live: PredictIt's Brazil-election market has separate
        # contracts for Michelle/Jair/Flávio/Eduardo Bolsonaro — all matching
        # "brazilian" as a shared boilerplate token alongside the surname.
        conf = _match_confidence(
            "Will Eduardo Bolsonaro win the 2026 Brazilian presidential election?", None,
            "Who will win the 2026 Brazilian presidential election? - Flávio Bolsonaro", None,
        )
        assert conf is None

    def test_accepts_accented_name_after_diacritic_normalization(self) -> None:
        # Before the fix, "Flávio" was mangled into unrecognizable
        # fragments by the ASCII-only regex, so even the CORRECT pairing
        # scored lower than it should have.
        conf = _match_confidence(
            "Will Flávio Bolsonaro win the 2026 Brazilian presidential election?", None,
            "Who will win the 2026 Brazilian presidential election? - Flávio Bolsonaro", None,
        )
        assert conf is not None
        assert conf > Decimal("0.8")


class TestDiscoverPredictitSkipsUnnamedContracts:
    def test_skips_multi_candidate_contract_with_blank_name(self) -> None:
        poly = [_make_market("0xabc", end_date=_now_utc() + timedelta(days=800))]
        questions = {"0xabc": "Will Kristi Noem win the 2028 Republican presidential nomination?"}
        predictit_markets = [{
            "name": "Who will win the 2028 Republican presidential nomination?",
            "contracts": [
                {"id": 1, "status": "Open", "shortName": ""},  # generic "Other"/"Field" bucket
                {"id": 2, "status": "Open", "shortName": "Noem"},
            ],
        }]
        pairs = discover_predictit(poly, questions, predictit_markets)
        assert len(pairs) == 1
        assert pairs[0].kalshi_ticker == "2"

    def test_single_contract_market_keeps_blank_name(self) -> None:
        # A market with exactly one contract has no ambiguity — the market
        # name alone IS the whole question, blank contract name is fine.
        poly = [_make_market("0xdef", end_date=_now_utc() + timedelta(days=20))]
        questions = {"0xdef": "Will President sign the housing bill by July 31?"}
        predictit_markets = [{
            "name": "Will President sign housing bill by July 31?",
            "contracts": [{"id": 99, "status": "Open", "shortName": ""}],
        }]
        pairs = discover_predictit(poly, questions, predictit_markets)
        assert len(pairs) == 1
        assert pairs[0].kalshi_ticker == "99"


class TestDiscoverSports:
    def test_matches_by_team_name_containment(self) -> None:
        poly = [_make_market("0x1", end_date=_now_utc() + timedelta(days=5))]
        questions = {"0x1": "Will Argentina win the 2026 FIFA World Cup?"}
        snapshot = _make_snapshot("evt1:Argentina", yes_ask="0.4", no_ask="0.6")
        candidates = {
            "evt1:Argentina": ("Will Argentina beat Brazil?", snapshot, ["Argentina", "Brazil", "2026-07-10"]),
        }
        pairs = discover_sports(poly, questions, candidates)
        assert len(pairs) == 1
        assert pairs[0].kalshi_ticker == "evt1:Argentina"

    def test_rejects_short_team_name_tokens(self) -> None:
        poly = [_make_market("0x2", end_date=_now_utc() + timedelta(days=5))]
        questions = {"0x2": "Will FC win the title?"}
        snapshot = _make_snapshot("evt2:FC", yes_ask="0.4", no_ask="0.6")
        candidates = {"evt2:FC": ("Will FC beat Rivals?", snapshot, ["FC", "Rivals", "2026-07-10"])}
        pairs = discover_sports(poly, questions, candidates)
        assert pairs == []

    def test_requires_a_winning_verb_in_the_poly_question(self) -> None:
        poly = [_make_market("0x3", end_date=_now_utc() + timedelta(days=5))]
        questions = {"0x3": "Argentina national team roster announcement"}
        snapshot = _make_snapshot("evt3:Argentina", yes_ask="0.4", no_ask="0.6")
        candidates = {
            "evt3:Argentina": ("Will Argentina beat Brazil?", snapshot, ["Argentina", "Brazil", "2026-07-10"]),
        }
        pairs = discover_sports(poly, questions, candidates)
        assert pairs == []
