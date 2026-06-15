"""
MT5 account and position queries.
Each public method calls client.ensure_connected() first so the system
recovers automatically if the terminal was restarted.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from src.brokers.mt5.client import Mt5Client, _MT5_LOCK
from src.brokers.mt5.types import Mt5PositionType
from src.domain.position import AccountInfo, Position, PositionSide, SymbolInfo
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


class Mt5Positions:
    def __init__(self, client: Mt5Client) -> None:
        self._client = client

    @property
    def _mt5(self):
        return self._client.mt5

    def _history_deals_get(self, from_dt: datetime, to_dt: datetime) -> list:
        """Call MT5 history_deals_get with one reconnect retry.

        The MetaTrader5 Python extension can occasionally raise SystemError
        with "returned a result with an exception set". Treat that as a broker
        API failure, reconnect once, and surface the MT5 last_error payload.
        """
        last_failure: RuntimeError | None = None

        for attempt in range(1, 3):
            self._client.ensure_connected()
            with _MT5_LOCK:
                try:
                    deals = self._mt5.history_deals_get(from_dt, to_dt)
                    if deals is None:
                        error = self._mt5.last_error()
                        raise RuntimeError(f"history_deals_get returned None: {error}")
                    return list(deals)
                except Exception as exc:
                    error = self._mt5.last_error()
                    last_failure = RuntimeError(
                        f"history_deals_get failed: {error}; exception={exc!r}"
                    )

            logger.warning(
                "history_deals_get failed on attempt %d/2: %s",
                attempt,
                last_failure,
                extra={"from_dt": from_dt, "to_dt": to_dt},
            )
            if attempt == 1:
                self._client.disconnect()

        assert last_failure is not None
        raise last_failure

    # ── Account ───────────────────────────────────────────────────────────

    def get_account_info(self) -> AccountInfo:
        self._client.ensure_connected()
        with _MT5_LOCK:
            info = self._mt5.account_info()
        if info is None:
            with _MT5_LOCK:
                error = self._mt5.last_error()
            raise RuntimeError(f"account_info() failed: {error}")

        return AccountInfo(
            login=info.login,
            server=info.server,
            currency=info.currency,
            balance=info.balance,
            equity=info.equity,
            margin=info.margin,
            free_margin=info.margin_free,
            margin_level=info.margin_level,
            leverage=info.leverage,
        )

    # ── Symbol ────────────────────────────────────────────────────────────

    def resolve_symbol(self, symbol: str) -> Optional[str]:
        return self._client.resolve_symbol(symbol)

    def get_symbol_info(self, symbol: str) -> SymbolInfo:
        _resolved_symbol = self.resolve_symbol(symbol)
        if not _resolved_symbol:
            raise RuntimeError(f"Unable to resolve and select symbol {symbol!r}")
        self._client.ensure_connected()
        with _MT5_LOCK:
            info = self._mt5.symbol_info(_resolved_symbol)
        if info is None:
            with _MT5_LOCK:
                error = self._mt5.last_error()
            raise RuntimeError(f"symbol_info({symbol!r}) failed: {error}")

        if not info.visible:
            with _MT5_LOCK:
                selected = self._mt5.symbol_select(_resolved_symbol, True)
                if not selected:
                    error = self._mt5.last_error()
                    raise RuntimeError(f"symbol_select({_resolved_symbol!r}) failed: {error}")
                info = self._mt5.symbol_info(_resolved_symbol)

        with _MT5_LOCK:
            tick = self._mt5.symbol_info_tick(_resolved_symbol)
        ask = tick.ask if tick else 0.0
        bid = tick.bid if tick else 0.0
        if tick is None or ask <= 0 or bid <= 0:
            raise RuntimeError(f"No valid live tick for {_resolved_symbol!r}")

        return SymbolInfo(
            # Identity
            symbol=info.name,
            description=info.description,
            currency_base=info.currency_base,
            currency_profit=info.currency_profit,
            currency_margin=info.currency_margin,
            # Price precision
            digits=info.digits,
            point=info.point,
            tick_size=info.trade_tick_size,
            tick_value=info.trade_tick_value,
            # Contract
            contract_size=info.trade_contract_size,
            lot_min=info.volume_min,
            lot_max=info.volume_max,
            lot_step=info.volume_step,
            # Quote
            ask=ask,
            bid=bid,
            spread=info.spread,
            spread_float=bool(info.spread_float),
            # Margin
            margin_initial=info.margin_initial,
            margin_maintenance=info.margin_maintenance,
            margin_hedged=info.margin_hedged,
            # Execution
            filling_mode=info.filling_mode,
            execution_mode=info.trade_exemode,
            trade_mode=info.trade_mode,
            # Swap
            swap_mode=info.swap_mode,
            swap_long=info.swap_long,
            swap_short=info.swap_short,
            swap_rollover3days=info.swap_rollover3days,
            # Stops
            stops_level=info.trade_stops_level,
            freeze_level=info.trade_freeze_level,
            # Volume (redundant with lot_* but kept for clarity)
            volume_min=info.volume_min,
            volume_max=info.volume_max,
            volume_step=info.volume_step,
            # Optional
            expiration_mode=info.expiration_mode,
            order_mode=info.order_mode,
        )

    # ── Positions ─────────────────────────────────────────────────────────

    def get_open_positions(self, magic: Optional[int] = None) -> List[Position]:
        self._client.ensure_connected()
        with _MT5_LOCK:
            raw = self._mt5.positions_get() or []
        if magic is not None:
            raw = [p for p in raw if p.magic == magic]

        return [
            Position(
                ticket=p.ticket,
                symbol=p.symbol,
                side=(
                    PositionSide.BUY
                    if p.type == Mt5PositionType.BUY
                    else PositionSide.SELL
                ),
                lots=p.volume,
                open_price=p.price_open,
                current_price=p.price_current,
                stop_loss=p.sl,
                take_profit=p.tp,
                swap=p.swap,
                profit=p.profit,
                open_time=int(p.time * 1000),
                comment=p.comment,
                magic=p.magic,
            )
            for p in raw
        ]

    def get_position_by_ticket(self, ticket: int) -> Optional[Position]:
        positions = self.get_open_positions()
        return next((p for p in positions if p.ticket == ticket), None)

    def get_daily_pnl_info(self, magic: int) -> tuple[float, float, float]:
        """Return (loss_pct, start_of_day_equity) for today.

        loss_pct is today's loss as a percentage of start-of-day equity.
        start_of_day_equity is the equity at session open — used by
        LossTracker to compute the daily risk budget for position sizing.

        Why equity, not balance?
          Prop-firm rules and sound risk management are expressed against the
          equity you had at the start of the day, not the running balance.
          Using balance understates the loss when floating drawdown is large;
          using equity captures both realised and unrealised losses instantly.

        Calculation:
          1. Sum today's closed P&L (profit + swap + commission) for our magic
             number  →  realised component.
          2. Sum floating (unrealised) P&L on open positions for our magic
             number  →  unrealised component.
          3. start_equity = current_equity − realised_pnl − unrealised_pnl
             (what equity was before any trade today ran)
          4. total_pnl    = realised_pnl + unrealised_pnl  (negative = loss)
          5. loss_pct     = abs(total_pnl) / start_equity × 100

        Returns (0.0, start_equity) when the day is net positive or flat.
        Returns (0.0, 0.0) on data failure.
        """
        self._client.ensure_connected()
        try:
            # ── Time window: broker-local midnight → next midnight ─────────
            broker_tz = ZoneInfo(self._client._config.broker_timezone)
            now_utc = datetime.now(timezone.utc)
            now_broker = now_utc.astimezone(broker_tz).replace(tzinfo=None)
            from_dt      = datetime(now_broker.year, now_broker.month, now_broker.day)
            to_dt        = from_dt + timedelta(days=1)

            # ── Closed P&L for our magic (realised) ───────────────────────
            deals = self._history_deals_get(from_dt, to_dt)
            realised_pnl = sum(
                d.profit + d.swap + d.commission
                for d in deals
                if d.entry == self._client.mt5.DEAL_ENTRY_OUT
            )

            # ── Floating P&L on currently open positions (unrealised) ─────
            with _MT5_LOCK:
                open_positions = self._mt5.positions_get() or []
            unrealised_pnl = sum(p.profit for p in open_positions)

            total_pnl = realised_pnl + unrealised_pnl

            # ── Account equity ─────────────────────────────────────────────
            with _MT5_LOCK:
                account = self._mt5.account_info()
            if not account or account.equity <= 0:
                logger.warning("get_daily_pnl_info: no valid account or equity <= 0")
                raise RuntimeError("No valid account equity for daily PnL calculation")

            current_equity = account.equity

            # start_equity = current_equity − total_pnl
            start_equity = current_equity - total_pnl
            if start_equity <= 0:
                logger.warning(
                    "get_daily_pnl_info: derived start_equity <= 0 "
                    "(equity=%.2f total_pnl=%.2f) — returning 0",
                    current_equity, total_pnl,
                )
                raise RuntimeError("Derived start equity is non-positive")

            # No loss today — return early
            if total_pnl >= 0:
                return 0.0, start_equity, current_equity

            loss_pct = (abs(total_pnl) / start_equity) * 100.0
            logger.debug(
                "daily_loss_pct=%.3f%%  realised=%.2f  unrealised=%.2f  "
                "start_equity=%.2f  current_equity=%.2f",
                loss_pct, realised_pnl, unrealised_pnl,
                start_equity, current_equity,
            )
            return loss_pct, start_equity, current_equity

        except Exception as e:
            with _MT5_LOCK:
                error = self._mt5.last_error()
            logger.warning(
                "Mt5Positions.get_daily_pnl_info failed: %s",
                e,
                extra={
                    "mt5_last_error": error,
                    "from_dt": locals().get("from_dt"),
                    "to_dt": locals().get("to_dt"),
                    "broker_utc_offset_hours": self._client.broker_utc_offset_hours,
                    "broker_timezone": self._client._config.broker_timezone,
                },
            )
            raise RuntimeError(f"Daily PnL calculation unavailable: {e}") from e

    def get_current_tick(self, symbol: str):
        self._client.ensure_connected()
        with _MT5_LOCK:
            tick = self._mt5.symbol_info_tick(symbol)
        if tick is None or tick.ask <= 0 or tick.bid <= 0:
            raise RuntimeError(f"No valid live tick for {symbol!r}")
        return tick

    def get_deal_price_for_ticket(self, ticket: int) -> Optional[float]:
        """
        Look up the close price of a deal by its position ticket in MT5 history.

        Used as a fallback in position_manager when _last_price has no entry
        for a ticket (e.g. the engine restarted while the position was open and
        the broker closed it during the downtime).  Returns None if no matching
        OUT deal is found.
        """
        self._client.ensure_connected()
        try:
            # Search the last 7 days — wide enough to catch any recent close.
            to_dt   = datetime.now(timezone.utc).replace(tzinfo=None)
            from_dt = to_dt - timedelta(days=7)

            deals = self._history_deals_get(from_dt, to_dt)
            for d in deals:
                # position_id on a deal == the ticket of the position it closed.
                if (
                    getattr(d, "position_id", None) == ticket
                    and d.entry == self._client.mt5.DEAL_ENTRY_OUT
                ):
                    return float(d.price)
            return None
        except Exception as exc:
            logger.warning("Mt5Positions.get_deal_price_for_ticket ticket=%s: %s", ticket, exc)
            return None
