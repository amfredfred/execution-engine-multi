"""Replay the RBA 42-month backtest through the real EquityThrottleTracker.

Dev sanity check (not a unit test): streams the combined close-ordered trade
sequence from signal-engine/results/RBA/*.csv through the production tracker,
mirroring live mechanics — preview at entry time using only trades already
closed, contribution scaled by the multiplier each trade actually received.

Expected ballpark vs streak_analysis.py's "half risk while DD > 8R" sim
(which had no release hysteresis): total R ≈ −2..−4% vs baseline,
max DD ≈ −15.5R vs −21.5R baseline.

Run:  python scripts/replay_throttle.py
"""

from __future__ import annotations

import heapq
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.risk.equity_throttle as et  # noqa: E402
from src.config.settings import EquityThrottleConfig  # noqa: E402

RESULTS = Path(__file__).resolve().parents[2] / "signal-engine" / "results" / "RBA"
SYMBOLS = ["US500", "US100", "XAUUSD"]


def max_drawdown(r_series: list[float]) -> float:
    total = peak = dd = 0.0
    for r in r_series:
        total += r
        peak = max(peak, total)
        dd = min(dd, total - peak)
    return dd


def main() -> None:
    frames = []
    for s in SYMBOLS:
        df = pd.read_csv(RESULTS / f"{s}.csv")
        frames.append(df[["entry_dt", "close_dt", "realized_rr", "direction"]])
    trades = pd.concat(frames, ignore_index=True)
    trades["entry_ms"] = pd.to_datetime(trades.entry_dt).astype("int64") // 10**6
    trades["close_ms"] = pd.to_datetime(trades.close_dt).astype("int64") // 10**6
    trades = trades.sort_values("entry_ms").reset_index(drop=True)

    tracker = et.EquityThrottleTracker(EquityThrottleConfig())

    # Drive the tracker on simulated time.
    sim_now = int(trades.entry_ms.iloc[0])
    et._now_ms = lambda: sim_now  # noqa: E731

    pending: list[tuple[int, int]] = []  # (close_ms, index)
    multipliers: dict[int, float] = {}
    engagements = 0
    was_engaged = False
    throttled_count = 0

    for i, row in enumerate(trades.itertuples()):
        sim_now = int(row.entry_ms)

        # Record everything that closed before this entry — live ordering.
        while pending and pending[0][0] <= sim_now:
            close_ms, j = heapq.heappop(pending)
            jrow = trades.iloc[j]
            tracker.record_trade_closed(
                SimpleNamespace(
                    id=f"t-{j}",
                    side=SimpleNamespace(
                        value="BUY" if jrow.direction == "LONG" else "SELL"
                    ),
                    realized_rr=float(jrow.realized_rr),
                    entry_price=None,
                    entry_lots=1.0,
                    tp1_lots=0.0,
                    tp1_hit=False,
                    tp1_close_price=None,
                    tp1=None,
                    close_price=None,
                    closed_at=int(close_ms),
                    plan=SimpleNamespace(
                        risk_multiplier=multipliers[j],
                        stop_loss=None,
                        entry_price=None,
                    ),
                )
            )

        preview = tracker.preview()
        if preview.engaged and not was_engaged:
            engagements += 1
        was_engaged = preview.engaged

        multipliers[i] = preview.multiplier
        if preview.multiplier < 1.0:
            throttled_count += 1
        heapq.heappush(pending, (int(row.close_ms), i))

    # Equity streams in close order.
    order = trades.close_ms.sort_values().index
    base = [float(trades.realized_rr.iloc[k]) for k in order]
    throttled = [float(trades.realized_rr.iloc[k]) * multipliers[k] for k in order]

    base_total, thr_total = sum(base), sum(throttled)
    print(f"trades: {len(trades)}")
    print(f"baseline:  total {base_total:+9.1f}R   maxDD {max_drawdown(base):+7.1f}R")
    print(f"throttled: total {thr_total:+9.1f}R   maxDD {max_drawdown(throttled):+7.1f}R")
    print(f"cost: {(thr_total / base_total - 1) * 100:+.2f}% of total R")
    print(f"engage episodes: {engagements}")
    print(f"trades at reduced risk: {throttled_count} ({throttled_count / len(trades):.2%})")


if __name__ == "__main__":
    main()
