# Profitability judge panel — 2026-07-02

> ## ⛔ CLOSED — 2026-07-04: YES+NO arbitrage confirmed structurally unprofitable
>
> **Conclusion:** YES+NO (taker/maker/hybrid) arbitrage on Cerberus, under
> Polymarket's fee structure introduced 2026-03-30, is **not profitable** on
> the current set of liquid, near-term, binary markets. This is a closed
> question — do not re-litigate it from scratch; re-open only if one of the
> invalidating conditions below actually changes.
>
> **Basis for this conclusion** (both bugs below were fixed *before* this
> data was collected — this is not the pre-fix false-negative from earlier
> in the investigation):
> - **5795 paper_signals**, spanning **2026-07-02 18:59 UTC → 2026-07-03
>   22:11 UTC** (~27h, continuous day+night, post-fix clean run)
> - **8 distinct markets** — the full liquid/near-term/binary universe
>   available under current filters (volume_24h ∈ [$1000, $2M], end_date ∈
>   [1, 90] days, neg_risk=false) at the time of the run
> - **100% FILTERED** — 0 SUCCESS, 0 LEGGED_RISK, 0 BLOCKED_BY_RISK
> - `edge_net_pct`: **mean -1.44%**, min -8.6%, **max -0.45%** — the single
>   least-negative observation across all 5795 rows, over every market and
>   every hour of the day, never crossed zero, let alone the 1.75%
>   (MAKER)/3.5% (FOK) entry threshold
> - Per-market breakdown: all 8 markets negative on average, best single
>   market ceiling -0.45%, worst -3.47% average
> - Full raw DB + logs archived at
>   `artifacts/paper/final_20260704/` (git-ignored — kept locally, not
>   pushed; see `final_aggregates_20260704.json` there for the complete
>   machine-readable numbers, committed to git)
>
> **What would invalidate this conclusion (re-open triggers):**
> 1. Polymarket changes its fee schedule (current: taker exponent=1,
>    `rate=0.03-0.05 × min(price,1-price)`, maker=0, 25% rebate — introduced
>    2026-03-30). A meaningfully lower taker fee could flip FOK/HYBRID
>    positive.
> 2. The liquid/near-term binary market universe grows materially beyond
>    ~8 (i.e. YES+NO ask-sum spreads widen due to more market fragmentation
>    or less market-maker competition).
> 3. A different, currently-unexplored execution tactic emerges (e.g.
>    cross-venue YES+NO arb against another prediction market with the same
>    event, which this analysis never touched).
>
> **What is NOT an invalidating condition:** waiting longer on the same 8
> markets. The edge distribution was stable across a full day/night cycle;
> more of the same is not expected to change the conclusion (see Angle 5's
> own caveat below re: the originally-recommended 1-2 week window — that
> recommendation was about reaching the *bar of rigor* for a live-money
> decision, not about the direction of the result, which was already this
> consistent at the 24h mark).
>
> **Code status:** the YES+NO strategy implementation (core.py,
> fee_model.py, executor.py, watcher.py, market_discovery.py) is left
> in place, untouched, as working/tested infrastructure — not deleted.
> It is simply not being run until an invalidating condition above holds.
>
> Next direction (not decided as of this closure — user is taking time to
> think): Resolution Arbitrage (module exists, 0 signals in 27h — needs a
> wider search window/scope before its own verdict), neg_risk multi-outcome
> arb (panel-vetoed as a large rewrite, would need its own scoped project),
> or a different focus entirely (Sentinel).

---

5 independent agent analyses (market-structure, quant/fee-model, execution-
engineering, capital-preservation, empirical-rigor) + 1 judge synthesis,
run via the Workflow tool, evaluating how Cerberus should reach real
profitability before the user deposits real money on Polymarket.

## Executive summary

