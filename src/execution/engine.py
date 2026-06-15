"""
Orchestrates the full execution pipeline for a triggered signal:

    1. Fetch live account + symbol info from MT5
    2. Risk check
    3. Build TradePlan
    4. Execute order via OrderManager
       - includes retry, slippage check, partial fill detection
    5. Recalculate TP1/TP2 lot split from ACTUAL filled volume  [4]
    6. Persist Trade in PositionStore + disk
    7. Emit TRADE_OPENED

Latency tracking  [5]:
    signal_to_trade_ms   — triggered_at → trade.opened_at  (full pipeline)
    broker_round_trip_ms — order sent → order confirmed     (MT5 only)
    Both are emitted as metrics gauges and appear in the monitoring dashboard.
"""

from __future__ import annotations

from dataclasses import replace

import logging
import threading
import uuid

from src.brokers.mt5.positions import Mt5Positions
from src.config.settings import ExecutionConfig
from src.core.event_bus import EventBus
from src.core.events import Events
from src.execution.order_manager import OrderManager
from src.execution.planner import TradePlanner
from src.infra.metrics import metrics
from src.positions.store import PositionStore
from src.risk.engine import RiskEngine
from src.infra.database import TradeRepository
from src.domain.signal_interface import InboundSignal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.risk.loss_tracker import LossTracker
    from src.risk.cluster_tracker import ClusterRiskTracker
from src.domain.trade import Trade, TradeStatus
from src.utils.time import now_ms

logger = logging.getLogger(__name__)


