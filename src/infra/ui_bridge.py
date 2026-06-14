"""
src/infra/ui_bridge.py — WebSocket bridge between the engine event bus and the dashboard UI.

The external dashboard connects once via WebSocket and receives:
  1. STATE_SNAPSHOT on connect (full current state)
  2. Incremental push messages as engine events fire
  3. METRICS_UPDATE every 5 s

Dashboard → engine commands (incoming messages):
  cmd.close_trade   {"trade_id": "T-00041"}
  cmd.pause         {}
  cmd.resume        {}

Message envelope:  {"type": "<name>", "payload": {...}}
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

import websockets
import websockets.exceptions

from src.infra.metrics import metrics

if TYPE_CHECKING:
    from src.app.container import AppContainer
    from src.config.settings import AppConfig

logger = logging.getLogger(__name__)

_started_at = time.time()
_METRICS_PUSH_INTERVAL_SEC = 1.5
_ACCOUNT_CACHE_TTL_SEC = 1.5
# BUG-14 — Backpressure limits: the event queue drops oldest entries when full,
# and a client that cannot accept a frame within the timeout is evicted so one
# stalled GUI connection cannot back up broadcasts for everyone else.
_EVENT_QUEUE_MAX = 500
_CLIENT_SEND_TIMEOUT_SEC = 5.0


# ── Helpers ───────────────────────────────────────────────────────────────────

def _uptime() -> float:
    return round(time.time() - _started_at, 1)

def _now_hms() -> str:
    return datetime.now().strftime("%H:%M:%S")

def _serialize_trade(trade: Any) -> dict:
    return {
        "id":            trade.id,
        "symbol":        trade.symbol,
        "side":          trade.side.value if hasattr(trade.side, "value") else str(trade.side),
        "state":         trade.status.value if hasattr(trade.status, "value") else str(trade.status),
        "entry_price":   trade.entry_price or 0.0,
        "current_price": trade.entry_price or 0.0,
        "sl":            trade.stop_loss,
        "tp1":           trade.tp1,
        "tp2":           trade.tp2,
        "lots":          trade.entry_lots,
        "tp1_lots":      trade.tp1_lots,
        "pnl":           0.0,
        "opened_at":     _now_hms(),
        "duration_sec":  0,
        "ticket":        trade.entry_ticket,
    }

def _extract_signal(payload: Any) -> dict | None:
    signal = None
    reason: str | None = None
    message: str | None = None

    if payload is None:
        return None
    if hasattr(payload, "symbol") and hasattr(payload, "direction"):
        signal = payload
    elif hasattr(payload, "plan") and hasattr(payload.plan, "signal"):
        signal = payload.plan.signal
    elif isinstance(payload, dict):
        signal = payload.get("signal")
        reason = str(payload.get("reason", "")) or None
        message = str(payload.get("message", "")) or None

    if signal is None or not hasattr(signal, "symbol"):
        return None

    direction = getattr(signal, "direction", None)
    htf_interval = getattr(signal, "htf_interval", None)
    ltf_interval = getattr(signal, "ltf_interval", None)
    pattern = None
    rejection = getattr(signal, "rejection_candle", None)
    if rejection is not None:
        pattern_value = getattr(rejection, "pattern", None)
        pattern = pattern_value.value if hasattr(pattern_value, "value") else pattern_value
    setup = pattern or getattr(signal, "setup", None)
    return {
        "id":              getattr(signal, "id", _now_hms()),
        "symbol":          signal.symbol,
        "direction":       direction.value if hasattr(direction, "value") else str(direction),
        "timeframe":       "/".join(str(v) for v in (htf_interval, ltf_interval) if v),
        "strategy":        pattern or getattr(signal, "strategy", None),
        "entryPrice":      getattr(signal, "entry_price", None),
        "stopLoss":        getattr(signal, "stop_loss", None),
        "tp1":             getattr(signal, "tp1", None),
        "tp2":             getattr(signal, "tp2", None),
        "takeProfit":      getattr(signal, "tp2", None),
        "riskRewardRatio": getattr(signal, "risk_reward_ratio", None),
        "setup":           setup,
        "reason":          reason,
        "message":         message,
    }

def _build_signal_event(event_name: str, payload: Any) -> dict | None:
    STATUS = {
        "signal.received":  "RECEIVED",
        "signal.triggered": "TRIGGERED",
        "risk.approved":    "APPROVED",
        "risk.rejected":    "REJECTED",
        "trade.opened":     "OPENED",
        "trade.error":      "FAILED",
    }
    status = STATUS.get(event_name)
    if status is None:
        return None
    info = _extract_signal(payload)
    if info is None:
        return None
    return {
        "id":              info["id"],
        "symbol":          info["symbol"],
        "timeframe":       info.get("timeframe"),
        "strategy":        info.get("strategy"),
        "direction":       info["direction"],
        "status":          status,
        "entryPrice":      info.get("entryPrice"),
        "stopLoss":        info.get("stopLoss"),
        "tp1":             info.get("tp1"),
        "tp2":             info.get("tp2"),
        "takeProfit":      info.get("takeProfit"),
        "riskRewardRatio": info.get("riskRewardRatio"),
        "setup":           info.get("setup"),
        "timestamp":       _now_hms(),
        "reason":          info.get("reason"),
        "message":         info.get("message"),
    }


def _enum_value(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _trade_outcome(trade: Any) -> str:
    reason = _enum_value(getattr(trade, "close_reason", "") or "").upper()
    if reason == "TP2_HIT":
        return "win"
    if reason == "SL_HIT":
        return "loss"

    side = _enum_value(getattr(trade, "side", "") or "").upper()
    entry = getattr(trade, "entry_price", None)
    close = getattr(trade, "close_price", None)
    if entry is not None and close is not None:
        if side == "BUY":
            if close > entry:
                return "win"
            if close < entry:
                return "loss"
        elif side == "SELL":
            if close < entry:
                return "win"
            if close > entry:
                return "loss"

    rr = getattr(trade, "realized_rr", None)
    if rr is None:
        return "breakeven"
    if rr > 0:
        return "win"
    if rr < 0:
        return "loss"
    return "breakeven"


# ── UIBridge ──────────────────────────────────────────────────────────────────

class UIBridge:
    def __init__(self, container: "AppContainer", config: "AppConfig", port: int = 8080) -> None:
        self._container = container
        self._config    = config
        self._port      = port

        self._loop:       asyncio.AbstractEventLoop | None = None
        self._queue:      asyncio.Queue | None             = None
        self._stop_event: asyncio.Event | None             = None
        self._thread:     threading.Thread | None          = None
        self._clients:    set[Any]                         = set()

        self._log_buf: collections.deque[dict] = collections.deque(maxlen=200)
        self._signal_buf: collections.deque[dict] = collections.deque(maxlen=100)
        self._log_handler = _LogHandler(self._log_buf)
        self._account_cache: dict | None = None
        self._account_cache_at: float = 0.0

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        self._log_handler.setFormatter(
            logging.Formatter("%(asctime)s  %(name)-20s  %(message)s", datefmt="%H:%M:%S")
        )
        self._log_handler.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(self._log_handler)

        self._container.event_bus.on_any(self.record_event)

        self._thread = threading.Thread(target=self._run_loop, name="ui-bridge", daemon=True)
        self._thread.start()
        logger.info("UIBridge started on ws://0.0.0.0:%d", self._port)

    def stop(self) -> None:
        if self._loop and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        logger.info("UIBridge stopped")

    # ── Thread-safe enqueue (called from engine threads) ──────────────────

    def record_event(self, event_name: str, payload: Any) -> None:
        signal = _build_signal_event(event_name, payload)
        if signal:
            self._signal_buf.appendleft(signal)
        if self._loop and self._queue:
            try:
                self._loop.call_soon_threadsafe(
                    self._enqueue_event, (event_name, payload)
                )
            except Exception:
                pass

    def _enqueue_event(self, item: tuple) -> None:
        """Runs on the bridge loop thread. BUG-14: drop-oldest when full."""
        q = self._queue
        if q is None:
            return
        while True:
            try:
                q.put_nowait(item)
                return
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    return

    def push_event(self, event_type: str, payload: Any) -> None:
        """Send an arbitrary typed message to all connected GUI clients.

        Used by bootstrap to push MT5 connection errors before the event bus
        is wired up, so the GUI always shows the real failure reason.
        """
        if self._loop and self._clients:
            frame = json.dumps({"type": event_type, "payload": payload}, default=str)
            try:
                asyncio.run_coroutine_threadsafe(self._broadcast_raw(frame), self._loop)
            except Exception:
                pass

    # ── Asyncio loop ──────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop       = loop
        self._queue      = asyncio.Queue(maxsize=_EVENT_QUEUE_MAX)
        self._stop_event = asyncio.Event()
        try:
            loop.run_until_complete(self._serve())
        finally:
            loop.close()

    async def _serve(self) -> None:
        async def handler(websocket: Any) -> None:
            self._clients.add(websocket)
            logger.debug("Dashboard connected (%d clients)", len(self._clients))
            try:
                await websocket.send(
                    json.dumps(
                        {"type": "STATE_SNAPSHOT", "payload": self._build_snapshot()},
                        default=str,
                    )
                )
                # Listen for commands
                async for raw in websocket:
                    try:
                        msg = json.loads(raw)
                        await self._handle_command(msg.get("type", ""), msg.get("payload", {}))
                    except Exception as e:
                        logger.warning("UIBridge command error: %s", e)
            except websockets.exceptions.ConnectionClosed:
                pass
            finally:
                self._clients.discard(websocket)
                logger.debug("Dashboard disconnected (%d clients)", len(self._clients))

        async def broadcaster() -> None:
            while not self._stop_event.is_set():  # type: ignore[union-attr]
                try:
                    event_name, payload = await asyncio.wait_for(
                        self._queue.get(), timeout=1.0  # type: ignore[union-attr]
                    )
                except asyncio.TimeoutError:
                    continue
                msg = self._serialize_event(event_name, payload)
                if msg and self._clients:
                    await self._broadcast_raw(json.dumps(msg, default=str))

        async def metrics_pusher() -> None:
            while not self._stop_event.is_set():  # type: ignore[union-attr]
                await asyncio.sleep(_METRICS_PUSH_INTERVAL_SEC)
                if not self._clients:
                    continue
                try:
                    frame = json.dumps(
                        {"type": "METRICS_UPDATE", "payload": self._build_metrics()},
                        default=str,
                    )
                    await self._broadcast_raw(frame)
                except Exception:
                    pass

        try:
            server = await websockets.serve(handler, "0.0.0.0", self._port)  # type: ignore[attr-defined]
        except OSError as exc:
            logger.warning(
                "UIBridge: port %d already in use — dashboard WS disabled. "
                "(%s)  Is another engine instance running?",
                self._port, exc,
            )
            # Keep the stop_event running so the engine itself isn't affected.
            await self._stop_event.wait()  # type: ignore[union-attr]
            return

        try:
            await asyncio.gather(
                broadcaster(),
                metrics_pusher(),
                self._stop_event.wait(),  # type: ignore[union-attr]
            )
        finally:
            server.close()
            await server.wait_closed()

    # ── Command handler ───────────────────────────────────────────────────

    async def _handle_command(self, cmd: str, payload: dict) -> None:
        loop = asyncio.get_event_loop()

        if cmd == "cmd.close_trade":
            trade_id = payload.get("trade_id", "")
            await loop.run_in_executor(None, self._close_trade, trade_id)

        elif cmd == "cmd.pause":
            self._container.signal_queue.pause()
            logger.info("UIBridge: engine paused via dashboard")
            await self._broadcast({"type": "engine.paused", "payload": {}})

        elif cmd == "cmd.resume":
            self._container.signal_queue.resume()
            logger.info("UIBridge: engine resumed via dashboard")
            await self._broadcast({"type": "engine.resumed", "payload": {}})

    def _close_trade(self, trade_id: str) -> None:
        from src.brokers.mt5.client import _MT5_LOCK
        from src.brokers.mt5.types import Mt5OrderType

        trade = self._container.position_store.get(trade_id)
        if not trade or not trade.entry_ticket:
            logger.warning("cmd.close_trade: trade %s not found or no ticket", trade_id)
            return

        try:
            self._container.mt5_client.ensure_connected()
            mt5 = self._container.mt5_client.mt5

            with _MT5_LOCK:
                tick = mt5.symbol_info_tick(trade.symbol)
            if tick is None:
                raise RuntimeError(f"No tick available for {trade.symbol}")

            if trade.side.value == "BUY":
                side_int = Mt5OrderType.BUY
                price    = tick.bid
            else:
                side_int = Mt5OrderType.SELL
                price    = tick.ask

            with _MT5_LOCK:
                sym_info = mt5.symbol_info(trade.symbol)
            filling_mode = getattr(sym_info, "filling_mode", 0) if sym_info else 0

            self._container.mt5_orders.close_position(
                ticket       = trade.entry_ticket,
                symbol       = trade.symbol,
                side         = side_int,
                volume       = trade.current_lots,
                price        = price,
                slippage     = self._config.execution.slippage,
                magic        = self._config.execution.magic,
                comment      = "dashboard_close",
                filling_mode = filling_mode,
            )
            logger.info("UIBridge: closed trade %s (ticket %d)", trade_id, trade.entry_ticket)

        except Exception as exc:
            logger.error("UIBridge: close_trade failed for %s: %s", trade_id, exc)

    async def _broadcast(self, msg: dict) -> None:
        await self._broadcast_raw(json.dumps(msg, default=str))

    # ── Event serialization ───────────────────────────────────────────────

    def _serialize_event(self, event_name: str, payload: Any) -> dict | None:
        if event_name == "trade.opened":
            try:
                trade_dict = _serialize_trade(payload)
                # Also push to signal feed as OPENED
                sig = _build_signal_event(event_name, payload)
                if sig and self._clients:
                    frame = json.dumps({"type": "signal.opened", "payload": sig}, default=str)
                    asyncio.ensure_future(self._broadcast_raw(frame))
                return {"type": event_name, "payload": trade_dict}
            except Exception:
                return None

        if event_name == "trade.tp1_hit":
            try:
                return {"type": event_name, "payload": {"trade_id": payload.id}}
            except Exception:
                return None

        if event_name in ("trade.tp2_hit", "trade.sl_hit", "trade.closed"):
            try:
                return {"type": event_name, "payload": {"trade_id": payload.id}}
            except Exception:
                return None

        if event_name in ("signal.received", "signal.triggered", "risk.approved", "risk.rejected"):
            sig = _build_signal_event(event_name, payload)
            return {"type": event_name, "payload": sig} if sig else None

        if event_name == "trade.error":
            try:
                sig = _build_signal_event(event_name, payload)
                return {"type": "trade.error", "payload": sig} if sig else None
            except Exception:
                return None

        return None

    async def _broadcast_raw(self, frame: str) -> None:
        # BUG-14 — wait_for bounds each send so one stalled client cannot
        # block the broadcaster/metrics pusher; timed-out clients are evicted.
        for client in list(self._clients):
            try:
                await asyncio.wait_for(
                    client.send(frame), timeout=_CLIENT_SEND_TIMEOUT_SEC
                )
            except Exception:
                self._clients.discard(client)

    # ── Snapshot builders ─────────────────────────────────────────────────

    def _build_snapshot(self) -> dict:
        c       = self._container
        config  = self._config
        lt      = c.loss_tracker.stats()
        snap    = metrics.snapshot()
        trades  = c.position_store.get_open_trades()
        account = self._account_snapshot()
        connected_mt5 = bool(account) or c.mt5_client.is_connected()

        return {
            "connected":   connected_mt5,
            "engine":      self._build_engine_info(lt),
            "config":      self._build_config_snapshot(config),
            "trades":      [_serialize_trade(t) for t in trades],
            "riskGuards":  self._build_risk_guards(lt, config),
            "clusterRisk": c.cluster_tracker.stats(),
            "metrics":     self._build_metrics_from(lt, snap.get("counters", {}), snap.get("gauges", {}), trades, config, account),
            "signals":     list(self._signal_buf),
            "logs":        list(self._log_buf)[-50:],
        }

    def build_remote_snapshot(self) -> dict:
        snapshot = self._build_snapshot()
        snapshot.pop("config", None)
        snapshot.pop("logs", None)
        return snapshot

    @staticmethod
    def _build_config_snapshot(config: Any) -> dict:
        return {
            "mode": "LIVE",
            "symbols": list(config.gateway.symbols),
            "signal_ws_url": config.gateway.ws_url,
            "risk": {
                "max_losing_streak": config.risk.max_losing_streak,
                "max_daily_loss_percent": config.risk.max_daily_loss_percent,
                "max_exposure_per_symbol": config.risk.max_exposure_per_symbol,
                "min_rr_ratio": config.risk.min_rr_ratio,
                "max_lot_size": config.risk.max_lot_size,
                "min_lot_size": config.risk.min_lot_size,
                "sl_ratio_threshold": config.risk.sl_ratio_threshold,
                "symbol_sl_ratio_threshold": dict(config.risk.symbol_sl_ratio_threshold),
                "no_hedging": config.risk.no_hedging,
                "max_profit_drawdown_percent": config.risk.max_profit_drawdown_percent,
                "rolling_window_size": config.risk.rolling_window_size,
                "rolling_drawdown_pct": config.risk.rolling_drawdown_pct,
                "equity_throttle": {
                    "enabled": config.risk.equity_throttle.enabled,
                    "drawdown_threshold_r": config.risk.equity_throttle.drawdown_threshold_r,
                    "release_threshold_r": config.risk.equity_throttle.release_threshold_r,
                    "risk_multiplier": config.risk.equity_throttle.risk_multiplier,
                    "window_days": config.risk.equity_throttle.window_days,
                },
                "cluster_risk": {
                    "enabled": config.risk.cluster_risk.enabled,
                    "groups": [
                        {
                            "name": g.name,
                            "symbols": list(g.symbols),
                            "max_same_day_loss_r": g.max_same_day_loss_r,
                            "max_concurrent_positions": g.max_concurrent_positions,
                            "max_same_day_losses": g.max_same_day_losses,
                            "after_first_loss_risk_multiplier": g.after_first_loss_risk_multiplier,
                            "min_trade_risk_multiplier": g.min_trade_risk_multiplier,
                        }
                        for g in config.risk.cluster_risk.groups
                    ],
                },
            },
            "execution": {
                "tp1_trigger_pct": config.execution.tp1_trigger_pct,
                "tp1_percentage": config.execution.tp1_percentage,
                "move_sl_to_be_on_tp1": config.execution.move_sl_to_be_on_tp1,
                "breakeven_spread_multiplier": config.execution.breakeven_spread_multiplier,
                "breakeven_max_buffer_pct_of_risk": config.execution.breakeven_max_buffer_pct_of_risk,
                "tf_overrides": dict(config.execution.tf_overrides),
                "spread_risk_multiplier": config.execution.spread_risk_multiplier,
                "max_entry_slippage_pct_of_stop": config.execution.max_entry_slippage_pct_of_stop,
                "max_signal_age_ms": config.execution.max_signal_age_ms,
                "close_on_slippage_exceed": config.execution.close_on_slippage_exceed,
                "adjust_levels_on_slippage": config.execution.adjust_levels_on_slippage,
                "order_retry_count": config.execution.order_retry_count,
                "order_retry_delay_sec": config.execution.order_retry_delay_sec,
            },
            "mt5": {
                "login": config.mt5.login,
                "server": config.mt5.server,
                "magic": config.execution.magic,
                "slippage": config.execution.slippage,
            },
            "engine": {
                "timezone": str(config.engine_timezone),
                "position_poll_interval": config.position_poll_interval,
                "monitoring_port": config.monitoring_port,
            },
        }

    def _account_snapshot(self) -> dict | None:
        now = time.monotonic()
        if self._account_cache and now - self._account_cache_at < _ACCOUNT_CACHE_TTL_SEC:
            return self._account_cache
        try:
            account = self._container.mt5_positions.get_account_info()
            snapshot = {
                "balance": account.balance,
                "equity": account.equity,
                "free_margin": account.free_margin,
                "margin": account.margin,
                "margin_level": account.margin_level,
                "currency": account.currency,
            }
            self._account_cache = snapshot
            self._account_cache_at = now
            return snapshot
        except Exception:
            return None

    def _build_engine_info(self, lt: dict) -> dict:
        c           = self._container
        # cmd_paused: set by dashboard command.pause / command.resume
        # risk_paused: set by loss-tracker risk guards
        cmd_paused  = c.signal_queue.is_paused()
        risk_paused = bool(lt.get("paused"))
        is_paused   = cmd_paused or risk_paused
        return {
            "status":                  "PAUSED" if is_paused else "RUNNING",
            # is_paused reflects command-driven pause only — used by dashboard
            # remote-control buttons to decide whether Resume is available.
            "is_paused":               cmd_paused,
            "uptime_sec":              int(_uptime()),
            "mode":                    getattr(self._config, "mode", "LIVE"),
            "connected_mt5":           c.mt5_client.is_connected(),
            "connected_signal_engine": True,
            "version":                 self._config.gateway.engine_version,
            "magic":                   self._config.execution.magic,
        }

    def _build_risk_guards(self, lt: dict, config: Any) -> list[dict]:
        daily_loss  = lt.get("daily_loss_pct", 0.0)
        eq_dd       = lt.get("profit_drawback_pct", 0.0)
        paused      = lt.get("paused", False)
        reason      = (lt.get("pause_reason") or "").lower()
        rolling_on  = config.risk.rolling_window_size > 0 and config.risk.rolling_drawdown_pct > 0
        cluster_on  = config.risk.cluster_risk.enabled

        def _s(key: str) -> str:
            if paused and key in reason:
                return "PAUSED"
            return "ACTIVE"

        cluster_stats = self._container.cluster_tracker.stats()
        cluster_used = 0.0
        cluster_threshold = 0.0
        if cluster_on and config.risk.cluster_risk.groups:
            first_group = config.risk.cluster_risk.groups[0]
            cluster_threshold = first_group.max_same_day_loss_r
            group_state = cluster_stats.get(first_group.name, {})
            cluster_used = (
                group_state.get("realized_loss_r", 0.0)
                + group_state.get("open_r", 0.0)
                + group_state.get("pending_r", 0.0)
            )

        throttle = self._container.equity_throttle.stats()
        if throttle.get("engaged"):
            throttle_desc = (
                f"Sizing at {throttle['multiplier']:g}× — "
                f"{throttle['drawdown_r']:.1f}R below {throttle['window_days']}-day peak"
            )
        else:
            throttle_desc = (
                f"Halves risk when >{throttle['threshold_r']:g}R below rolling peak"
            )

        return [
            {"id": "guard1", "name": "DAILY LOSS",      "description": "Pause until midnight on breach",
             "status": _s("daily loss"),       "current_value": round(daily_loss, 4), "threshold": config.risk.max_daily_loss_percent,    "unit": "%"},
            {"id": "guard2", "name": "PROFIT DRAWDOWN", "description": "Pause if session profit gives back this % of equity",
             "status": _s("profit drawdown"),  "current_value": round(eq_dd, 4),      "threshold": config.risk.max_profit_drawdown_percent, "unit": "%"},
            {"id": "guard3", "name": "ROLLING WINDOW",  "description": f"Rolling {config.risk.rolling_window_size}-trade drawdown",
             "status": "DISABLED" if not rolling_on else _s("rolling drawdown"),
             "current_value": round(eq_dd, 4), "threshold": config.risk.rolling_drawdown_pct, "unit": "%"},
            {"id": "guard4", "name": "CLUSTER RISK",    "description": "Shared risk bucket for correlated symbols",
             "status": "DISABLED" if not cluster_on else "ACTIVE",
             "current_value": round(cluster_used, 4), "threshold": cluster_threshold, "unit": "R"},
            {"id": "guard5", "name": "EQUITY THROTTLE", "description": throttle_desc,
             "status": "DISABLED" if not throttle.get("enabled") else "ACTIVE",
             "current_value": throttle.get("drawdown_r", 0.0), "threshold": throttle.get("threshold_r", 0.0), "unit": "R"},
        ]

    def _build_metrics(self) -> dict:
        c      = self._container
        lt     = c.loss_tracker.stats()
        snap   = metrics.snapshot()
        trades = c.position_store.get_open_trades()
        return self._build_metrics_from(
            lt,
            snap.get("counters", {}),
            snap.get("gauges", {}),
            trades,
            self._config,
            self._account_snapshot(),
        )

    def _build_metrics_from(self, lt: dict, counters: dict, gauges: dict, open_trades: list, config: Any, account: dict | None = None) -> dict:
        persisted_outcomes = self._persisted_trade_outcomes(config)
        if persisted_outcomes["total_closed"] > 0:
            wins = persisted_outcomes["wins"]
            losses = persisted_outcomes["losses"]
            breakeven = persisted_outcomes["breakeven"]
            total_closed = persisted_outcomes["total_closed"]
        else:
            wins = counters.get("trades.winning", 0)
            losses = counters.get("trades.losing", 0)
            breakeven = counters.get("trades.breakeven", 0)
            total_closed = counters.get("trades.closed", wins + losses + breakeven)

        start_eq   = lt.get("start_of_day_equity", 0.0)
        daily_loss = lt.get("daily_loss_pct", 0.0)
        daily_budget = lt.get("daily_budget", 0.0)
        peak_eq    = lt.get("equity_peak", 0.0)
        eq_dd      = lt.get("profit_drawback_pct", 0.0)

        current_equity = account["equity"] if account else (peak_eq if peak_eq else start_eq)
        peak_equity = max(peak_eq, current_equity) if current_equity else peak_eq
        daily_loss_amount = round(start_eq * daily_loss / 100.0, 2) if start_eq else 0.0
        daily_pnl = (
            round(current_equity - start_eq, 2)
            if current_equity and start_eq
            else round(-daily_loss_amount, 2)
        )
        daily_budget_left = max(daily_budget - daily_loss_amount, 0.0) if daily_budget else 0.0
        risk_slots = max(1, int(config.risk.max_losing_streak or 1))
        risk_per_trade = round(daily_budget / risk_slots, 2) if daily_budget else 0.0

        result = {
            "raw_counters":       counters,
            "raw_gauges":         gauges,
            "configured_symbols":  list(config.gateway.symbols),
            "active_symbol":       config.gateway.symbols[0] if config.gateway.symbols else None,
            "start_balance":      start_eq,
            "current_balance":    account["balance"] if account else current_equity,
            "equity":             current_equity,
            "peak_equity":        peak_equity,
            "drawdown_pct":       round(eq_dd, 4),
            "daily_pnl":          daily_pnl,
            "daily_loss_pct":     round(daily_loss, 4),
            "daily_budget":       daily_budget,
            "daily_budget_used":  daily_loss_amount,
            "daily_budget_left":  daily_budget_left,
            "daily_loss_limit_percent": config.risk.max_daily_loss_percent,
            "risk_per_trade":     risk_per_trade,
            "risk_slots":         risk_slots,
            "max_losing_streak":  config.risk.max_losing_streak,
            "open_trades":        len(open_trades),
            "max_trades":         risk_slots,
            "pending_signals":    self._container.signal_queue.depth(),
            "total_trades":       total_closed,
            "total_trades_today": counters.get("trades.opened", 0),
            "orders_opened":      counters.get("mt5.orders.opened", counters.get("orders.opened", 0)),
            "orders_filled":      counters.get("orders.filled", 0),
            "orders_rejected":    counters.get("orders.rejected", 0),
            "orders_retried":     counters.get("orders.retried", 0),
            "orders_partial_fills": counters.get("orders.partial_fills", 0),
            "orders_margin_reduced": counters.get("orders.margin_reduced", 0),
            "orders_slippage_rejected": counters.get("orders.slippage_rejected", 0),
            "orders_emergency_closes": counters.get("orders.emergency_closes", 0),
            "signals_received":   sum(
                value
                for key, value in counters.items()
                if key == "signal.received" or key.startswith("signal.received.")
            ),
            "signals_triggered":  counters.get("signal.triggered", 0),
            "signals_validation_failures": counters.get("signal.validation_failures", 0),
            "signals_parse_errors": counters.get("signal.parse_errors", 0),
            "signals_deserialise_errors": counters.get("signal.deserialise_errors", 0),
            "signal_duplicates_ignored": counters.get("signal.duplicates_ignored", 0),
            "risk_approved":      counters.get("risk.approved", 0),
            "risk_rejected":      counters.get("risk.rejected", 0),
            "trades_opened":      counters.get("trades.opened", 0),
            "trades_closed":      counters.get("trades.closed", 0),
            "trades_tp1_hit":     counters.get("trades.tp1_hit", 0),
            "trades_tp2_hit":     counters.get("trades.tp2_hit", 0),
            "trades_sl_hit":      counters.get("trades.sl_hit", 0),
            "trades_breakeven":   breakeven,
            "trades_open_count":  gauges.get("trades.open_count", len(open_trades)),
            "trades_tracking_failures": counters.get("trades.tracking_failures", 0),
            "trades_persistence_failures": counters.get("trades.persistence_failures", 0),
            "winning_trades":     wins,
            "losing_trades":      losses,
            "win_rate":           round(wins / total_closed * 100, 1) if total_closed else 0.0,
            "signal_to_trade_ms": int(gauges.get("latency.signal_to_trade_ms") or 0),
            "latency_signal_to_trade_ms": int(gauges.get("latency.signal_to_trade_ms") or 0),
            "latency_market_signal_age_ms": int(gauges.get("latency.market_signal_age_ms") or 0),
            "latency_emit_to_receive_ms": int(gauges.get("latency.emit_to_receive_ms") or 0),
            "latency_receive_to_execute_ms": int(gauges.get("latency.receive_to_execute_ms") or 0),
            "latency_execution_pipeline_ms": int(gauges.get("latency.execution_pipeline_ms") or 0),
            "latency_pipeline_ms": int(gauges.get("latency.execution_pipeline_ms") or gauges.get("latency.pipeline_ms") or 0),
            "latency_broker_round_trip_ms": int(gauges.get("latency.broker_round_trip_ms") or 0),
        }
        if account:
            result.update(
                {
                    "balance": account["balance"],
                    "free_margin": account["free_margin"],
                    "margin": account["margin"],
                    "margin_level": account["margin_level"],
                    "currency": account["currency"],
                    "net_pnl": account["equity"] - account["balance"],
                }
            )
        return result

    def _persisted_trade_outcomes(self, config: Any) -> dict:
        outcomes = {"wins": 0, "losses": 0, "breakeven": 0, "total_closed": 0}
        repo = getattr(self._container, "trade_repo", None)
        if repo is None or not hasattr(repo, "load_all"):
            return outcomes

        try:
            tz = getattr(config, "engine_timezone", None)
            today = datetime.now(tz=tz).replace(hour=0, minute=0, second=0, microsecond=0)
            today_ms = int(today.timestamp() * 1000)

            for trade in repo.load_all():
                status = _enum_value(getattr(trade, "status", "")).upper()
                closed_at = getattr(trade, "closed_at", None) or 0
                if status != "CLOSED" or closed_at < today_ms:
                    continue

                outcome = _trade_outcome(trade)
                outcomes["total_closed"] += 1
                if outcome == "win":
                    outcomes["wins"] += 1
                elif outcome == "loss":
                    outcomes["losses"] += 1
                else:
                    outcomes["breakeven"] += 1
        except Exception:
            logger.warning("UIBridge: failed to derive persisted trade outcomes")

        return outcomes


# ── Log handler ───────────────────────────────────────────────────────────────

class _LogHandler(logging.Handler):
    def __init__(self, buf: collections.deque) -> None:
        super().__init__()
        self._buf = buf

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._buf.append({
                "ts":    record.created,
                "level": record.levelname,
                "name":  record.name,
                "msg":   self.format(record),
            })
        except Exception:
            pass
