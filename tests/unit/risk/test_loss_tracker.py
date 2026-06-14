"""Tests for LossTracker — guard behaviour and daily reset."""

from __future__ import annotations

from datetime import date
from zoneinfo import ZoneInfo
from unittest.mock import patch

import pytest

from src.risk.loss_tracker import LossTracker

UTC = ZoneInfo("UTC")


def _make_tracker(**kwargs) -> LossTracker:
    defaults = dict(max_daily_loss_pct=5.0, engine_tz=UTC)
    defaults.update(kwargs)
    return LossTracker(**defaults)


def _day_ms(d: date) -> int:
    """Epoch-ms for midnight UTC of a given date."""
    from datetime import datetime, timezone
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


# ── Guard 1: Daily loss ───────────────────────────────────────────────────────

def test_first_poll_latches_start_equity():
    tracker = _make_tracker()
    day = date(2026, 1, 6)
    with patch("src.risk.loss_tracker._today", return_value=day), \
         patch("src.risk.loss_tracker._now_ms", return_value=_day_ms(day)):
        tracker.update_daily_loss_pct(0.0, 10_000.0)
    assert tracker.stats()["start_of_day_equity"] == 10_000.0
    assert tracker.stats()["equity_peak"] == 10_000.0


def test_day_boundary_resets_state():
    tracker = _make_tracker()
    day1 = date(2026, 1, 6)
    day2 = date(2026, 1, 7)

    with patch("src.risk.loss_tracker._today", return_value=day1), \
         patch("src.risk.loss_tracker._now_ms", return_value=_day_ms(day1)):
        tracker.update_daily_loss_pct(0.0, 10_000.0)
        tracker.update_equity(10_500.0)
        tracker.update_daily_loss_pct(5.0, 10_000.0)  # triggers Guard 1 pause

    with patch("src.risk.loss_tracker._now_ms", return_value=_day_ms(day1) + 3_600_000):
        paused, _ = tracker.is_paused()
        assert paused

    # New day with valid start_equity
    with patch("src.risk.loss_tracker._today", return_value=day2), \
         patch("src.risk.loss_tracker._now_ms", return_value=_day_ms(day2)):
        tracker.update_daily_loss_pct(0.0, 9_700.0)

    stats = tracker.stats()
    assert stats["start_of_day_equity"] == 9_700.0
    assert stats["equity_peak"] == 9_700.0
    assert stats["profit_drawback_pct"] == 0.0
    assert stats["session_closed_pnl"] == 0.0
    assert stats["session_closed_peak"] == 0.0
    with patch("src.risk.loss_tracker._now_ms", return_value=_day_ms(day2) + 60_000):
        paused, _ = tracker.is_paused()
        assert not paused


def test_day_boundary_resets_even_when_start_equity_zero():
    """Guards reset on day change even if broker data is unavailable (start_equity=0).
    start_of_day_equity is latched on the next valid poll."""
    tracker = _make_tracker()
    day1 = date(2026, 1, 6)
    day2 = date(2026, 1, 7)

    with patch("src.risk.loss_tracker._today", return_value=day1), \
         patch("src.risk.loss_tracker._now_ms", return_value=_day_ms(day1)):
        tracker.update_daily_loss_pct(0.0, 10_000.0)
        tracker.update_equity(10_500.0)
        tracker.update_daily_loss_pct(5.0, 10_000.0)  # pause

    # Day boundary — MT5 offline, start_equity=0
    with patch("src.risk.loss_tracker._today", return_value=day2), \
         patch("src.risk.loss_tracker._now_ms", return_value=_day_ms(day2)):
        tracker.update_daily_loss_pct(0.0, 0.0)

    assert tracker.stats()["profit_drawback_pct"] == 0.0
    paused, _ = tracker.is_paused()
    assert not paused
    assert tracker.stats()["start_of_day_equity"] == 0.0

    # Next poll — MT5 back online
    with patch("src.risk.loss_tracker._today", return_value=day2), \
         patch("src.risk.loss_tracker._now_ms", return_value=_day_ms(day2) + 30_000):
        tracker.update_daily_loss_pct(0.0, 9_700.0)

    assert tracker.stats()["start_of_day_equity"] == 9_700.0
    assert tracker.stats()["equity_peak"] == 9_700.0


def test_deferred_latch_does_not_overwrite_once_set():
    """start_of_day_equity must not be overwritten mid-day once latched."""
    tracker = _make_tracker()
    day = date(2026, 1, 6)

    with patch("src.risk.loss_tracker._today", return_value=day), \
         patch("src.risk.loss_tracker._now_ms", return_value=_day_ms(day)):
        tracker.update_daily_loss_pct(0.0, 10_000.0)   # latched
        tracker.update_daily_loss_pct(2.0, 9_800.0)    # mid-day, must not overwrite

    assert tracker.stats()["start_of_day_equity"] == 10_000.0


