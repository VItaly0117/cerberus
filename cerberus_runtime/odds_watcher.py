"""
odds_watcher.py — cross-venue measurement module (read-only), sports-odds variant.

Wraps The Odds API (https://the-odds-api.com) — a REST aggregator of
moneyline/outright odds across real-money sportsbooks (DraftKings, FanDuel,
Pinnacle, ...). Requires an ODDS_API_KEY (free tier: 500 requests/month at
the-odds-api.com — no card needed).

Two market shapes matter here, and they map to Polymarket very differently:
  - ``h2h`` (moneyline, per-game): exactly 2 outcomes for most sports (NBA,
    NFL) — a clean Yes/No pair. 3-way sports (soccer: home/away/draw) don't
    reduce to a single binary "will Team X win" question and are skipped.
  - ``outrights`` (tournament winner futures, e.g. "World Cup winner"): N
    outcomes, one per team — structurally identical to PredictIt's
    multi-contract "who will win X" markets, and matches what we verified
    has real volume on Polymarket (World Cup winner markets were the
    single largest volume category seen in this project's measurement).

No per-trade fee is modeled: a sportsbook's edge (the "vig") is already
baked into the two-sided quoted price itself, unlike an exchange's
fee-on-top-of-mid model. ODDS_API_TAKER_FEE_RATE is 0.0 deliberately — it is
not a simplification of a real fee, there simply isn't a separate one to
apply here.

Constraints
-----------
- Never import market_discovery.py, watcher.py, orderbook.py, core.py,
  risk.py, executor.py.
- HTTP via httpx (async), read-only GET only.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

import httpx

from cerberus_runtime.models import OrderBookSnapshot, PriceLevel

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL: str = "https://api.the-odds-api.com/v4"
_REQUEST_TIMEOUT: float = 10.0

ODDS_API_TAKER_FEE_RATE: float = 0.0


def american_odds_to_prob(odds: Any) -> Optional[Decimal]:
    """Convert American odds (e.g. -150, +120) to an implied probability in
    (0, 1). Returns None for malformed/zero input — 0 is not a valid
    American price (there is no such quote)."""
    try:
        o = Decimal(str(odds))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not o.is_finite() or o == 0:
        return None
    if o > 0:
        prob = Decimal("100") / (o + Decimal("100"))
    else:
        prob = (-o) / ((-o) + Decimal("100"))
    if not (Decimal("0") < prob < Decimal("1")):
        return None
    return prob


class OddsApiWatcher:
    """Polls The Odds API for a configured list of sports.

    No persistent connection: each call is a discrete HTTP request, same
    shape as kalshi_watcher.py/predictit_watcher.py.
    """

    def __init__(self, api_key: str, base_url: str = _DEFAULT_BASE_URL) -> None:
        self.api_key = api_key
        self.base_url = base_url

    async def fetch_odds(
        self, sport_key: str, regions: str = "us", markets: str = "h2h",
    ) -> Optional[List[Dict[str, Any]]]:
        """GET /v4/sports/{sport_key}/odds — list of events, each with a
        ``bookmakers`` list of per-book quoted outcomes.

        Returns ``None`` if no API key is configured, or on any HTTP/parse
        failure — the caller treats this the same as "no data this cycle",
        never as a crash.
        """
        if not self.api_key:
            logger.warning("OddsApiWatcher: no ODDS_API_KEY configured — skipping fetch.")
            return None
        url = f"{self.base_url}/sports/{sport_key}/odds"
        params = {
            "apiKey": self.api_key, "regions": regions,
            "markets": markets, "oddsFormat": "american",
        }
        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        except httpx.TimeoutException:
            logger.error("Odds API timeout for sport %s.", sport_key)
            return None
        except httpx.ConnectError:
            logger.critical("Odds API connection failed for sport %s.", sport_key)
            return None
        except httpx.HTTPStatusError as exc:
            logger.warning("Odds API HTTP %s for sport %s: %s", exc.response.status_code, sport_key, exc)
            return None
        except ValueError:
            logger.warning("Odds API: malformed JSON for sport %s.", sport_key)
            return None

        if not isinstance(data, list):
            logger.warning("Odds API: non-list payload for sport %s.", sport_key)
            return None
        return data

    def candidates_and_snapshots(
        self, events: List[Dict[str, Any]], notional_usdc: Decimal, ts_ms: int,
    ) -> Dict[str, Tuple[str, OrderBookSnapshot, List[str]]]:
        """Flatten events -> {outcome_key: (question, snapshot, team_tokens)}.

        For each event, for each outcome (team), take the BEST price quoted
        for that outcome across all bookmakers — since implied probability
        IS the ask price to buy that outcome (as core.calculate_effective_leg
        treats every venue), the best price for a buyer is the LOWEST
        implied probability, not the highest, mirroring how a real
        cross-book shopper would act. This is touch-only depth, same
        caveat as predictit_watcher.py: one synthetic level per side, sized
        to just cover ``notional_usdc`` at that price, not a real book walk.

        3-way markets (soccer home/away/draw) are skipped entirely: "not
        Team A" is ambiguous between "Team B wins" and "draw", so there is
        no clean Yes/No mapping to a Polymarket "will Team A win" question.
        """
        result: Dict[str, Tuple[str, OrderBookSnapshot, List[str]]] = {}
        for event in events:
            if not isinstance(event, dict):
                continue

            # Best price per outcome name, across all bookmakers/markets entries.
            best_prob: Dict[str, Decimal] = {}
            for bm in event.get("bookmakers") or []:
                if not isinstance(bm, dict):
                    continue
                for mkt in bm.get("markets") or []:
                    if not isinstance(mkt, dict):
                        continue
                    for outcome in mkt.get("outcomes") or []:
                        if not isinstance(outcome, dict):
                            continue
                        name = outcome.get("name")
                        prob = american_odds_to_prob(outcome.get("price"))
                        if name is None or prob is None:
                            continue
                        if name not in best_prob or prob < best_prob[name]:
                            best_prob[name] = prob

            if len(best_prob) != 2:
                # Skip 3-way (soccer draw) and anything malformed — only a
                # clean 2-outcome market maps to a binary Yes/No question.
                continue

            names = list(best_prob.keys())
            event_id = event.get("id")
            commence_time = event.get("commence_time")
            if not event_id:
                continue

            for name in names:
                other = names[1] if names[0] == name else names[0]
                yes_price = best_prob[name]
                no_price = Decimal("1") - yes_price
                if not (Decimal("0") < yes_price < Decimal("1")):
                    continue
                yes_size = notional_usdc / yes_price
                no_size = notional_usdc / no_price if no_price > 0 else Decimal("0")
                outcome_key = f"{event_id}:{name}"
                question = f"Will {name} beat {other}?"
                result[outcome_key] = (
                    question,
                    OrderBookSnapshot(
                        market_id=outcome_key,
                        yes_asks=[PriceLevel(price=yes_price, size=yes_size)],
                        no_asks=[PriceLevel(price=no_price, size=no_size)] if no_size > 0 else [],
                        timestamp=ts_ms / 1000.0,
                        condition_id=outcome_key,
                        fee_params=None,
                        ts_ms=ts_ms,
                    ),
                    [str(name), str(other), str(commence_time or "")],
                )
        return result
