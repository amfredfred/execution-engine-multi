"""
Converts a validated, risk-approved signal into a concrete TradePlan.

Live-account adjustment:
  [2] Spread surcharge — the real cost of entering a trade includes the spread
      paid on entry.  If SPREAD_RISK_MULTIPLIER > 0, the spread (in price units)
      is added to the SL distance before sizing, so the lot size already accounts
      for the wider effective risk on a live account.

      Example:  entry=1.08450, SL=1.08200, spread=0.00020 (2 pips), multiplier=1.0
                raw_sl_distance = 0.00250
                adjusted        = 0.00250 + 1.0 × 0.00020 = 0.00270
                Result: slightly smaller lot size — you risk the same $ amount
                        even after paying the spread.

      Set SPREAD_RISK_MULTIPLIER=0.0 to disable (demo / tight ECN accounts).

  [3] Pessimistic entry — lot size is calculated against the worst possible fill
      price within the configured slippage limit (max_entry_slippage_pct_of_stop).
      This ensures the position never risks more than the target amount even if
      the broker fills at the edge of the allowed slippage band.

      Example (SHORT):  entry=0.70193, SL=0.70300, stop_dist=0.00107, max_slip=20%
                        max_slip_price    = 0.00107 * 0.20 = 0.000214
                        pessimistic_entry = 0.70193 - 0.000214 = 0.701716
                        raw_sl_distance   = 0.70300 - 0.701716 = 0.001284  (vs 0.00107)
                        Result: smaller lot size — actual risk stays ≤ target
                                regardless of where within the slip band fill lands.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.config.settings import RiskConfig, ExecutionConfig
from src.domain.position import AccountInfo, SymbolInfo
from src.domain.signal_interface import InboundSignal, SignalDirection
from src.domain.trade import OrderSide, TradePlan
from src.utils.lot_calculator import calculate_lot_size
from src.utils.price import pip_size
from src.utils.time import now_ms

if TYPE_CHECKING:
    from src.risk.loss_tracker import LossTracker

logger = logging.getLogger(__name__)


class TradePlanner:
    def __init__(
        self,
        risk_config: RiskConfig,
        exec_config: ExecutionConfig,
        loss_tracker: "LossTracker",
    ) -> None:
        self._risk         = risk_config
        self._exec         = exec_config
        self._loss_tracker = loss_tracker

    def plan(
        self,
        signal: InboundSignal,
        account_info: AccountInfo,
        symbol_info: SymbolInfo,
        risk_multiplier: float = 1.0,
    ) -> TradePlan:
        side = (
            OrderSide.BUY
            if signal.direction == SignalDirection.LONG
            else OrderSide.SELL
        )
        pip = pip_size(symbol_info.point, symbol_info.digits)

        # ── [2] Spread-adjusted stop loss distance ─────────────────────────
        spread_price = (
            (symbol_info.ask - symbol_info.bid)
            if symbol_info.ask and symbol_info.bid
            else 0.0
        )
        spread_surcharge = spread_price * self._exec.spread_risk_multiplier

        # ── [3] Pessimistic entry — size to worst fill within slippage limit ─
        stop_distance = abs(signal.entry_price - signal.stop_loss)
        max_slip_price = self._exec.max_entry_slippage_pct_of_stop * stop_distance
        pessimistic_entry = (
            signal.entry_price - max_slip_price
            if signal.direction == SignalDirection.SHORT
            else signal.entry_price + max_slip_price
        )

        raw_sl_distance = abs(pessimistic_entry - signal.stop_loss)
        adjusted_sl_distance = raw_sl_distance + spread_surcharge

        if signal.direction == SignalDirection.LONG:
            sizing_sl = signal.entry_price - adjusted_sl_distance
        else:
            sizing_sl = signal.entry_price + adjusted_sl_distance

        # ── Streak-based risk amount ───────────────────────────────────────
        # daily_budget = start_of_day_equity × (MAX_DAILY_LOSS_PERCENT / 100)
        # risk_per_trade = daily_budget / MAX_LOSING_STREAK
        base_risk_amount = self._loss_tracker.daily_risk_amount(
            self._risk.max_losing_streak
        )
        clamped_multiplier = max(0.0, min(1.0, risk_multiplier))
        risk_amount = base_risk_amount * clamped_multiplier

        # ── Lot size calculation ───────────────────────────────────────────
        calc = calculate_lot_size(
            risk_amount=risk_amount,
            entry_price=signal.entry_price,
            stop_loss=sizing_sl,
            symbol_info=symbol_info,
            max_lot=self._risk.max_lot_size,
            min_lot=self._risk.min_lot_size,
        )

        # ── Static TP1 level — stored for poll-based partial-close detection ─
        # When the position manager poll sees price cross TP1 it closes tp1_lots
        # and (if configured) moves the broker SL to entry so the remaining
        # position runs to TP2 risk-free.
        # TP1 = entry ± (tp1_trigger_pct / 100 × |tp2 − entry|)
        # This keeps TP1 proportional to the actual trade range regardless of RRR,
        # so a 5R trade doesn't move SL to BE on a mere 1R move.
        trade_range = abs(signal.tp2 - signal.entry_price)
        tp1_trigger_pct = self._exec.tp1_trigger_pct_for(
            signal.symbol, signal.htf_interval, signal.ltf_interval
        )
        tp1_percentage = self._exec.tp1_percentage_for(
            signal.symbol, signal.htf_interval, signal.ltf_interval
        )
        tp1_offset = (tp1_trigger_pct / 100.0) * trade_range
        static_tp1 = (
            signal.entry_price + tp1_offset
            if signal.direction == SignalDirection.LONG
            else signal.entry_price - tp1_offset
        )

        # ── TP1 partial-close lot size ─────────────────────────────────────
        # Pre-calculate how many lots to close at TP1 so the poll handler
        # doesn't need to re-derive it.  Floored to volume_step so the broker
        # always accepts the volume.  0.0 means "no partial close".
        #
        # TP1 is eligible whenever tp1_trigger_pct is between 0 and 100 exclusive,
        # which guarantees static_tp1 is strictly between entry and tp2.
        import math
        volume_step = symbol_info.volume_step if symbol_info.volume_step else 0.01
        if tp1_percentage > 0 and 0 < tp1_trigger_pct < 100:
            raw_tp1_lots = calc.lot_size * (tp1_percentage / 100.0)
            tp1_lots = math.floor(raw_tp1_lots / volume_step) * volume_step
            tp1_lots = round(tp1_lots, 2)
            # Ensure at least one step and never consumes the full position
            if tp1_lots < volume_step:
                tp1_lots = 0.0
            elif tp1_lots >= calc.lot_size:
                tp1_lots = round(
                    math.floor((calc.lot_size - volume_step) / volume_step) * volume_step, 2
                )
        else:
            tp1_lots = 0.0
        risk_pct = (
            (calc.risk_amount / account_info.balance) * 100.0
            if account_info.balance
            else 0.0
        )

        plan = TradePlan(
            signal_id=signal.id,
            symbol=symbol_info.symbol,
            side=side,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            tp1=static_tp1,
            tp2=signal.tp2,
            lot_size=calc.lot_size,
            tp1_lots=tp1_lots,
            risk_amount=calc.risk_amount,
            risk_percent=risk_pct,
            risk_reward_ratio=signal.risk_reward_ratio,
            planned_at=now_ms(),
            signal=signal,
            risk_multiplier=clamped_multiplier,
        )

        logger.info(
            "Lot sizing adjustments applied",
            extra={
                "signal_id": signal.id,
                "symbol": symbol_info.symbol,
                "side": side.value,
                "lot_size": calc.lot_size,
                "risk_amount": round(calc.risk_amount, 2),
                "risk_pct": round(risk_pct, 2),
                "spread_pips": round(spread_price / pip, 1) if spread_price else 0,
                "surcharge_pips": round(spread_surcharge / pip, 1),
                "slippage_buffer_pct_of_stop": round(
                    self._exec.max_entry_slippage_pct_of_stop * 100, 1
                ),
                "raw_sl_pips": round(
                    abs(signal.entry_price - signal.stop_loss) / pip, 1
                ),
                "pessimistic_sl_pips": round(raw_sl_distance / pip, 1),
                "adjusted_sl_pips": round(adjusted_sl_distance / pip, 1),
                "tp1_trigger_pct": tp1_trigger_pct,
                "trade_range_pips": round(trade_range / pip, 1) if pip else 0,
                "tp1_eligible": 0 < tp1_trigger_pct < 100,
                "tp1_percentage": tp1_percentage,
                "tf_pair": f"{signal.htf_interval}:{signal.ltf_interval}",
                "tp1_lots": tp1_lots,
                "base_risk_amount": round(base_risk_amount, 2),
                "risk_multiplier": round(risk_multiplier, 4),
                "effective_risk_amount": round(risk_amount, 2),
                "signal_tp1": signal.tp1,
                "static_tp1": round(static_tp1, 5),
                "tp1_overridden": signal.tp1 != static_tp1,
            },
        )
        return plan
