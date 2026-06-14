# Architecture

This document describes the system architecture of the Execution Engine, following Clean Architecture and Domain-Driven Design principles.

## Overview

The Execution Engine is an event-driven system for automated trade execution with MetaTrader 5. It processes trading signals, applies risk management rules, and executes orders while maintaining real-time monitoring capabilities.

## System Components

### 1. Domain Layer (`src/domain/`)

Pure business logic with no external dependencies:

- **`signal.py`**: Signal types and validation
  - `InboundSignal`: External trading signal
  - `SignalDirection`: LONG/SHORT enums
  - Signal validation rules

- **`trade.py`**: Trade entities and business rules
  - `Trade`: Complete trade lifecycle
  - `TradePlan`: Execution planning
  - `OrderSide`: Order direction enums

- **`position.py`**: Position management
  - `Position`: Current positions
  - `AccountInfo`: Account state
  - `SymbolInfo`: Symbol specifications (ask, bid, tick_value, tick_size, digits, point)

### 2. Core Layer (`src/core/`)

Cross-cutting infrastructure:

- **`event_bus.py`**: Async event bus implementation
  - Event publishing/subscription
  - Async event handlers
  - Event filtering and routing

- **`event_types.py`**: Event definitions
  - `TradeEvent`: Trade lifecycle events
  - `SignalEvent`: Signal processing events
  - `RiskEvent`: Risk rule violations

- **`events.py`**: Event data structures
  - Typed event payloads
  - Event metadata

### 3. Execution Layer (`src/execution/`)

Trade execution pipeline:

- **`engine.py`**: Main execution engine
  - Signal processing orchestration
  - Risk validation integration
  - Order execution coordination
  - Receives `(loss_pct, start_equity)` from PositionManager and forwards to LossTracker

- **`planner.py`**: Trade planning
  - Streak-based risk amount computation via `LossTracker.daily_risk_amount()`
  - Lot size calculation from risk amount
  - Pessimistic entry and spread surcharge adjustments
  - Depends on `LossTracker` for live sizing — injected at construction

- **`order_manager.py`**: Order lifecycle management
  - Order placement and tracking
  - Fill confirmation and partial fill detection
  - Error handling and retry logic for transient broker errors
  - `[5]` Margin recovery: on `retcode=10019 NO_MONEY`, halves lot size once and retries immediately; drops trade cleanly if halved size is below `lot_min` or the retry still fails

### 4. Risk Management (`src/risk/`)

Risk control system:

- **`engine.py`**: Risk rule evaluation
  - Rule application pipeline
  - Violation handling
  - Risk state tracking

- **`rules.py`**: Risk rule definitions
  - `ALL_RULES`: Registry of active rules, ordered by cost (memory-only first, broker I/O last)
  - `RuleContext`: Rule evaluation context
  - Individual rule implementations
  - `max_open_trades` is derived as `config.max_losing_streak + 1` — not a separate config field

- **`loss_tracker.py`**: Daily loss circuit-breaker and risk budget provider
  - Latches `start_of_day_equity` once per calendar day from the first broker poll
  - Computes `daily_risk_amount(max_losing_streak)`: `start_equity × daily_loss_pct% / (streak + 1)`
  - Triggers trading pause at midnight when daily limit is reached
  - Thread-safe; called from both PositionManager poll thread and RiskEngine signal thread

### 5. Infrastructure Layer (`src/infra/`)

External system adapters:

- **`database.py`**: SQLite persistence
  - Trade storage and retrieval
  - Signal history
  - Metrics persistence

- **`ui_bridge.py`**: WebSocket bridge for the external dashboard
  - Streams state snapshots and incremental events
  - Accepts dashboard commands such as pause/resume and close trade
  - Derives `max_open_trades` from `config.risk.max_losing_streak + 1`

- **`websocket.py`**: WebSocket client
  - Signal ingestion
  - External system integration

- **`logger.py`**: Structured logging
  - Configurable log levels
  - Log aggregation
  - Error tracking

- **`metrics.py`**: Metrics collection
  - Performance counters
  - Trade statistics
  - System health metrics

### 6. Broker Adapters (`src/brokers/mt5/`)

MT5 integration:

- **`client.py`**: MT5 connection management
  - Terminal connection
  - Authentication
  - Session management

- **`orders.py`**: Order operations
  - Market order placement
  - Order status tracking
  - Cancellation handling

- **`positions.py`**: Position synchronization
  - Live position monitoring
  - Position reconciliation
  - `get_daily_pnl_info(magic)` → `(loss_pct, start_of_day_equity)`: returns both the daily loss percentage and the start-of-day equity in a single broker call; start equity is derived as `current_equity − total_pnl`

- **`types.py`**: MT5-specific types
  - MT5 API type mappings
  - Error code handling

### 7. Signal Processing (`src/signals/`)

Signal ingestion pipeline:

- **`consumer.py`**: Signal consumption
  - WebSocket signal intake
  - Signal parsing and validation
  - Signal queuing

- **`queue.py`**: Signal buffering
  - Async signal queue
  - Priority handling
  - Backpressure management

- **`validator.py`**: Signal validation
  - Business rule validation
  - Data integrity checks
  - Duplicate detection

- **`types.py`**: Signal type definitions
  - Signal event enums
  - Signal data structures

### 8. Strategy Routing (`src/strategies/`)

Signal-to-strategy mapping:

