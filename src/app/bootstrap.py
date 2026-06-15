"""
app/bootstrap.py — wires event bus subscriptions and starts all services.

Startup order (revised):
  1. DB + metrics init
  2. UIBridge WebSocket server starts immediately → GUI can always connect
  3. Non-MT5 services wire up
  4. MT5 connection attempted in a daemon thread with exponential back-off
     → trading services (position_manager, signal_queue, signal_consumer)
       start only after MT5 is authenticated
  5. If MT5 never connects the service stays alive, the GUI shows the error,
     and the engine retries automatically (no manual restarts needed).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace

from src.app.container import AppContainer
from src.config.settings import AppConfig
from src.core.event_types import Events
from src.infra.metrics import metrics
from src.domain.signal_interface import InboundSignal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def bootstrap(
    container: AppContainer,
    config: AppConfig,
    *,
    expose_local_ui: bool = True,
) -> None:
    """
    Start the engine.  Returns as soon as UIBridge is listening.
    MT5 is connected asynchronously — the GUI will update automatically
    when it comes online.
    """
    # ── DB ────────────────────────────────────────────────────────────────────
    container.db.init()
    container.trade_repo.init()
    metrics.init_db(container.db)
    container.loss_tracker.hydrate_state(container.db.load_device_state("risk_state"))
    container.loss_tracker.set_state_sink(
        lambda state: container.db.save_device_state("risk_state", state)
    )

    # UIBridge owns the canonical snapshot builder; workers keep it headless.
    from src.infra.ui_bridge import UIBridge  # local import avoids circular dep
    container.ui_bridge = UIBridge(container, config, port=config.monitoring_port)
    container.signal_consumer.set_snapshot_provider(
        container.ui_bridge.build_remote_snapshot
    )
    if expose_local_ui:
        container.ui_bridge.start()
    _wire_commands(container)

    # ── MT5 + trading services (background, retries forever) ──────────────────
    t = threading.Thread(
        target=_connect_mt5_with_retry,
        args=(container, config, expose_local_ui),
        name="mt5-connect",
        daemon=True,
    )
    t.start()

    logger.info("Engine started — UIBridge up, awaiting MT5 connection")


def shutdown(container: AppContainer) -> None:
    logger.info("Shutting down Execution Engine")
    container.runtime_ready.clear()
    container.event_bus.emit(Events.SYSTEM_STOPPING)
    container.signal_consumer.stop()
    container.signal_queue.stop()
    container.position_manager.stop()
    if container.ui_bridge:
        container.ui_bridge.stop()
    container.mt5_client.disconnect()
    metrics.stop()
    logger.info("Shutdown complete")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_MT5_RETRY_DELAYS = [5, 10, 15, 30, 60]   # seconds; last value repeats


def _connect_mt5_with_retry(
    container: AppContainer, config: AppConfig, expose_local_ui: bool
) -> None:
    """
    Keep trying to connect to MT5 until it succeeds, then start the
    trading services.  Runs in a daemon thread so the service (and UIBridge)
    stay alive regardless.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            _attempt_mt5_connect(container, config, expose_local_ui)
            # _attempt_mt5_connect only returns normally on success
            return
        except Exception as exc:
            container.runtime_error = str(exc)
            delay = _MT5_RETRY_DELAYS[min(attempt - 1, len(_MT5_RETRY_DELAYS) - 1)]
            logger.warning(
                "MT5 connection failed (attempt %d) — retrying in %ds: %s",
                attempt, delay, exc,
            )
            # Surface the error to the GUI via UIBridge snapshot
            _broadcast_mt5_error(container, str(exc))
            time.sleep(delay)


