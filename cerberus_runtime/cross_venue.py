"""
Cross-venue arbitrage measurement: Polymarket x Kalshi.

Answers one narrow question — "after real fees on both venues, is there a
capturable edge between Polymarket and Kalshi on markets referencing the
same real-world event?" — nothing more. No orders, no executor, read-only.

Constraints
-----------
- Never import risk.py or executor.py.
- discover() and evaluate_cross_venue_opportunity() are pure / read-only;
  the only I/O is the Gamma API question lookup inside discover().
"""
from __future__ import annotations

import difflib
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

import httpx

from cerberus_runtime.config import AppConfig
from cerberus_runtime.core import calculate_effective_leg
from cerberus_runtime.fee_model import FeeModel
from cerberus_runtime.kalshi_watcher import KALSHI_TAKER_FEE_RATE
from cerberus_runtime.predictit_watcher import PREDICTIT_TAKER_FEE_RATE
from cerberus_runtime.odds_watcher import ODDS_API_TAKER_FEE_RATE
from cerberus_runtime.models import (
    CrossVenueSignal, FeeParams, LegQuote, Market, OrderBookSnapshot, PriceLevel,
)

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 10.0
_MAX_QUESTION_LOOKUP_PAGES = 20

# ── Wide-net Polymarket candidate fetch, for cross-venue MATCHING only ──────
# market_discovery.py's end_date window (3-30 days) and volume floor are
# tuned for a different job entirely (single-venue legged-risk arbitrage)
# and happen to exclude almost everything the other cross-venue partners
# actually list — PredictIt's catalog is dominated by the 2026 midterms and
# 2028 presidential race, both many months out. Duplicating a minimal,
# widened binary-market parser here (instead of importing market_discovery.py)
# keeps this measurement path fully decoupled from the live trading bot's
# own candidate set — it can never perturb what the bot actually trades.
_MATCH_MIN_VOLUME_24H: float = 500.0
_MATCH_MAX_VOLUME_24H: float = 5_000_000.0
_MATCH_MAX_DAYS_TO_END: int = 1000  # covers the 2028 US presidential race (~875d out)
_MATCH_MAX_PAGES: int = 30

# ── Matching thresholds (conservative on purpose) ───────────────────────────
# A false match fabricates an "edge" out of two unrelated events; a missed
# match just under-counts. We bias toward missing matches over faking them.
_MIN_TEXT_SIMILARITY: float = 0.60
_MIN_TOKEN_JACCARD: float = 0.45
_MAX_CLOSE_DATE_DRIFT_DAYS: int = 3

_STOPWORDS = {
    "will", "the", "a", "an", "be", "to", "of", "in", "on", "for", "by",
    "and", "or", "is", "are", "this", "that", "at", "than", "before",
    "after", "win", "wins", "does", "do", "than", "who", "which",
    # Multi-candidate "race" templates (e.g. "Will X win the 2028 US
    # Presidential Election?") are near-identical across every candidate in
    # the same race — differing ONLY by name. Verified: leaving these words
    # in as "significant" let dozens of unrelated Poly markets ("Tucker
    # Carlson", "AOC", "Kristi Noem", "Mike Pence" — all different people)
    # jaccard-match the exact same PredictIt contract, because the shared
    # boilerplate alone cleared the 0.45 threshold. Excluding them forces the
    # jaccard gate to actually require the candidate's NAME to overlap.
    "president", "presidential", "election", "elections", "nomination",
    "nominee", "republican", "democrat", "democratic", "senate", "house",
    "governor", "party", "seat", "control", "primary", "midterm", "midterms",
    "next",
    # Country/nationality adjectives are the SAME boilerplate problem for
    # non-US races — verified: "french" alone let "Marine Le Pen" (real
    # match) and "Marine Tondelier" (different candidate, same first name,
    # LE PEN's own surname too short to survive tokenization) both clear
    # jaccard on {marine, french} alone. Add as observed, not exhaustive.
    "french", "brazilian",
}