- **`adapter.py`**: Strategy adapters
  - BaseAdapter interface
  - PassthroughAdapter implementation
  - Custom strategy logic

- **`router.py`**: Strategy routing
  - Signal routing rules
  - Strategy selection
  - Fallback handling

### 9. Utility Functions (`src/utils/`)

Shared utilities:

- **`price.py`**: Price calculations
  - Pip size calculations
  - Lot size normalization
  - Price formatting

- **`symbol.py`**: Symbol handling
  - Symbol normalization
  - Symbol validation
  - Market data utilities

- **`time.py`**: Time utilities
  - Timestamp generation
  - Timezone handling
  - Duration calculations

- **`lot_calculator.py`**: Position sizing
  - Accepts a pre-computed `risk_amount` (currency) from the caller
  - Computes lot size: `risk_amount / (risk_pips × pip_value_per_lot)`
  - Normalises to broker-accepted volume step and enforces min/max lot bounds
  - Single responsibility: sizing math only — risk amount resolution is owned by `LossTracker`

## Data Flow

```
External Signal Source
        ↓
    WebSocket Server
        ↓
    Signal Consumer
        ↓
    Signal Validator
        ↓
    Signal Queue
        ↓
    Event Bus
        ↓
    Strategy Router
        ↓
    Risk Engine (ALL_RULES — memory-only first, broker I/O last)
        ↓
    Execution Engine
        ↓
    Trade Planner  ←── LossTracker.daily_risk_amount()
        ↓
    Order Manager
        ↓
    MT5 Client
        ↓
    MetaTrader 5 Terminal
```

## Daily Loss / Sizing Data Flow

```
MT5 Terminal
    ↓
Mt5Positions.get_daily_pnl_info()
    → (loss_pct, start_of_day_equity)
    ↓
PositionManager._poll()
    ↓
ExecutionEngine.update_daily_loss(loss_pct, start_equity)
    ↓
LossTracker.update_daily_loss_pct(pct, start_equity)
    ├── latches start_of_day_equity once per calendar day
    ├── triggers pause if pct >= MAX_DAILY_LOSS_PERCENT
    └── exposes daily_risk_amount(max_losing_streak)
              ↓
         TradePlanner.plan()  →  calculate_lot_size(risk_amount=...)
```

## Event Flow

1. **Signal Ingestion**: External signals received via WebSocket
2. **Validation**: Signals validated against business rules
3. **Enrichment**: Signals enriched with market data
4. **Risk Check**: Risk rules evaluated — memory-only rules first, live market rules last
5. **Planning**: Trade parameters calculated (lots from streak-based risk amount, slippage, spread surcharge)
6. **Execution**: Orders placed with MT5
7. **Confirmation**: Fill confirmations processed
8. **Persistence**: Trade data stored in database
9. **Monitoring**: Metrics updated and broadcast

## Database Schema

### trades
- id (PRIMARY KEY)
- symbol
- direction
- volume
- open_price
- close_price
- open_time
- close_time
- profit
- commission
- status

### signals
- id (PRIMARY KEY)
- timestamp
- symbol
- direction
- volume
- price
- source
- processed

### metrics_counters
- name (PRIMARY KEY)
- value
- updated_at

### metrics_gauges
- name (PRIMARY KEY)
- value
- updated_at

## Configuration

The system uses a hierarchical configuration:

1. **Environment Variables**: Runtime secrets and overrides (`.env`)
2. **Settings Dataclass**: Typed configuration with defaults (`RiskConfig`, `ExecutionConfig`, etc.)
3. **Validation**: Configuration validated on startup — `MAX_LOSING_STREAK < 1` raises `ValueError` before any broker connection

Key derived values (computed from config, never set directly):
- `max_open_trades = MAX_LOSING_STREAK + 1`
- `risk_per_trade  = daily_budget / (MAX_LOSING_STREAK + 1)`
- `daily_budget    = start_of_day_equity × (MAX_DAILY_LOSS_PERCENT / 100)`

## Error Handling

- **Circuit Breakers**: Automatic trading pause on daily loss limit
- **Retry Logic**: Configurable retry count and delay for broker order rejections
- **Margin Recovery**: On `retcode=10019`, lot size is halved and retried once — no config required; `orders.margin_reduced` metric incremented on recovery
- **Logging**: Structured error logging with context on every rule rejection
- **Recovery**: Graceful recovery from MT5 connection losses; daily loss is re-primed from broker on reconnect

## Performance Considerations

- **Rule Ordering**: Memory-only rules short-circuit before any broker I/O; live market rules only reached when all memory checks pass
- **Async Processing**: Non-blocking I/O operations
- **Connection Pooling**: Reused database connections
- **Event Buffering**: High-throughput event processing
- **Memory Management**: Bounded queues and cleanup

## Security

- **API Authentication**: WebSocket secret validation
- **Input Validation**: Strict signal and configuration validation; `MAX_LOSING_STREAK` validated to be >= 1 at startup
- **Error Masking**: Sensitive data not exposed in logs
- **Access Control**: MT5 credentials stored in environment, never in source

## Extensibility

The modular architecture allows for:

- **New Brokers**: Additional broker adapters implementing the same interface
- **Custom Strategies**: Strategy-specific signal processing via `StrategyRouter`
- **Additional Risk Rules**: Add a function to `rules.py` and append to `ALL_RULES`
- **Monitoring Integrations**: External monitoring systems via the metrics endpoint
- **Signal Sources**: Multiple signal ingestion methods
