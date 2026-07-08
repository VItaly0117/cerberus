#!/usr/bin/env python3
"""
cross_venue_test.py — read-only cross-venue (Polymarket x Kalshi) edge measurement.

Answers ONE question: after real fees on both venues, is there a capturable
edge between matched Polymarket/Kalshi markets? No orders, no executor,
no risk.py/executor.py involvement — this is a measurement tool only.

Usage
-----
  python3 cross_venue_test.py --minutes 20
      Smoke test — verify matching works and nothing crashes.

  python3 cross_venue_test.py --minutes 90
      Full measurement run. Writes every evaluated pair to cross_venue_signals,
      prints a RAPID READ verdict at the end.

Fully decoupled from the live trading bot's own state: candidates come from
cross_venue.fetch_poly_match_candidates() (a wide-net Gamma API fetch, NOT
market_discovery.py's 3-30-day/volume-floor window — that window is tuned for
the bot's own legged-risk arbitrage and excludes almost everything the other
cross-venue partners actually list), and both legs of every matched pair are
polled directly via REST (cross_venue.fetch_poly_orderbook_snapshot() for the
Polymarket leg, venue_adapter.get_snapshot() for the other venue) — no
dependency on watcher.py's WS subscriptions or market_discovery.py's own
tracked set.

New for this measurement:
  - cerberus_runtime.kalshi_watcher.KalshiWatcher (Kalshi public REST polling)
  - cerberus_runtime.predictit_watcher.PredictItWatcher (PredictIt public REST polling)
  - cerberus_runtime.cross_venue (wide-net poly fetch, matcher, direct book fetch, evaluator)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from decimal import Decimal
from typing import Dict, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cross_venue_test")

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from cerberus_runtime.config import get_app_config, get_config  # noqa: E402
from cerberus_runtime.storage import Storage  # noqa: E402
from cerberus_runtime.fee_model import FeeModel  # noqa: E402
from cerberus_runtime.kalshi_watcher import KalshiWatcher  # noqa: E402
from cerberus_runtime.predictit_watcher import PredictItWatcher  # noqa: E402
from cerberus_runtime.odds_watcher import OddsApiWatcher  # noqa: E402
from cerberus_runtime import cross_venue  # noqa: E402

# ── Tunables ─────────────────────────────────────────────────────────────────
_REMATCH_INTERVAL_S = 300       # re-run discover() every 5 minutes
_STATUS_INTERVAL_S = 60         # live progress print
_KALSHI_STALE_AFTER_S = 120     # kalshi fetch considered "not keeping up" past this
_POLY_STALE_AFTER_S = 120       # no poly snapshot considered "feed stalled" past this
_PREDICTIT_POLL_INTERVAL_S = 30.0  # PredictIt returns all markets in one call — poll fast
_SUSPICIOUS_GROSS_EDGE_PCT = 0.08
_STALE_WINDOW_MS = 10 * 60 * 1000  # 10 minutes — a "protuhший" cross-venue window


class _KalshiAdapter:
    """Wraps KalshiWatcher behind the small interface _rematch_loop/_core_loop
    need, so both loops work unmodified against either venue."""

    label = "Kalshi"

    def __init__(self, watcher: KalshiWatcher) -> None:
        self.watcher = watcher

    async def discover(self, active, poly_questions) -> list:
        return await cross_venue.discover(active, poly_questions, self.watcher)

    async def get_snapshot(self, ticker: str):
        return await self.watcher.fetch_orderbook_snapshot(ticker)

    def fee_params(self):
        return cross_venue.kalshi_fee_params()

    def fee_fn(self):
        return None  # Kalshi's real fee IS a flat per-trade rate — fee_params above is correct as-is.

    def start_poll_task(self, notional_usdc: Decimal, stop_event: asyncio.Event) -> Optional["asyncio.Task"]:
        return None  # Kalshi fetches live, per-ticker, on demand — no separate poll needed.


class _PredictItAdapter:
    """Wraps PredictItWatcher behind the same interface as _KalshiAdapter.

    Unlike Kalshi, PredictIt's public endpoint returns EVERY open market's
    prices in one call, so a background poll loop refreshes an in-memory
    snapshot cache every _PREDICTIT_POLL_INTERVAL_S rather than fetching
    per-ticker on demand.

    KNOWN SIMPLIFICATION (see predictit_watcher.py): touch-only depth, and
    fee_params() reuses a per-trade rate that does not reflect PredictIt's
    real profit-based fee — signals from this venue are illustrative only.
    """

    label = "PredictIt"

    def __init__(self, watcher: PredictItWatcher) -> None:
        self.watcher = watcher
        self._raw_markets: Optional[list] = None
        self._snapshots: dict = {}

    async def _refresh(self, notional_usdc: Decimal) -> None:
        raw = await self.watcher.fetch_all()
        if raw is None:
            return
        ts_ms = int(time.time() * 1000)
        candidates = self.watcher.candidates_and_snapshots(raw, notional_usdc, ts_ms)
        self._raw_markets = raw
        self._snapshots = {cid: snap for cid, (_q, snap) in candidates.items()}

    def start_poll_task(self, notional_usdc: Decimal, stop_event: asyncio.Event) -> "asyncio.Task":
        async def _poll() -> None:
            while not stop_event.is_set():
                try:
                    await self._refresh(notional_usdc)
                except Exception:
                    logger.exception("PredictIt poll: unhandled error, continuing.")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=_PREDICTIT_POLL_INTERVAL_S)
                except asyncio.TimeoutError:
                    pass
        return asyncio.create_task(_poll(), name="predictit_poll")

    async def discover(self, active, poly_questions) -> list:
        if not self._raw_markets:
            return []
        return cross_venue.discover_predictit(active, poly_questions, self._raw_markets)

    async def get_snapshot(self, ticker: str):
        return self._snapshots.get(ticker)

    def fee_params(self):
        return cross_venue.predictit_fee_params()

    def fee_fn(self):
        # PredictIt's real fee is 10% of PROFIT at resolution (winning side
        # only), not a per-trade rate on notional — see
        # cross_venue.predictit_expected_fee_usdc's docstring. This
        # overrides fee_params' (now-zeroed) per-trade calculation.
        return cross_venue.predictit_expected_fee_usdc


_ODDS_API_POLL_INTERVAL_S = 900.0  # 15 min
# Free tier is 500 requests/month total. Each poll cycle costs
# len(_OddsApiAdapter._SPORT_KEYS) requests, so at the default 3 sport keys
# and this interval that's ~3*4/hr = 12 req/hr — fine for an hours-long
# measurement run, but NOT safe to leave running unattended for weeks.


class _OddsApiAdapter:
    """Wraps OddsApiWatcher behind the same interface as
    _KalshiAdapter/_PredictItAdapter. Polls a fixed list of sport keys on a
    timer (The Odds API also returns everything for one sport per call, no
    per-outcome on-demand endpoint) and matches deterministically by team
    name (cross_venue.discover_sports) instead of fuzzy text — see that
    function's docstring for why sports doesn't need fuzzy scoring.

    KNOWN LIMITATION: built and unit-tested against synthetic data only —
    no ODDS_API_KEY was available while writing this, so the real response
    shape from the-odds-api.com has not been verified live. Watch the first
    live run's logs closely before trusting its numbers.
    """

    label = "Sports (OddsAPI)"

    _SPORT_KEYS = [
        "soccer_fifa_world_cup_winner",  # tournament outright futures — matches Polymarket's
                                          # highest-volume sports category, verified this project
        "basketball_nba",
        "americanfootball_nfl",
    ]

    def __init__(self, watcher: OddsApiWatcher) -> None:
        self.watcher = watcher
        self._candidates: dict = {}

    async def _refresh(self, notional_usdc: Decimal) -> None:
        ts_ms = int(time.time() * 1000)
        merged: dict = {}
        for sport_key in self._SPORT_KEYS:
            markets = "outrights" if sport_key.endswith("_winner") else "h2h"
            events = await self.watcher.fetch_odds(sport_key, markets=markets)
            if not events:
                continue
            merged.update(self.watcher.candidates_and_snapshots(events, notional_usdc, ts_ms))
        self._candidates = merged

    def start_poll_task(self, notional_usdc: Decimal, stop_event: asyncio.Event) -> "asyncio.Task":
        async def _poll() -> None:
            while not stop_event.is_set():
                try:
                    await self._refresh(notional_usdc)
                except Exception:
                    logger.exception("OddsAPI poll: unhandled error, continuing.")
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=_ODDS_API_POLL_INTERVAL_S)
                except asyncio.TimeoutError:
                    pass
        return asyncio.create_task(_poll(), name="odds_api_poll")

    async def discover(self, active, poly_questions) -> list:
        return cross_venue.discover_sports(active, poly_questions, self._candidates)

    async def get_snapshot(self, ticker: str):
        cand = self._candidates.get(ticker)
        return cand[1] if cand else None

    def fee_params(self):
        return cross_venue.odds_fee_params()

    def fee_fn(self):
        return None  # no separate fee to model — the vig is already in the quoted price.


class _State:
    """Mutable counters shared across the background tasks — a plain object
    beats threading six separate nonlocal counters through closures."""

    def __init__(self) -> None:
        self.matched_pairs: Dict[str, object] = {}  # condition_id -> MatchedPair
        self.poly_fetch_ok = 0
        self.poly_fetch_fail = 0
        self.matched_snapshots_evaluated = 0
        self.kalshi_fetch_ok = 0
        self.kalshi_fetch_fail = 0
        self.viable = 0
        self.filtered = 0
        self.last_poly_snapshot_ts = 0.0
        self.last_kalshi_fetch_ok_ts = 0.0
        self.rematch_rounds = 0


async def _rematch_loop(gamma_host: str, venue_adapter,
                         state: _State, stop_event: asyncio.Event) -> None:
    """Periodically re-run discover() over a wide-net Polymarket candidate
    fetch (cross_venue.fetch_poly_match_candidates — NOT market_discovery.py's
    3-30-day/volume-floor window, which excludes almost everything the other
    cross-venue partners list) so matches aren't limited to whatever the live
    trading bot happens to be tracking for its own unrelated purpose."""
    while not stop_event.is_set():
        try:
            active = await cross_venue.fetch_poly_match_candidates(gamma_host)
            if active:
                poly_questions = await cross_venue.fetch_poly_questions(
                    gamma_host, {m.condition_id for m in active}
                )
                pairs = await venue_adapter.discover(active, poly_questions)
                state.matched_pairs = {p.poly_market.condition_id: p for p in pairs}
                state.rematch_rounds += 1
                logger.info(
                    "Rematch round %d: %d wide-net poly candidates, %d matched pairs.",
                    state.rematch_rounds, len(active), len(pairs),
                )
                for p in pairs:
                    logger.info(
                        "  MATCH confidence=%.2f  poly=%r  %s=%s (%r)",
                        float(p.match_confidence), p.poly_question[:60],
                        venue_adapter.label, p.kalshi_ticker, p.kalshi_title[:60],
                    )
        except Exception:
            logger.exception("rematch_loop: unhandled error, continuing.")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_REMATCH_INTERVAL_S)
        except asyncio.TimeoutError:
            pass


_POLY_LEG_POLL_INTERVAL_S = 15.0  # how often every currently-matched pair gets re-priced


async def _core_loop(
    clob_rest_url: str,
    storage: Storage,
    app_config,
    fee_model: FeeModel,
    venue_adapter,
    state: _State,
    stop_event: asyncio.Event,
) -> None:
    """Poll BOTH legs of every currently-matched pair directly over REST —
    the Polymarket leg via cross_venue.fetch_poly_orderbook_snapshot(), the
    other venue's leg via venue_adapter.get_snapshot(). No dependency on
    watcher.py's WS subscriptions: matched pairs come from the wide-net fetch
    and are usually markets the live trading bot isn't tracking at all."""
    while not stop_event.is_set():
        pairs = list(state.matched_pairs.values())
        for pair in pairs:
            if stop_event.is_set():
                break

            poly_snapshot = await cross_venue.fetch_poly_orderbook_snapshot(
                clob_rest_url, pair.poly_market.condition_id,
                pair.poly_market.yes_token_id, pair.poly_market.no_token_id,
            )
            if poly_snapshot is None:
                state.poly_fetch_fail += 1
                continue
            state.poly_fetch_ok += 1
            state.last_poly_snapshot_ts = time.time()

            other_snapshot = await venue_adapter.get_snapshot(pair.kalshi_ticker)
            if other_snapshot is None:
                state.kalshi_fetch_fail += 1
                continue
            state.kalshi_fetch_ok += 1
            state.last_kalshi_fetch_ok_ts = time.time()
            state.matched_snapshots_evaluated += 1

            reason_box: dict = {}
            signal = cross_venue.evaluate_cross_venue_opportunity(
                poly_snapshot=poly_snapshot,
                kalshi_snapshot=other_snapshot,
                config=app_config,
                fee_model=fee_model,
                match_confidence=pair.match_confidence,
                question=pair.poly_question,
                other_venue_fee_params=venue_adapter.fee_params(),
                other_venue_fee_fn=venue_adapter.fee_fn(),
                _reason=reason_box,
            )

            if signal is not None:
                state.viable += 1
                gross_pct = float(signal.edge_gross) / (2.0 * float(app_config.trade_notional_usdc))
                flag = ""
                if gross_pct > _SUSPICIOUS_GROSS_EDGE_PCT:
                    flag = "  !! SUSPICIOUS (>8%% gross — likely matcher bug, not real arb)"
                if signal.window_ms > _STALE_WINDOW_MS:
                    flag += "  !! STALE WINDOW (>10min gap between venue snapshots)"
                logger.info(
                    "VIABLE  poly=%s  kalshi=%s  combo=%s  edge_net=%.4f (%.2f%%)  window=%dms%s",
                    pair.poly_market.condition_id, pair.kalshi_ticker, signal.combo,
                    float(signal.edge_net), float(signal.edge_net_pct) * 100,
                    signal.window_ms, flag,
                )
                await storage.insert_cross_venue_signal(
                    market_id_poly=pair.poly_market.condition_id,
                    ticker_kalshi=pair.kalshi_ticker,
                    question=pair.poly_question,
                    match_confidence=pair.match_confidence,
                    result="VIABLE",
                    trade_notional_usdc=app_config.trade_notional_usdc,
                    signal=signal,
                    ts_ms=signal.ts_ms,
                )
            else:
                state.filtered += 1
                await storage.insert_cross_venue_signal(
                    market_id_poly=pair.poly_market.condition_id,
                    ticker_kalshi=pair.kalshi_ticker,
                    question=pair.poly_question,
                    match_confidence=pair.match_confidence,
                    result="FILTERED",
                    trade_notional_usdc=app_config.trade_notional_usdc,
                    rejection_reason=reason_box.get("reason", "unknown"),
                    rejected_edge=reason_box or None,
                    ts_ms=int(time.time() * 1000),
                )

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_POLY_LEG_POLL_INTERVAL_S)
        except asyncio.TimeoutError:
            pass


