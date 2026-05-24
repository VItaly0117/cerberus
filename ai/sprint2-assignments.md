---
# Sprint 2 branch assignments

## sprint2/risk-manager — Agent A
Owns: cerberus_runtime/risk.py
Reads: cerberus_runtime/models.py, cerberus_runtime/core.py, cerberus_runtime/config.py
Does NOT touch: fee_model.py, market_discovery.py, executor.py, watcher.py, orderbook.py
Tag required: [CERBERUS-STRATEGY-UPDATE]

Position-size calculator and exposure-limit enforcer.
Input: ArbitrageSignal from core
Output: PositionRisk (max order size, margin requirement, leverage cap)

## sprint2/executor — Agent B
Owns: cerberus_runtime/executor.py
Reads: cerberus_runtime/models.py, cerberus_runtime/risk.py, cerberus_runtime/config.py
Does NOT touch: fee_model.py, core.py, market_discovery.py, watcher.py, orderbook.py
Tag required: [CERBERUS-STRATEGY-UPDATE]

Order placement and fill-tracking against Polymarket CLOB HTTP REST API.
Input: ArbitrageSignal + PositionRisk approval
Output: ExecutionResult (YES fill, NO fill, realized_edge)

## sprint2/watcher-ws-loop — Agent C
Owns: cerberus_runtime/watcher.py (full implementation)
Reads: cerberus_runtime/models.py, cerberus_runtime/orderbook.py, cerberus_runtime/config.py
Does NOT touch: fee_model.py, core.py, risk.py, executor.py, market_discovery.py

WebSocket event loop and dispatcher.
Maintains LocalOrderBook state, emits fresh OrderBookSnapshot to the evaluator queue.
Handles CLOB WebSocket subscribe/reconnect/cleanup lifecycle.

---

## Merge order (must be strict, in-order):
1. sprint2/risk-manager ← (post-Sprint 1 state, no deps)
2. sprint2/executor ← (depends on risk.py in codebase)
3. sprint2/watcher-ws-loop ← (depends on risk.py + executor.py for full integration test)

All tests must pass after each merge before proceeding to next.
