"""
MT5 order execution.
Each method calls client.ensure_connected() first so the system
recovers automatically if the terminal was restarted.
"""

from __future__ import annotations

import logging

from src.brokers.mt5.client import Mt5Client, _MT5_LOCK
from src.brokers.mt5.types import (
    Mt5TradeAction,
    Mt5OrderType,
    MT5_RETCODE_DONE,
    MT5_RETCODE_PLACED,
    OrderResult,
    ModifyResult,
)
from src.infra.metrics import metrics

logger = logging.getLogger(__name__)


class Mt5Orders:
    def __init__(self, client: Mt5Client) -> None:
        self._client = client

    @property
    def _mt5(self):
        return self._client.mt5

    # ── Market order ──────────────────────────────────────────────────────

    def open_market_order(
        self,
        symbol: str,
        order_type: int,
        volume: float,
        price: float,
        sl: float,
        tp: float,
        slippage: int,
        magic: int,
        comment: str,
        filling_mode: int,
    ) -> OrderResult:
        self._client.ensure_connected()

        request = {
            "action": Mt5TradeAction.DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": order_type,
            "price": price,
            "sl": sl,
            "tp": tp,
            "deviation": slippage,
            "magic": magic,
            "comment": comment,
            "type_filling": filling_mode,
        }

        logger.info(
            "Sending market order",
            extra={
                "symbol": symbol,
                "type": "BUY" if order_type == Mt5OrderType.BUY else "SELL",
                "volume": volume,
                "sl": sl,
                "tp": tp,
            },
        )

        with _MT5_LOCK:
            result = self._mt5.order_send(request)
            if result is None:
                error = self._mt5.last_error()

        if result is None:
            raise RuntimeError(f"order_send returned None — MT5 error: {error}")

        if result.retcode == MT5_RETCODE_PLACED:
            raise RuntimeError(
                "order_send placed but not filled; broker confirmation required"
            )
        if result.retcode != MT5_RETCODE_DONE:
            raise RuntimeError(
                f"order_send failed: retcode={result.retcode} comment={result.comment}"
            )

        logger.info(
            "Market order executed",
            extra={
                "ticket": result.order,
                "price": result.price,
                "volume": result.volume,
            },
        )
        metrics.increment("mt5.orders.opened")
        self._confirm_open(result.order, result.volume)

        return OrderResult(
            ticket=result.order,
            executed_price=result.price,
            volume=result.volume,
            retcode=result.retcode,
            comment=result.comment,
        )

    # ── Modify SL/TP ─────────────────────────────────────────────────────

    def modify_position(self, ticket: int, sl: float, tp: float) -> ModifyResult:
        self._client.ensure_connected()

        request = {
            "action": Mt5TradeAction.SLTP,
            "position": ticket,
            "sl": sl,
            "tp": tp,
        }

        with _MT5_LOCK:
            result = self._mt5.order_send(request)
            if result is None:
                error = self._mt5.last_error()

        if result is None:
            raise RuntimeError(f"modify_position returned None: {error}")

        if result.retcode != MT5_RETCODE_DONE:
            raise RuntimeError(
                f"modify_position failed: retcode={result.retcode} comment={result.comment}"
            )

        logger.info("Position modified", extra={"ticket": ticket, "sl": sl, "tp": tp})
        metrics.increment("mt5.orders.modified")
        return ModifyResult(retcode=result.retcode, comment=result.comment)

    # ── Close ─────────────────────────────────────────────────────────────

    def close_position(
        self,
        ticket: int,
        symbol: str,
        side: int,
        volume: float,
        price: float,
        slippage: int,
        magic: int,
        comment: str,
        filling_mode: int,
    ) -> OrderResult:
        self._client.ensure_connected()

        close_type = Mt5OrderType.SELL if side == Mt5OrderType.BUY else Mt5OrderType.BUY

        request = {
            "action": Mt5TradeAction.DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": close_type,
            "position": ticket,
            "price": price,
            "deviation": slippage,
            "magic": magic,
            "comment": comment,
            "type_filling": filling_mode,
        }

        with _MT5_LOCK:
            before = self._mt5.positions_get(ticket=ticket)
            result = self._mt5.order_send(request)
            if result is None:
                error = self._mt5.last_error()

        if result is None:
            raise RuntimeError(f"close_position returned None: {error}")

        if result.retcode == MT5_RETCODE_PLACED:
            raise RuntimeError(
                "close_position placed but not completed; broker confirmation required"
            )
        if result.retcode != MT5_RETCODE_DONE:
            raise RuntimeError(
                f"close_position failed: retcode={result.retcode} comment={result.comment}"
            )

        logger.info(
            "Position closed",
            extra={"ticket": ticket, "volume": volume, "price": result.price},
        )
        metrics.increment("mt5.orders.closed")
        self._confirm_close(ticket, volume, before)

        return OrderResult(
            ticket=result.order,
            executed_price=result.price,
            volume=result.volume,
            retcode=result.retcode,
            comment=result.comment,
        )

    def _confirm_open(self, ticket: int, filled_volume: float) -> None:
        with _MT5_LOCK:
            positions = self._mt5.positions_get(ticket=ticket)
            error = self._mt5.last_error() if positions is None else None
        if positions is None:
            raise RuntimeError(f"Cannot confirm opened position {ticket}: {error}")
        if not positions or float(positions[0].volume) + 1e-9 < filled_volume:
            raise RuntimeError(
                f"Broker did not confirm opened position {ticket} volume {filled_volume}"
            )

    def _confirm_close(self, ticket: int, closed_volume: float, before) -> None:
        if before is None:
            raise RuntimeError(f"Cannot confirm pre-close position state for {ticket}")
        before_volume = float(before[0].volume) if before else 0.0
        with _MT5_LOCK:
            after = self._mt5.positions_get(ticket=ticket)
            error = self._mt5.last_error() if after is None else None
        if after is None:
            raise RuntimeError(f"Cannot confirm closed position {ticket}: {error}")
        after_volume = float(after[0].volume) if after else 0.0
        expected_max = max(0.0, before_volume - closed_volume)
        if after_volume > expected_max + 1e-9:
            raise RuntimeError(
                f"Broker position {ticket} volume did not decrease as requested "
                f"({before_volume} -> {after_volume}, requested {closed_volume})"
            )
