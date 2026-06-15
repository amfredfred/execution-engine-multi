"""
Converts a TradePlan into a broker order and executes it via MT5.

Live-account protections:
  [1] Post-fill slippage validation — per-symbol pip threshold, not flat
  [3] Retry on requote / rejection with fresh price each attempt
      10016 INVALID_STOPS — widens SL/TP to broker stop level before retry
  [4] Partial fill detection — returns actual filled volume
  [5] Margin recovery — on 10019 NO_MONEY the lot size is halved once and
      retried immediately.  If the halved size is below the broker minimum,
      or the retry still fails, the trade is dropped cleanly.
"""

from __future__ import annotations

import logging
import time

from src.brokers.mt5.orders import Mt5Orders
from src.brokers.mt5.positions import Mt5Positions
from src.brokers.mt5.types import Mt5OrderType
from src.config.settings import ExecutionConfig
from src.infra.metrics import metrics
from src.domain.position import SymbolInfo
from src.domain.trade import OrderSide, TradePlan
from src.utils.price import normalise_lots

logger = logging.getLogger(__name__)

# Retcodes that are transient and worth retrying with a fresh price
_RETRYABLE_RETCODES = {
    10004,  # TRADE_RETCODE_REQUOTE
    10006,  # TRADE_RETCODE_REJECT
    10007,  # TRADE_RETCODE_CANCEL
    10016,  # TRADE_RETCODE_INVALID_STOPS  — handled specially below
    10018,  # TRADE_RETCODE_MARKET_CLOSED
}

# [5] Insufficient margin — handled separately because the volume must change,
# not just the price.  We halve the lot size once and retry immediately.
_RETCODE_NO_MONEY = 10019

# Slippage is validated as a fraction of each trade's own stop distance.
# e.g. 0.20 means adverse slippage must be < 20% of (entry − SL).
# This is fully dynamic — no per-symbol overrides needed.