def kalshi_fee_params() -> FeeParams:
    """Kalshi has no per-market fee endpoint among the two used here — this
    just wraps the module-level constant into the same ``FeeParams`` shape
    core.calculate_effective_leg already expects, so no new fee logic is
    needed."""
    return FeeParams(
        fees_enabled=True,
        maker_fee_rate=0.0,  # Kalshi maker orders are not modeled here (taker-only measurement)
        taker_fee_rate=KALSHI_TAKER_FEE_RATE,
    )


def predictit_fee_params() -> FeeParams:
    """SUPERSEDED for fee purposes by ``predictit_expected_fee_usdc`` below
    — kept only because ``calculate_effective_leg`` requires a FeeParams to
    walk the book at all; pass ``other_venue_fee_fn=predictit_expected_fee_usdc``
    to ``evaluate_cross_venue_opportunity`` so the correct resolution-time
    fee overrides whatever this rate would have produced. taker_fee_rate is
    set to 0.0 here deliberately, so nothing double-counts if a caller
    forgets to also pass the fee_fn."""
    return FeeParams(
        fees_enabled=False,
        maker_fee_rate=0.0,
        taker_fee_rate=0.0,
    )


def predictit_expected_fee_usdc(avg_price: Decimal, accumulated_tokens: Decimal) -> Decimal:
    """PredictIt's REAL fee mechanism: 10% of PROFIT, charged only if this
    leg's contract resolves TRUE, only at resolution — not a per-trade fee
    on notional like every other venue this codebase models. We don't know
    the outcome at measurement time, so this returns the EXPECTED fee under
    risk-neutral pricing (the market price IS the best available estimate
    of this leg's own win probability):

        E[fee] = P(win) * 0.10 * profit_if_win
               = avg_price * 0.10 * accumulated_tokens * (1 - avg_price)

    Still an approximation (assumes risk-neutral pricing; ignores
    PredictIt's separate 5% WITHDRAWAL fee, which is an account-level cost,
    not a per-trade one) — but it is now the right MECHANISM instead of the
    wrong one. A signal's edge_net computed with this should be treated as
    a meaningfully better estimate than the old per-trade approximation,
    not as a guaranteed-accurate one.
    """
    if not (Decimal("0") < avg_price < Decimal("1")) or accumulated_tokens <= 0:
        return Decimal("0")
    fee = avg_price * Decimal("0.10") * accumulated_tokens * (Decimal("1") - avg_price)
    return fee.quantize(Decimal("0.000001"))


def odds_fee_params() -> FeeParams:
    """A sportsbook's edge (the vig) is already baked into the two-sided
    quoted price itself — see odds_watcher.py module docstring. There is no
    separate per-trade fee to layer on top, unlike Kalshi/PredictIt."""
    return FeeParams(
        fees_enabled=False,
        maker_fee_rate=0.0,
        taker_fee_rate=ODDS_API_TAKER_FEE_RATE,
    )


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


@dataclass
class MatchedPair:
    """One Polymarket <-> Kalshi market pairing surfaced by discover()."""

    poly_market: Market
    poly_question: str
    kalshi_ticker: str
    kalshi_title: str
    match_confidence: Decimal


def _normalize(text: str) -> str:
    # Strip diacritics to their ASCII base letter (á -> a) BEFORE dropping
    # non-ASCII characters — otherwise accented names (e.g. "Flávio") get
    # mangled into unrecognizable fragments and lose all matching signal.
    # Verified live: this is why a PredictIt "Flávio Bolsonaro" contract's
    # first name never survived tokenization, leaving only the shared
    # surname "Bolsonaro" to (wrongly) match all four Bolsonaro-family
    # Polymarket candidates.
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9\s]", " ", ascii_text.lower()).strip()


def _significant_tokens(text: str) -> set:
    """Words len>=4 not in the stopword set, plus any digit runs (years,
    percentages, dollar figures) — the numbers are what disambiguate
    "wins PA" from "wins popular vote"."""
    normalized = _normalize(text)
    tokens = set()
    for word in normalized.split():
        if word.isdigit():
            tokens.add(word)
        elif len(word) >= 4 and word not in _STOPWORDS:
            tokens.add(word)
    return tokens