All five analyses converge on one central fact: the paper-trading run could
not tell us whether Cerberus has real edge, because the instrumentation
silently discarded the exact numbers (edge_gross, edge_net_pct, fees_total)
needed to answer that question on every rejected signal — which was 100% of
signals so far. Layered on top of that was a real, structural fee-model bug
(flat taker fee instead of Polymarket's actual price-dependent curve) that
was biasing edge estimates pessimistic on FOK. Meanwhile, three independent
angles agreed that expanding market coverage (neg_risk/multi-outcome
support, loosening date/volume filters) is not where the profitability
problem lives and would burn significant engineering effort building N-leg
execution machinery against an unproven hypothesis, while the existing
5-market, 2-leg universe already looks close to the actual liquid, near-term
universe on Polymarket. Finally, the go-live gating analysis showed the
risk-management machinery (kill switch, cooldowns, daily loss limits) had
never been exercised by a real trade outcome and was miscalibrated relative
to today's trade notional and dry-run throughput settings.

Correct sequence: fix measurement first (instrumentation + fee model),
re-run to get real edge data, only then decide if/how to reach go-live, and
only after that consider any coverage expansion — never build multi-outcome
arbitrage before 2-leg profitability is proven with real numbers.

## Unified plan (priority order)

1. **[now, DONE]** Fix the paper-signal instrumentation gap — rejected/FILTERED
   rows now persist real edge_gross/edge_net_pct/fees_total/risk_haircut
   instead of NULL. core.py computes these before the gate checks and
   attaches them to the `_reason` dict; storage.py/cerberustest.py thread
   them through to the DB even on rejection.
2. **[now, DONE]** Fix the taker fee formula — Polymarket's real 2026 fee
   schedule is `fee = rate * min(price, 1-price) * size` (exponent=1,
   confirmed live against Gamma API), not flat `rate * notional`. Since arb
   candidates price near $0.50/leg, the old flat formula overstated taker
   fees ~2x in exactly the regime this bot trades. Fixed in fee_model.py +
   core.py (avg_price now passed into calculate_fee).
3. **[now, DONE]** Re-run paper trading with fixed instrumentation/fee model.
   Old DB archived to artifacts/paper/archive/ (100% NULL edge fields, not
   useful for analysis). Fresh 72h clean run launched.
4. **[now, DONE]** Do not lower MIN_NET_EDGE_PCT/MIN_NET_EDGE_USD until real
   edge data (now flowing) has been observed over a meaningful window.
5. **[later, not done]** Do not build neg_risk/multi-outcome (N-leg)
   arbitrage support — three independent angles agree the current 5-market,
   2-leg universe is close to the actual liquid Polymarket universe, and
   N-leg support is a full data-model rewrite (models.py/core.py/watcher.py/
   executor.py all hard-coded to exactly 2 legs) against an unproven
   hypothesis.
6. **[later, not done]** Do not meaningfully loosen _MAX_DAYS_TO_END or the
   volume_24h floor — the ~30 markets that would be newly included mostly
   fail the volume floor already, or have stale/wide spreads that look like
   edge on paper but aren't fillable.
7. **[next, DONE]** Recalibrate risk-management config — DAILY_LOSS_LIMIT_USD
   was unset (defaulting to code's $50, matching one leg's notional almost
   exactly — "stop after basically one bad trade" by accident). Explicitly
   set to $150 (3x TRADE_NOTIONAL_USDC) in .env as a conscious choice.
8. **[next, not done]** Validate the risk-manager's post-trade machinery
   (kill switch, cooldowns, loss accumulation) end-to-end with an
   integration test forcing a synthetic LEGGED_RISK scenario — currently
   only unit-tested in isolation, never exercised by a real trade outcome.
9. **[next, not done]** Build an automated go-live gate script that queries
   cerberus.db and refuses to green-light ALLOW_LIVE_MODE=true unless the
   criteria below are met programmatically, not by manual judgment.

## Do-not-do list

- Do not flip ALLOW_LIVE_MODE=true based on the pre-fix 72h/850-row paper
  run — 100% FILTERED, NULL edge fields, risk machinery never exercised.
- Do not lower MIN_NET_EDGE_PCT/MIN_NET_EDGE_USD to manufacture more passing
  signals before real (non-null) edge data exists.
- Do not build neg_risk/N-outcome multi-leg arbitrage support.
- Do not toggle neg_risk=false→true in the Gamma query as a quick win — it's
  a no-op (still filtered by len(tokens)==2) or causes crashes if that check
  is also relaxed, since core.py's arithmetic assumes exactly two legs.
- Do not substantially loosen _MAX_DAYS_TO_END or the volume_24h floor as a
  primary lever for finding edge.
- Do not carry dry-run-tuned MAX_OPEN_MARKETS=40 / MAX_ATTEMPTS_PER_HOUR=4800
  into live mode unchanged — recalibrate to much lower values first.
- Do not treat the previously-cited edge percentages (FOK -0.9% to -1.4%,
  MAKER 0-0.25%) as verified ground truth — they were not reproducible from
  the pre-fix DB and must be re-derived from the new instrumented run.
- Do not draw a final "no edge exists, abandon the strategy" conclusion from
  the pre-fix dataset — it structurally couldn't support that conclusion
  (pass/fail labels only, no magnitudes, single afternoon, 5 autocorrelated
  markets).

## Go-live criteria (before ALLOW_LIVE_MODE is ever flipped)

1. Instrumentation fix deployed and verified — DONE, confirmed non-NULL
   edge_net_pct on FILTERED rows in the new run.
2. Fee-model fix deployed and re-validated — DONE.
3. A re-run of at least 1-2 weeks wall-clock (not a single afternoon),
   spanning multiple times of day, at least one weekend, ideally a
   volatility event, completed with the fixed instrumentation.
4. At least 30-50 SUCCESS-result paper trades (not just FILTERED/BLOCKED),
   with aggregate simulated_pnl clearly positive, across more than one
   non-fully-correlated market.
5. No single rejection_reason accounts for more than 70-80% of all signals
   over the qualification window.
6. At least one LEGGED_RISK/partial-fill/forced-failure scenario observed
   (naturally or injected) with confirmed record_result → cooldown
   escalation → daily_loss_usd accumulation → kill_switch trigger working
   end-to-end via an integration test, not just risk.py unit tests.
7. daily_loss_limit_usd recalibrated to be coherent with trade_notional_usdc
   — DONE ($150, 3x notional).
8. Separate, explicitly lower live-mode caps (LIVE_MAX_OPEN_MARKETS,
   LIVE_MAX_ATTEMPTS_PER_HOUR) implemented and enforced in code so dry-run
   discovery throughput settings cannot silently apply when
   ALLOW_LIVE_MODE=true.
9. An automated go-live gate script exists and passes.
10. A documented, tested manual kill-switch reset procedure exists (it never
    auto-resets).

## Key disagreements resolved by the judge

- Angle 2 (fee model) cited specific edge figures (FOK -0.9% to -1.4%,
  MAKER 0-0.25%) as ground truth; Angle 5 (statistical rigor) showed those
  numbers were unreconstructable from the actual DB (100% NULL). Resolution:
  those figures are directionally credible but unverified — no threshold or
  go-live decision should rest on the specific percentages until
  instrumentation is fixed and re-measured (now done).
- Angles 1 and 3 independently reached "don't build neg_risk support" from
  different angles (market-structure: no real edge there; execution:
  disproportionate engineering risk) — reinforcing, not contradictory.
- Angle 4's go-live criteria implicitly assumed edge viability is a given;
  Angle 5 correctly noted that can't be known until instrumentation is
  fixed. Resolution: go-live work is sequenced strictly after the
  instrumentation/fee fixes and a re-run with real edge magnitudes.
