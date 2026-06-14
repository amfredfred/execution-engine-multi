"""
src/risk/equity_throttle.py — equity-curve risk throttle.

Sizes new positions at a reduced multiplier while the engine's realized
R-equity sits more than `drawdown_threshold_r` below its rolling-window
peak, and releases once drawdown recovers below `release_threshold_r`
(hysteresis so the state doesn't flap around one threshold).

Rationale (RBA 42-month backtest, signal-engine/results/RBA): losing
streaks are statistically i.i.d. and the edge persists through them, so
skip/pause rules destroy expectancy. Scaling risk down while in drawdown
kept ~97.6% of total R while cutting max drawdown ~28% in simulation.

R accounting: each closed trade contributes  money_r × plan.risk_multiplier,
so a half-sized trade that loses 1R counts −0.5R — the same convention the
cluster tracker uses for cluster damage. money_r is TP1-weighted: when a
partial close happened at TP1 the contribution blends both legs against the
ORIGINAL stop distance (trades.stop_loss is mutated to breakeven after TP1,
which is why the stored realized_rr alone understates TP1-managed wins).

Unlike LossTracker this state deliberately spans days — no midnight reset.
The rolling window itself is the only forgetting mechanism.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Iterable, Optional

from src.config.settings import EquityThrottleConfig
from src.infra.metrics import metrics

logger = logging.getLogger(__name__)

_DAY_MS = 86_400_000


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class ThrottlePreview:
    multiplier: float
    drawdown_r: float
    engaged: bool


# ── Pure helpers ──────────────────────────────────────────────────────────────


def money_r(
    *,
    side: str,
    entry_price: Optional[float],
    original_sl: Optional[float],
    close_price: Optional[float],
    realized_rr: Optional[float],
    tp1_hit: bool,
    tp1_fraction: float,
    tp1_exit_price: Optional[float],
) -> Optional[float]:
    """Realized R of a closed trade in money terms.

    Plain trades return realized_rr unchanged. TP1-managed trades blend the
    partial-close leg and the final leg, both measured against the original
    stop distance. Returns None when the trade carries no outcome.
    """
    if realized_rr is None:
        return None
    if not tp1_hit or tp1_fraction <= 0:
        return float(realized_rr)
    if not entry_price or not original_sl or not close_price or not tp1_exit_price:
        return float(realized_rr)

    if side == "BUY":
        risk = entry_price - original_sl
        reward = lambda price: price - entry_price  # noqa: E731
    elif side == "SELL":
        risk = original_sl - entry_price
        reward = lambda price: entry_price - price  # noqa: E731
    else:
        return float(realized_rr)

    if risk <= 0:
        return float(realized_rr)

    fraction = min(1.0, max(0.0, tp1_fraction))
    r_tp1 = reward(tp1_exit_price) / risk
    r_final = reward(close_price) / risk
    return fraction * r_tp1 + (1.0 - fraction) * r_final


def compute_drawdown_r(contributions: Iterable[float]) -> float:
    """Drawdown of the cumulative R series from its running peak.

    The peak starts at 0 (the window's implicit baseline), so a window that
    opens with losses is already counted as drawdown — conservative.
    """
    total = 0.0
    peak = 0.0
    for c in contributions:
        total += c
        if total > peak:
            peak = total
    return max(0.0, peak - total)


def _contribution_from_trade(trade: object) -> Optional[tuple[int, float]]:
    """(closed_at_ms, weighted R) from a live Trade object, or None to skip."""
    rr = getattr(trade, "realized_rr", None)
    if rr is None:
        return None

    plan = getattr(trade, "plan", None)
    multiplier = float(getattr(plan, "risk_multiplier", 1.0) or 1.0)
    original_sl = getattr(plan, "stop_loss", None) if plan else None
    entry_price = getattr(trade, "entry_price", None) or (
        getattr(plan, "entry_price", None) if plan else None
    )

    entry_lots = float(getattr(trade, "entry_lots", 0.0) or 0.0)
    tp1_lots = float(getattr(trade, "tp1_lots", 0.0) or 0.0)
    tp1_fraction = (tp1_lots / entry_lots) if entry_lots > 0 else 0.0
    tp1_exit = getattr(trade, "tp1_close_price", None) or getattr(trade, "tp1", None)

    side = getattr(getattr(trade, "side", None), "value", "BUY")
    r = money_r(
        side=side,
        entry_price=entry_price,
        original_sl=original_sl,
        close_price=getattr(trade, "close_price", None),
        realized_rr=float(rr),
        tp1_hit=bool(getattr(trade, "tp1_hit", False)),
        tp1_fraction=tp1_fraction,
        tp1_exit_price=tp1_exit,
    )
    if r is None:
        return None
    closed_at = getattr(trade, "closed_at", None) or _now_ms()
    return int(closed_at), r * multiplier


def _contribution_from_row(row: dict) -> Optional[tuple[int, float]]:
    """(closed_at_ms, weighted R) from a raw trades-table row, or None to skip.

    No tp1_close_price column exists, so the TP1 leg uses the planned tp1
    level; the TP1 fraction is recovered from entry_lots vs current_lots.
    Legacy rows without plan_json originals fall back to realized_rr.
    """
    rr = row.get("realized_rr")
    closed_at = row.get("closed_at")
    if rr is None or closed_at is None:
        return None

    plan_d: dict = {}
    if row.get("plan_json"):
        try:
            plan_d = json.loads(row["plan_json"])
        except Exception:
            plan_d = {}

    multiplier = float(plan_d.get("riskMultiplier", 1.0) or 1.0)
    entry_lots = float(row.get("entry_lots") or 0.0)
    current_lots = float(row.get("current_lots") or 0.0)
    tp1_hit = bool(row.get("tp1_hit"))
    tp1_fraction = (
        1.0 - (current_lots / entry_lots)
        if tp1_hit and entry_lots > 0 and 0 <= current_lots < entry_lots
        else 0.0
    )

    r = money_r(
        side=str(row.get("side") or "BUY"),
        entry_price=plan_d.get("entryPrice") or row.get("entry_price"),
        # Only the plan_json original is trustworthy — the column may have
        # been moved to breakeven.
        original_sl=plan_d.get("stopLoss"),
        close_price=row.get("close_price"),
        realized_rr=float(rr),
        tp1_hit=tp1_hit,
        tp1_fraction=tp1_fraction,
        tp1_exit_price=row.get("tp1"),
    )
    if r is None:
        return None
    return int(closed_at), r * multiplier


# ── Tracker ───────────────────────────────────────────────────────────────────


class EquityThrottleTracker:
    """Rolling R-equity drawdown tracker producing a sizing multiplier."""

    def __init__(self, config: EquityThrottleConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._window: Deque[tuple[int, float]] = deque()
        self._engaged = False
        self._dd_r = 0.0

    # ── Public API ──────────────────────────────────────────────────────────

    def record_trade_closed(self, trade: object) -> None:
        entry = _contribution_from_trade(trade)
        if entry is None:
            return
        with self._lock:
            self._window.append(entry)
            self._refresh_locked()
            dd = self._dd_r
        logger.info(
            "Equity throttle recorded close",
            extra={
                "trade_id": getattr(trade, "id", None),
                "contribution_r": round(entry[1], 4),
                "drawdown_r": round(dd, 4),
            },
        )

    def hydrate(self, rows: list[dict]) -> None:
        """Rebuild the rolling window from persisted closed trades.

        Idempotent — clears and rebuilds, so repeated calls are safe.
        """
        entries = []
        for row in rows:
            entry = _contribution_from_row(row)
            if entry is not None:
                entries.append(entry)
        entries.sort(key=lambda e: e[0])
        with self._lock:
            self._window.clear()
            self._window.extend(entries)
            self._refresh_locked()
            dd = self._dd_r
            engaged = self._engaged
        logger.info(
            "Equity throttle hydrated",
            extra={
                "samples": len(entries),
                "drawdown_r": round(dd, 4),
                "engaged": engaged,
            },
        )

    def preview(self) -> ThrottlePreview:
        with self._lock:
            self._refresh_locked()
            engaged = self._engaged and self._config.enabled
            return ThrottlePreview(
                multiplier=self._config.risk_multiplier if engaged else 1.0,
                drawdown_r=self._dd_r,
                engaged=engaged,
            )

    def stats(self) -> dict:
        with self._lock:
            self._refresh_locked()
            engaged = self._engaged and self._config.enabled
            return {
                "enabled": self._config.enabled,
                "engaged": engaged,
                "multiplier": self._config.risk_multiplier if engaged else 1.0,
                "drawdown_r": round(self._dd_r, 4),
                "threshold_r": self._config.drawdown_threshold_r,
                "release_r": self._config.release_threshold_r,
                "window_days": self._config.window_days,
                "samples": len(self._window),
            }

    # ── Internal ────────────────────────────────────────────────────────────

    def _refresh_locked(self) -> None:
        cutoff = _now_ms() - self._config.window_days * _DAY_MS
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()

        self._dd_r = compute_drawdown_r(c for _, c in self._window)

        was_engaged = self._engaged
        if self._dd_r > self._config.drawdown_threshold_r:
            self._engaged = True
        elif self._dd_r < self._config.release_threshold_r:
            self._engaged = False
        # Between release and engage thresholds the previous state holds.

        if self._engaged != was_engaged:
            if self._engaged:
                metrics.increment("risk.throttle_engaged")
                log = logger.warning if self._config.enabled else logger.info
                log(
                    "Equity throttle ENGAGED%s — drawdown %.2fR > %.2fR, sizing at %.2fx",
                    "" if self._config.enabled else " (disabled — sizing unchanged)",
                    self._dd_r,
                    self._config.drawdown_threshold_r,
                    self._config.risk_multiplier,
                )
            else:
                metrics.increment("risk.throttle_released")
                logger.info(
                    "Equity throttle released — drawdown %.2fR < %.2fR",
                    self._dd_r,
                    self._config.release_threshold_r,
                )

        metrics.set_gauge("risk.equity_throttle_dd_r", round(self._dd_r, 4))
        metrics.set_gauge(
            "risk.equity_throttle_multiplier",
            self._config.risk_multiplier
            if (self._engaged and self._config.enabled)
            else 1.0,
        )