def test_daily_risk_amount_after_zero_start_equity_poll():
    """daily_risk_amount returns 0 until start_equity is latched, then correct value."""
    tracker = _make_tracker(max_daily_loss_pct=5.0)
    day = date(2026, 1, 6)

    with patch("src.risk.loss_tracker._today", return_value=day), \
         patch("src.risk.loss_tracker._now_ms", return_value=_day_ms(day)):
        tracker.update_daily_loss_pct(0.0, 0.0)
        assert tracker.daily_risk_amount(3) == 0.0

        tracker.update_daily_loss_pct(0.0, 10_000.0)
        # budget = 10_000 × 5% = 500 / 3 slots ≈ 166.67
        assert pytest.approx(tracker.daily_risk_amount(3), rel=1e-4) == 500.0 / 3


# ── Guard 2: Session profit drawdown ─────────────────────────────────────────

def test_guard2_dormant_with_no_profit():
    """Guard 2 must not fire when there are no profitable closes yet."""
    tracker = _make_tracker(max_equity_drawdown_pct=2.0)
    day = date(2026, 1, 6)

    with patch("src.risk.loss_tracker._today", return_value=day), \
         patch("src.risk.loss_tracker._now_ms", return_value=_day_ms(day)):
        tracker.update_daily_loss_pct(0.0, 10_000.0)
        # Only losses — nothing to protect
        tracker.record_trade_closed(-100.0)
        tracker.record_trade_closed(-80.0)

    paused, _ = tracker.is_paused()
    assert not paused
    assert tracker.stats()["profit_drawback_pct"] == 0.0


def test_guard2_fires_when_profit_given_back():
    """Guard 2 fires when realized gains drop enough from session peak."""
    # start equity = $10,000, threshold = 2% = $200
    tracker = _make_tracker(max_equity_drawdown_pct=2.0)
    day = date(2026, 1, 6)
    t = _day_ms(day)

    with patch("src.risk.loss_tracker._today", return_value=day), \
         patch("src.risk.loss_tracker._now_ms", return_value=t):
        tracker.update_daily_loss_pct(0.0, 10_000.0)
        tracker.record_trade_closed(+500.0)   # peak = +500
        tracker.record_trade_closed(-150.0)   # current = +350, drawback = 150 = 1.5% — OK
        paused, _ = tracker.is_paused()
        assert not paused
        tracker.record_trade_closed(-100.0)   # current = +250, drawback = 250 = 2.5% — fires
        paused, reason = tracker.is_paused()
        assert paused
        assert "profit drawdown" in reason.lower()


def test_guard2_peak_does_not_retreat():
    """Peak is a high-water mark — giving back profit then recovering doesn't lower the peak."""
    tracker = _make_tracker(max_equity_drawdown_pct=5.0)
    day = date(2026, 1, 6)
    t = _day_ms(day)

    with patch("src.risk.loss_tracker._today", return_value=day), \
         patch("src.risk.loss_tracker._now_ms", return_value=t):
        tracker.update_daily_loss_pct(0.0, 10_000.0)
        tracker.record_trade_closed(+300.0)   # peak = 300
        tracker.record_trade_closed(-100.0)   # current = 200, drawback = 100 = 1%
        tracker.record_trade_closed(+50.0)    # current = 250, peak stays at 300

    stats = tracker.stats()
    assert stats["session_closed_peak"] == 300.0
    assert stats["session_closed_pnl"] == 250.0
    assert pytest.approx(stats["profit_drawback_pct"], rel=1e-4) == 50.0 / 10_000.0 * 100.0


def test_guard2_dormant_when_disabled():
    """Guard 2 does not fire when max_equity_drawdown_pct is 0."""
    tracker = _make_tracker(max_equity_drawdown_pct=0.0)
    day = date(2026, 1, 6)
    t = _day_ms(day)

    with patch("src.risk.loss_tracker._today", return_value=day), \
         patch("src.risk.loss_tracker._now_ms", return_value=t):
        tracker.update_daily_loss_pct(0.0, 10_000.0)
        tracker.record_trade_closed(+500.0)
        tracker.record_trade_closed(-1000.0)  # would fire if enabled

    paused, _ = tracker.is_paused()
    assert not paused


def test_guard2_resets_on_new_day():
    """Session P&L tracking resets at midnight."""
    tracker = _make_tracker(max_equity_drawdown_pct=2.0)
    day1 = date(2026, 1, 6)
    day2 = date(2026, 1, 7)

    with patch("src.risk.loss_tracker._today", return_value=day1), \
         patch("src.risk.loss_tracker._now_ms", return_value=_day_ms(day1)):
        tracker.update_daily_loss_pct(0.0, 10_000.0)
        tracker.record_trade_closed(+800.0)   # session peak set

    with patch("src.risk.loss_tracker._today", return_value=day2), \
         patch("src.risk.loss_tracker._now_ms", return_value=_day_ms(day2)):
        tracker.update_daily_loss_pct(0.0, 10_200.0)

    stats = tracker.stats()
    assert stats["session_closed_pnl"] == 0.0
    assert stats["session_closed_peak"] == 0.0
    assert stats["profit_drawback_pct"] == 0.0