class OrderManager:
    def __init__(
        self,
        mt5_orders: Mt5Orders,
        mt5_positions: Mt5Positions,
        exec_config: ExecutionConfig,
    ) -> None:
        self._orders = mt5_orders
        self._positions = mt5_positions
        self._cfg = exec_config

    def execute_market_order(
        self,
        plan: TradePlan,
        symbol_info: SymbolInfo,
        tp_override: float | None = None,
        comment: str | None = "_market_order",
    ) -> tuple[int, float, float]:
        """
        Submit a market order for *plan*.

        tp_override: explicit broker TP level — defaults to plan.tp2 when None.

        Returns (ticket, executed_price, filled_volume).
        Raises on exhausted retries or unacceptable slippage.
        """
        order_type = (
            Mt5OrderType.BUY if plan.side == OrderSide.BUY else Mt5OrderType.SELL
        )
        stop_distance = abs(plan.entry_price - plan.stop_loss)
        max_slip_pct_of_stop = self._cfg.max_entry_slippage_pct_of_stop
        last_error: Exception | None = None
        max_attempts = 1 + self._cfg.order_retry_count

        # Working SL/TP — may be adjusted on INVALID_STOPS retry
        sl = plan.stop_loss
        tp = tp_override if tp_override is not None else plan.tp2

        # [5] Working volume — may be halved once on NO_MONEY
        volume = plan.lot_size
        _margin_halved = False

        for attempt in range(1, max_attempts + 1):

            # Fresh price on every attempt
            tick = self._positions.get_current_tick(plan.symbol)
            if tick is None:
                raise RuntimeError(f"Cannot get current tick for {plan.symbol}")
            price = tick.ask if plan.side == OrderSide.BUY else tick.bid

            try:
                result = self._orders.open_market_order(
                    symbol=plan.symbol,
                    order_type=order_type,
                    volume=volume,
                    price=price,
                    sl=sl,
                    tp=tp,
                    slippage=self._cfg.slippage,
                    magic=self._cfg.magic,
                    comment=f"{comment}_{self._cfg.comment}",
                    filling_mode=symbol_info.order_filling_mode,
                )

            except RuntimeError as exc:
                retcode = _extract_retcode(exc)

                # [5] NO_MONEY — insufficient margin.  Halve the volume once
                # and retry immediately.  No sleep: this isn't a timing issue.
                if retcode == _RETCODE_NO_MONEY:
                    if _margin_halved:
                        # Already tried once — drop the trade.
                        logger.warning(
                            "Insufficient margin — halved lot size still rejected, dropping trade",
                            extra={
                                "symbol": plan.symbol,
                                "volume": volume,
                                "retcode": retcode,
                            },
                        )
                        raise

                    halved = normalise_lots(
                        volume / 2,
                        symbol_info.lot_step,
                        symbol_info.lot_min,
                        symbol_info.lot_max,
                    )
                    if halved < symbol_info.lot_min:
                        logger.warning(
                            "Insufficient margin — halved lot size below broker minimum, dropping trade",
                            extra={
                                "symbol": plan.symbol,
                                "original_lots": plan.lot_size,
                                "halved_lots": halved,
                                "min_lot": symbol_info.lot_min,
                            },
                        )
                        raise

                    logger.warning(
                        "Insufficient margin — halving lot size and retrying",
                        extra={
                            "symbol": plan.symbol,
                            "original_lots": volume,
                            "halved_lots": halved,
                            "retcode": retcode,
                        },
                    )
                    metrics.increment("orders.margin_reduced")
                    volume = halved
                    _margin_halved = True
                    last_error = exc
                    continue

                if retcode not in _RETRYABLE_RETCODES or attempt >= max_attempts:
                    raise

                if retcode == 10016:
                    # INVALID_STOPS — SL or TP is inside the broker's stop level.
                    # Widen both by the stop level distance and retry.
                    stop_level_price = symbol_info.stops_level * symbol_info.point
                    sl, tp = _widen_stops(
                        side=plan.side,
                        entry=price,
                        sl=sl,
                        tp=tp,
                        min_dist=stop_level_price,
                    )
                    logger.warning(
                        "INVALID_STOPS — widening to broker stop level and retrying",
                        extra={
                            "attempt": attempt,
                            "symbol": plan.symbol,
                            "stop_level": symbol_info.stops_level,
                            "new_sl": sl,
                            "new_tp": tp,
                        },
                    )
                else:
                    logger.warning(
                        "Order retryable error — retrying with fresh price",
                        extra={
                            "attempt": attempt,
                            "max": max_attempts,
                            "retcode": retcode,
                            "symbol": plan.symbol,
                        },
                    )

                metrics.increment("orders.retried")
                time.sleep(self._cfg.order_retry_delay_sec)
                last_error = exc
                continue

            # ── Order accepted ────────────────────────────────────────────

            # [4] Partial fill
            filled_volume = result.volume
            if filled_volume < volume:
                logger.warning(
                    "Partial fill detected",
                    extra={
                        "ticket": result.ticket,
                        "symbol": plan.symbol,
                        "requested_lots": volume,
                        "filled_lots": filled_volume,
                        "shortfall_lots": round(volume - filled_volume, 2),
                    },
                )
                metrics.increment("orders.partial_fills")

            # [1] Post-fill slippage — as a fraction of this trade's stop distance.
            # slippage_pct_of_stop = |fill - planned_entry| / |planned_entry - SL|
            # A 0.20 reading means the fill ate 20% of the budgeted risk before the
            # trade even started. Threshold from config (e.g. 0.20 = 20%).
            raw_slippage = abs(result.executed_price - plan.entry_price)
            slippage_pct_of_stop = (
                (raw_slippage / stop_distance) if stop_distance > 0 else 0.0
            )
            is_worse = not _is_better_price(
                plan.side, result.executed_price, plan.entry_price
            )

            if raw_slippage > 0:
                direction = "worse" if is_worse else "better"
                if is_worse and slippage_pct_of_stop > max_slip_pct_of_stop:
                    logger.warning(
                        "Fill slippage exceeds limit — closing position",
                        extra={
                            "symbol": plan.symbol,
                            "slippage_pct_of_stop": round(
                                slippage_pct_of_stop * 100, 1
                            ),
                            "max_pct_of_stop": round(max_slip_pct_of_stop * 100, 1),
                            "stop_distance": round(stop_distance, 5),
                            "direction": direction,
                            "ticket": result.ticket,
                        },
                    )
                    metrics.increment("orders.slippage_rejected")
                    if self._cfg.close_on_slippage_exceed:
                        self._emergency_close(
                            result.ticket,
                            plan,
                            order_type,
                            result.executed_price,
                            symbol_info,
                            filled_volume,
                        )
                        raise RuntimeError(
                            f"Slippage {slippage_pct_of_stop * 100:.1f}% of stop distance "
                            f"exceeds limit {max_slip_pct_of_stop * 100:.1f}% "
                            f"({direction}) — position closed"
                        )
                    else:
                        logger.warning(
                            "Fill slippage exceeds limit — accepting fill (CLOSE_ON_SLIPPAGE_EXCEED=false)",
                            extra={
                                "symbol": plan.symbol,
                                "slippage_pct_of_stop": round(
                                    slippage_pct_of_stop * 100, 1
                                ),
                                "max_pct_of_stop": round(max_slip_pct_of_stop * 100, 1),
                                "ticket": result.ticket,
                            },
                        )
                logger.info(
                    "Fill slippage within limit",
                    extra={
                        "symbol": plan.symbol,
                        "slippage_pct_of_stop": round(slippage_pct_of_stop * 100, 1),
                        "max_pct_of_stop": round(max_slip_pct_of_stop * 100, 1),
                        "direction": direction,
                    },
                )

            metrics.increment("orders.filled")
            return result.ticket, result.executed_price, filled_volume

        raise last_error or RuntimeError("Order failed after all retries")

    def modify_position_levels(self, ticket: int, sl: float, tp: float) -> None:
        """Update SL and TP on an already-filled position."""
        self._orders.modify_position(ticket=ticket, sl=sl, tp=tp)

    # ── Emergency close ───────────────────────────────────────────────────────

    def _emergency_close(
        self,
        ticket: int,
        plan: TradePlan,
        order_type: int,
        price: float,
        symbol_info: SymbolInfo,
        filled_volume: float,
    ) -> None:
        try:
            tick = self._positions.get_current_tick(plan.symbol)
            close_price = (
                (tick.bid if plan.side == OrderSide.BUY else tick.ask)
                if tick
                else price
            )
            self._orders.close_position(
                ticket=ticket,
                symbol=plan.symbol,
                side=order_type,
                volume=normalise_lots(
                    filled_volume,
                    symbol_info.volume_step,
                    symbol_info.volume_min,
                    symbol_info.volume_max,
                ),
                price=close_price,
                slippage=self._cfg.slippage,
                magic=self._cfg.magic,
                comment=f"slippage-close {self._cfg.comment}".strip(),
                filling_mode=symbol_info.order_filling_mode,
            )
            logger.info("Emergency close executed", extra={"ticket": ticket})
            metrics.increment("orders.emergency_closes")
        except Exception:
            logger.exception(
                "Emergency close FAILED — manual intervention required",
                extra={"ticket": ticket, "symbol": plan.symbol},
            )
            raise


# ── Helpers ───────────────────────────────────────────────────────────────────


def _widen_stops(
    side: OrderSide,
    entry: float,
    sl: float,
    tp: float,
    min_dist: float,
) -> tuple[float, float]:
    """
    Ensure SL and TP are at least min_dist away from entry.
    Moves them outward — never inward — to preserve trade direction.
    """
    if side == OrderSide.BUY:
        new_sl = min(sl, entry - min_dist)  # SL must be below entry
        new_tp = max(tp, entry + min_dist)  # TP must be above entry
    else:
        new_sl = max(sl, entry + min_dist)  # SL must be above entry
        new_tp = min(tp, entry - min_dist)  # TP must be below entry
    return new_sl, new_tp


def _extract_retcode(exc: RuntimeError) -> int:
    for part in str(exc).split():
        part = part.rstrip(")")
        if part.startswith("retcode="):
            try:
                return int(part.split("=")[1])
            except ValueError:
                pass
    return -1


def _is_better_price(side: OrderSide, executed: float, planned: float) -> bool:
    if side == OrderSide.BUY:
        return executed < planned
    return executed > planned
