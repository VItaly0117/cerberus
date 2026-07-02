"""
Cerberus correlation / logical dependency scanner.

Detects logical pricing violations between related binary markets.

Classic example: P("Candidate X wins primary") < P("Candidate X wins general")
is logically impossible because winning the general requires winning the primary.
The spread between them is a pure arb opportunity.

Signal windows: minutes to hours — no sub-second infrastructure required.
Strategy: log and notify; do not auto-trade until signal quality is validated.
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from cerberus_runtime.models import CorrelationSignal, Market

logger = logging.getLogger(__name__)

# Minimum spread (as fraction) to emit a signal.
_MIN_SPREAD_PCT = Decimal("0.02")  # 2%

# Keyword pairs that imply logical dependency: (prerequisite_kw, dependent_kw).
# If a market matches dependent_kw, it logically requires the prerequisite.
_DEPENDENCY_PAIRS: List[Tuple[str, str]] = [
    ("primary", "general"),
    ("primary", "election"),
    ("qualify", "win"),
    ("qualify", "champion"),
    ("qualify", "final"),
    ("semifinal", "final"),
    ("nomination", "win"),
    ("nomination", "president"),
    ("reach playoffs", "win championship"),
    ("reach finals", "win finals"),
    ("survive", "win"),
]

# Category groups where correlation scanning is most reliable.
_TRUSTED_CATEGORIES = {"politics", "sports", "crypto", "economics"}


class CorrelationScanner:
    """Scans a list of active markets for logical dependency violations.

    Usage::

        scanner = CorrelationScanner()
        signals = scanner.scan(markets)
        for s in signals:
            print(s.spread_pct, s.suggested_action)
    """

    def scan(self, markets: List[Market]) -> List[CorrelationSignal]:
        """Scan markets for logical dependency pricing violations.

        Groups markets by shared entity name (candidate, team, etc.), then
        checks if dependent events are priced above their prerequisites.

        Args:
            markets: Active binary markets to scan.

        Returns:
            List of :class:`~cerberus_runtime.models.CorrelationSignal` ordered
            by descending spread.
        """
        signals: List[CorrelationSignal] = []
        ts_ms = int(time.time() * 1000)

        # Build a lookup: category → list of (market, best_ask_yes)
        by_category: Dict[str, List[Tuple[Market, Decimal]]] = {}
        for m in markets:
            if not m.active or m.closed:
                continue
            cat = (m.category or "").lower()
            best_ask = self._estimate_best_ask(m)
            if best_ask is None:
                continue
            by_category.setdefault(cat, []).append((m, best_ask))

        for cat_markets in by_category.values():
            for i, (m1, ask1) in enumerate(cat_markets):
                for m2, ask2 in cat_markets[i + 1:]:
                    sig = self._check_pair(m1, ask1, m2, ask2, ts_ms)
                    if sig is not None:
                        signals.append(sig)

        signals.sort(key=lambda s: s.spread_pct, reverse=True)
        logger.debug("CorrelationScanner: %d signals from %d markets", len(signals), len(markets))
        return signals

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _estimate_best_ask(market: Market) -> Optional[Decimal]:
        """Return a best-ask estimate for YES token from market metadata.

        The scanner operates on Market objects without live order book data,
        so it uses volume_24h as a proxy signal — markets with higher volume
        tend to have tighter spreads.  When the watcher injects live price
        data into Market objects in future sprints, replace this method.
        """
        # Placeholder: return None until live price feed is injected.
        # The scanner is wired into the main loop which passes snapshots;
        # real implementation in cerberustest.py passes live asks.
        return None

    def _check_pair(
        self,
        m1: Market,
        ask1: Decimal,
        m2: Market,
        ask2: Decimal,
        ts_ms: int,
    ) -> Optional[CorrelationSignal]:
        """Check if (m1, m2) represent a logical prerequisite/dependent pair.

        Returns a signal if the dependent market is priced above the
        prerequisite market by at least _MIN_SPREAD_PCT.
        """
        title1 = (m1.condition_id or "").lower()
        title2 = (m2.condition_id or "").lower()

        for prereq_kw, dep_kw in _DEPENDENCY_PAIRS:
            m1_is_prereq = prereq_kw in title1 and dep_kw not in title1
            m2_is_dep = dep_kw in title2 and prereq_kw not in title2
            m2_is_prereq = prereq_kw in title2 and dep_kw not in title2
            m1_is_dep = dep_kw in title1 and prereq_kw not in title1

            if m1_is_prereq and m2_is_dep:
                prereq_market, dep_market = m1, m2
                prereq_ask, dep_ask = ask1, ask2
            elif m2_is_prereq and m1_is_dep:
                prereq_market, dep_market = m2, m1
                prereq_ask, dep_ask = ask2, ask1
            else:
                continue

            # Violation: dependent priced ABOVE prerequisite.
            if dep_ask <= prereq_ask:
                continue

            spread = dep_ask - prereq_ask
            spread_pct = spread / prereq_ask if prereq_ask > Decimal("0") else Decimal("0")

            if spread_pct < _MIN_SPREAD_PCT:
                continue

            action = (
                f"BUY {prereq_market.condition_id} YES @ {prereq_ask:.2f}  |  "
                f"SELL {dep_market.condition_id} YES @ {dep_ask:.2f}  |  "
                f"spread={spread_pct:.1%}"
            )
            return CorrelationSignal(
                market_prereq=prereq_market,
                market_dep=dep_market,
                prereq_best_ask=prereq_ask,
                dep_best_ask=dep_ask,
                spread_pct=spread_pct,
                suggested_action=action,
                detected_at_ms=ts_ms,
            )

        return None
