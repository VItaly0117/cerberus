# Current State of Cerberus Project

## Sprint 1 Status
**Started:** 2026-05-24

Cerberus is in early development. Sprint 1 focuses on building the core infrastructure for market discovery, order book monitoring, fee modeling, and test infrastructure.

### Active Branches

1. **sprint1/market-discovery** (Agent A)
   - Building: `cerberus_runtime/market_discovery.py`
   - Status: In development

2. **sprint1/watcher-orderbook** (Agent B)
   - Building: `cerberus_runtime/watcher.py`, `cerberus_runtime/orderbook.py`
   - Status: In development

3. **sprint1/fee-model-core** (Agent C)
   - Building: `cerberus_runtime/fee_model.py`, `cerberus_runtime/core.py`
   - Tag required: `[CERBERUS-STRATEGY-UPDATE]`
   - Status: In development

4. **sprint1/tests-infra** (Agent D)
   - Building: Test fixtures, CI pipeline, integration tests
   - Status: In development
   - Deliverables:
     - `tests/conftest.py` - Shared test fixtures
     - `.github/workflows/cerberus-ci.yml` - CI pipeline
     - `tests/test_integration_paper.py` - Paper trading integration tests

### Core Modules (To Be Implemented)

- `cerberus_runtime/models.py` - Data models and types
- `cerberus_runtime/config.py` - Configuration management
- `cerberus_runtime/storage.py` - Database storage layer

### Agent Assignments

See `ai/sprint1-assignments.md` for detailed branch responsibilities and constraints.

### Key Invariants

- **Dry Run Mode**: All paper trading must use `dry_run=True` in AppConfig
- **Type Safety**: Use `Decimal` for all price/fee calculations, no floats in signal fields
- **Database**: SQLite storage with temporary in-memory databases for testing
- **CI/CD**: Automated pytest on all sprint1/* branch pushes

### Next Steps

1. Agent A: Complete market discovery implementation
2. Agent B: Complete order book watcher implementation
3. Agent C: Implement fee model and arbitrage detection (with `[CERBERUS-STRATEGY-UPDATE]` tag)
4. Agent D: Add tests for all implemented components
