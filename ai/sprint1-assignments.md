---
# Sprint 1 branch assignments

## sprint1/market-discovery — Agent A
Owns: cerberus_runtime/market_discovery.py
Reads: cerberus_runtime/config.py, cerberus_runtime/models.py, cerberus_runtime/storage.py
Does NOT touch: fee_model.py, core.py, risk.py, executor.py, watcher.py, orderbook.py

## sprint1/watcher-orderbook — Agent B  
Owns: cerberus_runtime/watcher.py, cerberus_runtime/orderbook.py
Reads: cerberus_runtime/models.py, cerberus_runtime/config.py
Does NOT touch: market_discovery.py, fee_model.py, core.py, risk.py, executor.py

## sprint1/fee-model-core — Agent C
Owns: cerberus_runtime/fee_model.py, cerberus_runtime/core.py
Tag required: [CERBERUS-STRATEGY-UPDATE]
Reads: cerberus_runtime/models.py, cerberus_runtime/config.py
Does NOT touch: any other file

## sprint1/tests-infra — Agent D
Owns: tests/test_fee_model.py, tests/test_orderbook_vwap.py, tests/test_core_opportunity.py
Reads: models.py, stubs of fee_model.py and core.py
Does NOT touch: runtime files directly
---