async def _status_printer(state: _State, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_STATUS_INTERVAL_S)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        logger.info(
            "[STATUS] matched_pairs=%d  poly_ok=%d  poly_fail=%d  matched_evaluated=%d  "
            "kalshi_ok=%d  kalshi_fail=%d  viable=%d  filtered=%d",
            len(state.matched_pairs), state.poly_fetch_ok, state.poly_fetch_fail,
            state.matched_snapshots_evaluated,
            state.kalshi_fetch_ok, state.kalshi_fetch_fail,
            state.viable, state.filtered,
        )


def _print_rapid_read(state: _State, summary: dict, run_minutes: float, venue_label: str = "Kalshi") -> None:
    now = time.time()
    poly_alive = (
        state.last_poly_snapshot_ts > 0
        and (now - state.last_poly_snapshot_ts) < _POLY_STALE_AFTER_S
    )
    kalshi_alive = (
        state.last_kalshi_fetch_ok_ts > 0
        and (now - state.last_kalshi_fetch_ok_ts) < _KALSHI_STALE_AFTER_S
    )
    kalshi_attempted = state.kalshi_fetch_ok + state.kalshi_fetch_fail
    kalshi_success_rate = (state.kalshi_fetch_ok / kalshi_attempted) if kalshi_attempted else 0.0

    print("\n" + "=" * 72)
    print(f"  CROSS-VENUE (Polymarket x {venue_label}) — RAPID READ")
    print("=" * 72)
    print(f"  Run duration:              {run_minutes:.1f} min")
    print(f"  Rematch rounds:            {state.rematch_rounds}")
    print(f"  Matched pairs (last round): {len(state.matched_pairs)}")
    print(f"  Poly leg fetch ok/fail:    {state.poly_fetch_ok}/{state.poly_fetch_fail}")
    print(f"  Matched & evaluated:       {state.matched_snapshots_evaluated}")
    print(f"  {venue_label} fetch ok/fail:    {state.kalshi_fetch_ok}/{state.kalshi_fetch_fail} "
          f"(success rate {kalshi_success_rate*100:.0f}%)")
    print("-" * 72)
    print(f"  VIABLE signals:            {summary['viable_signals']}")
    print(f"  Distinct Poly markets:     {summary['distinct_poly_markets']}")
    print(f"  Distinct {venue_label} tickers: {summary['distinct_kalshi_tickers']}")
    print(f"  Median edge_net_pct:       {float(summary['median_edge_net_pct'])*100:.3f}%")
    print(f"  Median window (staleness): {float(summary['median_window_ms']):.0f} ms")
    print(f"  Suspicious (>8% gross):    {summary['suspicious_high_edge']}")
    print("-" * 72)
    print(f"  Polymarket feed alive at end:  {'YES' if poly_alive else 'NO — STALLED'}")
    print(f"  {venue_label} feed alive at end:      {'YES' if kalshi_alive else 'NO — STALLED'}")
    print("=" * 72)

    if not poly_alive or kalshi_success_rate < 0.2:
        verdict = (
            "INCONCLUSIVE — feed coverage too poor to trust this window "
            "(fix connectivity/coverage before drawing any conclusion)."
        )
    elif summary["viable_signals"] == 0:
        verdict = "NO SIGNAL IN THIS WINDOW."
    elif summary["distinct_poly_markets"] < 2 or summary["suspicious_high_edge"] > 0:
        verdict = (
            "BORDERLINE — signals present but too thin/suspicious to trust yet "
            "(few distinct markets and/or >8% gross-edge outliers present)."
        )
    else:
        verdict = "SIGNAL PRESENT — see numbers above; still verify match_confidence by hand."

    print(f"  VERDICT: {verdict}")
    print("=" * 72 + "\n")