def _text_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _match_confidence(poly_question: str, poly_end: Optional[datetime],
                       kalshi_title: str, kalshi_close: Optional[datetime]) -> Optional[Decimal]:
    """Return a confidence score in [0, 1] if the pair clears all gates,
    else None (no match)."""
    if poly_end is not None and kalshi_close is not None:
        drift = abs((poly_end - kalshi_close).total_seconds()) / 86400.0
        if drift > _MAX_CLOSE_DATE_DRIFT_DAYS:
            return None

    similarity = _text_similarity(poly_question, kalshi_title)
    if similarity < _MIN_TEXT_SIMILARITY:
        return None

    tokens_a, tokens_b = _significant_tokens(poly_question), _significant_tokens(kalshi_title)
    # A shared year token (e.g. both mention "2028") pads the jaccard ratio
    # without being evidence of the same ENTITY — verified: this alone let
    # "Pete Hegseth"/"Pete Buttigieg" and "Mark Cuban"/"Mark Kelly" (shared
    # first name only, different people) clear the jaccard gate, because
    # {"2028", "pete"} ∩ / {"2028","pete","hegseth","buttigieg"} ∪ = 0.5.
    # Compute the ratio on name-only tokens; the year still gets its own
    # date-drift gate above (poly_end/kalshi_close), so nothing is lost.
    name_tokens_a = {t for t in tokens_a if not t.isdigit()}
    name_tokens_b = {t for t in tokens_b if not t.isdigit()}
    jaccard = _jaccard(name_tokens_a, name_tokens_b)
    if jaccard < _MIN_TOKEN_JACCARD:
        return None

    # "Nomination" and "election" (general/presidency) are different
    # bettable propositions about the same person — winning a primary and
    # winning the general are correlated but NOT the same real-world event,
    # so they must never be priced as a same-outcome cross-venue arb.
    # "nomination"/"president"/"election" etc. were stripped from the
    # significant-token set above (they're boilerplate WITHIN one race), so
    # this has to be checked explicitly against the raw text instead.
    if ("nomination" in poly_question.lower()) != ("nomination" in kalshi_title.lower()):
        return None

    # Same reasoning, same fix shape: "James Talarico for president" and
    # "James Talarico for VICE president" is the same person, two different
    # offices — verified this exact pair otherwise clears every gate above
    # on name-overlap alone.
    if ("vice" in poly_question.lower()) != ("vice" in kalshi_title.lower()):
        return None

    # Weighted blend — text similarity captures phrasing, token jaccard
    # captures "did they actually mention the same entities/numbers".
    confidence = Decimal(str(round(0.5 * similarity + 0.5 * jaccard, 4)))
    return confidence


async def fetch_poly_questions(
    gamma_host: str, condition_ids: set
) -> Dict[str, str]:
    """Independent, read-only lookup of {condition_id: question} from the
    Gamma API for markets already discovered by MarketDiscovery. Does not
    touch market_discovery.py or storage's markets table — those own their
    own Market objects; this is purely for the fuzzy-match text."""
    url = f"{gamma_host}/markets"
    found: Dict[str, str] = {}
    remaining = set(condition_ids)

    for page in range(_MAX_QUESTION_LOOKUP_PAGES):
        if not remaining:
            break
        params = {"active": "true", "closed": "false", "limit": 100, "offset": page * 100}
        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("cross_venue: Gamma question lookup failed on page %d: %s", page, exc)
            break

        rows = data if isinstance(data, list) else data.get("markets") or data.get("data") or []
        if not rows:
            break

        for raw in rows:
            cid = raw.get("conditionId") or raw.get("condition_id")
            if cid in remaining:
                question = raw.get("question") or raw.get("title") or ""
                if question:
                    found[cid] = question
                    remaining.discard(cid)

        if len(rows) < 100:
            break

    return found


