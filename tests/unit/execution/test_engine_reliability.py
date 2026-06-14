from types import SimpleNamespace

from src.config.settings import ExecutionConfig
from src.core.events import Events
from src.domain.position import AccountInfo, SymbolInfo
from dataclasses import replace
from src.domain.signal_interface import (
    BosDirection,
    CandlePattern,
    HtfRange,
    InboundSignal,
    LtfRange,
    RejectionCandle,
    SignalDirection,
    SignalStatus,
)
from src.domain.trade import OrderSide, TradePlan
from src.execution.engine import ExecutionEngine
from src.utils.time import now_ms


def test_duplicate_signal_is_rejected_before_risk_or_order_execution() -> None:
    signal = _signal("sig-1")
    bus = _Bus()
    risk = _Risk()
    orders = _Orders()

    engine = ExecutionEngine(
        risk_engine=risk,
        trade_planner=_Planner(),
        order_manager=orders,
        mt5_positions=_Positions(),
        position_store=_Store(existing_signal_id="sig-1"),
        trade_repo=_Repo(),
        event_bus=bus,
        exec_config=_execution_config(),
    )

    assert engine.execute(signal) is None
    assert risk.calls == 0
    assert orders.calls == 0
    assert bus.events[0][0] == Events.RISK_REJECTED
    assert bus.events[0][1]["signal"].id == signal.id
    assert bus.events[0][1]["reason"] == "duplicate_signal"


def test_stale_signal_is_rejected_before_broker_or_order_execution() -> None:
    ts = now_ms()
    signal = replace(
        _signal("sig-stale"),
        setup_candle_close_at=ts - 120_000,
        emitted_at=ts - 119_000,
        received_at=ts - 118_000,
    )
    bus = _Bus()
    risk = _Risk()
    orders = _Orders()
    positions = _Positions()

    engine = ExecutionEngine(
        risk_engine=risk,
        trade_planner=_Planner(),
        order_manager=orders,
        mt5_positions=positions,
        position_store=_Store(),
        trade_repo=_Repo(),
        event_bus=bus,
        exec_config=_execution_config(max_signal_age_ms=90_000),
    )

    assert engine.execute(signal) is None
    assert positions.calls == 0
    assert risk.calls == 0
    assert orders.calls == 0
    assert bus.events[0][0] == Events.RISK_REJECTED
    assert bus.events[0][1]["reason"] == "stale_signal"


def test_fresh_signal_executes_with_timing_fields() -> None:
    ts = now_ms()
    signal = replace(
        _signal("sig-fresh"),
        setup_candle_close_at=ts - 1_000,
        emitted_at=ts - 900,
        received_at=ts - 800,
        queued_at=ts - 700,
    )
    bus = _Bus()
    risk = _Risk()
    orders = _Orders()
    store = _Store()

    engine = ExecutionEngine(
        risk_engine=risk,
        trade_planner=_Planner(),
        order_manager=orders,
        mt5_positions=_Positions(),
        position_store=store,
        trade_repo=_Repo(),
        event_bus=bus,
        exec_config=_execution_config(max_signal_age_ms=90_000),
    )

    trade = engine.execute(signal)

    assert trade is not None
    assert risk.calls == 1
    assert orders.calls == 1
    assert store.added
    assert any(event == Events.TRADE_OPENED for event, _ in bus.events)


def test_price_drift_does_not_bypass_live_rr_risk_evaluation() -> None:
    ts = now_ms()
    signal = replace(
        _signal("sig-price-drift"),
        setup_candle_close_at=ts - 1_000,
        emitted_at=ts - 900,
        received_at=ts - 800,
    )
    bus = _Bus()
    risk = _Risk()
    orders = _Orders()

    engine = ExecutionEngine(
        risk_engine=risk,
        trade_planner=_Planner(),
        order_manager=orders,
        mt5_positions=_Positions(ask=1.0800, bid=1.0798),
        position_store=_Store(),
        trade_repo=_Repo(),
        event_bus=bus,
        exec_config=_execution_config(max_signal_age_ms=90_000),
    )

    trade = engine.execute(signal)

    assert trade is not None
    assert risk.calls == 1
    assert orders.calls == 1
    assert any(event == Events.TRADE_OPENED for event, _ in bus.events)


class _Bus:
    def __init__(self) -> None:
        self.events = []

    def emit(self, event: str, payload=None) -> None:
        self.events.append((event, payload))


class _Risk:
    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, *args, **kwargs):
        self.calls += 1
        return SimpleNamespace(approved=True, reason=None, data={}, risk_multiplier=1.0)


