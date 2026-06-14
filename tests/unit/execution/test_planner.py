"""Test trade planner."""

from __future__ import annotations

from src.config.settings import ExecutionConfig, RiskConfig
from src.domain.position import AccountInfo, SymbolInfo
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
from src.execution.planner import TradePlanner


class _LossTracker:
    def daily_risk_amount(self, max_losing_streak: int) -> float:
        return 100.0


def _account() -> AccountInfo:
    return AccountInfo(
        login=1,
        server="test",
        currency="USD",
        balance=10_000.0,
        equity=10_000.0,
        margin=0.0,
        free_margin=10_000.0,
        margin_level=0.0,
        leverage=100,
    )


def _planner() -> TradePlanner:
    return TradePlanner(
        RiskConfig(
            max_losing_streak=3,
            max_daily_loss_percent=2.0,
            max_exposure_per_symbol=2,
            min_rr_ratio=1.0,
            max_lot_size=100.0,
            min_lot_size=0.01,
            sl_ratio_threshold=0.35,
            symbol_sl_ratio_threshold={},
        ),
        ExecutionConfig(
            tp1_trigger_pct=50.0,
            tp1_percentage=0.0,
            move_sl_to_be_on_tp1=True,
            slippage=10,
            magic=12345,
            comment="test",
            spread_risk_multiplier=0.0,
            order_retry_count=0,
            max_entry_slippage_pct_of_stop=0.0,
            close_on_slippage_exceed=False,
            order_retry_delay_sec=0.0,
            tf_overrides={},
        ),
        _LossTracker(),
    )


def test_planner_records_applied_risk_multiplier_and_scales_size():
    planner = _planner()
    sig = _signal(htf_interval="1min", ltf_interval="1min")

    full = planner.plan(sig, _account(), _symbol_info())
    half = planner.plan(sig, _account(), _symbol_info(), risk_multiplier=0.5)

    assert full.risk_multiplier == 1.0
    assert half.risk_multiplier == 0.5
    assert half.risk_amount == full.risk_amount * 0.5
    assert half.lot_size == full.lot_size * 0.5


def test_planner_clamps_risk_multiplier_to_one():
    planner = _planner()
    sig = _signal(htf_interval="1min", ltf_interval="1min")
    plan = planner.plan(sig, _account(), _symbol_info(), risk_multiplier=1.7)
    assert plan.risk_multiplier == 1.0


def test_planner_uses_symbol_and_timeframe_specific_tp1_trigger_pct():
    planner = TradePlanner(
        RiskConfig(
            max_losing_streak=3,
            max_daily_loss_percent=2.0,
            max_exposure_per_symbol=2,
            min_rr_ratio=1.0,
            max_lot_size=100.0,
            min_lot_size=0.01,
            sl_ratio_threshold=0.35,
            symbol_sl_ratio_threshold={},
        ),
        ExecutionConfig(
            tp1_trigger_pct=50.0,
            tp1_percentage=0.0,
            move_sl_to_be_on_tp1=True,
            slippage=10,
            magic=12345,
            comment="test",
            spread_risk_multiplier=0.0,
            order_retry_count=0,
            max_entry_slippage_pct_of_stop=0.0,
            close_on_slippage_exceed=False,
            order_retry_delay_sec=0.0,
            tf_overrides={"XAUUSD": {"1/1": {"tp1_trigger_pct": 5.0}}},
        ),
        _LossTracker(),
    )

    plan = planner.plan(
        _signal(htf_interval="1min", ltf_interval="1min"),
        AccountInfo(
            login=1,
            server="test",
            currency="USD",
            balance=10_000.0,
            equity=10_000.0,
            margin=0.0,
            free_margin=10_000.0,
            margin_level=0.0,
            leverage=100,
        ),
        _symbol_info(),
    )

    assert plan.tp1 == 100.5


