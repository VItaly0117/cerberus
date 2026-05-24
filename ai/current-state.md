# Cerberus — Current State

## Status: Sprint 3 IN PROGRESS (branch: sprint3/paper-run)
**Date updated:** 2026-05-24

---

## Sprint 3 — Paper Run Pipeline

### Branch: `sprint3/paper-run` (ready to merge)

### Task 1 — Unified AppConfig ✅
- `cerberus_runtime/config.py`: canonical `AppConfig` dataclass merging all trading + risk fields
- `cerberus_runtime/core.py`: removed local `AppConfig`, re-exports from config
- `cerberus_runtime/risk.py`: removed local `AppConfig`, imports from config
- `cerberus_runtime/executor.py`: imports `AppConfig` from config

### Task 2 — paper_signals table ✅
- `cerberus_runtime/storage.py`:
  - Added `paper_signals` DDL table
  - `insert_paper_signal(signal, result, rejection_reason, simulated_pnl)`
  - `insert_paper_signal_from_snapshot(market_id, condition_id, ts_ms, signal, result, ...)`
  - `get_paper_summary(since_ts_ms)` → dict with 9 fields
  - `insert_risk_event()` stub satisfies CerberusStorage protocol

### Task 3 — cerberustest.py + test_paper_run.py ✅
- `cerberustest.py`:
  - `--preflight`: checks imports, AppConfig, deps, env vars; exit 0/1
  - `--paper [--duration-hours N] [--max-signals N]`: full async paper loop
  - Safety: aborts rc=1 if ALLOW_LIVE_MODE=true; forces dry_run_mode=True
  - Saves `artifacts/paper/report_{timestamp}.json` on exit
  - Exit code: 0 = median_edge_net_pct > 0, 2 = ≤ 0
- `tests/test_paper_run.py`: 7 tests all passing

### Task 4 — Final checks ✅
- Full test suite: **106 passed, 1 skipped**
- `python3 cerberustest.py --preflight` → exit 0
- Safety checks:
  - PASS: No asyncio.gather() for order legs in executor.py
  - PASS: No float() in core/fee_model/risk
  - PASS: No PRIVATE_KEY outside config.py/preflight.py
  - PASS: allow_live_mode gate in cerberustest.py (lines 386-391, 404-405)
  - PASS: dry_run_mode forced True (lines 404, 415)
- `artifacts/paper/.gitkeep` created

---

## Full test count: 106 passed, 1 skipped

| Test file | Tests | Sprint |
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

## Files added/modified in Sprint 3

| File | Change | Description |
|------|--------|-------------|
| cerberus_runtime/config.py | REWRITTEN | Unified AppConfig, get_app_config() factory |
| cerberus_runtime/core.py | MODIFIED | Removed local AppConfig, re-exports from config |
| cerberus_runtime/risk.py | MODIFIED | Removed local AppConfig, imports from config |
| cerberus_runtime/executor.py | MODIFIED | Imports AppConfig from config |
| cerberus_runtime/storage.py | EXTENDED | paper_signals table + 3 new async methods |
| cerberustest.py | CREATED | CLI entry: --preflight and --paper modes |
| tests/test_paper_run.py | CREATED | 7 paper-run pipeline tests |
| tests/test_storage.py | EXTENDED | test_paper_signal_insert_and_summary |
| artifacts/paper/.gitkeep | CREATED | Output dir placeholder |

---

## Invariants verified
- No asyncio.gather() in executor.py for order legs (sequential guaranteed)
- No float() in core/fee_model/risk (Decimal-only arithmetic)
- allow_live_mode=False enforced in paper mode (cerberustest.py:387-391)
- dry_run_mode=True forced in paper mode (cerberustest.py:404, 415)
- No live CLOB API calls in dry_run_mode (RuntimeError guard in _send_order)
- PRIVATE_KEY/private_key only in config.py and preflight.py

---

## Architecture: Paper Run Signal Flow

```
MarketDiscovery.run()
    │
    └─► candidate_queue
              │
              ▼
          Watcher.run()
              │
              └─► opportunity_queue
                        │
                        ▼
                  _core_loop()
                    ├─ risk_manager.allows(snapshot)
                    │     BLOCKED_BY_RISK → insert_paper_signal_from_snapshot()
                    ├─ core.evaluate_opportunity(snapshot, config, fee_model)
                    │     FILTERED → insert_paper_signal_from_snapshot()
                    └─ executor.execute_pair(signal, snapshot)  [dry_run=True]
                          SUCCESS/CLEAN_MISS/LEGGED_RISK → insert_paper_signal_from_snapshot()
                                                          → risk_manager.record_result()
```

---

## Next: Merge to main
```bash
git checkout main
git merge sprint3/paper-run --no-ff -m "merge: sprint3/paper-run — full paper trading pipeline"
pytest tests/ -v --tb=short   # must pass 106+, 1 skip
```