def _extract_binary_tokens(raw: Dict[str, Any]) -> Optional[tuple]:
    """Return (yes_token_id, no_token_id) for a raw Gamma market dict, or
    None if it isn't a clean 2-outcome binary market. Mirrors
    market_discovery.py's token extraction (old ``tokens`` list format and
    the 2026 ``clobTokenIds`` JSON-string format), duplicated intentionally
    — see module-level comment on why this stays decoupled."""
    tokens: List[Dict[str, Any]] = raw.get("tokens") or []
    clob_ids_raw = raw.get("clobTokenIds")
    if not tokens and clob_ids_raw:
        try:
            import json as _json
            clob_ids = _json.loads(clob_ids_raw) if isinstance(clob_ids_raw, str) else clob_ids_raw
            if isinstance(clob_ids, list) and len(clob_ids) == 2:
                tokens = [
                    {"token_id": clob_ids[0], "outcome": "YES"},
                    {"token_id": clob_ids[1], "outcome": "NO"},
                ]
        except Exception:
            return None
    if len(tokens) != 2:
        return None
    yes_tid = next((t.get("token_id") for t in tokens if str(t.get("outcome", "")).upper() == "YES"), None)
    no_tid = next((t.get("token_id") for t in tokens if str(t.get("outcome", "")).upper() == "NO"), None)
    if not yes_tid or not no_tid:
        return None
    return (yes_tid, no_tid)


async def fetch_poly_match_candidates(gamma_host: str) -> List[Market]:
    """Wide-net Polymarket candidate fetch for cross-venue MATCHING only.

    Deliberately looser than market_discovery.py's own filter:
      - end_date any time in the future up to ``_MATCH_MAX_DAYS_TO_END`` days
        (vs. the bot's narrow 3-30 day legged-risk window)
      - volume_24h in [``_MATCH_MIN_VOLUME_24H``, ``_MATCH_MAX_VOLUME_24H``]

    ``fee_params``/``min_order_size``/``tick_size`` on the returned ``Market``
    are unused placeholders — evaluate_cross_venue_opportunity() always
    prices the Polymarket leg off ``config.fee_params``/``config.min_order_size``/
    ``config.tick_size``, never the per-market fields. Only condition_id,
    end_date, yes_token_id, no_token_id are load-bearing here.
    """
    url = f"{gamma_host}/markets"
    now = datetime.now(timezone.utc)
    max_end = now + timedelta(days=_MATCH_MAX_DAYS_TO_END)
    candidates: List[Market] = []

    for page in range(_MATCH_MAX_PAGES):
        params = {
            "active": "true", "closed": "false", "limit": 100, "offset": page * 100,
            # Highest-volume markets first — otherwise Gamma's default order
            # buries high-value candidates (verified: the 2028 presidential
            # markets don't surface within ~2000 unordered rows, but rank in
            # the top 20 by volume) deep past where pagination realistically
            # reaches before the API starts rejecting large offsets.
            "order": "volume24hr", "ascending": "false",
        }
        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("cross_venue: wide-net poly fetch failed on page %d: %s", page, exc)
            break

        rows = data if isinstance(data, list) else (data.get("markets") or data.get("data") or [])
        if not rows:
            break

        for raw in rows:
            if not raw.get("active", False) or raw.get("closed", True):
                continue
            # Deliberately NOT excluding negRisk here (unlike market_discovery.py):
            # negRisk only marks correlated-position netting for the bot's own
            # multi-leg execution, which this measurement path never does. Every
            # candidate-in-a-race market (e.g. "Will X win the 2028 election?")
            # is negRisk=true and STILL has its own valid standalone YES/NO token
            # pair and order book — excluding them would silently drop the exact
            # category (multi-candidate races) most likely to overlap with
            # PredictIt's catalog.
            tokens = _extract_binary_tokens(raw)
            if tokens is None:
                continue
            condition_id = raw.get("conditionId") or raw.get("condition_id")
            end_date_raw = raw.get("endDate") or raw.get("end_date_iso") or raw.get("end_date")
            if not condition_id or not end_date_raw:
                continue
            end_date = _parse_kalshi_dt(end_date_raw)
            if end_date is None or end_date <= now or end_date > max_end:
                continue
            volume = float(raw.get("volume24hr") or raw.get("volume_24hr") or raw.get("volume_24h") or 0)
            if volume < _MATCH_MIN_VOLUME_24H or volume > _MATCH_MAX_VOLUME_24H:
                continue

            candidates.append(Market(
                condition_id=condition_id,
                yes_token_id=tokens[0],
                no_token_id=tokens[1],
                category=raw.get("category") or raw.get("groupItemTitle") or raw.get("marketType") or "",
                fee_params=FeeParams(fees_enabled=False, maker_fee_rate=0.0, taker_fee_rate=0.0),
                min_order_size=0.0,
                tick_size=0.0,
                end_date=end_date,
                volume_24h=volume,
                active=True,
                closed=False,
            ))

        if len(rows) < 100:
            break

    return candidates


