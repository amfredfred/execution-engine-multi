from types import SimpleNamespace

from src.domain.trade import OrderSide, Trade, TradePlan, TradeStatus
from src.positions.manager import calculate_realized_rr, _valid_breakeven_sl


def test_calculate_realized_rr_buy_manual_loss_is_negative() -> None:
    trade = _trade(OrderSide.BUY, entry=100.0, stop=95.0)

    assert calculate_realized_rr(trade, 97.5) == -0.5


def test_calculate_realized_rr_sell_manual_loss_is_negative() -> None:
    trade = _trade(OrderSide.SELL, entry=100.0, stop=105.0)

    assert calculate_realized_rr(trade, 102.5) == -0.5


def test_calculate_realized_rr_sell_profit_is_positive() -> None:
    trade = _trade(OrderSide.SELL, entry=100.0, stop=105.0)

    assert calculate_realized_rr(trade, 90.0) == 2.0


def test_valid_breakeven_sl_defers_buy_when_entry_inside_stop_distance() -> None:
    trade = _trade(OrderSide.BUY, entry=100.0, stop=99.0)
    symbol = SimpleNamespace(stops_level=50, freeze_level=0, point=0.01, digits=2)
    tick = SimpleNamespace(bid=100.40, ask=100.45)

    assert _valid_breakeven_sl(trade, symbol, tick) is None


def test_valid_breakeven_sl_allows_buy_when_entry_outside_stop_distance() -> None:
    trade = _trade(OrderSide.BUY, entry=100.0, stop=99.0)
    symbol = SimpleNamespace(stops_level=50, freeze_level=0, point=0.01, digits=2)
    tick = SimpleNamespace(bid=100.60, ask=100.65)

    assert _valid_breakeven_sl(trade, symbol, tick) == 100.0


def test_valid_breakeven_sl_adds_spread_buffer_for_buy() -> None:
    trade = _trade(OrderSide.BUY, entry=100.0, stop=95.0)
    symbol = SimpleNamespace(stops_level=10, freeze_level=0, point=0.01, digits=2)
    tick = SimpleNamespace(bid=101.0, ask=101.10)

    assert _valid_breakeven_sl(trade, symbol, tick, 1.5, 10.0) == 100.15


def test_valid_breakeven_sl_defers_buffered_buy_inside_stop_distance() -> None:
    trade = _trade(OrderSide.BUY, entry=100.0, stop=95.0)
    symbol = SimpleNamespace(stops_level=50, freeze_level=0, point=0.01, digits=2)
    tick = SimpleNamespace(bid=100.50, ask=100.55)

    assert _valid_breakeven_sl(trade, symbol, tick, 1.5, 10.0) is None


def test_valid_breakeven_sl_defers_sell_when_entry_inside_stop_distance() -> None:
    trade = _trade(OrderSide.SELL, entry=100.0, stop=101.0)
    symbol = SimpleNamespace(stops_level=50, freeze_level=0, point=0.01, digits=2)
    tick = SimpleNamespace(bid=99.55, ask=99.60)

    assert _valid_breakeven_sl(trade, symbol, tick) is None


def test_valid_breakeven_sl_allows_sell_when_entry_outside_stop_distance() -> None:
    trade = _trade(OrderSide.SELL, entry=100.0, stop=101.0)
    symbol = SimpleNamespace(stops_level=50, freeze_level=0, point=0.01, digits=2)
    tick = SimpleNamespace(bid=99.35, ask=99.40)

    assert _valid_breakeven_sl(trade, symbol, tick) == 100.0


def test_valid_breakeven_sl_adds_spread_buffer_for_sell() -> None:
    trade = _trade(OrderSide.SELL, entry=100.0, stop=105.0)
    symbol = SimpleNamespace(stops_level=10, freeze_level=0, point=0.01, digits=2)
    tick = SimpleNamespace(bid=98.90, ask=99.0)

    assert _valid_breakeven_sl(trade, symbol, tick, 1.5, 10.0) == 99.85


def test_valid_breakeven_sl_cap_never_reduces_buffer_below_spread() -> None:
    trade = _trade(OrderSide.BUY, entry=100.0, stop=95.0)
    symbol = SimpleNamespace(stops_level=10, freeze_level=0, point=0.01, digits=2)
    tick = SimpleNamespace(bid=102.0, ask=103.0)

    assert _valid_breakeven_sl(trade, symbol, tick, 2.0, 10.0) == 101.0


def test_valid_breakeven_sl_zero_multiplier_uses_entry() -> None:
    trade = _trade(OrderSide.BUY, entry=100.0, stop=95.0)
    symbol = SimpleNamespace(stops_level=10, freeze_level=0, point=0.01, digits=2)
    tick = SimpleNamespace(bid=101.0, ask=101.10)

    assert _valid_breakeven_sl(trade, symbol, tick, 0.0, 10.0) == 100.0


def _trade(side: OrderSide, entry: float, stop: float) -> Trade:
    plan = TradePlan(
        signal_id="sig",
        symbol="XAUUSD",
        side=side,
        entry_price=entry,
        stop_loss=stop,
        tp1=0.0,
        tp2=0.0,
        lot_size=0.01,
        risk_amount=10.0,
        risk_percent=1.0,
        risk_reward_ratio=2.0,
        planned_at=1,
        signal=None,
    )
    return Trade(
        id="trade",
        signal_id="sig",
        symbol="XAUUSD",
        side=side,
        status=TradeStatus.OPEN,
        plan=plan,
        entry_price=entry,
        stop_loss=stop,
    )