def test_execution_config_wildcard_tf_fallback():
    cfg = ExecutionConfig(
        tp1_trigger_pct=50.0,
        tp1_percentage=0.0,
        move_sl_to_be_on_tp1=True,
        slippage=10,
        magic=12345,
        comment="test",
        spread_risk_multiplier=0.0,
        order_retry_count=0,
        max_entry_slippage_pct_of_stop=0.0,
        close_on_slippage_exceed=False,
        order_retry_delay_sec=0.0,
        tf_overrides={
            "XAUUSD": {
                "1/1": {"tp1_trigger_pct": 30.0},
                "*":   {"tp1_trigger_pct": 40.0},  # fallback TF
            },
        },
    )
    # Exact symbol+TF match
    assert cfg.tp1_trigger_pct_for("XAUUSD", "1min", "1min") == 30.0
    # Symbol matches, TF falls back to "*"
    assert cfg.tp1_trigger_pct_for("XAUUSD", "5min", "5min") == 40.0
    # Unknown symbol falls back to global default
    assert cfg.tp1_trigger_pct_for("US100", "1min", "1min") == 50.0


def test_execution_config_wildcard_symbol_fallback():
    cfg = ExecutionConfig(
        tp1_trigger_pct=50.0,
        tp1_percentage=0.0,
        move_sl_to_be_on_tp1=True,
        slippage=10,
        magic=12345,
        comment="test",
        spread_risk_multiplier=0.0,
        order_retry_count=0,
        max_entry_slippage_pct_of_stop=0.0,
        close_on_slippage_exceed=False,
        order_retry_delay_sec=0.0,
        tf_overrides={
            "XAUUSD": {"1/1": {"tp1_trigger_pct": 30.0}},
            "*":       {"1/1": {"tp1_trigger_pct": 20.0}},  # wildcard symbol
        },
    )
    # Exact symbol+TF
    assert cfg.tp1_trigger_pct_for("XAUUSD", "1min", "1min") == 30.0
    # Unknown symbol falls back to "*" symbol entry
    assert cfg.tp1_trigger_pct_for("US100", "1min", "1min") == 20.0
    # Unknown symbol, unknown TF → global default
    assert cfg.tp1_trigger_pct_for("US100", "5min", "5min") == 50.0


def test_execution_config_supports_tp1_close_pct_override_alias():
    cfg = ExecutionConfig(
        tp1_trigger_pct=50.0,
        tp1_percentage=0.0,
        move_sl_to_be_on_tp1=True,
        slippage=10,
        magic=12345,
        comment="test",
        spread_risk_multiplier=0.0,
        order_retry_count=0,
        max_entry_slippage_pct_of_stop=0.0,
        close_on_slippage_exceed=False,
        order_retry_delay_sec=0.0,
        tf_overrides={"XAUUSD": {"1/1": {"tp1_percentage": 25.0}}},
    )

    assert cfg.tp1_percentage_for("XAUUSD", "1min", "1min") == 25.0
    assert cfg.tp1_percentage_for("XAUUSD", "5min", "5min") == 0.0


def _signal(*, htf_interval: str, ltf_interval: str) -> InboundSignal:
    return InboundSignal(
        id="sig-1",
        symbol="XAUUSD",
        direction=SignalDirection.LONG,
        status=SignalStatus.TRIGGERED,
        entry_price=100.0,
        stop_loss=99.0,
        tp1=105.0,
        tp2=110.0,
        risk_reward_ratio=10.0,
        risk_pips=1.0,
        htf_range=HtfRange(
            range_high=101.0,
            range_low=99.0,
            bos_direction=BosDirection.BULLISH,
            timestamp=1,
            broken_at=1,
            tp_level=110.0,
            midpoint=100.0,
            height=2.0,
            htf_candle_open=1,
            htf_candle_close=2,
        ),
        ltf_range=LtfRange(
            range_high=101.0,
            range_low=99.0,
            timestamp=1,
            direction=SignalDirection.LONG,
            sl_level=99.0,
        ),
        rejection_candle=RejectionCandle(
            open=99.5,
            high=100.5,
            low=99.0,
            close=100.0,
            timestamp=1,
            wick_ratio=0.7,
            pattern=CandlePattern.CRT_BUY,
            wick_tip=99.0,
        ),
        created_at=1,
        htf_interval=htf_interval,
        ltf_interval=ltf_interval,
    )


def _symbol_info() -> SymbolInfo:
    return SymbolInfo(
        symbol="XAUUSD",
        description="Gold",
        currency_base="XAU",
        currency_profit="USD",
        currency_margin="USD",
        digits=2,
        point=0.01,
        tick_size=0.01,
        tick_value=1.0,
        contract_size=100.0,
        lot_min=0.01,
        lot_max=100.0,
        lot_step=0.01,
        ask=100.01,
        bid=100.00,
        spread=1,
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
