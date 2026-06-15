"""
Dependency injection container.

Builds and wires all components from configuration.
Returns a plain dataclass — no framework required.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import TYPE_CHECKING

from src.brokers.mt5.client import Mt5Client
from src.brokers.mt5.orders import Mt5Orders
from src.brokers.mt5.positions import Mt5Positions

from src.config.settings import AppConfig
from src.core.event_bus import EventBus
from src.execution.engine import ExecutionEngine
from src.execution.order_manager import OrderManager
from src.execution.planner import TradePlanner
from src.infra.db import Database
from src.positions.manager import PositionManager
from src.positions.store import PositionStore
from src.risk.cluster_tracker import ClusterRiskTracker
from src.risk.engine import RiskEngine
from src.risk.equity_throttle import EquityThrottleTracker
from src.risk.loss_tracker import LossTracker
from src.signals.consumer import SignalConsumer
from src.signals.queue import SignalQueue
from src.signals.signal_validator import SignalValidator
from src.infra.database import TradeRepository
from src.strategies.adapter import PassthroughAdapter
from src.strategies.router import StrategyRouter

if TYPE_CHECKING:
    from src.infra.ui_bridge import UIBridge


@dataclass
class AppContainer:
    db: Database
    event_bus: EventBus
    signal_consumer: SignalConsumer
    signal_queue: SignalQueue
    execution_engine: ExecutionEngine
    position_manager: PositionManager
    mt5_client: Mt5Client
    mt5_orders: Mt5Orders
    mt5_positions: Mt5Positions
    position_store: PositionStore
    trade_repo: TradeRepository
    strategy_router: StrategyRouter
    loss_tracker: LossTracker
    cluster_tracker: ClusterRiskTracker
    equity_throttle: EquityThrottleTracker
    ui_bridge: "UIBridge | None" = None
    runtime_ready: threading.Event = field(default_factory=threading.Event)
    runtime_error: str | None = None


def build_container(config: AppConfig) -> AppContainer:
    # ── Core ──────────────────────────────────────────────────────────────
    event_bus = EventBus()

    # ── Database ──────────────────────────────────────────────────────────
    db = Database(config.storage_path)

    # ── Broker ────────────────────────────────────────────────────────────
    mt5_client = Mt5Client(config.mt5)
    mt5_orders = Mt5Orders(mt5_client)
    mt5_positions = Mt5Positions(mt5_client)

    # ── Storage ───────────────────────────────────────────────────────────
    trade_repo = TradeRepository(db)
    position_store = PositionStore()

    # ── Risk + execution ──────────────────────────────────────────────────
    loss_tracker = LossTracker(
        max_daily_loss_pct      = config.risk.max_daily_loss_percent,
        engine_tz               = config.engine_timezone,
        max_equity_drawdown_pct = config.risk.max_profit_drawdown_percent,
        rolling_window_size     = config.risk.rolling_window_size,
        rolling_drawdown_pct    = config.risk.rolling_drawdown_pct,
    )
    cluster_tracker = ClusterRiskTracker(
        config=config.risk.cluster_risk,
        engine_tz=config.engine_timezone,
    )
    equity_throttle = EquityThrottleTracker(config.risk.equity_throttle)
    risk_engine = RiskEngine(
        config.risk,
        loss_tracker=loss_tracker,
        cluster_tracker=cluster_tracker,
        equity_throttle=equity_throttle,
    )
    trade_planner = TradePlanner(config.risk, config.execution, loss_tracker)
    order_manager = OrderManager(mt5_orders, mt5_positions, config.execution)

    execution_engine = ExecutionEngine(
        risk_engine=risk_engine,
        trade_planner=trade_planner,
        order_manager=order_manager,
        mt5_positions=mt5_positions,
        position_store=position_store,
        trade_repo=trade_repo,
        event_bus=event_bus,
        exec_config=config.execution,
        loss_tracker=loss_tracker,
        cluster_tracker=cluster_tracker,
    )

    # ── Position management ───────────────────────────────────────────────
    position_manager = PositionManager(
        store=position_store,
        mt5_pos=mt5_positions,
        mt5_orders=mt5_orders,
        repository=trade_repo,
        execution_engine=execution_engine,
        event_bus=event_bus,
        exec_config=config.execution,
        poll_interval=config.position_poll_interval,
    )

    # ── Signal ingestion ──────────────────────────────────────────────────
    signal_queue = SignalQueue(on_signal=execution_engine.execute)
    validator = SignalValidator(max_age_ms=config.execution.max_signal_age_ms)
    signal_consumer = SignalConsumer(
        event_bus=event_bus,
        validator=validator,
        ws_url=config.gateway.ws_url,
        activation_key=config.gateway.activation_key,
        symbols=config.gateway.symbols,
        engine_id=config.gateway.engine_id,
        engine_version=config.gateway.engine_version,
        room_ttl_seconds=config.gateway.room_ttl_seconds,
        account_login=str(config.mt5.login),
        signal_hmac_secret=config.gateway.signal_hmac_secret,
        db=db,
    )

    # ── Strategies ────────────────────────────────────────────────────────
    strategy_router = StrategyRouter()
    strategy_router.register("default", PassthroughAdapter())

    return AppContainer(
        db=db,
        event_bus=event_bus,
        signal_consumer=signal_consumer,
        signal_queue=signal_queue,
        execution_engine=execution_engine,
        position_manager=position_manager,
        mt5_client=mt5_client,
        mt5_orders=mt5_orders,
        mt5_positions=mt5_positions,
        trade_repo=trade_repo,
        position_store=position_store,
        strategy_router=strategy_router,
        loss_tracker=loss_tracker,
        cluster_tracker=cluster_tracker,
        equity_throttle=equity_throttle,
    )
