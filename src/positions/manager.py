"""
Polls MT5 positions on a timer to manage the full trade lifecycle:

  - Startup hydration from MT5 + saved trade records
  - Partial close at TP1 + optional SL to breakeven
  - Full close at TP2
  - Detect SL/manual closes (position absent from MT5)
  - Persist plan updates, delete on close
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from src.brokers.mt5.orders import Mt5Orders
from src.brokers.mt5.positions import Mt5Positions
from src.brokers.mt5.types import Mt5OrderType
from src.config.settings import ExecutionConfig
from src.core.event_bus import EventBus
from src.core.events import Events
from src.infra.metrics import metrics
from .store import PositionStore
from src.infra.database import TradeRepository
from src.domain.position import PositionSide
from src.domain.trade import CloseReason, Trade, TradeStatus
from src.utils.time import now_ms

logger = logging.getLogger(__name__)


class PositionManager:
    def __init__(
        self,
        store: PositionStore,
        mt5_pos: Mt5Positions,
        mt5_orders: Mt5Orders,
        repository: TradeRepository,
        execution_engine,  # ExecutionEngine — avoid circular import
        event_bus: EventBus,
        exec_config: ExecutionConfig,
        poll_interval: float = 5.0,
    ) -> None:
        self._store = store
        self._mt5_pos = mt5_pos
        self._mt5_orders = mt5_orders
        self._repo = repository
        self._execution_engine = execution_engine
        self._bus = event_bus
        self._cfg = exec_config
        self._poll_interval = poll_interval
        self._stopped = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._stub_miss_count: dict[str, int] = {}  # trade_id → consecutive poll misses
        self._stub_miss_limit = 3  # evict stub after this many consecutive misses
        # Last price seen per ticket — used to classify close reason on disappearance
        self._last_price: dict[int, float] = {}  # entry_ticket → current_price

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._stopped.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="position-manager",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "PositionManager started", extra={"poll_interval": self._poll_interval}
        )

    def stop(self) -> None:
        self._stopped.set()
        if self._thread:
            self._thread.join(timeout=10)

    # ── Emergency stop ────────────────────────────────────────────────────────

    def emergency_close_all(self) -> None:
        """
        Immediately close every open position tracked in the store at market
        price.  Intended for the remote ``command.emergency_stop`` handler.

        Each position is closed in sequence. Individual failures are logged but
        do not abort the loop — we attempt to close every position regardless.
        Raises `RuntimeError` only if the broker is unreachable.

        Note: the caller is responsible for pausing the signal queue to prevent
        new positions from opening while this runs.
        """
        try:
            broker_positions = self._mt5_pos.get_open_positions(self._cfg.magic)
        except Exception as exc:
            raise RuntimeError(
                f"Emergency stop: cannot fetch open positions from MT5 — {exc}"
            ) from exc

        if not broker_positions:
            logger.info("Emergency stop: no open positions to close")
            return

        logger.warning(
            "Emergency stop: closing %d open position(s)", len(broker_positions)
        )
        errors: list[str] = []
        for pos in broker_positions:
            try:
                symbol_info = self._mt5_pos.get_symbol_info(pos.symbol)
                tick = self._mt5_pos.get_current_tick(pos.symbol)
                if tick is None:
                    raise RuntimeError(f"No tick data for {pos.symbol}")

                is_buy = pos.side == PositionSide.BUY
                close_price = tick.bid if is_buy else tick.ask
                # Pass the original position side — close_position() flips it internally
                order_type = Mt5OrderType.BUY if is_buy else Mt5OrderType.SELL

                self._mt5_orders.close_position(
                    ticket=pos.ticket,
                    symbol=pos.symbol,
                    side=order_type,
                    volume=pos.lots,
                    price=close_price,
                    slippage=self._cfg.slippage,
                    magic=self._cfg.magic,
                    comment="emergency-stop",
                    filling_mode=symbol_info.order_filling_mode,
                )
                logger.warning(
                    "Emergency stop: closed ticket=%d %s %.2f lots @ %.5f",
                    pos.ticket,
                    pos.symbol,
                    pos.lots,
                    close_price,
                )
                metrics.increment("emergency_stop.positions_closed")
            except Exception as exc:
                msg = f"Emergency stop: failed to close ticket={pos.ticket} {pos.symbol}: {exc}"
                logger.exception(msg)
                errors.append(msg)

        if errors:
            raise RuntimeError(
                f"Emergency stop completed with {len(errors)} error(s): "
                + "; ".join(errors)
            )

    # ── Startup hydration ─────────────────────────────────────────────────────

    def hydrate_from_broker(self) -> None:
        """
        Called once at startup. Fetches open positions from MT5 and populates
        the in-memory store.

        MT5 is the source of truth for what is open. Saved trade records in
        data/trades/ are merged in to restore plan data (signal_id, tp1/tp2
        levels, lot split) that MT5 doesn't store. Positions with no saved
        record get a minimal stub — they are tracked but TP1 partial close
        is unavailable for them.

        The first _poll() cycle handles anything closed while the engine was
        down — no separate reconciliation step needed.
        """
        try:
            broker_positions = self._mt5_pos.get_open_positions(self._cfg.magic)
        except Exception:
            logger.warning(
                "PositionManager.hydrate_from_broker: cannot fetch MT5 positions — "
                "store will be empty, first poll will populate it"
            )
            return

        if not broker_positions:
            logger.info("PositionManager.hydrate_from_broker: no open positions in MT5")
            return

        # Load saved records to restore plan data MT5 doesn't store.
        # Index by entry_ticket only — one ticket per trade now.
        saved_by_ticket: dict[int, Trade] = {}
        try:
            for t in self._repo.load_open_trades():
                if t.entry_ticket is not None:
                    saved_by_ticket[t.entry_ticket] = t
        except Exception:
            logger.warning(
                "PositionManager.hydrate_from_broker: could not read saved records — "
                "using broker data only, tp1 BE management unavailable for existing positions"
            )

        trades: list[Trade] = []
        for pos in broker_positions:
            if pos.ticket in saved_by_ticket:
                trade = saved_by_ticket[pos.ticket]
                trade.current_lots = pos.lots
                trade.stop_loss = pos.stop_loss
            else:
                trade = _trade_stub_from_position(pos)
                self._repo.save(trade)
                logger.warning(
                    "PositionManager.hydrate_from_broker: no saved record for ticket=%d "
                    "symbol=%s — stub created and saved",
                    pos.ticket,
                    pos.symbol,
                )
            trades.append(trade)

        self._store.hydrate(trades)
        logger.info(
            "PositionManager.hydrate_from_broker complete",
            extra={
                "positions_from_mt5": len(broker_positions),
                "matched_to_records": len(saved_by_ticket),
                "stubs_created": len(broker_positions) - len(saved_by_ticket),
                "store_size": len(self._store.get_open_trades()),
            },
        )

    # ── Poll loop ─────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stopped.is_set():
            try:
                self._poll()
            except Exception:
                logger.exception("PositionManager: unhandled error in poll")
            self._stopped.wait(timeout=self._poll_interval)

    def _poll(self) -> None:
        # Refresh daily loss cache on every poll cycle
        try:
            loss_pct, start_equity, current_equity = self._mt5_pos.get_daily_pnl_info(self._cfg.magic)
            self._execution_engine.update_daily_loss(loss_pct, start_equity, current_equity)
        except Exception:
            logger.warning("PositionManager: failed to refresh daily loss pct")

        try:
            broker_positions = self._mt5_pos.get_open_positions(self._cfg.magic)
        except Exception:
            logger.warning("PositionManager: failed to fetch broker positions")
            return

        broker_tickets = {p.ticket for p in broker_positions}
        broker_by_ticket = {p.ticket: p for p in broker_positions}
        store_trades = self._store.get_open_trades()

        # All tickets we track (one per trade)
        tracked_tickets: set[int] = set()
        for t in store_trades:
            if t.entry_ticket:
                tracked_tickets.add(t.entry_ticket)

        # ── Reconcile: positions in MT5 but missing from store ────────────
        for pos in broker_positions:
            if pos.ticket not in tracked_tickets:
                saved = self._repo.load_by_ticket(pos.ticket)
                trade = saved if saved else _trade_stub_from_position(pos)
                if not saved:
                    self._repo.save(trade)
                    logger.warning(
                        "PositionManager._poll: ticket=%d %s not in store — stub added",
                        pos.ticket,
                        pos.symbol,
                    )
                if not self._store.get_by_ticket(pos.ticket):
                    self._store.add(trade)

        # ── Refresh store_trades after reconcile ──────────────────────────
        open_trades = self._store.get_open_trades()

        # ── Lifecycle management ──────────────────────────────────────────
        for trade in open_trades:
            if trade.entry_ticket is None:
                continue

            is_stub = trade.id.startswith("STUB_")

            # ── Position absent from broker ───────────────────────────────
            if trade.entry_ticket not in broker_tickets:
                if is_stub:
                    misses = self._stub_miss_count.get(trade.id, 0) + 1
                    self._stub_miss_count[trade.id] = misses
                    if misses < self._stub_miss_limit:
                        logger.warning(
                            "PositionManager: STUB ticket=%s not in broker — miss %d/%d",
                            trade.entry_ticket,
                            misses,
                            self._stub_miss_limit,
                        )
                        continue
                    self._stub_miss_count.pop(trade.id, None)
                self._handle_position_gone(trade)
                continue

            # ── Position still open ───────────────────────────────────────
            self._stub_miss_count.pop(trade.id, None)
            current_price = broker_by_ticket[trade.entry_ticket].current_price
            self._last_price[trade.entry_ticket] = current_price

            # ── Poll-based TP1 detection: move SL to BE when price hits TP1 ─
            if not trade.tp1_hit and not is_stub and trade.tp1:
                is_buy = trade.side.value == "BUY"
                tp1_reached = (
                    current_price >= trade.tp1 if is_buy else current_price <= trade.tp1
                )
                if tp1_reached:
                    self._handle_tp1_price_reached(trade)

    # ── Trade lifecycle handlers ──────────────────────────────────────────────

    def _handle_tp1_price_reached(self, trade: Trade) -> None:
        """
        Poll detected that current price has crossed the TP1 level.

        Step 1 — Partial close: if tp1_lots > 0, close that volume at market.
        Step 2 — BE move: if MOVE_SL_TO_BE_ON_TP1 and the partial close
                 succeeded, move the broker SL to entry so the remaining
                 position runs to TP2 risk-free.

        Moving SL to breakeven is only meaningful after taking profits —
        the two actions are intentionally coupled in that order.
        """
        logger.info(
            "TP1 price reached — processing partial close",
            extra={
                "trade_id": trade.id,
                "symbol": trade.symbol,
                "ticket": trade.entry_ticket,
                "tp1": trade.tp1,
                "tp1_lots": trade.tp1_lots,
                "current_lots": trade.current_lots,
            },
        )

        # ── Step 1: Partial close ─────────────────────────────────────────
        partial_closed = False
        tp1_close_price: float | None = None
        symbol_info = None
        tick = None

        if trade.tp1_lots > 0 and trade.entry_ticket:
            try:
                symbol_info = self._mt5_pos.get_symbol_info(trade.symbol)
                tick = self._mt5_pos.get_current_tick(trade.symbol)
                if tick is None:
                    raise RuntimeError(f"Cannot get tick for {trade.symbol}")

                is_buy = trade.side.value == "BUY"
                close_price = tick.bid if is_buy else tick.ask
                order_type = Mt5OrderType.BUY if is_buy else Mt5OrderType.SELL

                self._mt5_orders.close_position(
                    ticket=trade.entry_ticket,
                    symbol=trade.symbol,
                    side=order_type,
                    volume=trade.tp1_lots,
                    price=close_price,
                    slippage=self._cfg.slippage,
                    magic=self._cfg.magic,
                    comment="tp1-partial",
                    filling_mode=symbol_info.order_filling_mode,
                )
                tp1_close_price = close_price
                partial_closed = True
                logger.info(
                    "TP1 partial close executed",
                    extra={
                        "trade_id": trade.id,
                        "ticket": trade.entry_ticket,
                        "tp1_lots_closed": trade.tp1_lots,
                        "close_price": close_price,
                        "remaining_lots": round(trade.current_lots - trade.tp1_lots, 2),
                    },
                )
                metrics.increment("trades.tp1_partial_close")
            except Exception:
                logger.exception(
                    "PositionManager: TP1 partial close failed — SL not moved",
                    extra={"trade_id": trade.id, "ticket": trade.entry_ticket},
                )

        # ── Step 2: Move SL to breakeven (only after a successful partial close) ──
        be_ok = False
        if self._cfg.move_sl_to_be_on_tp1 and trade.entry_ticket:
            try:
                if symbol_info is None:
                    symbol_info = self._mt5_pos.get_symbol_info(trade.symbol)
                if tick is None:
                    tick = self._mt5_pos.get_current_tick(trade.symbol)
                if tick is None:
                    raise RuntimeError(f"Cannot get tick for {trade.symbol}")

                be_sl = _valid_breakeven_sl(
                    trade,
                    symbol_info,
                    tick,
                    self._cfg.breakeven_spread_multiplier,
                    self._cfg.breakeven_max_buffer_pct_of_risk,
                )
                if be_sl is None:
                    if not partial_closed:
                        logger.warning(
                            "PositionManager: BE move deferred until broker stop distance allows it",
                            extra={
                                "trade_id": trade.id,
                                "ticket": trade.entry_ticket,
                                "symbol": trade.symbol,
                                "entry_price": trade.entry_price,
                                "breakeven_spread_multiplier": self._cfg.breakeven_spread_multiplier,
                                "breakeven_max_buffer_pct_of_risk": self._cfg.breakeven_max_buffer_pct_of_risk,
                                "bid": getattr(tick, "bid", None),
                                "ask": getattr(tick, "ask", None),
                                "stops_level": symbol_info.stops_level,
                                "freeze_level": symbol_info.freeze_level,
                                "point": symbol_info.point,
                            },
                        )
                        return
                    raise RuntimeError(
                        "Breakeven SL violates broker stop/freeze distance"
                    )

                self._mt5_orders.modify_position(
                    ticket=trade.entry_ticket,
                    sl=be_sl,
                    tp=trade.tp2,
                )
                be_ok = True
                logger.info(
                    "SL moved to breakeven after TP1 partial close",
                    extra={
                        "trade_id": trade.id,
                        "ticket": trade.entry_ticket,
                        "be_price": be_sl,
                        "breakeven_spread_multiplier": self._cfg.breakeven_spread_multiplier,
                        "breakeven_max_buffer_pct_of_risk": self._cfg.breakeven_max_buffer_pct_of_risk,
                    },
                )
            except Exception:
                logger.exception(
                    "PositionManager: BE move failed — SL stays at original",
                    extra={"trade_id": trade.id, "ticket": trade.entry_ticket},
                )

        # ── Step 3: Update in-memory store and persist ────────────────────
        new_sl = be_sl if be_ok else trade.stop_loss
        new_current_lots = (
            round(trade.current_lots - trade.tp1_lots, 2)
            if partial_closed
            else trade.current_lots
        )
        from src.domain.trade import TradeStatus as _TS

        new_status = _TS.PARTIALLY_CLOSED if partial_closed else trade.status

        updated = self._store.update(
            trade.id,
            tp1_hit=True,
            tp1_hit_at=now_ms(),
            tp1_close_price=tp1_close_price,
            current_lots=new_current_lots,
            status=new_status,
            stop_loss=new_sl,
        )
        if updated:
            self._repo.save(updated)
            self._bus.emit(Events.TRADE_TP1_HIT, updated)
            metrics.increment("trades.tp1_hit")

    def _handle_position_gone(self, trade: Trade) -> None:
        """
        The single broker position has disappeared.
        Classify close reason using last known price vs TP2 and SL levels.
        """
        if trade.tp2_hit:
            return

        is_stub = trade.id.startswith("STUB_")

        if is_stub:
            close_reason = CloseReason.CLOSED_WHILE_DOWN
            tp2_hit = False
            sl_hit = False
            realized_rr = 0.0
            close_price = trade.entry_price or 0.0
        else:
            last_price = self._last_price.pop(trade.entry_ticket, None)
            if last_price is None:
                deal_price = self._mt5_pos.get_deal_price_for_ticket(trade.entry_ticket)
                if deal_price is not None:
                    last_price = deal_price
                    logger.info(
                        "PositionManager._handle_position_gone: resolved close price "
                        "from deal history  ticket=%s  price=%s",
                        trade.entry_ticket,
                        deal_price,
                    )
                else:
                    logger.warning(
                        "PositionManager._handle_position_gone: no price or deal history "
                        "for ticket=%s — defaulting to SL_HIT; verify manually",
                        trade.entry_ticket,
                    )

            is_buy = trade.side.value == "BUY"
            if last_price is not None and trade.tp2:
                tp2_hit = (
                    (last_price >= trade.tp2) if is_buy else (last_price <= trade.tp2)
                )
                sl_hit = (
                    (last_price <= trade.stop_loss)
                    if is_buy
                    else (last_price >= trade.stop_loss)
                )
            else:
                tp2_hit = False
                sl_hit = True

            if tp2_hit:
                close_reason = CloseReason.TP2_HIT
            elif sl_hit:
                close_reason = CloseReason.SL_HIT
            else:
                close_reason = CloseReason.MANUAL

            close_price = last_price or trade.tp2 or trade.entry_price or 0.0
            realized_rr = calculate_realized_rr(trade, close_price)

        logger.info(
            "Position gone from broker",
            extra={
                "trade_id": trade.id,
                "symbol": trade.symbol,
                "ticket": trade.entry_ticket,
                "close_reason": close_reason.value,
                "close_price": close_price if not is_stub else None,
                "realized_rr": round(realized_rr, 2) if not is_stub else None,
            },
        )

        updated = self._store.update(
            trade.id,
            tp2_hit=tp2_hit if not is_stub else False,
            tp2_hit_at=now_ms() if (not is_stub and tp2_hit) else None,
            sl_hit=sl_hit if not is_stub else False,
            sl_hit_at=now_ms() if (not is_stub and sl_hit) else None,
            status=TradeStatus.CLOSED,
            close_reason=close_reason,
            close_price=close_price if not is_stub else None,
            closed_at=now_ms(),
            realized_rr=realized_rr if not is_stub else None,
        )
        if updated:
            self._store.remove(updated.id)
            self._repo.save(updated)
            if not is_stub:
                if tp2_hit:
                    self._bus.emit(Events.TRADE_TP2_HIT, updated)
                elif sl_hit:
                    self._bus.emit(Events.TRADE_SL_HIT, updated)
                # MANUAL closes: TRADE_CLOSED emitted by the unconditional line below
            self._bus.emit(Events.TRADE_CLOSED, updated)  # always fires once
            metric_key = (
                "trades.tp2_hit"
                if tp2_hit
                else (
                    "trades.sl_hit"
                    if sl_hit
                    else ("trades.stub_closed" if is_stub else "trades.manual_close")
                )
            )
            metrics.increment(metric_key)
            if not is_stub:
                metrics.increment("trades.closed")
                closed_in_profit = is_profitable_close(updated)
                closed_in_loss = is_losing_close(updated)
                if close_reason == CloseReason.TP2_HIT or closed_in_profit or realized_rr > 0:
                    metrics.increment("trades.winning")
                    if close_reason == CloseReason.MANUAL:
                        metrics.increment("trades.manual_profit")
                elif close_reason == CloseReason.SL_HIT or closed_in_loss or realized_rr < 0:
                    metrics.increment("trades.losing")
                    if close_reason == CloseReason.MANUAL:
                        metrics.increment("trades.manual_loss")
                else:
                    metrics.increment("trades.breakeven")
                    if close_reason == CloseReason.MANUAL:
                        metrics.increment("trades.manual_breakeven")
            metrics.set_gauge("trades.open_count", len(self._store.get_open_trades()))


# ── Module-level helpers ──────────────────────────────────────────────────────


def calculate_realized_rr(trade: Trade, close_price: float) -> float:
    if not trade.entry_price or trade.stop_loss == trade.entry_price:
        return 0.0

    if trade.side.value == "BUY":
        risk = trade.entry_price - trade.stop_loss
        reward = close_price - trade.entry_price
    elif trade.side.value == "SELL":
        risk = trade.stop_loss - trade.entry_price
        reward = trade.entry_price - close_price
    else:
        return 0.0

    if risk <= 0:
        return 0.0
    return reward / risk


def _valid_breakeven_sl(
    trade: Trade,
    symbol_info,
    tick,
    spread_multiplier: float = 0.0,
    max_buffer_pct_of_risk: float = 100.0,
) -> float | None:
    """Return a buffered breakeven SL when MT5 stop/freeze distance allows it."""
    if trade.entry_price is None:
        return None

    # Use the original signal analysis entry as the BE anchor so it matches the
    # signal engine's BE check (which uses signal.entry_price, not the fill price).
    # Slippage shifts the fill but not the analysis levels — BE should agree with them.
    _signal = getattr(getattr(trade, "plan", None), "signal", None)
    _signal_entry = getattr(_signal, "entry_price", None)
    entry = float(_signal_entry if _signal_entry is not None else trade.entry_price)
    original_stop = float(getattr(trade.plan, "stop_loss", trade.stop_loss))
    initial_risk = abs(entry - original_stop)
    bid = float(getattr(tick, "bid", 0.0) or 0.0)
    ask = float(getattr(tick, "ask", 0.0) or 0.0)
    spread = max(ask - bid, 0.0)
    multiplier = max(float(spread_multiplier), 0.0)
    spread_buffer = spread * multiplier
    risk_cap = initial_risk * max(float(max_buffer_pct_of_risk), 0.0) / 100.0
    # Once enabled, never let the risk cap reduce protected breakeven below
    # one spread; otherwise a "breakeven" stop still realizes a net loss.
    buffer = 0.0 if multiplier == 0.0 else max(spread, min(spread_buffer, risk_cap))
    min_points = max(
        int(getattr(symbol_info, "stops_level", 0) or 0),
        int(getattr(symbol_info, "freeze_level", 0) or 0),
    )
    # One extra point keeps us off the exact broker boundary, where some MT5
    # servers still answer INVALID_STOPS because price moves during the request.
    min_distance = (min_points + 1) * float(symbol_info.point)
    digits = int(getattr(symbol_info, "digits", 5) or 5)

    if trade.side.value == "BUY":
        protected_sl = entry + buffer
        if bid <= 0.0:
            return None
        if protected_sl <= bid - min_distance:
            return round(protected_sl, digits)
        return None

    protected_sl = entry - buffer
    if ask <= 0.0:
        return None
    if protected_sl >= ask + min_distance:
        return round(protected_sl, digits)
    return None


def is_profitable_close(trade: Trade) -> bool:
    if trade.entry_price is None or trade.close_price is None:
        return False
    if trade.side.value == "BUY":
        return trade.close_price > trade.entry_price
    if trade.side.value == "SELL":
        return trade.close_price < trade.entry_price
    return False


def is_losing_close(trade: Trade) -> bool:
    if trade.entry_price is None or trade.close_price is None:
        return False
    if trade.side.value == "BUY":
        return trade.close_price < trade.entry_price
    if trade.side.value == "SELL":
        return trade.close_price > trade.entry_price
    return False


def _trade_stub_from_position(pos) -> Trade:
    """
    Build a minimal Trade from an MT5 Position.
    Used when a broker position has no matching saved record —
    e.g. manually opened trades or records lost to a storage failure.

    Stubs are tracked for SL/TP2 detection but poll-based TP1 BE management
    is unavailable (tp1=0.0 so the tp1_reached check never fires).
    """
    from src.domain.trade import OrderSide, TradePlan

    ts = now_ms()
    side = OrderSide.BUY if pos.side == PositionSide.BUY else OrderSide.SELL

    plan = TradePlan(
        signal_id="unknown",
        symbol=pos.symbol,
        side=side,
        entry_price=pos.open_price,
        stop_loss=pos.stop_loss,
        tp1=pos.take_profit,
        tp2=pos.take_profit,
        lot_size=pos.lots,
        risk_amount=0.0,
        risk_percent=0.0,
        risk_reward_ratio=0.0,
        planned_at=ts,
        signal=None,
    )

    return Trade(
        id=f"STUB_{pos.symbol}_{pos.ticket}_{side.value}",
        signal_id="unknown",
        symbol=pos.symbol,
        side=side,
        status=TradeStatus.OPEN,
        plan=plan,
        entry_ticket=pos.ticket,
        entry_price=pos.open_price,
        entry_lots=pos.lots,
        current_lots=pos.lots,
        stop_loss=pos.stop_loss,
        tp1=pos.take_profit,
        tp2=pos.take_profit,
        opened_at=pos.open_time,
        created_at=ts,
        updated_at=ts,
    )