async def run_measurement(minutes: float, venue: str) -> int:
    infra_cfg = get_config()
    app_config = get_app_config()

    storage = Storage(infra_cfg.db_path)
    await storage.connect()

    fee_model = FeeModel()
    if venue == "kalshi":
        venue_adapter = _KalshiAdapter(KalshiWatcher())
    elif venue == "sports":
        odds_api_key = os.getenv("ODDS_API_KEY", "")
        if not odds_api_key:
            logger.error("--venue sports requires ODDS_API_KEY in the environment/.env — aborting.")
            return 1
        venue_adapter = _OddsApiAdapter(OddsApiWatcher(api_key=odds_api_key))
    else:
        venue_adapter = _PredictItAdapter(PredictItWatcher())
    state = _State()

    stop_event = asyncio.Event()
    start_ts = time.monotonic()
    start_ts_ms = int(time.time() * 1000)

    async def _duration_guard() -> None:
        await asyncio.sleep(minutes * 60)
        logger.info("Duration %.1f min reached — stopping.", minutes)
        stop_event.set()

    tasks = [
        asyncio.create_task(
            _rematch_loop(infra_cfg.gamma_host, venue_adapter, state, stop_event),
            name="rematch",
        ),
        asyncio.create_task(
            _core_loop(infra_cfg.clob_rest_url, storage, app_config, fee_model, venue_adapter, state, stop_event),
            name="core",
        ),
        asyncio.create_task(_status_printer(state, stop_event), name="status"),
        asyncio.create_task(_duration_guard(), name="duration"),
    ]

    poll_task = venue_adapter.start_poll_task(app_config.trade_notional_usdc, stop_event)
    if poll_task is not None:
        tasks.append(poll_task)

    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    run_minutes = (time.monotonic() - start_ts) / 60
    # Scoped to THIS run's own start — otherwise this reads the whole
    # cross_venue_signals table's history, silently mixing in rows from any
    # earlier/concurrent run against the same DB file (verified: this is
    # exactly what made an old-architecture run's RAPID READ show numbers
    # from an unrelated ad-hoc smoke test of newer code run in parallel).
    summary = await storage.get_cross_venue_summary(since_ts_ms=start_ts_ms)
    _print_rapid_read(state, summary, run_minutes, venue_adapter.label)

    await storage.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Cross-venue (Polymarket x other-venue) edge measurement")
    parser.add_argument("--minutes", type=float, default=90.0, metavar="N",
                         help="Measurement window in minutes (default: 90).")
    parser.add_argument("--venue", choices=["kalshi", "predictit", "sports"], default="predictit",
                         help="Second venue to match against Polymarket. Defaults to predictit "
                              "because Kalshi's public REST API is geo-blocked outside the US. "
                              "'sports' requires ODDS_API_KEY in the environment/.env.")
    args = parser.parse_args()

    try:
        return asyncio.run(run_measurement(args.minutes, args.venue))
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