class _Orders:
    def __init__(self) -> None:
        self.calls = 0

    def execute_market_order(self, *args, **kwargs):
        self.calls += 1
        return 1, 1.0, 0.01


class _Planner:
    def plan(self, signal: InboundSignal, *args, **kwargs):
        side = OrderSide.BUY if signal.direction == SignalDirection.LONG else OrderSide.SELL
        return TradePlan(
            signal_id=signal.id,
            symbol=signal.resolved_symbol or signal.symbol,
            side=side,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            tp1=signal.tp1,
            tp2=signal.tp2,
            lot_size=0.01,
            risk_amount=10.0,
            risk_percent=1.0,
            risk_reward_ratio=signal.risk_reward_ratio,
            planned_at=now_ms(),
            signal=signal,
        )


class _Positions:
    def __init__(self, ask: float = 1.10002, bid: float = 1.10000) -> None:
        self.calls = 0
        self.ask = ask
        self.bid = bid

    def get_account_info(self) -> AccountInfo:
        self.calls += 1
        return AccountInfo(
            login=1,
            server="demo",
            currency="USD",
            balance=10_000.0,
            equity=10_000.0,
            margin=0.0,
            free_margin=10_000.0,
            margin_level=0.0,
            leverage=100,
        )

    def get_symbol_info(self, symbol: str) -> SymbolInfo:
        self.calls += 1
        return SymbolInfo(
            symbol=symbol,
            description=symbol,
            currency_base="EUR",
            currency_profit="USD",
            currency_margin="USD",
            digits=5,
            point=0.00001,
            tick_size=0.00001,
            tick_value=1.0,
            contract_size=100_000.0,
            lot_min=0.01,
            lot_max=100.0,
            lot_step=0.01,
            ask=self.ask,
            bid=self.bid,
            spread=int(abs(self.ask - self.bid) / 0.00001),
            spread_float=True,
            margin_initial=0.0,
            margin_maintenance=0.0,
            margin_hedged=0.0,
            filling_mode=1,
            execution_mode=0,
            trade_mode=0,
            swap_mode=0,
            swap_long=0.0,
            swap_short=0.0,
            swap_rollover3days=3,
            stops_level=0,
            freeze_level=0,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
        )


class _Store:
    def __init__(self, existing_signal_id: str | None = None) -> None:
        self._existing_signal_id = existing_signal_id
        self.added = []

    def get_by_signal_id(self, signal_id: str):
        return object() if signal_id == self._existing_signal_id else None

    def get_open_trades(self):
        return list(self.added)

    def add(self, trade):
        self.added.append(trade)


class _Repo:
    def save(self, trade):
        return True


def _signal(signal_id: str) -> InboundSignal:
    ts = now_ms()
    return InboundSignal(
        id=signal_id,
        symbol="EUR/USD",
        resolved_symbol="EURUSD",
        direction=SignalDirection.LONG,
        status=SignalStatus.TRIGGERED,
        entry_price=1.1000,
        stop_loss=1.0950,
        tp1=1.1050,
        tp2=1.1100,
        risk_reward_ratio=2.0,
        risk_pips=50.0,
        htf_range=HtfRange(
            range_high=1.1100,
            range_low=1.0900,
            bos_direction=BosDirection.BULLISH,
            timestamp=ts,
            broken_at=ts,
            tp_level=1.1100,
            midpoint=1.1000,
            height=0.0200,
            htf_candle_open=ts,
            htf_candle_close=ts + 60_000,
        ),
        ltf_range=LtfRange(
            range_high=1.1050,
            range_low=1.0950,
            timestamp=ts,
            direction=SignalDirection.LONG,
            sl_level=1.0950,
        ),
        rejection_candle=RejectionCandle(
            open=1.0990,
            high=1.1010,
            low=1.0950,
            close=1.1000,
            timestamp=ts,
            wick_ratio=0.5,
            pattern=CandlePattern.HAMMER,
            wick_tip=1.0950,
        ),
        created_at=ts,
        triggered_at=ts,
        setup_candle_open_at=ts - 60_000,
        setup_candle_close_at=ts,
    )


def _execution_config(max_signal_age_ms: int = 90_000) -> ExecutionConfig:
    return ExecutionConfig(
        tp1_trigger_pct=50.0,
        tp1_percentage=50.0,
        move_sl_to_be_on_tp1=True,
        slippage=10,
        magic=12345,
        comment="test",
        spread_risk_multiplier=1.0,
        order_retry_count=0,
        max_entry_slippage_pct_of_stop=0.2,
        close_on_slippage_exceed=True,
        order_retry_delay_sec=0.0,
        max_signal_age_ms=max_signal_age_ms,
    )
