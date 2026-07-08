"""
PredictItWatcher — cross-venue measurement module (read-only), PredictIt variant.

PredictIt's public ``/api/marketdata/all/`` endpoint returns every open
market's contracts with best bid/ask ALREADY as direct prices
(``bestBuyYesCost`` / ``bestBuyNoCost``) — no bid->ask inversion needed,
unlike Kalshi. The trade-off: this endpoint exposes only the single best
price per side, not book depth. Every "leg" evaluated against PredictIt
data is therefore a TOUCH-ONLY quote, not a depth-walked fill — a single
synthetic ``PriceLevel`` is sized to fully cover ``trade_notional_usdc`` at
the touch price, which optimistically assumes that price holds for the
whole clip. This is weaker evidence than the Polymarket/Kalshi depth walk
and should be read as such.

Fee-model caveat — explicitly waived for this smoke measurement, per
instruction: PredictIt charges a 10% fee on PROFIT at contract resolution,
not a per-trade rate on notional. Reusing FeeModel's per-trade-rate
mechanism here (via ``cross_venue.predictit_fee_params()``) is a known
simplification for a first look, not a faithful cost model — edge_net
numbers against this venue are illustrative only, not trustworthy the way
the Kalshi path (real per-trade taker fee) is.

Constraints
-----------
- Never import market_discovery.py, watcher.py, orderbook.py, core.py,
  risk.py, executor.py.
- HTTP via httpx (async), no API keys — public read-only endpoint.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

import httpx

from cerberus_runtime.models import OrderBookSnapshot, PriceLevel

logger = logging.getLogger(__name__)

_DEFAULT_URL: str = "https://www.predictit.org/api/marketdata/all/"
_REQUEST_TIMEOUT: float = 10.0

# See module docstring — NOT PredictIt's real fee (10% of profit at
# resolution). Kept at the headline rate so the number is at least in the
# right ballpark for a smoke-level read, not zeroed out and pretending fees
# don't exist.
PREDICTIT_TAKER_FEE_RATE: float = 0.10


class PredictItWatcher:
    """Polls the public PredictIt market-data endpoint.

    Unlike Kalshi, one call returns every open market + contract + price in
    a single response, so there is no separate per-ticker order-book call.
    """

    def __init__(self, url: str = _DEFAULT_URL) -> None:
        self.url = url

    async def fetch_all(self) -> Optional[List[Dict[str, Any]]]:
        """GET /api/marketdata/all/ -> list of market dicts (each with a
        ``contracts`` list). Returns ``None`` on any HTTP/parse failure."""
        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                response = await client.get(self.url)
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException:
            logger.error("PredictIt API timeout (%ss).", _REQUEST_TIMEOUT)
            return None
        except httpx.ConnectError:
            logger.critical("PredictIt API connection failed (%s).", self.url)
            return None
        except httpx.HTTPStatusError as exc:
            logger.warning("PredictIt API returned HTTP %s.", exc.response.status_code)
            return None
        except ValueError:
            logger.warning("PredictIt API: malformed JSON.")
            return None

        if not isinstance(data, dict):
            logger.warning("PredictIt API: non-dict payload.")
            return None
        markets = data.get("markets")
        if not isinstance(markets, list):
            logger.warning("PredictIt API: missing/invalid 'markets' field.")
            return None
        return markets

    def candidates_and_snapshots(
        self,
        markets: List[Dict[str, Any]],
        notional_usdc: Decimal,
        ts_ms: int,
    ) -> Dict[str, Tuple[str, OrderBookSnapshot]]:
        """Flatten market->contracts into {contract_id: (question, snapshot)}.

        Each PredictIt "contract" (not "market") is its own binary Yes/No
        proposition — a market with 6 contracts is 6 independent binary
        questions, not one. Skips contracts that are not ``status=="Open"``
        or have missing/out-of-range prices.
        """
        result: Dict[str, Tuple[str, OrderBookSnapshot]] = {}
        for market in markets:
            if not isinstance(market, dict):
                continue
            market_name = market.get("name") or market.get("shortName") or ""
            contracts = market.get("contracts") or []
            if not isinstance(contracts, list):
                continue

            for c in contracts:
                if not isinstance(c, dict) or c.get("status") != "Open":
                    continue
                cid = c.get("id")
                if cid is None:
                    continue

                try:
                    yes_ask = Decimal(str(c.get("bestBuyYesCost")))
                    no_ask = Decimal(str(c.get("bestBuyNoCost")))
                except (InvalidOperation, TypeError):
                    continue
                if not (Decimal("0") < yes_ask < Decimal("1")):
                    continue
                if not (Decimal("0") < no_ask < Decimal("1")):
                    continue

                contract_name = c.get("shortName") or c.get("name") or ""
                question = f"{market_name} - {contract_name}".strip(" -")
                if not question:
                    continue

                # Touch-only depth (see module docstring): one synthetic
                # level per side, sized to just cover the requested notional
                # at the touch price.
                yes_size = notional_usdc / yes_ask
                no_size = notional_usdc / no_ask

                result[str(cid)] = (
                    question,
                    OrderBookSnapshot(
                        market_id=str(cid),
                        yes_asks=[PriceLevel(price=yes_ask, size=yes_size)],
                        no_asks=[PriceLevel(price=no_ask, size=no_size)],
                        timestamp=ts_ms / 1000.0,
                        condition_id=str(cid),
                        fee_params=None,
                        ts_ms=ts_ms,
                    ),
                )
        return result
