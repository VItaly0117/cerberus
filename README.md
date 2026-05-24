# Cerberus — Polymarket Arbitrage Paper Trader

Cerberus is a dry-run / paper-trading engine that monitors [Polymarket](https://polymarket.com)
binary-outcome markets for YES/NO arbitrage opportunities and simulates execution.
**No real orders are ever placed.** The `--paper` flag is permanently enforced in
`dry_run_mode=True`.

---

## Quick start

```bash
# Install dependencies
pip install -r requirements.txt

# Check all systems are go
python3 cerberustest.py --preflight

# Paper-trade for up to 5 signals (stop early for dev testing)
python3 cerberustest.py --paper --max-signals 5

# Full 72-hour paper run
python3 cerberustest.py --paper --duration-hours 72
```

---

## Architecture

```
MarketDiscovery → candidate_queue → Watcher → opportunity_queue → Core → Risk → Executor
```

| Component | Module | Description |
|-----------|--------|-------------|
| MarketDiscovery | `cerberus_runtime/market_discovery.py` | Polls Gamma API, emits new markets |
| Watcher | `cerberus_runtime/watcher.py` | WebSocket order-book subscriber, snapshot emitter |
| Core | `cerberus_runtime/core.py` | Depth-walk evaluator, edge calculation |
| RiskManager | `cerberus_runtime/risk.py` | Kill switch, daily loss limit, cooldowns, hourly cap |
| Executor | `cerberus_runtime/executor.py` | Sequential FOK/FAK simulator (dry-run only) |
| Storage | `cerberus_runtime/storage.py` | Async SQLite via `aiosqlite` |

---

## Safety invariants

| Invariant | Where enforced |
|-----------|----------------|
| `dry_run_mode = True` always in `--paper` | `cerberustest.py:404` |
| `allow_live_mode = False` in `--paper`; abort if env overrides | `cerberustest.py:386-391` |
| Order legs execute **sequentially** (no `asyncio.gather`) | `executor.py` design rule |
| All arithmetic uses `Decimal` (no `float` in core math) | `core.py`, `fee_model.py`, `risk.py` |
| `PRIVATE_KEY` only in `config.py` / `preflight.py` | grep check in CI |
| Kill switch latches permanently (never resets without restart) | `risk.py:RiskManager` |

---

## Configuration (environment variables)

| Variable | Default | Description |
|----------|---------|-------------|
| `GAMMA_HOST` | `gamma-api.polymarket.com` | Gamma API hostname |
| `CLOB_REST_URL` | `https://clob.polymarket.com` | CLOB REST endpoint |
| `WS_URL` | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | WebSocket endpoint |
| `TRADE_NOTIONAL_USDC` | `25` | Per-leg notional in USDC |
| `MIN_NET_EDGE_PCT` | `0.0125` | Minimum net edge as fraction of notional |
| `DB_PATH` | `cerberus.db` | SQLite database file path |
| `ALLOW_LIVE_MODE` | `false` | **Must be `false` for paper runs; abort if `true`** |

---

## Running tests

```bash
pytest tests/ -v --tb=short
# Expected: 106 passed, 1 skipped
```

| Test file | Count | Sprint |
|-----------|-------|--------|
| test_fee_model.py | 12 | 1 |
| test_core_opportunity.py | 12 | 1 |
| test_market_discovery.py | 24 | 1 |
| test_orderbook.py | 12 | 1 |
| test_config.py | 2 | 1 |
| test_storage.py | 5 | 1+3 |
| test_integration_paper.py | 2+1skip | 1 |
| test_risk_manager.py | 10 | 2 |
| test_executor.py | 10 | 2 |
| test_watcher_loop.py | 10 | 2 |
| test_paper_run.py | 7 | 3 |

---

## Paper run report

After each run, a JSON report is saved to `artifacts/paper/report_{timestamp}.json`:

```json
{
  "run_duration_hours": 1.0,
  "config": { "trade_notional_usdc": "25", ... },
  "results": {
    "total_snapshots_evaluated": 500,
    "viable_signals": 12,
    "successes": 8,
    "legged_incidents": 1,
    "total_simulated_pnl": "3.7500",
    "median_edge_net_pct": "0.0215"
  },
  "gate_status": {
    "stale_book_rate_pct": 2.4,
    "legged_incident_rate_pct": 8.3,
    "median_edge_positive": true
  },
  "stop_conditions_triggered": []
}
```

Exit codes: `0` = median edge positive, `1` = safety abort, `2` = median edge ≤ 0.

---

## Sprint history

| Sprint | Branches | Status |
|--------|----------|--------|
| Sprint 1 | market-discovery, watcher-orderbook, fee-model-core, tests-infra | ✅ merged |
| Sprint 2 | risk-manager, executor, watcher-ws-loop | ✅ merged |
| Sprint 3 | paper-run | ✅ ready to merge |

---

## GitHub

Repository: <https://github.com/VItaly0117/cerberus>