class ExecutionEngine:
    def __init__(
        self,
        risk_engine: RiskEngine,
        trade_planner: TradePlanner,
        order_manager: OrderManager,
        mt5_positions: Mt5Positions,
        position_store: PositionStore,
        trade_repo: TradeRepository,
        event_bus: EventBus,
        exec_config: ExecutionConfig,
        loss_tracker: "LossTracker | None" = None,
        cluster_tracker: "ClusterRiskTracker | None" = None,
    ) -> None:
        self._risk = risk_engine
        self._planner = trade_planner
        self._orders = order_manager
        self._mt5_positions = mt5_positions
        self._store = position_store
        self._repo = trade_repo
        self._bus = event_bus
        self._cfg = exec_config
        self._pending: dict[str, int] = {}
        self._pending_signal_ids: set[str] = set()
        self._pending_lock = threading.Lock()
        self._daily_loss_pct: float = 0.0  # cached — refreshed by position manager poll
        self._loss_tracker: "LossTracker | None" = loss_tracker
        self._cluster_tracker: "ClusterRiskTracker | None" = cluster_tracker

    def update_daily_loss(
        self, loss_pct: float, start_equity: float, current_equity: float = 0.0
    ) -> None:
        """Called by PositionManager on each poll cycle.

        Forwards two separate updates to the LossTracker (parity with Node pipeline):
          - update_daily_loss_pct: drives the daily-loss circuit-breaker (guard 1).
          - update_equity:         drives equity-peak and rolling-window guards (guards 2 & 3).
        """
        self._daily_loss_pct = loss_pct
        if self._loss_tracker is not None:
            self._loss_tracker.update_daily_loss_pct(loss_pct, start_equity)
            if current_equity > 0:
                self._loss_tracker.update_equity(current_equity)

    def _pending_total(self) -> int:
        return sum(self._pending.values())

    def _pending_for(self, symbol: str) -> int:
        return self._pending.get(symbol, 0)

    def _reserve(self, symbol: str, signal_id: str) -> None:
        self._pending[symbol] = self._pending.get(symbol, 0) + 1
        self._pending_signal_ids.add(signal_id)

    def _release(self, symbol: str, signal_id: str) -> None:
        self._pending[symbol] = max(0, self._pending.get(symbol, 0) - 1)
        if self._pending[symbol] == 0:
            self._pending.pop(symbol, None)
        self._pending_signal_ids.discard(signal_id)

    def execute(self, signal: InboundSignal) -> Trade | None:
        # ── [5] Pipeline start time ──────────────────────────────────
        pipeline_start_ms = now_ms()
        signal = replace(signal, execution_started_at=pipeline_start_ms)
        _resolved = signal.resolved_symbol

        logger.info(
            "ExecutionEngine processing signal",
            extra={
                "signal_id": signal.id,
                "symbol": f"{signal.symbol} -> {_resolved}",
                "direction": signal.direction.value,
                "setup_candle_open_at": signal.setup_candle_open_at,
                "setup_candle_close_at": signal.setup_candle_close_at,
                "detected_at": signal.detected_at,
                "emitted_at": signal.emitted_at,
                "received_at": signal.received_at,
                "queued_at": signal.queued_at,
                "execution_started_at": signal.execution_started_at,
            },
        )

        # ── 1. Fetch broker state ──────────────────────────────────────────
        actionable_at = _actionable_signal_at(signal)
        signal_age_ms = (
            pipeline_start_ms - actionable_at if actionable_at is not None else None
        )
        logger.info(
            "Execution timing check",
            extra={
                "signal_id": signal.id,
                "actionable_at": actionable_at,
                "now": pipeline_start_ms,
                "age_ms": signal_age_ms,
                "max_signal_age_ms": self._cfg.max_signal_age_ms,
            },
        )
        if (
            signal_age_ms is not None
            and signal_age_ms > self._cfg.max_signal_age_ms
        ):
            logger.warning(
                "Stale signal rejected before broker execution",
                extra={
                    "signal_id": signal.id,
                    "symbol": signal.resolved_symbol,
                    "signal_age_ms": signal_age_ms,
                    "max_signal_age_ms": self._cfg.max_signal_age_ms,
                    "setup_candle_open_at": signal.setup_candle_open_at,
                    "setup_candle_close_at": signal.setup_candle_close_at,
                    "detected_at": signal.detected_at,
                    "emitted_at": signal.emitted_at,
                    "received_at": signal.received_at,
                    "queued_at": signal.queued_at,
                    "execution_started_at": signal.execution_started_at,
                },
            )
            metrics.increment("signal.stale_rejected")
            _set_latency_gauge("latency.market_signal_age_ms", signal_age_ms)
            _set_latency_gauge(
                "latency.emit_to_receive_ms",
                _elapsed(signal.emitted_at, signal.received_at),
            )
            _set_latency_gauge(
                "latency.receive_to_execute_ms",
                _elapsed(signal.received_at, pipeline_start_ms),
            )
            self._bus.emit(
                Events.RISK_REJECTED, {"signal": signal, "reason": "stale_signal"}
            )
            return None

        try:
            account_info = self._mt5_positions.get_account_info()
            symbol_info = self._mt5_positions.get_symbol_info(_resolved)
        except Exception:
            logger.exception("ExecutionEngine: failed to fetch broker state")
            self._bus.emit(
                Events.TRADE_ERROR, {"signal": signal, "reason": "broker_unavailable"}
            )
            return None

        # ── 2. Risk check ──────────────────────────────────────────────────
        with self._pending_lock:
            duplicate_signal = (
                signal.id in self._pending_signal_ids
                or self._store.get_by_signal_id(signal.id)
            )
            if duplicate_signal:
                logger.warning(
                    "Duplicate signal ignored",
                    extra={"signal_id": signal.id, "symbol": signal.resolved_symbol},
                )
                metrics.increment("signal.duplicates_ignored")
                self._bus.emit(
                    Events.RISK_REJECTED, {"signal": signal, "reason": "duplicate_signal"}
                )
                return None

            open_trades = self._store.get_open_trades()
            effective_open = len(open_trades) + self._pending_total()
            effective_symbol = len(
                [t for t in open_trades if t.symbol == signal.resolved_symbol]
            ) + self._pending_for(signal.resolved_symbol)
            decision = self._risk.evaluate(
                signal,
                open_trades,
                self._daily_loss_pct,
                effective_open,
                effective_symbol,
                symbol_info,
            )

            if not decision.approved:
                self._bus.emit(
                    Events.RISK_REJECTED, {"signal": signal, "reason": decision.reason}
                )
                return None

            risk_multiplier = decision.risk_multiplier
            planned_cluster_risk_r = float(
                decision.data.get("planned_cluster_risk_r", risk_multiplier)
            )

            if self._cluster_tracker is not None:
                self._cluster_tracker.reserve_signal(signal, planned_cluster_risk_r)

            self._reserve(signal.resolved_symbol, signal.id)

        self._bus.emit(Events.RISK_APPROVED, {"signal": signal})
        self._bus.emit(Events.EXECUTION_ATTEMPTED, signal)

        # ── 3. Plan trade ──────────────────────────────────────────────────
        try:
            plan = self._planner.plan(signal, account_info, symbol_info, risk_multiplier=risk_multiplier)
        except Exception:
            with self._pending_lock:
                self._release(signal.resolved_symbol, signal.id)
            if self._cluster_tracker is not None:
                self._cluster_tracker.release_signal(signal)
            logger.exception("ExecutionEngine: trade planning failed")
            self._bus.emit(
                Events.TRADE_ERROR, {"signal": signal, "reason": "planning_failed"}
            )
            return None

        self._bus.emit(Events.TRADE_PLANNED, {"plan": plan})

        # ── 4. Execute single order — full lot, broker TP at tp2 ───────────
        # TP1 level is stored in plan.tp1 and monitored by the position manager
        # poll; when price reaches TP1 the poll moves the broker SL to breakeven
        # so the trade runs the rest of the way to TP2 risk-free.
        broker_send_ms = now_ms()  # [5] broker round-trip start
        signal = replace(signal, order_sent_at=broker_send_ms)

        try:
            ticket, executed_price, filled_volume = self._orders.execute_market_order(
                plan, symbol_info, tp_override=plan.tp2, comment="xcom"
            )
        except Exception as exc:
            with self._pending_lock:
                self._release(signal.resolved_symbol, signal.id)
            if self._cluster_tracker is not None:
                self._cluster_tracker.release_signal(signal)

            exc_str = str(exc)
            is_autotrading_disabled = (
                "10027" in exc_str
                or "autotrading disabled" in exc_str.lower()
            )

            if is_autotrading_disabled:
                logger.error(
                    "Signal rejected — AutoTrading is DISABLED in MT5 terminal."
                    " Enable AutoTrading (Algo Trading button) to allow order execution.",
                    extra={
                        "signal_id": signal.id,
                        "symbol": signal.resolved_symbol,
                        "direction": signal.direction.value,
                        "reason": "AUTOTRADING_DISABLED",
                        "retcode": 10027,
                    },
                )
                self._bus.emit(
                    Events.TRADE_ERROR,
                    {
                        "signal": signal,
                        "reason": "AUTOTRADING_DISABLED",
                        "message": (
                            "Signal rejected: AutoTrading is disabled in the MT5 terminal. "
                            "Click the 'Algo Trading' button in MT5 to enable it."
                        ),
                    },
                )
            else:
                logger.exception("ExecutionEngine: order execution failed")
                self._bus.emit(
                    Events.TRADE_ERROR,
                    {
                        "signal": signal,
                        "reason": "ORDER_FAILED",
                        "message": f"Order execution failed: {exc}",
                    },
                )

            metrics.increment("orders.rejected")
            return None

        order_filled_at = now_ms()
        signal = replace(signal, order_filled_at=order_filled_at)
        broker_round_trip_ms = order_filled_at - broker_send_ms  # [5]

        # ── 5. Resolve final SL/TP levels ─────────────────────────────────
        # fill_slippage is signed: positive = filled above signal entry (buy
        # paid more / sell received more), negative = filled below.
        fill_slippage = executed_price - plan.entry_price  # signed

        if abs(fill_slippage) > 1e-8 and self._cfg.adjust_levels_on_slippage:
            # USE_SLIPPAGE_ADJUSTED_LEVELS=true: shift every level by the fill
            # delta so stop distance and R:R are preserved relative to fill.
            # Note: this moves SL/TP away from their analysis-derived prices.
            adjusted_sl = plan.stop_loss + fill_slippage
            adjusted_tp1 = plan.tp1 + fill_slippage
            adjusted_tp2 = plan.tp2 + fill_slippage
            logger.info(
                "Plan levels shifted to actual fill price (USE_SLIPPAGE_ADJUSTED_LEVELS=true)",
                extra={
                    "symbol": signal.resolved_symbol,
                    "signal_entry": plan.entry_price,
                    "fill_price": executed_price,
                    "fill_slippage": round(fill_slippage, 5),
                    "original_sl": plan.stop_loss,
                    "adjusted_sl": round(adjusted_sl, 5),
                    "original_tp1": plan.tp1,
                    "adjusted_tp1": round(adjusted_tp1, 5),
                    "original_tp2": plan.tp2,
                    "adjusted_tp2": round(adjusted_tp2, 5),
                },
            )
            try:
                self._orders.modify_position_levels(
                    ticket=ticket, sl=adjusted_sl, tp=adjusted_tp2
                )
            except Exception:
                logger.warning(
                    "ExecutionEngine: could not sync slippage-adjusted levels to broker — "
                    "levels may drift; position manager will still track correctly",
                    extra={
                        "symbol": signal.resolved_symbol,
                        "slippage": round(fill_slippage, 5),
                    },
                )
        else:
            # Default (USE_SLIPPAGE_ADJUSTED_LEVELS=false): hold levels at the
            # signal's original analysis-derived prices.  The fill price is
            # recorded for PnL tracking only — SL/TP are not moved.
            adjusted_sl = plan.stop_loss
            adjusted_tp1 = plan.tp1
            adjusted_tp2 = plan.tp2
            if abs(fill_slippage) > 1e-8:
                logger.info(
                    "Fill slippage recorded — levels held at analysis prices",
                    extra={
                        "symbol": signal.resolved_symbol,
                        "signal_entry": plan.entry_price,
                        "fill_price": executed_price,
                        "fill_slippage": round(fill_slippage, 5),
                        "sl": plan.stop_loss,
                        "tp1": plan.tp1,
                        "tp2": plan.tp2,
                    },
                )

        plan = replace(
            plan,
            lot_size=filled_volume,
            entry_price=executed_price,
            tp1=adjusted_tp1,
            tp2=adjusted_tp2,
            stop_loss=adjusted_sl,
        )

        # ── 6. Create Trade record ─────────────────────────────────────────
        ts = now_ms()
        signal = replace(signal, trade_opened_at=ts)
        trade = Trade(
            id=str(uuid.uuid4()),
            signal_id=signal.id,
            symbol=signal.resolved_symbol,
            side=plan.side,
            status=TradeStatus.OPEN,
            plan=plan,
            entry_ticket=ticket,
            entry_price=executed_price,
            entry_lots=filled_volume,
            current_lots=filled_volume,
            tp1_lots=plan.tp1_lots,
            stop_loss=plan.stop_loss,
            tp1=plan.tp1,
            tp2=plan.tp2,
            opened_at=ts,
            created_at=ts,
            updated_at=ts,
        )

        try:
            self._store.add(trade)
        except Exception:
            logger.exception(
                "Trade opened but in-memory tracking failed; manual intervention required",
                extra={
                    "trade_id": trade.id,
                    "signal_id": signal.id,
                    "ticket": ticket,
                    "symbol": trade.symbol,
                },
            )
            metrics.increment("trades.tracking_failures")
            # The MT5 position IS live — still notify downstream systems so the
            # cluster tracker and equity throttle reflect the real exposure.
            if self._cluster_tracker is not None:
                self._cluster_tracker.mark_trade_opened(trade)
            with self._pending_lock:
                self._release(signal.resolved_symbol, signal.id)
            self._repo.save(trade)
            self._bus.emit(Events.TRADE_OPENED, trade)
            self._bus.emit(
                Events.TRADE_ERROR,
                {"signal": signal, "reason": "trade_tracking_failed_after_fill"},
            )
            return None

        persisted = self._repo.save(trade)
        if not persisted:
            logger.error(
                "Trade opened but persistence failed",
                extra={
                    "trade_id": trade.id,
                    "signal_id": signal.id,
                    "ticket": ticket,
                    "symbol": trade.symbol,
                },
            )
            metrics.increment("trades.persistence_failures")
            self._bus.emit(
                Events.TRADE_ERROR,
                {"signal": signal, "reason": "trade_persistence_failed_after_fill"},
            )

        if self._cluster_tracker is not None:
            self._cluster_tracker.mark_trade_opened(trade)

        with self._pending_lock:
            self._release(signal.resolved_symbol, signal.id)

        # ── 7. Emit ────────────────────────────────────────────────────────
        self._bus.emit(Events.TRADE_OPENED, trade)
        metrics.increment("trades.opened")
        metrics.set_gauge("trades.open_count", len(self._store.get_open_trades()))

        # ── [5] Latency metrics ────────────────────────────────────────────
        market_signal_age_ms = (
            pipeline_start_ms - signal.setup_candle_close_at
            if signal.setup_candle_close_at is not None
            else signal_age_ms
        )
        emit_to_receive_ms = _elapsed(signal.emitted_at, signal.received_at)
        receive_to_execute_ms = _elapsed(signal.received_at, pipeline_start_ms)
        execution_pipeline_ms = ts - pipeline_start_ms
        signal_to_trade_ms = _elapsed(signal.emitted_at, ts)

        _set_latency_gauge("latency.market_signal_age_ms", market_signal_age_ms)
        _set_latency_gauge("latency.emit_to_receive_ms", emit_to_receive_ms)
        _set_latency_gauge("latency.receive_to_execute_ms", receive_to_execute_ms)
        metrics.set_gauge("latency.execution_pipeline_ms", execution_pipeline_ms)
        metrics.set_gauge("latency.pipeline_ms", execution_pipeline_ms)
        metrics.set_gauge("latency.broker_round_trip_ms", broker_round_trip_ms)
        _set_latency_gauge("latency.signal_to_trade_ms", signal_to_trade_ms)

        logger.info(
            "Trade opened",
            extra={
                "trade_id": trade.id,
                "signal_id": signal.id,
                "ticket": ticket,
                "entry_price": executed_price,
                "filled_lots": filled_volume,
                "tp1": round(plan.tp1, 5),
                "tp2": round(plan.tp2, 5),
                "signal_to_trade_ms": signal_to_trade_ms,
                "market_signal_age_ms": market_signal_age_ms,
                "emit_to_receive_ms": emit_to_receive_ms,
                "receive_to_execute_ms": receive_to_execute_ms,
                "execution_pipeline_ms": execution_pipeline_ms,
                "broker_round_trip_ms": broker_round_trip_ms,
                "setup_candle_open_at": signal.setup_candle_open_at,
                "setup_candle_close_at": signal.setup_candle_close_at,
                "detected_at": signal.detected_at,
                "emitted_at": signal.emitted_at,
                "received_at": signal.received_at,
                "queued_at": signal.queued_at,
                "execution_started_at": signal.execution_started_at,
                "order_sent_at": signal.order_sent_at,
                "order_filled_at": signal.order_filled_at,
                "trade_opened_at": signal.trade_opened_at,
            },
        )
        return trade


def _actionable_signal_at(signal: InboundSignal) -> int | None:
    return (
        signal.setup_candle_close_at
        or signal.triggered_at
        or signal.emitted_at
        or signal.received_at
    )


def _elapsed(start: int | None, end: int | None) -> int | None:
    if start is None or end is None:
        return None
    return end - start


def _set_latency_gauge(name: str, value: int | None) -> None:
    if value is not None:
        metrics.set_gauge(name, value)
