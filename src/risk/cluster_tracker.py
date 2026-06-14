"""
src/risk/cluster_tracker.py — shared risk bucket for correlated symbols.

Tracks daily cluster exposure (pending + open + realised loss) and returns
a risk multiplier before the planner calculates lot size.

Symbol matching is symbol-only — timeframe pair is intentionally ignored so
a US100 15min/5min trade is treated the same as US100 5min/5min.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, date
from zoneinfo import ZoneInfo

from src.config.settings import ClusterRiskConfig, ClusterGroupConfig
from src.utils.symbol import normalise_symbol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClusterPreview:
    approved: bool
    reason: str = ""
    cluster_name: str | None = None
    risk_multiplier: float = 1.0
    planned_risk_r: float = 1.0


@dataclass
class ClusterState:
    day: date
    pending: dict[str, float] = field(default_factory=dict)   # signal_id -> risk_r
    open: dict[str, float] = field(default_factory=dict)      # trade_id  -> risk_r
    open_signal_map: dict[str, str] = field(default_factory=dict)  # signal_id -> trade_id
    realized_loss_r: float = 0.0
    loss_count: int = 0


class ClusterRiskTracker:
    """Tracks per-cluster risk budget across pending, open, and realised loss."""

    def __init__(self, config: ClusterRiskConfig, engine_tz: ZoneInfo) -> None:
        self._config = config
        self._tz = engine_tz
        self._lock = threading.Lock()
        self._states: dict[str, ClusterState] = {}

    # ── Public API ──────────────────────────────────────────────────────────

    def preview(self, signal: object) -> ClusterPreview:
        """Return approval + risk multiplier for an incoming signal."""
        group = self._match(signal)
        if not self._config.enabled or group is None:
            return ClusterPreview(approved=True)

        with self._lock:
            state = self._state_for(group)
            used_r = self._used_r(state)
            concurrent = len(state.pending) + len(state.open)

            if state.loss_count >= group.max_same_day_losses:
                reason = (
                    f"Cluster {group.name} blocked: "
                    f"{state.loss_count}/{group.max_same_day_losses} losses today"
                )
                logger.warning(
                    "Cluster risk rejected",
                    extra={
                        "signal_id": getattr(signal, "id", None),
                        "symbol": getattr(signal, "resolved_symbol", None)
                            or getattr(signal, "symbol", None),
                        "cluster": group.name,
                        "reason": reason,
                    },
                )
                return ClusterPreview(
                    approved=False,
                    cluster_name=group.name,
                    reason=reason,
                )

            if concurrent >= group.max_concurrent_positions:
                reason = (
                    f"Cluster {group.name} concurrent limit reached: "
                    f"{concurrent}/{group.max_concurrent_positions}"
                )
                logger.warning(
                    "Cluster risk rejected",
                    extra={
                        "signal_id": getattr(signal, "id", None),
                        "symbol": getattr(signal, "resolved_symbol", None)
                            or getattr(signal, "symbol", None),
                        "cluster": group.name,
                        "reason": reason,
                    },
                )
                return ClusterPreview(
                    approved=False,
                    cluster_name=group.name,
                    reason=reason,
                )

            base_multiplier = (
                group.after_first_loss_risk_multiplier
                if state.loss_count >= 1
                else 1.0
            )

            remaining_r = group.max_same_day_loss_r - used_r
            planned_risk_r = min(base_multiplier, remaining_r)

            if planned_risk_r < group.min_trade_risk_multiplier:
                reason = (
                    f"Cluster {group.name} risk budget exhausted: "
                    f"used {used_r:.2f}R / {group.max_same_day_loss_r:.2f}R, "
                    f"remaining {remaining_r:.2f}R"
                )
                logger.warning(
                    "Cluster risk rejected",
                    extra={
                        "signal_id": getattr(signal, "id", None),
                        "symbol": getattr(signal, "resolved_symbol", None)
                            or getattr(signal, "symbol", None),
                        "cluster": group.name,
                        "reason": reason,
                    },
                )
                return ClusterPreview(
                    approved=False,
                    cluster_name=group.name,
                    reason=reason,
                )

            logger.info(
                "Cluster risk approved",
                extra={
                    "signal_id": getattr(signal, "id", None),
                    "symbol": getattr(signal, "resolved_symbol", None)
                        or getattr(signal, "symbol", None),
                    "cluster": group.name,
                    "risk_multiplier": planned_risk_r,
                    "planned_risk_r": planned_risk_r,
                },
            )
            return ClusterPreview(
                approved=True,
                cluster_name=group.name,
                risk_multiplier=planned_risk_r,
                planned_risk_r=planned_risk_r,
            )

    def reserve_signal(self, signal: object, planned_risk_r: float) -> None:
        group = self._match(signal)
        if not self._config.enabled or group is None:
            return
        with self._lock:
            state = self._state_for(group)
            state.pending[getattr(signal, "id")] = planned_risk_r

    def release_signal(self, signal: object) -> None:
        group = self._match(signal)
        if not self._config.enabled or group is None:
            return
        with self._lock:
            state = self._state_for(group)
            state.pending.pop(getattr(signal, "id"), None)

    def mark_trade_opened(self, trade: object) -> None:
        group = self._match_trade(trade)
        if not self._config.enabled or group is None:
            return
        with self._lock:
            state = self._state_for(group)
            signal_id = getattr(trade, "signal_id", None) or ""
            risk_r = state.pending.pop(signal_id, 1.0)
            trade_id = getattr(trade, "id")
            state.open[trade_id] = risk_r
            if signal_id:
                state.open_signal_map[signal_id] = trade_id

    def mark_trade_closed(self, trade: object) -> None:
        group = self._match_trade(trade)
        if not self._config.enabled or group is None:
            return
        with self._lock:
            state = self._state_for(group)
            trade_id = getattr(trade, "id")
            signal_id = getattr(trade, "signal_id", None) or ""
            risk_r = state.open.pop(trade_id, 1.0)
            state.open_signal_map.pop(signal_id, None)

            rr = float(getattr(trade, "realized_rr", None) or 0.0)
            if rr < 0:
                # Convert trade-local R into cluster-budget R.
                # A 0.5R-sized trade closing at -1R trade-local = -0.5R cluster damage.
                cluster_damage = abs(rr) * risk_r
                state.realized_loss_r += cluster_damage
                state.loss_count += 1
                logger.info(
                    "Cluster trade closed (loss)",
                    extra={
                        "trade_id": trade_id,
                        "signal_id": signal_id,
                        "symbol": getattr(trade, "symbol", None),
                        "realized_rr": rr,
                        "cluster_risk_r": risk_r,
                        "cluster_damage_r": round(cluster_damage, 4),
                        "realized_cluster_loss_r": round(state.realized_loss_r, 4),
                        "loss_count": state.loss_count,
                    },
                )
            else:
                logger.info(
                    "Cluster trade closed (win/BE — budget not consumed)",
                    extra={
                        "trade_id": trade_id,
                        "symbol": getattr(trade, "symbol", None),
                        "realized_rr": rr,
                    },
                )

    def hydrate_open_trades(self, trades: list) -> None:
        """Restore open cluster exposure after a restart.

        Uses 1.0R per trade conservatively — the exact multiplier is not yet
        persisted on the Trade. This ensures we never undercount exposure.
        """
        if not self._config.enabled:
            return
        with self._lock:
            for trade in trades:
                group = self._match_trade(trade)
                if group is None:
                    continue
                state = self._state_for(group)
                trade_id = getattr(trade, "id")
                state.open.setdefault(trade_id, 1.0)

    def stats(self) -> dict:
        with self._lock:
            return {
                name: {
                    "day": state.day.isoformat(),
                    "pending_r": round(sum(state.pending.values()), 4),
                    "open_r": round(sum(state.open.values()), 4),
                    "realized_loss_r": round(state.realized_loss_r, 4),
                    "loss_count": state.loss_count,
                    "pending_count": len(state.pending),
                    "open_count": len(state.open),
                }
                for name, state in self._states.items()
            }

    # ── Internal helpers ────────────────────────────────────────────────────

    def _match(self, signal: object) -> ClusterGroupConfig | None:
        resolved = getattr(signal, "resolved_symbol", None)
        raw = getattr(signal, "symbol", None)
        symbol = normalise_symbol(str(resolved or raw or ""))
        for group in self._config.groups:
            if symbol in group.symbols:
                return group
        return None

    def _match_trade(self, trade: object) -> ClusterGroupConfig | None:
        symbol = normalise_symbol(str(getattr(trade, "symbol", "") or ""))
        for group in self._config.groups:
            if symbol in group.symbols:
                return group
        return None

    def _state_for(self, group: ClusterGroupConfig) -> ClusterState:
        today = datetime.now(tz=self._tz).date()
        state = self._states.get(group.name)
        if state is None or state.day != today:
            state = ClusterState(day=today)
            self._states[group.name] = state
        return state

    @staticmethod
    def _used_r(state: ClusterState) -> float:
        return state.realized_loss_r + sum(state.pending.values()) + sum(state.open.values())
