# Risk Rules Reference

This document provides a comprehensive reference for the risk management rules implemented in the Execution Engine, along with configuration guidance and tuning recommendations.

## Overview

The risk management system uses a rule-based approach where each trading signal is validated against a series of risk checks before execution. Rules are evaluated in order, and any rule failure prevents the trade from being executed.

## Rule Categories

### Guard Rules
Rules that run first and can short-circuit all other checks:

- **Loss Guard Rule**: Circuit breaker based on daily loss percentage
- **No Hedging Rule**: Prevents opposing positions on the same symbol

### Memory-Only Rules
Fast validation against in-memory state — no broker I/O:

- **Maximum Open Trades**: Limits concurrent positions (derived from `MAX_LOSING_STREAK`)
- **Maximum Symbol Exposure**: Limits positions per symbol
- **Duplicate Signal**: Prevents duplicate signal processing
- **Daily Loss Limit**: Monetary loss threshold with safety buffers

### Live Market Rules
Run last — require a live broker tick:

- **Minimum Risk-Reward Ratio**: Validates actual R:R from current fill price
- **Spread Quality**: Validates spread vs stop loss ratio from current fill price

## Rule Details

### Loss Guard Rule

**Purpose**: Emergency circuit breaker — pauses all trading when the daily loss limit is hit.

**Configuration**:
```
MAX_DAILY_LOSS_PERCENT=5.0
```

**Behavior**:
- Runs first in the pipeline; short-circuits all other checks when paused
- Monitors realised + unrealised losses against start-of-day equity
- Automatically pauses trading when threshold is reached
- Resumes at midnight (engine timezone)

**Tuning**:
- Conservative: 1–2% for high-frequency strategies
- Standard: 3–5% for swing/intraday trading
- Consider account size and strategy drawdown characteristics

---

### No Hedging Rule

**Purpose**: Prevents conflicting positions on the same symbol.

**Configuration**:
```
NO_HEDGING=true
```

**Behavior**:
- Blocks BUY signals when a SELL position is open (and vice versa)
- Checks PLANNED, OPEN, and PARTIALLY_CLOSED trades
- Allows multiple positions in the same direction

**Tuning**:
- `true` for directional strategies (recommended)
- `false` for arbitrage or intentional hedging strategies

---

### Maximum Open Trades Rule

**Purpose**: Limits portfolio exposure through position count.

**Configuration**:
```
MAX_LOSING_STREAK=4
```

**Behavior**:
- `max_open_trades` is **derived**, not configured directly:
  ```
  max_open_trades = MAX_LOSING_STREAK + 1
  ```
- This is a mathematical guarantee: if every open trade hits SL simultaneously,
  total loss equals exactly the daily budget — never more.

**Budget coherence proof** (with `MAX_LOSING_STREAK=4`, `MAX_DAILY_LOSS_PERCENT=5%`, $10,000 account):
```
daily_budget    = $10,000 × 5%  = $500
risk_per_trade  = $500 / 5      = $100
max_open_trades = 5
max_exposure    = 5 × $100      = $500  ✓
```

**Tuning**: Set `MAX_LOSING_STREAK` to your system's worst recorded consecutive losing streak.
- Losing streak of 3 → max 4 concurrent trades
- Losing streak of 6 → max 7 concurrent trades
- Minimum value: 1 (enforced at startup)

---

### Maximum Symbol Exposure Rule

**Purpose**: Prevents over-concentration in a single symbol.

**Configuration**:
```
MAX_EXPOSURE_PER_SYMBOL=2
```

**Behavior**:
- Counts all open and planned positions per symbol
- Independent of the hedging rule
- Allows multiple same-direction entries up to the limit

**Tuning**:
- Major pairs: 2 positions
- Exotic pairs / crypto: 1 position (higher volatility)

---

### Duplicate Signal Rule

**Purpose**: Prevents the same signal from being executed more than once.

**Configuration**: None — automatic.

**Behavior**:
- Checks signal ID against all open trades
- Signals with ID `"unknown"` are excluded from duplicate checking
- Idempotent: safe to receive the same signal twice

---

### Daily Loss Limit Rule

**Purpose**: Monetary loss control with two safety layers.

**Configuration**:
```
MAX_DAILY_LOSS_PERCENT=5.0
MAX_LOSING_STREAK=4
```

**Behavior — two layers**:

**Layer 1 — Hard safety stop at 95% of limit**:
New trades are refused once realised loss reaches 95% of the configured limit. The 5% buffer ensures open positions cannot push the account past 100% of the limit even if they all hit SL simultaneously.

**Layer 2 — Pre-trade budget projection**:
Before opening a trade, the engine checks whether adding this trade's per-trade risk to today's loss would exceed the 95% threshold.

```
per_trade_risk_pct = MAX_DAILY_LOSS_PERCENT / (MAX_LOSING_STREAK + 1)
```