async def fetch_poly_orderbook_snapshot(
    clob_rest_url: str, condition_id: str, yes_token_id: str, no_token_id: str,
) -> Optional[OrderBookSnapshot]:
    """One-shot direct poll of CLOB REST ``/book`` for both legs of a
    matched Polymarket market — the same endpoint watcher.py's WS-resync
    path hits, but standalone here so a matched candidate outside
    MarketDiscovery's own tracked set (see ``fetch_poly_match_candidates``)
    still gets real depth without needing a live WS subscription.

    Two-phase: both legs must come back with non-empty asks before either
    is applied, so a lopsided fetch (one leg fresh, one stale/failed) never
    produces a snapshot — same invariant as Watcher._resync_from_rest.
    """
    fetched: Dict[str, List[Dict[str, Any]]] = {}
    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
        for token_id in (yes_token_id, no_token_id):
            try:
                resp = await client.get(f"{clob_rest_url}/book", params={"token_id": token_id})
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.debug("cross_venue: poly book fetch failed for token %s: %s", token_id, exc)
                return None
            if not isinstance(data, dict):
                return None
            asks = data.get("asks", [])
            if not isinstance(asks, list) or len(asks) == 0:
                return None
            fetched[token_id] = asks

    def _parse_levels(raw_levels: List[Dict[str, Any]]) -> List[PriceLevel]:
        levels: List[PriceLevel] = []
        for entry in raw_levels:
            try:
                price = Decimal(str(entry["price"]))
                size = Decimal(str(entry.get("size", "0")))
                if not price.is_finite() or not size.is_finite() or size <= 0:
                    continue
                levels.append(PriceLevel(price=price, size=size))
            except (KeyError, InvalidOperation, ValueError, TypeError):
                continue
        levels.sort(key=lambda pl: pl.price)
        return levels

    yes_asks = _parse_levels(fetched[yes_token_id])
    no_asks = _parse_levels(fetched[no_token_id])
    if not yes_asks or not no_asks:
        return None

    ts_ms = int(time.time() * 1000)
    return OrderBookSnapshot(
        market_id=condition_id,
        yes_asks=yes_asks,
        no_asks=no_asks,
        timestamp=ts_ms / 1000.0,
        condition_id=condition_id,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        fee_params=None,
        ts_ms=ts_ms,
    )


async def _fetch_kalshi_candidates(kalshi_watcher) -> List[Dict[str, Any]]:
    """Fetch open Kalshi markets, keeping only fields needed for matching."""
    raw_markets = await kalshi_watcher.fetch_markets(status="open")
    if not raw_markets:
        return []

    candidates = []
    for m in raw_markets:
        ticker = m.get("ticker")
        title = m.get("title") or m.get("subtitle") or ""
        if not ticker or not title:
            continue
        close_raw = m.get("close_time") or m.get("expiration_time")
        close_dt = _parse_kalshi_dt(close_raw) if close_raw else None
        candidates.append({"ticker": ticker, "title": title, "close_time": close_dt})
    return candidates


