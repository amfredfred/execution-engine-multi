"""Test risk rules."""

from src.config.settings import RiskConfig
from src.domain.position import SymbolInfo
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
from src.risk.rules import RuleContext, min_rr_rule, spread_quality_rule


def test_spread_quality_uses_xauusd_threshold_override() -> None:
    ctx = _context(
        symbol="XAUUSD",
        ask=66864.0,
        bid=66850.0,
        stop_loss=66886.7,
        symbol_thresholds={"XAUUSD": 0.40},
    )

    result = spread_quality_rule(ctx)

    assert result.approved is True


def test_spread_quality_keeps_global_threshold_for_unknown_symbols() -> None:
    ctx = _context(
        symbol="UNKNOWN",
        ask=66864.0,
        bid=66850.0,
        stop_loss=66886.7,
        symbol_thresholds={"XAUUSD": 0.40},
    )

    result = spread_quality_rule(ctx)

    assert result.approved is False
    assert "0.38 > 0.25" in result.reason


def test_min_rr_uses_better_long_pullback_fill() -> None:
    ctx = _context(
        symbol="XAUUSD",
        direction=SignalDirection.LONG,
        ask=99.5,
        bid=99.4,
        entry_price=100.0,
        stop_loss=99.0,
        tp2=105.0,
        risk_reward_ratio=5.0,
        min_rr_ratio=6.0,
        symbol_thresholds={},
    )

    result = min_rr_rule(ctx)

    assert result.approved is True


def test_min_rr_rejects_long_fill_past_stop() -> None:
    ctx = _context(
        symbol="XAUUSD",
        direction=SignalDirection.LONG,
        ask=98.5,
        bid=98.4,
        entry_price=100.0,
        stop_loss=99.0,
        tp2=105.0,
        risk_reward_ratio=5.0,
        min_rr_ratio=1.0,
        symbol_thresholds={},
    )

    result = min_rr_rule(ctx)

    assert result.approved is False
    assert "at/below LONG stop" in result.reason


def test_min_rr_rejects_short_chase_that_loses_reward() -> None:
    ctx = _context(
        symbol="XAUUSD",
        direction=SignalDirection.SHORT,
        ask=95.8,
        bid=95.7,
        entry_price=100.0,
        stop_loss=101.0,
        tp2=95.0,
        risk_reward_ratio=5.0,
        min_rr_ratio=1.0,
        symbol_thresholds={},
    )

    result = min_rr_rule(ctx)

    assert result.approved is False
    assert "Actual R:R 0.13 < minimum 1.0" in result.reason


def _context(
    *,
    symbol: str,
    ask: float,
    bid: float,
    stop_loss: float,
    symbol_thresholds: dict[str, float],
    direction: SignalDirection = SignalDirection.SHORT,
    entry_price: float = 66855.0,
    tp2: float = 66791.6,
    risk_reward_ratio: float = 2.0,
    min_rr_ratio: float = 1.0,
) -> RuleContext:
    return RuleContext(
        signal=_signal(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            tp2=tp2,
            risk_reward_ratio=risk_reward_ratio,
        ),
        open_trades=[],
        config=_risk_config(symbol_thresholds, min_rr_ratio=min_rr_ratio),
        daily_loss_pct=0.0,
        effective_open=0,
        effective_symbol=0,
        symbol_info=_symbol_info(symbol=symbol, ask=ask, bid=bid),
    )


def _risk_config(
    symbol_thresholds: dict[str, float], min_rr_ratio: float = 1.0
) -> RiskConfig:
    return RiskConfig(
        max_losing_streak=3,
        max_daily_loss_percent=2.5,
        max_exposure_per_symbol=2,
        min_rr_ratio=min_rr_ratio,
        max_lot_size=100.0,
        min_lot_size=0.01,
        sl_ratio_threshold=0.25,
        symbol_sl_ratio_threshold=symbol_thresholds,
    )


def _symbol_info(*, symbol: str, ask: float, bid: float) -> SymbolInfo:
    return SymbolInfo(
        symbol=symbol,
        description=symbol,
        currency_base=symbol,
        currency_profit="USD",
        currency_margin="USD",
        digits=1,
        point=1.0,
        tick_size=1.0,
        tick_value=1.0,
        contract_size=1.0,
        lot_min=0.01,
        lot_max=100.0,
        lot_step=0.01,
        ask=ask,
        bid=bid,
        spread=int(ask - bid),
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


def _signal(
    *,
    symbol: str,
    direction: SignalDirection,
    entry_price: float,
    stop_loss: float,
    tp2: float,
    risk_reward_ratio: float,
) -> InboundSignal:
    is_short = direction == SignalDirection.SHORT
    return InboundSignal(
        id=f"{symbol}-test",
        symbol=symbol,
        resolved_symbol=symbol,
        direction=direction,
        status=SignalStatus.TRIGGERED,
        entry_price=entry_price,
        stop_loss=stop_loss,
        tp1=entry_price + (tp2 - entry_price) * 0.5,
        tp2=tp2,
        risk_reward_ratio=risk_reward_ratio,
        risk_pips=abs(entry_price - stop_loss),
        htf_range=HtfRange(
            range_high=66980.0,
            range_low=66790.0,
            bos_direction=BosDirection.BEARISH if is_short else BosDirection.BULLISH,
            timestamp=1,
            broken_at=1,
            tp_level=tp2,
            midpoint=66885.0,
            height=190.0,
            htf_candle_open=1,
            htf_candle_close=2,
        ),
        ltf_range=LtfRange(
            range_high=66980.0,
            range_low=66915.0,
            timestamp=1,
            direction=direction,
            sl_level=stop_loss,
        ),
        rejection_candle=RejectionCandle(
            open=66940.0,
            high=66980.0,
            low=66915.0,
            close=entry_price,
            timestamp=1,
            wick_ratio=0.3,
            pattern=CandlePattern.CRT_SELL if is_short else CandlePattern.CRT_BUY,
            wick_tip=66980.0,
        ),
        created_at=1,
        triggered_at=1,
    )