Example (`MAX_DAILY_LOSS_PERCENT=5`, `MAX_LOSING_STREAK=4`):
```
per_trade_risk_pct = 5 / 5      = 1%
safety_threshold   = 5 × 0.95  = 4.75%

daily_loss_pct=3.8% → 3.8 + 1.0 = 4.8 > 4.75 → REJECTED
daily_loss_pct=3.7% → 3.7 + 1.0 = 4.7 < 4.75 → ALLOWED
```

---

### Minimum Risk-Reward Ratio Rule

**Purpose**: Ensures trades have adequate profit potential at the **actual fill price**.

**Configuration**:
```
MIN_RR_RATIO=1.0
```

**Behavior**:
- Computes R:R from the live `si.ask` (long) or `si.bid` (short) — not the stale `signal.entry_price`
- A signal generated at one price may arrive at a materially different ask/bid by execution time
- If actual R:R from fill price is below the minimum, the trade is rejected
- The rejection reason surfaces both the actual R:R and the signal's original R:R for comparison

**Tuning**:
- Conservative: 2.0+ (2:1 reward-to-risk)
- Balanced: 1.5–2.0
- Aggressive: 1.0–1.5

---

### Spread Quality Rule

**Purpose**: Validates market conditions by comparing current spread to actual risk distance.

**Configuration**:
```
SL_RATIO_THRESHOLD=0.34
```

**Behavior**:
- Uses live fill price (`si.ask` for long, `si.bid` for short) as the anchor
- Computes `spread_pips / sl_pips` from fill price — not stale signal entry price
- Rejects if ratio exceeds `SL_RATIO_THRESHOLD`
- Includes production guards: zero/negative prices, zero pip size, negative spread, zero SL distance

**Tuning**:
- Conservative: 0.25 (spread ≤ 25% of SL — tight, suitable for wide-SL swing setups)
- Balanced: 0.34 (spread ≤ 34% of SL)
- Aggressive: 0.50 (spread ≤ 50% of SL)

At `SL_RATIO_THRESHOLD=0.25`:

| SL (pips) | Max allowed spread | Verdict |
|---|---|---|
| 20 | 5.0 pips | Fine for any major |
| 10 | 2.5 pips | Fine for majors, tight for minors |
| 5  | 1.25 pips | Rejects most non-majors |

---

## Execution-Level Protections