def _attempt_mt5_connect(
    container: AppContainer, config: AppConfig, expose_local_ui: bool
) -> None:
    """Single connection attempt.  Raises on any failure."""
    container.mt5_client.connect()

    try:
        account = container.mt5_positions.get_account_info()
    except Exception as exc:
        raise ConnectionError(
            f"MT5 authenticated but account_info() failed — "
            f"check login/password/server in config.yaml.  Detail: {exc}"
        ) from exc

    container.event_bus.emit(Events.BROKER_CONNECTED)
    logger.info(
        "MT5 connected",
        extra={
            "login":       account.login,
            "server":      account.server,
            "currency":    account.currency,
            "balance":     account.balance,
            "equity":      account.equity,
            "leverage":    account.leverage,
            "free_margin": account.free_margin,
        },
    )

    # Hydrate position store from live MT5 positions
    container.position_manager.hydrate_from_broker()
    container.cluster_tracker.hydrate_open_trades(
        container.position_store.get_open_trades()
    )

    # Rebuild the equity-throttle rolling window from persisted closed trades
    throttle_window_ms = (
        config.risk.equity_throttle.window_days * 86_400_000
    )
    container.equity_throttle.hydrate(
        container.trade_repo.load_closed_trades_since(
            int(time.time() * 1000) - throttle_window_ms
        )
    )

    # Prime daily loss tracker
    try:
        loss_pct, start_equity, current_equity = (
            container.mt5_positions.get_daily_pnl_info(config.execution.magic)
        )
        container.execution_engine.update_daily_loss(loss_pct, start_equity, current_equity)
        logger.info(
            "Daily loss primed",
            extra={
                "daily_loss_pct":      loss_pct,
                "start_of_day_equity": start_equity,
                "current_equity":      current_equity,
            },
        )
    except Exception as exc:
        raise ConnectionError(
            f"Risk data unavailable; refusing to start trading: {exc}"
        ) from exc

    # Wire event handlers (idempotent — safe to call once)
    _wire_events(container)

    # Start trading services
    container.signal_queue.start()
    container.position_manager.start()
    # Workers receive signals through manager-owned IPC — not the gateway WS.
    if expose_local_ui:
        container.signal_consumer.start()

    container.event_bus.emit(Events.SYSTEM_STARTED)
    container.runtime_error = None
    container.runtime_ready.set()
    logger.info(
        "Execution Engine fully online",
        extra={
            "symbols":           config.gateway.symbols,
            "gateway_ws":        config.gateway.ws_url,
            "max_losing_streak": config.risk.max_losing_streak,
        },
    )


def _broadcast_mt5_error(container: AppContainer, error: str) -> None:
    """Push the MT5 error into the UIBridge so the GUI sees it immediately."""
    try:
        if container.ui_bridge:
            container.ui_bridge.push_event("mt5.error", {"message": error})
    except Exception:
        pass   # UIBridge might not be ready yet on the very first attempt


def _wire_events(container: AppContainer) -> None:
    """Register event-bus subscribers.  Called once after MT5 connects."""
    def on_signal_triggered(signal: InboundSignal) -> None:
        resolved = container.mt5_positions.resolve_symbol(signal.symbol)
        if not resolved:
            logger.error(
                "Signal rejected: symbol not found in MT5",
                extra={"signal_id": signal.id, "symbol": signal.symbol},
            )
            container.event_bus.emit(
                Events.SIGNAL_REJECTED,
                {"signal": signal, "reason": "symbol_not_found"},
            )
            return
        enriched = replace(signal, resolved_symbol=resolved)
        adapted = container.strategy_router.route(enriched)
        container.signal_queue.put(adapted)

    container.event_bus.on(Events.SIGNAL_TRIGGERED, on_signal_triggered)

    def on_trade_closed(trade) -> None:
        container.cluster_tracker.mark_trade_closed(trade)
        container.equity_throttle.record_trade_closed(trade)
        pnl = getattr(trade, "realized_pnl", None)
        if pnl is not None:
            container.loss_tracker.record_trade_closed(float(pnl))

    container.event_bus.on(Events.TRADE_CLOSED, on_trade_closed)

    def on_any_event(event: str, _payload) -> None:
        metrics.increment(f"events.{event}")

    container.event_bus.on_any(on_any_event)
    container.event_bus.on_any(container.signal_consumer.report_event)


def _wire_commands(container: AppContainer) -> None:
    """Register command callbacks for pause/resume/emergency-stop."""
    def _on_pause() -> None:
        if container.signal_queue.is_paused():
            raise ValueError("ENGINE_ALREADY_PAUSED")
        container.signal_queue.pause()

    def _on_resume() -> None:
        if not container.signal_queue.is_paused():
            raise ValueError("ENGINE_NOT_PAUSED")
        container.signal_queue.resume()

    def _on_emergency_stop() -> None:
        open_trades = container.position_store.get_open_trades()
        if not open_trades:
            raise ValueError("NO_OPEN_POSITIONS")
        container.signal_queue.pause()
        container.position_manager.emergency_close_all()

    container.signal_consumer.set_command_callbacks(
        on_pause=_on_pause,
        on_resume=_on_resume,
        on_emergency_stop=_on_emergency_stop,
    )
