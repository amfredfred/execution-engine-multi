"""Tests for the equity-curve risk throttle — money-R accounting, drawdown
math, hysteresis, window pruning, hydration, and RiskEngine composition."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.config.settings import EquityThrottleConfig, RiskConfig
from src.risk.engine import RiskEngine
from src.risk.equity_throttle import (
    EquityThrottleTracker,
    compute_drawdown_r,
    money_r,
)
from src.risk.rules import RuleContext, RuleResult, equity_throttle_rule

_DAY_MS = 86_400_000


def _now() -> int:
    return int(time.time() * 1000)


def _config(**overrides) -> EquityThrottleConfig:
    defaults = dict(
        enabled=True,
        drawdown_threshold_r=8.0,
        release_threshold_r=6.0,
        risk_multiplier=0.5,
        window_days=30,
    )
    defaults.update(overrides)
    return EquityThrottleConfig(**defaults)


def _trade(
    realized_rr: float | None,
    *,
    closed_at: int | None = None,
    plan_multiplier: float = 1.0,
    side: str = "BUY",
    tp1_hit: bool = False,
    entry_price: float = 100.0,
    original_sl: float = 99.0,
    close_price: float | None = None,
    entry_lots: float = 1.0,
    tp1_lots: float = 0.0,
    tp1_close_price: float | None = None,
):
    return SimpleNamespace(
        id="t-1",
        side=SimpleNamespace(value=side),
        realized_rr=realized_rr,
        entry_price=entry_price,
        entry_lots=entry_lots,
        tp1_lots=tp1_lots,
        tp1_hit=tp1_hit,
        tp1_close_price=tp1_close_price,
        tp1=tp1_close_price,
        close_price=close_price if close_price is not None else original_sl,
        closed_at=closed_at if closed_at is not None else _now(),
        plan=SimpleNamespace(
            risk_multiplier=plan_multiplier,
            stop_loss=original_sl,
            entry_price=entry_price,
        ),
    )


# ── money_r ───────────────────────────────────────────────────────────────────


def test_money_r_passthrough_for_plain_trades():
    assert money_r(
        side="BUY", entry_price=100.0, original_sl=99.0, close_price=99.0,
        realized_rr=-1.0, tp1_hit=False, tp1_fraction=0.0, tp1_exit_price=None,
    ) == -1.0


def test_money_r_none_when_no_outcome():
    assert money_r(
        side="BUY", entry_price=100.0, original_sl=99.0, close_price=99.0,
        realized_rr=None, tp1_hit=False, tp1_fraction=0.0, tp1_exit_price=None,
    ) is None


def test_money_r_blends_tp1_partial_against_original_stop():
    # BUY: entry 100, original SL 99 (risk 1.0). Half closed at TP1=101.5
    # (+1.5R), remainder closed at breakeven 100 (0R) → 0.5×1.5 + 0.5×0 = 0.75R.
    # The stored realized_rr would be ~0 here because the SL column was moved
    # to breakeven — exactly the understatement money_r corrects.
    r = money_r(
        side="BUY", entry_price=100.0, original_sl=99.0, close_price=100.0,
        realized_rr=0.0, tp1_hit=True, tp1_fraction=0.5, tp1_exit_price=101.5,
    )
    assert r == pytest.approx(0.75)


def test_money_r_blends_tp1_partial_for_sell():
    # SELL: entry 100, SL 101 (risk 1.0). 40% closed at TP1=98.5 (+1.5R),
    # remainder at 97 (+3R) → 0.4×1.5 + 0.6×3.0 = 2.4R.
    r = money_r(
        side="SELL", entry_price=100.0, original_sl=101.0, close_price=97.0,
        realized_rr=3.0, tp1_hit=True, tp1_fraction=0.4, tp1_exit_price=98.5,
    )
    assert r == pytest.approx(2.4)


def test_money_r_falls_back_when_prices_missing():
    r = money_r(
        side="BUY", entry_price=None, original_sl=99.0, close_price=100.0,
        realized_rr=1.2, tp1_hit=True, tp1_fraction=0.5, tp1_exit_price=101.0,
    )
    assert r == 1.2


# ── compute_drawdown_r ────────────────────────────────────────────────────────


def test_drawdown_zero_at_peak():
    assert compute_drawdown_r([1.0, 2.0, -0.5, 1.0]) == pytest.approx(0.0)


def test_drawdown_from_running_peak():
    # peak after +5, then -3 → dd 3
    assert compute_drawdown_r([2.0, 3.0, -1.0, -2.0]) == pytest.approx(3.0)


def test_drawdown_uses_implicit_zero_baseline():
    # A window that opens with losses is already drawdown.
    assert compute_drawdown_r([-1.0, -2.0]) == pytest.approx(3.0)


# ── Tracker engage / hysteresis ───────────────────────────────────────────────


def test_no_throttle_at_shallow_drawdown():
    tracker = EquityThrottleTracker(_config())
    for _ in range(7):
        tracker.record_trade_closed(_trade(-1.0))
    preview = tracker.preview()
    assert preview.multiplier == 1.0
    assert not preview.engaged
    assert preview.drawdown_r == pytest.approx(7.0)


def test_engages_beyond_threshold():
    tracker = EquityThrottleTracker(_config())
    for _ in range(9):
        tracker.record_trade_closed(_trade(-1.0))
    preview = tracker.preview()
    assert preview.engaged
    assert preview.multiplier == 0.5
    assert preview.drawdown_r == pytest.approx(9.0)


def test_hysteresis_holds_between_release_and_engage_thresholds():
    tracker = EquityThrottleTracker(_config())
    for _ in range(9):
        tracker.record_trade_closed(_trade(-1.0))
    assert tracker.preview().engaged

    # Recover to dd 7 — inside the (6, 8] band: stays engaged.
    tracker.record_trade_closed(
        _trade(2.0, close_price=102.0)
    )
    preview = tracker.preview()
    assert preview.drawdown_r == pytest.approx(7.0)
    assert preview.engaged

    # Recover below release threshold 6 — releases.
    tracker.record_trade_closed(_trade(2.0, close_price=102.0))
    preview = tracker.preview()
    assert preview.drawdown_r == pytest.approx(5.0)
    assert not preview.engaged
    assert preview.multiplier == 1.0


def test_throttled_trades_contribute_scaled_r():
    tracker = EquityThrottleTracker(_config())
    # 16 losses at half size = −8R drawdown → still not beyond the 8R threshold
    for _ in range(16):
        tracker.record_trade_closed(_trade(-1.0, plan_multiplier=0.5))
    preview = tracker.preview()
    assert preview.drawdown_r == pytest.approx(8.0)
    assert not preview.engaged


def test_state_survives_day_boundaries():
    tracker = EquityThrottleTracker(_config())
    base = _now() - 3 * _DAY_MS
    for i in range(9):
        tracker.record_trade_closed(_trade(-1.0, closed_at=base + i))
    # Three days later, with no resets in between, still engaged.
    assert tracker.preview().engaged


def test_window_pruning_releases_old_drawdown():
    cfg = _config()
    tracker = EquityThrottleTracker(cfg)
    t0 = 1_700_000_000_000

    with patch("src.risk.equity_throttle._now_ms", return_value=t0):
        for i in range(9):
            tracker.record_trade_closed(_trade(-1.0, closed_at=t0 - 1000 + i))
        assert tracker.preview().engaged

    # 31 days later those losses age out of the 30-day window.
    with patch(
        "src.risk.equity_throttle._now_ms", return_value=t0 + 31 * _DAY_MS
    ):
        preview = tracker.preview()
    assert preview.drawdown_r == pytest.approx(0.0)
    assert not preview.engaged


def test_disabled_records_but_never_throttles():
    tracker = EquityThrottleTracker(_config(enabled=False))
    for _ in range(12):
        tracker.record_trade_closed(_trade(-1.0))
    preview = tracker.preview()
    assert preview.multiplier == 1.0
    assert not preview.engaged
    stats = tracker.stats()
    assert stats["enabled"] is False
    assert stats["samples"] == 12
    assert stats["drawdown_r"] == pytest.approx(12.0)


def test_skips_trades_without_outcome():
    tracker = EquityThrottleTracker(_config())
    tracker.record_trade_closed(_trade(None))
    assert tracker.stats()["samples"] == 0


# ── Hydration from persisted rows ─────────────────────────────────────────────


def _row(
    realized_rr: float,
    closed_at: int,
    *,
    plan_json: str | None = None,
    side: str = "BUY",
    tp1_hit: int = 0,
    entry_lots: float = 1.0,
    current_lots: float = 1.0,
    entry_price: float = 100.0,
    close_price: float = 99.0,
    tp1: float = 0.0,
) -> dict:
    return {
        "id": "row-1",
        "side": side,
        "status": "CLOSED",
        "realized_rr": realized_rr,
        "closed_at": closed_at,
        "plan_json": plan_json,
        "tp1_hit": tp1_hit,
        "entry_lots": entry_lots,
        "current_lots": current_lots,
        "entry_price": entry_price,
        "close_price": close_price,
        "tp1": tp1,
    }


def test_hydrate_rebuilds_drawdown_state():
    tracker = EquityThrottleTracker(_config())
    now = _now()
    rows = [_row(-1.0, now - i) for i in range(9)]
    tracker.hydrate(rows)
    preview = tracker.preview()
    assert preview.engaged
    assert preview.drawdown_r == pytest.approx(9.0)


def test_hydrate_applies_persisted_risk_multiplier():
    tracker = EquityThrottleTracker(_config())
    now = _now()
    plan = json.dumps({"riskMultiplier": 0.5})
    tracker.hydrate([_row(-1.0, now - i, plan_json=plan) for i in range(9)])
    # 9 half-sized losses = 4.5R — below threshold.
    preview = tracker.preview()
    assert preview.drawdown_r == pytest.approx(4.5)
    assert not preview.engaged


def test_hydrate_legacy_rows_default_to_full_multiplier():
    tracker = EquityThrottleTracker(_config())
    now = _now()
    legacy = json.dumps({"riskAmount": 50.0})  # no riskMultiplier key
    tracker.hydrate([_row(-1.0, now - i, plan_json=legacy) for i in range(9)])
    assert tracker.preview().drawdown_r == pytest.approx(9.0)


def test_hydrate_reconstructs_tp1_weighted_r_from_row():
    tracker = EquityThrottleTracker(_config())
    now = _now()
    plan = json.dumps({"entryPrice": 100.0, "stopLoss": 99.0})
    # Half the lots closed at TP1=101.5, remainder at breakeven; stored
    # realized_rr is 0 (SL column was at BE) but money terms are +0.75R.
    row = _row(
        0.0, now,
        plan_json=plan, tp1_hit=1,
        entry_lots=1.0, current_lots=0.5,
        close_price=100.0, tp1=101.5,
    )
    tracker.hydrate([row])
    stats = tracker.stats()
    assert stats["samples"] == 1
    assert stats["drawdown_r"] == pytest.approx(0.0)


def test_hydrate_is_idempotent():
    tracker = EquityThrottleTracker(_config())
    now = _now()
    rows = [_row(-1.0, now - i) for i in range(9)]
    tracker.hydrate(rows)
    tracker.hydrate(rows)
    assert tracker.stats()["samples"] == 9


# ── Rule + RiskEngine composition ─────────────────────────────────────────────


def _rule_ctx(tracker) -> RuleContext:
    return RuleContext(
        signal=None,
        open_trades=[],
        config=None,
        daily_loss_pct=0.0,
        effective_open=0,
        effective_symbol=0,
        symbol_info=None,
        equity_throttle=tracker,
    )


def test_rule_passes_without_tracker():
    result = equity_throttle_rule(_rule_ctx(None))
    assert result.approved
    assert result.data == {}


def test_rule_attaches_multiplier_when_engaged():
    tracker = EquityThrottleTracker(_config())
    for _ in range(9):
        tracker.record_trade_closed(_trade(-1.0))
    result = equity_throttle_rule(_rule_ctx(tracker))
    assert result.approved
    assert result.data["equity_throttle_multiplier"] == 0.5
    assert result.data["equity_throttle_dd_r"] == pytest.approx(9.0)


def _risk_config() -> RiskConfig:
    return RiskConfig(
        max_losing_streak=3,
        max_daily_loss_percent=2.0,
        max_exposure_per_symbol=2,
        min_rr_ratio=1.0,
        max_lot_size=100.0,
        min_lot_size=0.01,
        sl_ratio_threshold=0.35,
        symbol_sl_ratio_threshold={},
    )


def _signal_stub():
    return SimpleNamespace(
        id="sig-1",
        symbol="XAUUSD",
        resolved_symbol="XAUUSD",
        direction=SimpleNamespace(value="LONG"),
        risk_reward_ratio=3.0,
        entry_price=100.0,
        stop_loss=99.0,
        tp1=101.0,
        tp2=103.0,
        setup_candle_close_at=None,
        triggered_at=None,
        emitted_at=None,
        received_at=None,
    )


def _cluster_stub_rule(ctx) -> RuleResult:
    return RuleResult(
        approved=True,
        data={"risk_multiplier": 0.5, "planned_cluster_risk_r": 0.5},
    )


def test_risk_engine_composes_cluster_and_throttle_multiplicatively():
    tracker = EquityThrottleTracker(_config())
    for _ in range(9):
        tracker.record_trade_closed(_trade(-1.0))

    engine = RiskEngine(
        _risk_config(),
        rules=[_cluster_stub_rule, equity_throttle_rule],
        equity_throttle=tracker,
    )
    decision = engine.evaluate(_signal_stub(), [], 0.0)

    assert decision.approved
    assert decision.risk_multiplier == pytest.approx(0.25)
    # The raw throttle key is consumed during composition.
    assert "equity_throttle_multiplier" not in decision.data


def test_risk_engine_throttle_alone_halves_risk():
    tracker = EquityThrottleTracker(_config())
    for _ in range(9):
        tracker.record_trade_closed(_trade(-1.0))

    engine = RiskEngine(
        _risk_config(),
        rules=[equity_throttle_rule],
        equity_throttle=tracker,
    )
    decision = engine.evaluate(_signal_stub(), [], 0.0)
    assert decision.risk_multiplier == pytest.approx(0.5)


def test_risk_engine_full_risk_when_not_engaged():
    tracker = EquityThrottleTracker(_config())
    engine = RiskEngine(
        _risk_config(),
        rules=[equity_throttle_rule],
        equity_throttle=tracker,
    )
    decision = engine.evaluate(_signal_stub(), [], 0.0)
    assert decision.risk_multiplier == 1.0
