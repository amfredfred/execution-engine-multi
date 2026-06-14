"""
src/risk/loss_tracker.py

Three intraday guards — all reset at midnight:

  Guard 1 — Daily loss %
      Pauses until midnight when cumulative daily loss (from MT5) reaches
      max_daily_loss_pct of start-of-day equity.

  Guard 2 — Session profit drawdown
      Tracks the running total of realized P&L from closed trades today.
      Pauses until midnight when that total gives back max_equity_drawdown_pct
      of start-of-day equity from its intraday peak.
      Guard is dormant until the first profitable close (nothing to protect
      until you have realized gains).

  Guard 3 — Rolling equity window
      Pauses until midnight when the peak-to-trough swing across the last
      rolling_window_size equity readings exceeds rolling_drawdown_pct.
      Disabled when rolling_window_size < 3 (default config).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime, date
from typing import Deque
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _day_end_ms(day: date, tz: ZoneInfo) -> int:
    return (
        int(
            datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=tz).timestamp()
            * 1000
        )
        + 24 * 3_600_000
    )


def _today(tz: ZoneInfo) -> date:
    return datetime.now(tz=tz).date()


class LossTracker:
    def __init__(
        self,
        max_daily_loss_pct: float,
        engine_tz: ZoneInfo,
        max_equity_drawdown_pct: float = 2.0,
        rolling_window_size: int = 0,
        rolling_drawdown_pct: float = 0.0,
    ) -> None:
        self._limit = max_daily_loss_pct
        self._tz = engine_tz
        self._max_profit_drawdown = max_equity_drawdown_pct
        self._rolling_window_size = rolling_window_size
        self._rolling_drawdown_pct = rolling_drawdown_pct

        self._lock = threading.Lock()

        # Daily state
        self._current_pct: float = 0.0
        self._start_of_day_equity: float = 0.0
        self._tracked_day: date | None = None

        # Pause state
        self._paused_until: int = 0
        self._pause_reason: str = ""

        # Guard 2 — Session profit drawdown (closed trades only)
        self._session_closed_pnl: float = 0.0   # running sum of realized P&L today
        self._session_closed_peak: float = 0.0  # HWM of _session_closed_pnl today
        self._profit_drawback_pct: float = 0.0  # current drawback as % of start equity

        # Guard 3 — Rolling equity window (broker equity readings)
        self._equity_window: Deque[float] = deque(maxlen=rolling_window_size)

        # For display only — highest live equity seen today
        self._equity_peak: float = 0.0

    # ── Guard 1: Daily Loss ─────────────────────────────────────────────
    def update_daily_loss_pct(self, pct: float, start_equity: float) -> None:
        with self._lock:
            self._current_pct = pct
            today = _today(self._tz)
            now = _now_ms()

            # New trading day — reset all guards, latch equity when valid.
            if self._tracked_day != today:
                self._tracked_day = today
                self._profit_drawback_pct = 0.0
                self._session_closed_pnl = 0.0
                self._session_closed_peak = 0.0
                self._equity_window.clear()
                self._paused_until = 0
                self._pause_reason = ""
                if start_equity > 0:
                    self._start_of_day_equity = start_equity
                    self._equity_peak = start_equity
                    logger.info(
                        "New trading day %s — start equity latched at %.2f",
                        today.isoformat(),
                        start_equity,
                    )
                else:
                    self._start_of_day_equity = 0.0
                    self._equity_peak = 0.0
                    logger.warning(
                        "New trading day %s — start equity unavailable, will retry",
                        today.isoformat(),
                    )

            # Deferred equity latch on first valid poll after a day boundary.
            elif self._start_of_day_equity <= 0 and start_equity > 0:
                self._start_of_day_equity = start_equity
                self._equity_peak = start_equity
                logger.info(
                    "Start-of-day equity latched (deferred) for %s — %.2f",
                    today.isoformat(),
                    start_equity,
                )

            if self._paused_until and now >= self._paused_until:
                self._paused_until = 0
                self._pause_reason = ""

            # Guard 1 — daily loss %
            if pct >= self._limit:
                self._paused_until = _day_end_ms(today, self._tz)
                self._pause_reason = (
                    f"Daily loss limit reached ({pct:.2f}% >= {self._limit:.2f}%)"
                )
                logger.warning(self._pause_reason)

    # ── Guard 2: Session Profit Drawdown ────────────────────────────────
    def record_trade_closed(self, realized_pnl: float) -> None:
        """Call when any trade closes. Guards against giving back realized profit.

        Only activates once the session has a positive profit peak —
        there is nothing to protect until you have banked a gain.
        Denominator is start-of-day equity (same as the daily loss limit)
        so the threshold is expressed in the same units the user configured.
        """
        with self._lock:
            self._session_closed_pnl += realized_pnl
            if self._session_closed_pnl > self._session_closed_peak:
                self._session_closed_peak = self._session_closed_pnl

            if self._session_closed_peak > 0 and self._start_of_day_equity > 0:
                drawback = self._session_closed_peak - self._session_closed_pnl
                self._profit_drawback_pct = (
                    drawback / self._start_of_day_equity
                ) * 100.0
            else:
                self._profit_drawback_pct = 0.0

            if (
                self._max_profit_drawdown > 0
                and self._profit_drawback_pct >= self._max_profit_drawdown
            ):
                today = _today(self._tz)
                now = _now_ms()
                if not self._paused_until or now >= self._paused_until:
                    self._paused_until = _day_end_ms(today, self._tz)
                    self._pause_reason = (
                        f"Profit drawdown: gave back {self._profit_drawback_pct:.2f}% "
                        f"of equity from session peak "
                        f"(limit {self._max_profit_drawdown:.2f}%, "
                        f"peak +{self._session_closed_peak:,.2f}, "
                        f"current {self._session_closed_pnl:+,.2f})"
                    )
                    logger.warning(self._pause_reason)

    # ── Guard 3: Rolling Equity Window (+ display equity peak) ──────────
    def update_equity(self, equity: float) -> None:
        """Update the live equity reading.

        Used for two purposes:
        1. Tracking the intraday equity peak (display only — not a guard trigger).
        2. Guard 3: rolling equity window drawdown.
        """
        if equity <= 0:
            return

        with self._lock:
            now = _now_ms()
            today = _today(self._tz)

            # Display-only peak tracking
            if equity > self._equity_peak:
                self._equity_peak = equity

            # Guard 3 — rolling window
            if self._rolling_window_size > 0 and self._rolling_drawdown_pct > 0:
                self._equity_window.append(equity)
                if len(self._equity_window) >= 3:
                    w_peak = max(self._equity_window)
                    w_trough = min(self._equity_window)
                    if w_peak > 0:
                        rolling_dd = ((w_peak - w_trough) / w_peak) * 100.0
                        if rolling_dd >= self._rolling_drawdown_pct:
                            if not self._paused_until or now >= self._paused_until:
                                self._paused_until = _day_end_ms(today, self._tz)
                                self._pause_reason = (
                                    f"Rolling drawdown: {rolling_dd:.2f}% "
                                    f"(limit {self._rolling_drawdown_pct:.2f}%, "
                                    f"window {len(self._equity_window)})"
                                )
                                logger.warning(self._pause_reason)

    # ── Public API ──────────────────────────────────────────────────────
    def is_paused(self) -> tuple[bool, str]:
        with self._lock:
            now = _now_ms()
            if self._paused_until and now < self._paused_until:
                mins_left = int((self._paused_until - now) // 60_000)
                reason = self._pause_reason or "Capital Protection Guard Active"
                return True, f"{reason} — {mins_left} min until midnight reset"
            return False, ""

    def daily_risk_amount(self, max_losing_streak: int) -> float:
        with self._lock:
            if self._start_of_day_equity <= 0:
                return 0.0
            daily_budget = self._start_of_day_equity * (self._limit / 100.0)
            risk_slots = max(1, int(max_losing_streak))
            return daily_budget / risk_slots

    def stats(self) -> dict:
        with self._lock:
            now = _now_ms()
            paused = bool(self._paused_until and now < self._paused_until)
            daily_budget = (
                round(self._start_of_day_equity * (self._limit / 100.0), 2)
                if self._start_of_day_equity > 0
                else 0.0
            )

            return {
                "daily_loss_pct":          round(self._current_pct, 4),
                "start_of_day_equity":     round(self._start_of_day_equity, 2),
                "daily_budget":            daily_budget,
                "paused":                  paused,
                "pause_reason":            self._pause_reason if paused else "",
                # Guard 2 — session profit drawdown
                "session_closed_pnl":      round(self._session_closed_pnl, 2),
                "session_closed_peak":     round(self._session_closed_peak, 2),
                "profit_drawback_pct":     round(self._profit_drawback_pct, 4),
                # Display — highest live equity seen today (not a guard trigger)
                "equity_peak":             round(self._equity_peak, 2),
                # Guard 3
                "rolling_window_samples":  len(self._equity_window),
            }
