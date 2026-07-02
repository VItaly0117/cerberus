"""
MarketDiscovery — Agent A owned module.

Continuously fetches binary prediction markets from the Gamma API,
filters candidates to a narrow universe, persists them in storage,
and emits qualifying markets into an asyncio.Queue for downstream
pipeline stages.

Constraints:
  - Never import from fee_model.py, core.py, risk.py, executor.py, watcher.py
  - HTTP via httpx (async)
  - Rescan every 5 minutes
  - Exponential back-off on HTTP 429 (2s → 4s → 8s … capped at 64s)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

import httpx

from cerberus_runtime.config import Config
from cerberus_runtime.models import FeeParams, Market
from cerberus_runtime.storage import Storage

logger = logging.getLogger(__name__)

# ── Tunable constants ─────────────────────────────────────────────────────────
_RESCAN_INTERVAL: int = 300          # seconds between full scans
_REQUEST_TIMEOUT: float = 10.0       # HTTP timeout (seconds)
_BACKOFF_BASE: int = 2               # first back-off delay on 429 (seconds)
_BACKOFF_MAX: int = 64               # ceiling for exponential back-off
_MIN_VOLUME_24H: float = 1_000.0     # USDC — floor raised from 200: sub-$1k/24h markets
                                     # have too little depth for $50 notional legs without
                                     # crossing multiple price levels (see min_levels_gate
                                     # rejections in core.py), so they were dead weight.
_MAX_VOLUME_24H: float = 2_000_000.0  # USDC — raised: Polymarket $26B/mo in 2026
_MIN_DAYS_TO_END: int = 1    # lowered: include markets resolving tomorrow
_MAX_DAYS_TO_END: int = 90   # raised: include longer-dated markets
_MAX_DISCOVERY_PAGES: int = 20  # 20 * 100 = 2 000 markets/scan ceiling — plenty
                                # to fill MAX_OPEN_MARKETS=40+ after filtering,
                                # bounded so one scan cycle can't run away.
# ─────────────────────────────────────────────────────────────────────────────


class MarketDiscovery:
    """
    Async service that discovers eligible binary markets on Polymarket.

    Parameters
    ----------
    config:
        Runtime configuration (gamma_host, timeouts, …).
    storage:
        Async storage backend — must already be connected.
    candidate_queue:
        asyncio.Queue into which newly discovered Market objects are put.
        Downstream consumers (watcher, core, …) read from this queue.
    """

    def __init__(
        self,
        config: Config,
        storage: Storage,
        candidate_queue: asyncio.Queue,
    ) -> None:
        self.config = config
        self.storage = storage
        self.candidate_queue = candidate_queue

        # condition_ids of markets currently in the active candidate set
        self._active_candidates: Set[str] = set()

    # ── Public API ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """
        Infinite scan loop — call once via asyncio.create_task().

        Each iteration:
          1. Fetch all active/non-closed binary markets from Gamma API.
          2. Apply filter criteria.
          3. Upsert surviving markets into storage.
          4. Emit newly-discovered markets into candidate_queue.
          5. Mark as closed any markets that disappeared from the API response.
          6. Sleep RESCAN_INTERVAL seconds, then repeat.
        """
        logger.info("MarketDiscovery starting (rescan every %ds).", _RESCAN_INTERVAL)
        while True:
            try:
                await self._scan()
            except Exception:
                logger.exception("Unhandled error in MarketDiscovery._scan(); continuing.")
            await asyncio.sleep(_RESCAN_INTERVAL)

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _scan(self) -> None:
        """One complete fetch-filter-upsert-emit cycle."""
        raw_markets = await self._fetch_markets()
        if raw_markets is None:
            logger.warning("Scan cycle skipped (no data returned from Gamma API).")
            return

        seen_ids: Set[str] = set()

        for raw in raw_markets:
            condition_id: str = raw.get("condition_id", "<unknown>")
            try:
                if not self._passes_filter(raw):
                    continue

                market: Market = self._parse_market(raw)
                await self.storage.upsert_market(market)
                seen_ids.add(market.condition_id)

                if market.condition_id not in self._active_candidates:
                    self._active_candidates.add(market.condition_id)
                    await self.candidate_queue.put(market)
                    logger.info(
                        "New candidate market discovered: %s  vol24h=%.0f",
                        market.condition_id,
                        market.volume_24h,
                    )

            except Exception:
                logger.warning(
                    "Skipping malformed market %s.",
                    condition_id,
                    exc_info=True,
                )
                continue

        # Detect markets that were active last cycle but absent this cycle →
        # they have been resolved or closed by Polymarket.
        resolved_ids: Set[str] = self._active_candidates - seen_ids
        for cid in resolved_ids:
            logger.info("Market %s no longer active — marking closed.", cid)
            await self.storage.mark_market_closed(cid)
            self._active_candidates.discard(cid)

    async def _fetch_markets(self) -> Optional[List[Dict[str, Any]]]:
        """
        GET {gamma_host}/markets?active=true&closed=false&neg_risk=false

        Paginates via limit/offset — Gamma caps each page at 100 rows
        regardless of the requested limit, and without offset the API
        silently returns only its first page. A single unpaginated call
        was capping candidate discovery at ~20-100 markets, making
        MAX_OPEN_MARKETS values above that unreachable. Pages are walked
        until an empty page or _MAX_DISCOVERY_PAGES is hit.

        Handles per page:
          - HTTP 429 → exponential back-off (2, 4, 8, … 64 s), logs runtime_event
          - timeout > 10 s → log error, stop paginating, return pages collected so far
          - ConnectError → log critical, sleep 60 s, return pages collected so far
          - Other HTTP errors → log error, return pages collected so far
        """
        url = f"{self.config.gamma_host}/markets"
        page_size = 100
        all_markets: List[Dict[str, Any]] = []

        for page in range(_MAX_DISCOVERY_PAGES):
            params = {
                "active": "true",
                "closed": "false",
                "neg_risk": "false",
                "limit": page_size,
                "offset": page * page_size,
            }
            backoff: int = _BACKOFF_BASE

            while True:
                try:
                    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                        response = await client.get(url, params=params)

                    if response.status_code == 429:
                        logger.warning(
                            "Gamma API rate-limited (HTTP 429). Back-off %ds before retry.",
                            backoff,
                        )
                        await self._log_runtime_event(
                            "rate_limit_backoff", {"backoff_secs": backoff, "url": url}
                        )
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, _BACKOFF_MAX)
                        continue  # retry this page

                    response.raise_for_status()
                    data = response.json()

                    # Gamma may wrap the list in a top-level object
                    if isinstance(data, dict):
                        data = data.get("markets") or data.get("data") or []

                    break  # page fetched successfully, exit retry loop

                except httpx.TimeoutException:
                    logger.error(
                        "Gamma API request timed out (>%.0fs) on page %d. "
                        "Using %d markets collected so far.",
                        _REQUEST_TIMEOUT,
                        page,
                        len(all_markets),
                    )
                    return all_markets or None

                except httpx.ConnectError:
                    logger.critical(
                        "Gamma host '%s' is unreachable. Sleeping 60s before next cycle.",
                        self.config.gamma_host,
                    )
                    await asyncio.sleep(60)
                    return all_markets or None

                except httpx.HTTPStatusError as exc:
                    logger.error(
                        "Gamma API returned HTTP %s on page %d. "
                        "Using %d markets collected so far. Detail: %s",
                        exc.response.status_code,
                        page,
                        len(all_markets),
                        exc,
                    )
                    return all_markets or None

            if not data:
                break  # no more pages
            all_markets.extend(data)
            if len(data) < page_size:
                break  # last page (short page)

        return all_markets

    # ── Filtering ─────────────────────────────────────────────────────────────

    def _passes_filter(self, raw: Dict[str, Any]) -> bool:
        """
        Return True only if the raw market dict satisfies ALL criteria:

          ✓ active == True
          ✓ closed == False
          ✓ neg_risk == False
          ✓ exactly 2 outcome tokens (binary market)
          ✓ tokens include both a YES and a NO token (each with a non-empty id)
          ✓ end_date in (now + 3d, now + 30d)
          ✓ volume_24h in [1 000, 2 000 000] USDC
        """
        # ── Basic flags ──────────────────────────────────────────────────────
        if not raw.get("active", False):
            return False
        if raw.get("closed", True):
            return False
        # API returns camelCase "negRisk"; fall back to snake_case for compatibility.
        if raw.get("negRisk") or raw.get("neg_risk"):
            return False

        # ── Binary token structure ───────────────────────────────────────────
        # Gamma API may provide tokens as objects with outcome/token_id fields
        # (old format) or as a JSON string in clobTokenIds (new format, 2026).
        tokens: List[Dict[str, Any]] = raw.get("tokens") or []
        clob_ids_raw = raw.get("clobTokenIds")
        if not tokens and clob_ids_raw:
            # clobTokenIds is a JSON string: '["id1","id2"]'
            try:
                import json as _json
                clob_ids = _json.loads(clob_ids_raw) if isinstance(clob_ids_raw, str) else clob_ids_raw
                if len(clob_ids) == 2:
                    # Convention: index 0 = YES, index 1 = NO
                    tokens = [
                        {"token_id": clob_ids[0], "outcome": "YES"},
                        {"token_id": clob_ids[1], "outcome": "NO"},
                    ]
            except Exception:
                return False

        if len(tokens) != 2:
            return False

        outcomes = {str(t.get("outcome", "")).upper() for t in tokens}
        if "YES" not in outcomes or "NO" not in outcomes:
            return False

        if not all(t.get("token_id") for t in tokens):
            return False

        # ── End-date window ──────────────────────────────────────────────────
        end_date_raw: Optional[str] = (
            raw.get("endDate") or raw.get("end_date_iso") or raw.get("end_date")
        )
        if not end_date_raw:
            return False
        try:
            end_date = _parse_dt(end_date_raw)
        except (ValueError, TypeError):
            return False

        now = datetime.now(timezone.utc)
        if end_date < now + timedelta(days=_MIN_DAYS_TO_END):
            return False
        if end_date > now + timedelta(days=_MAX_DAYS_TO_END):
            return False

        # ── 24-hour volume ───────────────────────────────────────────────────
        volume = _coerce_float(raw.get("volume24hr") or raw.get("volume_24hr") or raw.get("volume_24h"))
        if volume < _MIN_VOLUME_24H or volume > _MAX_VOLUME_24H:
            return False

        return True

    # ── Parsing ───────────────────────────────────────────────────────────────

    def _parse_market(self, raw: Dict[str, Any]) -> Market:
        """
        Convert a raw Gamma API market dict into a Market domain object.

        Raises KeyError / ValueError on unexpected structure so callers can
        log a warning and skip the market.
        """
        condition_id: str = (
            raw.get("conditionId") or raw.get("condition_id") or raw["conditionId"]
        )

        # Build token list from tokens field or clobTokenIds (2026 API format).
        tokens: List[Dict[str, Any]] = raw.get("tokens") or []
        if not tokens:
            import json as _json
            clob_ids_raw = raw.get("clobTokenIds")
            if clob_ids_raw:
                clob_ids = _json.loads(clob_ids_raw) if isinstance(clob_ids_raw, str) else clob_ids_raw
                tokens = [
                    {"token_id": clob_ids[0], "outcome": "YES"},
                    {"token_id": clob_ids[1], "outcome": "NO"},
                ]
        yes_token_id = next(
            t["token_id"]
            for t in tokens
            if str(t.get("outcome", "")).upper() == "YES"
        )
        no_token_id = next(
            t["token_id"]
            for t in tokens
            if str(t.get("outcome", "")).upper() == "NO"
        )

        category: str = (
            raw.get("category") or raw.get("groupItemTitle") or raw.get("marketType") or ""
        )

        # 2026 API: fees in feeSchedule dict; fallback to fees dict or top-level fields.
        fee_schedule: Dict[str, Any] = raw.get("feeSchedule") or {}
        fees_raw: Dict[str, Any] = raw.get("fees") or {}
        taker_fee_rate = _coerce_float(
            fee_schedule.get("rate")
            or fees_raw.get("taker_fee_rate")
            or raw.get("takerBaseFee", 0) / 10000  # basis points → fraction
        )
        # Maker rebate from feeSchedule.rebateRate (e.g. 0.25 = 25% of taker fee)
        maker_fee_rate = 0.0  # Polymarket maker fee is zero
        fees_enabled: bool = taker_fee_rate > 0
        fee_params = FeeParams(
            fees_enabled=fees_enabled,
            maker_fee_rate=maker_fee_rate,
            taker_fee_rate=taker_fee_rate,
        )

        min_order_size = _coerce_float(
            raw.get("minimum_order_size") or raw.get("min_order_size") or 0
        )
        tick_size = _coerce_float(
            raw.get("minimum_tick_size") or raw.get("tick_size") or 0
        )

        end_date_raw: str = (
            raw.get("endDate") or raw.get("end_date_iso") or raw.get("end_date") or ""
        )
        if not end_date_raw:
            raise ValueError("no end_date field in market response")
        end_date = _parse_dt(end_date_raw)

        volume_24h = _coerce_float(raw.get("volume24hr") or raw.get("volume_24hr") or raw.get("volume_24h") or 0)

        return Market(
            condition_id=condition_id,
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            category=category,
            fee_params=fee_params,
            min_order_size=min_order_size,
            tick_size=tick_size,
            end_date=end_date,
            volume_24h=volume_24h,
        )

    # ── Observability ─────────────────────────────────────────────────────────

    async def _log_runtime_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Structured runtime event sink — extend to push to metrics/alerting."""
        logger.info("runtime_event type=%s data=%s", event_type, data)


# ── Module-level utilities ────────────────────────────────────────────────────

def _parse_dt(raw: str) -> datetime:
    """Parse an ISO-8601 datetime string into a UTC-aware datetime."""
    # Python 3.10 fromisoformat doesn't handle trailing 'Z'
    normalised = raw.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalised)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _coerce_float(value: Any) -> float:
    """Safely coerce a value (str, int, float, None) to float."""
    if value is None:
        return 0.0
    return float(value)