def _parse_kalshi_dt(raw: str) -> Optional[datetime]:
    try:
        normalized = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _match_all(
    poly_markets: List[Market],
    poly_questions: Dict[str, str],
    candidates: List[Dict[str, Any]],
) -> List[MatchedPair]:
    """Shared matching loop: for each Polymarket market with a known
    question, pick the single best-scoring candidate (if any clears the
    confidence gates). ``candidates`` entries need ``ticker``/``title``/
    ``close_time`` keys — shape is identical whether they came from Kalshi
    tickers or PredictIt contracts."""
    pairs: List[MatchedPair] = []
    for market in poly_markets:
        question = poly_questions.get(market.condition_id)
        if not question:
            continue

        best: Optional[MatchedPair] = None
        for cand in candidates:
            confidence = _match_confidence(
                question, market.end_date, cand["title"], cand["close_time"]
            )
            if confidence is None:
                continue
            if best is None or confidence > best.match_confidence:
                best = MatchedPair(
                    poly_market=market,
                    poly_question=question,
                    kalshi_ticker=cand["ticker"],
                    kalshi_title=cand["title"],
                    match_confidence=confidence,
                )
        if best is not None:
            pairs.append(best)

    return pairs


async def discover(
    poly_markets: List[Market],
    poly_questions: Dict[str, str],
    kalshi_watcher,
) -> List[MatchedPair]:
    """Match active Polymarket markets against open Kalshi markets by
    question text + close-date proximity.

    Args:
        poly_markets:   Active Market objects (from storage.get_active_markets()).
        poly_questions: {condition_id: question} — see fetch_poly_questions().
        kalshi_watcher: A KalshiWatcher instance.

    Returns:
        One MatchedPair per Polymarket market that found a Kalshi candidate
        clearing the confidence gates — at most one Kalshi ticker per
        Polymarket market (the single best-scoring candidate).
    """
    kalshi_candidates = await _fetch_kalshi_candidates(kalshi_watcher)
    if not kalshi_candidates:
        logger.warning("cross_venue.discover: no open Kalshi markets returned.")
        return []
    return _match_all(poly_markets, poly_questions, kalshi_candidates)


def discover_predictit(
    poly_markets: List[Market],
    poly_questions: Dict[str, str],
    predictit_markets: List[Dict[str, Any]],
) -> List[MatchedPair]:
    """Match active Polymarket markets against open PredictIt CONTRACTS
    (not "markets" — a PredictIt market often bundles several independent
    binary contracts, e.g. "192 or fewer" / "193 to 197"; each contract is
    matched on its own, same as one Kalshi ticker).

    ``MatchedPair.kalshi_ticker``/``kalshi_title`` hold the PredictIt
    contract id / question text — field names kept as-is (not renamed to
    "other_venue_*") to avoid a churny rename for a two-venue need; see
    predictit_watcher.py for the touch-only-depth and fee-model caveats
    that apply to signals produced against this candidate set.
    """
    candidates: List[Dict[str, Any]] = []
    for market in predictit_markets:
        if not isinstance(market, dict):
            continue
        market_name = market.get("name") or market.get("shortName") or ""
        contracts = market.get("contracts") or []
        for c in contracts:
            if not isinstance(c, dict) or c.get("status") != "Open":
                continue
            cid = c.get("id")
            # ``name`` (full name, e.g. "Flávio Bolsonaro") preferred over
            # ``shortName`` (often surname-only, e.g. "Bolsonaro") —
            # verified live: PredictIt's Brazil-election market has FOUR
            # Bolsonaro-family contracts, each a different person, and their
            # shortName is the bare surname for all of them. Same pattern
            # hit the Trump family (Eric/Donald/Donald Jr./Ivanka, all
            # shortName="Trump"/"Trump Jr."). Preferring the fuller ``name``
            # field is what actually disambiguates family members.
            contract_name = (c.get("name") or c.get("shortName") or "").strip()
            # In a multi-candidate race, a contract with no name is a generic
            # "Other"/"Field" bucket — not a comparable single entity, and
            # matching it collapses the question to pure boilerplate (verified:
            # this is what let unrelated candidates all jaccard-match the same
            # blank-name contract). A market with exactly one contract has no
            # such ambiguity — market_name alone is the whole question.
            if len(contracts) > 1 and not contract_name:
                continue
            question = f"{market_name} - {contract_name}".strip(" -")
            if cid is None or not question:
                continue
            close_raw = c.get("dateEnd")
            close_dt = _parse_kalshi_dt(close_raw) if close_raw and close_raw != "NA" else None
            candidates.append({"ticker": str(cid), "title": question, "close_time": close_dt})

    if not candidates:
        logger.warning("cross_venue.discover_predictit: no open PredictIt contracts.")
        return []
    return _match_all(poly_markets, poly_questions, candidates)


