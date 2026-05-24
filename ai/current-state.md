# Cerberus — Current State

## Status: Sprint 2 COMPLETE ✅
**Date merged:** 2026-05-24

---

## Sprint 2 — All branches merged to main

### Merge order
1. `sprint2/risk-manager` → main  (no conflicts)
2. `sprint2/executor` → main  (no conflicts)
3. `sprint2/watcher-ws-loop` → main  (no conflicts — watcher.py fully replaced stub)

### Files added in Sprint 2
| File | Owner | Description |
|------|-------|-------------|
| cerberus_runtime/risk.py | Agent A | RiskManager: kill switch, cooldowns, daily loss limit, hourly cap, live-mode gate |
| cerberus_runtime/executor.py | Agent B | Sequential FOK/FAK executor, emergency repair, dry-run simulation |
| cerberus_runtime/watcher.py | Agent C | Full WebSocket loop: subscribe, reconnect back-off, resync, snapshot emission |

---

## Safety checks (post Sprint 2 merge)
- PASS: No asyncio.gather() call in executor.py (prohibition note in docstring only)
- PASS: No float() calls in risk.py or executor.py
- PASS: allow_live_mode guard present in executor.py (line 164 comment + risk.py gate)
- PASS: Executor import OK (`Executor.__new__(Executor)`)
- PASS: 98 tests pass, 1 skipped

---

## Full test count: 98 passed, 1 skipped

| Test file | Tests | Sprint |
|-----------|-------|--------|
| test_fee_model.py | 12 | 1 |
| test_core_opportunity.py | 12 | 1 |
| test_market_discovery.py | 24 | 1 |
| test_orderbook.py | 12 | 1 |
| test_config.py | 2 | 1 |
| test_storage.py | 4 | 1 |
| test_integration_paper.py | 2+1skip | 1 |
| test_risk_manager.py | 10 | 2 |
| test_executor.py | 10 | 2 |
| test_watcher_loop.py | 10 | 2 |

---

## Invariants verified
- No asyncio.gather() in executor.py (sequential legs guaranteed)
- No float() in risk.py or executor.py (Decimal-only arithmetic)
- allow_live_mode guard in executor comment + enforced by RiskManager
- No live CLOB API calls in dry_run_mode (RuntimeError guard in _send_order)

---

## Sprint 3 targets — Paper 72h run

### Goal
Run `cerberustest.py --paper` for 72 hours in a sandboxed environment
against Polymarket's live WebSocket feed (no real orders placed).

### What Sprint 3 needs
1. **cerberustest.py** — CLI entry point that wires all Sprint 1+2 components:
   - Initialise: Config, Storage, MarketDiscovery, Watcher, RiskManager, Executor
   - candidate_queue → MarketDiscovery → Watcher → opportunity_queue
   - opportunity_queue → core.evaluate_opportunity → RiskManager.allows → Executor.execute_pair (dry_run=True)
   - --paper flag: forces dry_run_mode=True, allow_live_mode=False
   - Logs every SIGNAL, BLOCKED, MISS, SUCCESS, LEGGED event with timestamps

2. **Paper-run reporting** — after 72 h, emit JSON summary:
   - total_signals_seen, total_attempts, success_rate
   - edge_gross_usdc (sum), fees_usdc (sum), net_edge_usdc (sum)
   - legged_events (count + avg loss)
   - top_markets by signal frequency

3. **Sprint 3 branches** (TBD):
   - sprint3/cerberustest-entry — cerberustest.py + CLI wiring
   - sprint3/paper-reporter — JSON/Markdown summary generator
   - sprint3/72h-paper-run — integration test + paper run results

### Known gaps before paper run
- cerberustest.py does not yet exist
- Storage schema needs INSERT for paper-run signals table
- Config dataclass needs clob_rest_url, ws_url, dry_run_mode fields unified
- RiskManager.AppConfig and core.AppConfig are separate (acceptable for Sprint 3)