The following protections run **after** risk rules pass, inside `OrderManager` during order placement. They are not risk rules (they don't reject signals) — they are recovery mechanisms that activate when the broker itself rejects an order.

### Margin Recovery `[5]`

**Trigger**: `retcode=10019 NO_MONEY` from MT5.

**Why it happens**: The lot-sizing formula computes position size from risk amount and SL distance — it does not account for the broker's margin requirement. On a small account, the margin needed to hold the position (frozen collateral) can exceed free margin even when the intended dollar risk is modest.

**Behavior**:
1. Lot size is halved and normalised to the broker's `lot_step`
2. Order is retried immediately (no delay — this is a capital constraint, not a timing issue)
3. If the halved size is below `lot_min`, or the retry fails for any reason, the trade is dropped with a `WARNING` log

**One recovery attempt only** — no spiral. The `_margin_halved` flag ensures this.

**Logged fields** (on halve): `symbol`, `original_lots`, `halved_lots`, `retcode`  
**Logged fields** (on drop): `symbol`, `original_lots`, `halved_lots` or `volume`, `min_lot` or `retcode`  
**Metric**: `orders.margin_reduced` incremented on each successful halve-and-retry

**Note**: This is distinct from `_RETRYABLE_RETCODES` — those retry with identical parameters (fresh price only). Margin recovery changes the volume, so it is handled in a separate block.

---

## Configuration Reference

All risk parameters live in `.env`:

```dotenv
# Streak-based position sizing
MAX_LOSING_STREAK=4          # Worst recorded consecutive losing streak (min: 1)
                             # Derives: max_open_trades = MAX_LOSING_STREAK + 1
                             # Derives: risk_per_trade  = daily_budget / (MAX_LOSING_STREAK + 1)

MAX_DAILY_LOSS_PERCENT=5.0   # Daily loss budget as % of start-of-day equity

# Per-symbol and R:R limits
MAX_EXPOSURE_PER_SYMBOL=2
MIN_RR_RATIO=1.0

# Spread filter
SL_RATIO_THRESHOLD=0.34

# Hedging
NO_HEDGING=true
```

### Example: Conservative

```dotenv
MAX_LOSING_STREAK=3          # max 4 concurrent trades
MAX_DAILY_LOSS_PERCENT=2.0   # 2% daily limit → $50/trade on $10k account
MAX_EXPOSURE_PER_SYMBOL=1
MIN_RR_RATIO=2.0
SL_RATIO_THRESHOLD=0.25
NO_HEDGING=true
```

### Example: Standard

```dotenv
MAX_LOSING_STREAK=4          # max 5 concurrent trades
MAX_DAILY_LOSS_PERCENT=5.0   # 5% daily limit → $100/trade on $10k account
MAX_EXPOSURE_PER_SYMBOL=2
MIN_RR_RATIO=1.0
SL_RATIO_THRESHOLD=0.34
NO_HEDGING=true
```

### Example: Aggressive

```dotenv
MAX_LOSING_STREAK=6          # max 7 concurrent trades
MAX_DAILY_LOSS_PERCENT=10.0  # 10% daily limit → $142/trade on $10k account
MAX_EXPOSURE_PER_SYMBOL=3
MIN_RR_RATIO=1.0
SL_RATIO_THRESHOLD=0.50
NO_HEDGING=false
```

---

## How Sizing Works

Position size is computed once per signal in `TradePlanner`, using the risk amount from `LossTracker`:

```
daily_budget   = start_of_day_equity × (MAX_DAILY_LOSS_PERCENT / 100)
risk_per_trade = daily_budget / (MAX_LOSING_STREAK + 1)
```

`start_of_day_equity` is latched from the broker on the first poll cycle of each calendar day and held fixed for the session. Lot size is then computed from `risk_per_trade`, the actual SL distance from fill price, and the instrument's pip value.

---

## Monitoring and Alerts

### Key Metrics to Watch
- Rule rejection rates by type
- Daily loss percentage vs safety threshold (95% of limit)
- Open trades vs derived max (`MAX_LOSING_STREAK + 1`)
- Symbol exposure distribution
- Spread quality failure rate
- `orders.margin_reduced` counter — repeated hits indicate the account is consistently undercapitalised for the lot sizes the risk % produces; consider depositing more capital or lowering `MAX_DAILY_LOSS_PERCENT`

### Alert Thresholds
- Daily loss > 50% of limit
- Rule rejection rate > 20%
- Spread quality failures > 5%
- Loss guard activation

---

## Testing Risk Rules

```bash
# Unit tests
pytest tests/unit/risk/test_rules.py -v

# Integration tests
pytest tests/integration/ -k risk -v
```

Use the monitoring dashboard to observe rule behavior in real-time.

---

## Troubleshooting

**All trades rejected**: Check loss guard status and daily loss percentage in the external dashboard risk guard panel.

**Signal duplicates**: Verify signal source provides unique IDs.

**Spread quality failures**: Review market conditions, SL distances, and session timing. Widen `SL_RATIO_THRESHOLD` or check if signals arrive during low-liquidity windows.

**Symbol exposure limits**: Check open position count per symbol in monitoring dashboard.

**Debug mode**:
```bash
LOG_LEVEL=DEBUG python -m src
```

**Rule bypass (development only)**:
```python
# src/risk/rules.py
ALL_RULES: List[RiskRule] = [
    # loss_guard_rule,  # Commented out for testing
]
```

---

## Performance

- Rules evaluate synchronously before execution
- Memory-only rules run first and short-circuit broker I/O when they fail
- Live market rules (min_rr_rule, spread_quality_rule) only reached if all memory checks pass
- Rule evaluation is typically < 10ms per signal

---

## Extending Risk Rules

1. Define a rule function in `src/risk/rules.py`:
```python
def custom_rule(ctx: RuleContext) -> RuleResult:
    # RuleContext provides:
    #   ctx.signal          — inbound signal
    #   ctx.open_trades     — current open positions
    #   ctx.config          — RiskConfig
    #   ctx.daily_loss_pct  — current daily loss %
    #   ctx.effective_open  — count of open trades
    #   ctx.effective_symbol — positions for this symbol
    #   ctx.symbol_info     — live market data (ask, bid, spread)
    #   ctx.loss_tracker    — LossTracker instance
    return RuleResult(approved=True)
```

2. Add to `ALL_RULES` in the correct position:
```python
ALL_RULES: List[RiskRule] = [
    loss_guard_rule,          # memory-only: paused state check
    no_hedging_rule,          # memory-only: open trades scan
    max_open_trades_rule,     # memory-only: counter check
    max_symbol_exposure_rule, # memory-only: counter check
    duplicate_signal_rule,    # memory-only: open trades scan
    daily_loss_limit_rule,    # memory-only: loss budget check
    custom_rule,              # place memory-only rules before this line
    min_rr_rule,              # broker I/O: live fill price
    spread_quality_rule,      # broker I/O: live spread
]
```

3. Add configuration to `RiskConfig` if needed.
4. Write unit tests in `tests/unit/risk/test_rules.py`.

---

## Best Practices

1. **Start Conservative**: Use tight limits when deploying new strategies
2. **Set `MAX_LOSING_STREAK` from data**: Run your backtest, find the worst consecutive loss run, use that number
3. **Monitor Regularly**: Review rule rejection patterns weekly
4. **Test Thoroughly**: Validate rules with historical data before live deployment
5. **Gradual Relaxation**: Increase limits incrementally based on live performance
6. **Backup Guards**: Never rely on a single rule for critical protection
7. **Version Your Config**: Keep `.env` changes in version control (without secrets)