_MIN_SPORTS_TEAM_NAME_LEN = 4  # avoid matching on short/common tokens like "FC"/"SC"
_SPORTS_WIN_WORDS = ("win", "beat", "advance", "champion")


def discover_sports(
    poly_markets: List[Market],
    poly_questions: Dict[str, str],
    sports_candidates: Dict[str, tuple],
) -> List["MatchedPair"]:
    """Deterministic team-name matching against sports-odds outcomes.

    Unlike discover()/discover_predictit() (fuzzy text similarity — needed
    because political-market phrasing varies a lot), sports questions are
    structured enough to match by plain containment: does the Poly question
    mention the outcome's team name AND a winning-type verb? Much lower
    false-positive surface than fuzzy scoring, so this reports a fixed high
    confidence rather than a computed one — there's no continuous
    "how similar" axis here, just match / no-match.

    ``sports_candidates`` values are ``(question, snapshot, tokens)`` tuples
    from ``odds_watcher.OddsApiWatcher.candidates_and_snapshots()``, where
    ``tokens[0]`` is this outcome's team/competitor name.
    """
    pairs: List[MatchedPair] = []
    for market in poly_markets:
        question = poly_questions.get(market.condition_id)
        if not question:
            continue
        q_lower = question.lower()
        if not any(w in q_lower for w in _SPORTS_WIN_WORDS):
            continue

        matched_key = None
        for key, (cand_question, _snapshot, tokens) in sports_candidates.items():
            team_name = (tokens[0] if tokens else "") or ""
            if len(team_name) < _MIN_SPORTS_TEAM_NAME_LEN:
                continue
            if team_name.lower() in q_lower:
                matched_key = key
                break

        if matched_key is None:
            continue

        cand_question, _snapshot, _tokens = sports_candidates[matched_key]
        pairs.append(MatchedPair(
            poly_market=market,
            poly_question=question,
            kalshi_ticker=matched_key,
            kalshi_title=cand_question,
            match_confidence=Decimal("0.90"),
        ))
    return pairs


# ---------------------------------------------------------------------------
# Evaluation — mirrors core.evaluate_opportunity() structure exactly,
# except one leg walks a Polymarket book and the other walks a Kalshi book.
# ---------------------------------------------------------------------------


