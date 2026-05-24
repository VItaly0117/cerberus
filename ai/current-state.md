# Cerberus — Current State

## Status: Sprint 1 COMPLETE ✅
**Date merged:** 2026-05-24

---

## Sprint 1 — All branches merged to main

### Merge order
1. `sprint1/tests-infra` → main  (no conflicts)
2. `sprint1/fee-model-core` → main  (no conflicts — pre-merge fix applied)
3. `sprint1/market-discovery` → main  (no conflicts)
4. `sprint1/watcher-orderbook` → main  (conflict resolved — see below)

### Pre-merge fix (fee-model-core)
- Created canonical `cerberus_runtime/models.py` consolidating Agent B and Agent C types.
- Removed duplicate dataclass definitions from `core.py`; replaced with imports from models.
- `setup.cfg` updated with `pythonpath = .` so `pytest` works without env-var prefix.

### Conflict resolution (watcher-orderbook)
- `cerberus_runtime/models.py`: kept HEAD (Decimal-canonical). Agent B's float-based
  `PriceLevel` superseded; extended fields (condition_id, ts_ms, book_hash, etc.)
  preserved as optional fields on `OrderBookSnapshot`.
- `cerberus_runtime/orderbook.py`: _parse_levels and _apply_changes updated to
  produce PriceLevel with Decimal fields; get_snapshot() received required
  timestamp field (converted from ts_ms / 1000.0).
- `requirements.txt`: merged both sets (websockets, aiohttp added from Agent B).
- `tests/test_orderbook.py`: hash helpers and assertions updated for Decimal.

---

## Files added in Sprint 1

### cerberus_runtime/
| File | Owner | Description |
|------|-------|-------------|
| models.py | Canonical (B+C) | All shared dataclasses: PriceLevel, FeeParams, OrderBookSnapshot, LegQuote, ArbitrageSignal, ArbitrageResult, Market |
| core.py | Agent C | Depth-walk engine and opportunity evaluator (Decimal-only) |
| fee_model.py | Agent C | Taker-fee calculator sourced from live FeeParams |
| market_discovery.py | Agent A | Async Gamma API market scanner with back-off |
| orderbook.py | Agent B | LocalOrderBook - L2 state machine for YES/NO legs |
| watcher.py | Agent B | WebSocket event dispatcher (stub, connects orderbook) |

### tests/ — 68 passed, 1 skipped
| File | Tests | Focus |
|------|-------|-------|
| test_fee_model.py | 12 | FeeModel calculate_fee + fallback |
| test_core_opportunity.py | 12 | Depth walk + evaluate_opportunity |
| test_market_discovery.py | 24 | Filter, backoff, scan behavior |
| test_orderbook.py | 12 | LocalOrderBook event handling + hashing |
| test_config.py | 2 | AppConfig defaults |
| test_storage.py | 4 | SQLite storage lifecycle |
| test_integration_paper.py | 2+1skip | Paper-trading dry-run |

---

## Invariants verified post-merge
- PASS: cerberus_runtime/ does not import from cerberus_runtime.core
- PASS: No hardcoded fee constants in fee_model.py
- PASS: No float() calls in core.py or fee_model.py
- PASS: 68 tests pass; 1 skipped (live WebSocket integration placeholder)

---

## Known gaps / Sprint 2 targets
- watcher.py: WebSocket connection and event-dispatch loop not yet implemented
- risk.py: position-size and exposure limits not yet built
- executor.py: order placement against CLOB API not yet built
- ArbitrageResult is currently an alias for ArbitrageSignal; Sprint 2 will extend
  with execution metadata (fill prices, actual fees, slippage realised)
- No end-to-end integration test with live or mock WebSocket feed
- CI workflow (.github/workflows/cerberus-ci.yml) present but not yet triggered