def evaluate_cross_venue_opportunity(
    poly_snapshot: OrderBookSnapshot,
    kalshi_snapshot: OrderBookSnapshot,
    config: AppConfig,
    fee_model: FeeModel,
    match_confidence: Decimal,
    question: str,
    other_venue_fee_params: Optional[FeeParams] = None,
    other_venue_fee_fn: Optional[Any] = None,
    _reason: Optional[dict] = None,
) -> Optional[CrossVenueSignal]:
    """Evaluate whether a matched Polymarket/<other venue> pair presents
    net-positive cross-venue arbitrage. ``kalshi_snapshot``/``other_venue_fee_params``
    generically mean "the second venue" — pass ``predictit_fee_params()``
    for a PredictIt-sourced snapshot; defaults to Kalshi for backward compat.

    ``other_venue_fee_fn``, if given, is ``(avg_price, accumulated_tokens) ->
    Decimal`` and OVERRIDES whatever fee ``other_venue_fee_params`` would
    have produced for the other-venue leg. Needed for venues whose fee
    mechanism isn't "rate * notional at entry" at all — e.g. PredictIt's
    real fee is 10% of profit, charged only at resolution
    (``predictit_expected_fee_usdc``). Leave ``None`` for venues where a
    flat per-trade rate is the actual mechanism (Kalshi) or there's no
    separate fee to model (sports odds — vig is already in the price).

    Same binary-market invariant as core.evaluate_opportunity(): YES + NO
    payouts sum to exactly $1.00 on EACH venue independently, so buying YES
    on one venue and NO on the other for a combined cost below $1.00 is
    profitable before fees/risk, PROVIDED both venues resolve identically —
    which is exactly the assumption match_confidence is meant to flag as
    unverified, not guaranteed.

    Tries both leg pairings (poly YES + kalshi NO, poly NO + kalshi YES) and
    returns whichever clears thresholds with the higher net edge.
    """
    payout_per_pair = Decimal("1")
    kalshi_params = other_venue_fee_params or kalshi_fee_params()

    def _combo(poly_asks, kalshi_asks, combo_name: str) -> Optional[tuple]:
        poly_quote: Optional[LegQuote] = calculate_effective_leg(
            asks=poly_asks,
            notional_usdc=config.trade_notional_usdc,
            fee_model=fee_model,
            fee_params=config.fee_params,
            min_order_size=config.min_order_size,
            tick_size=config.tick_size,
        )
        kalshi_quote: Optional[LegQuote] = calculate_effective_leg(
            asks=kalshi_asks,
            notional_usdc=config.trade_notional_usdc,
            fee_model=fee_model,
            fee_params=kalshi_params,
            min_order_size=config.min_order_size,
            tick_size=config.tick_size,
        )
        if kalshi_quote is not None and other_venue_fee_fn is not None:
            kalshi_quote = replace(
                kalshi_quote,
                fee_usdc=other_venue_fee_fn(kalshi_quote.avg_price, kalshi_quote.accumulated_tokens),
            )
        if poly_quote is None or kalshi_quote is None:
            return None

        total_cost = (poly_quote.avg_price + kalshi_quote.avg_price) * config.trade_notional_usdc
        edge_gross = payout_per_pair * config.trade_notional_usdc - total_cost
        fees_total = poly_quote.fee_usdc + kalshi_quote.fee_usdc
        risk_haircut = config.trade_notional_usdc * (
            config.slippage_buffer_pct + config.legged_risk_buffer_pct
        )
        edge_net = edge_gross - fees_total - risk_haircut
        edge_net_pct = edge_net / (config.trade_notional_usdc * Decimal("2"))
        return (combo_name, poly_quote, kalshi_quote, edge_gross, fees_total, risk_haircut, edge_net, edge_net_pct)

    combo_a = _combo(poly_snapshot.yes_asks, kalshi_snapshot.no_asks, "poly_yes_kalshi_no")
    combo_b = _combo(poly_snapshot.no_asks, kalshi_snapshot.yes_asks, "poly_no_kalshi_yes")

    candidates = [c for c in (combo_a, combo_b) if c is not None]
    if not candidates:
        if _reason is not None:
            _reason["reason"] = "insufficient_depth"
        return None

    # Pick the combo with the higher net edge.
    combo_name, poly_quote, kalshi_quote, edge_gross, fees_total, risk_haircut, edge_net, edge_net_pct = max(
        candidates, key=lambda c: c[6]
    )

    now_ms = int(time.time() * 1000)
    window_ms = now_ms - min(poly_snapshot.ts_ms or now_ms, kalshi_snapshot.ts_ms or now_ms)

    if _reason is not None:
        _reason.update({
            "combo": combo_name,
            "poly_best_ask": poly_quote.avg_price,
            "kalshi_best_ask": kalshi_quote.avg_price,
            "edge_gross": edge_gross,
            "fees_total": fees_total,
            "risk_haircut": risk_haircut,
            "edge_net": edge_net,
            "edge_net_pct": edge_net_pct,
            "window_ms": window_ms,
        })

    if edge_net < config.min_net_edge_usd:
        if _reason is not None:
            _reason["reason"] = "edge_below_threshold"
        return None
    if edge_net_pct < config.min_net_edge_pct:
        if _reason is not None:
            _reason["reason"] = "edge_below_threshold"
        return None

    return CrossVenueSignal(
        market_id_poly=poly_snapshot.market_id,
        ticker_kalshi=kalshi_snapshot.market_id,
        question=question,
        match_confidence=match_confidence,
        combo=combo_name,
        poly_quote=poly_quote,
        kalshi_quote=kalshi_quote,
        edge_gross=edge_gross,
        fees_total=fees_total,
        risk_haircut=risk_haircut,
        edge_net=edge_net,
        edge_net_pct=edge_net_pct,
        window_ms=window_ms,
        ts_ms=now_ms,
    )
