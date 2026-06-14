"""Tests for ClusterRiskTracker — budget, multiplier, and daily-reset behaviour."""

from __future__ import annotations

from datetime import datetime, date, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from src.config.settings import ClusterGroupConfig, ClusterRiskConfig
from src.risk.cluster_tracker import ClusterRiskTracker, ClusterPreview

UTC = ZoneInfo("UTC")

_GROUP = ClusterGroupConfig(
    name="index_gold_cluster",
    symbols=("US100", "US500", "XAUUSD"),
    max_same_day_loss_r=1.5,
    max_concurrent_positions=2,
    max_same_day_losses=2,
    after_first_loss_risk_multiplier=0.5,
    min_trade_risk_multiplier=0.25,
)
_CONFIG = ClusterRiskConfig(enabled=True, groups=(_GROUP,))


def _make_tracker() -> ClusterRiskTracker:
    return ClusterRiskTracker(config=_CONFIG, engine_tz=UTC)


class _Signal:
    def __init__(self, symbol: str, signal_id: str = "sig-1", resolved: str | None = None):
        self.symbol = symbol
        self.id = signal_id
        self.resolved_symbol = resolved or symbol


class _Trade:
    def __init__(
        self,
        trade_id: str,
        symbol: str,
        signal_id: str = "sig-1",
        realized_rr: float | None = None,
    ):
        self.id = trade_id
        self.symbol = symbol
        self.signal_id = signal_id
        self.realized_rr = realized_rr


def _fixed_today(d: date):
    """Context manager that pins datetime.now() to midnight of the given date."""
    fixed = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return patch(
        "src.risk.cluster_tracker.datetime",
        **{"now.return_value": fixed},
    )


# ── Test 1 — first trade gets full 1.0R ───────────────────────────────────────

def test_first_trade_full_risk():
    tracker = _make_tracker()
    day = date(2026, 1, 6)
    with _fixed_today(day):
        preview = tracker.preview(_Signal("US100"))
    assert preview.approved
    assert preview.risk_multiplier == 1.0
    assert preview.cluster_name == "index_gold_cluster"


# ── Test 2 — second concurrent trade reduced to remaining budget ───────────────

def test_second_trade_reduced_to_remaining_budget():
    tracker = _make_tracker()
    day = date(2026, 1, 6)
    with _fixed_today(day):
        sig1 = _Signal("US100", "sig-1")
        preview1 = tracker.preview(sig1)
        tracker.reserve_signal(sig1, preview1.planned_risk_r)
        tracker.mark_trade_opened(_Trade("t-1", "US100", "sig-1"))

        sig2 = _Signal("XAUUSD", "sig-2")
        preview2 = tracker.preview(sig2)

    assert preview2.approved
    # open_r = 1.0, remaining = 0.5, base_multiplier = 1.0 → planned = 0.5
    assert preview2.risk_multiplier == pytest.approx(0.5)


# ── Test 3 — third concurrent trade rejected (concurrent limit reached first) ──

def test_third_trade_rejected_concurrent_limit():
    """With 2 open trades the concurrent limit (2) fires before the budget check."""
    tracker = _make_tracker()
    day = date(2026, 1, 6)
    with _fixed_today(day):
        sig1 = _Signal("US100", "sig-1")
        p1 = tracker.preview(sig1)
        tracker.reserve_signal(sig1, p1.planned_risk_r)
        tracker.mark_trade_opened(_Trade("t-1", "US100", "sig-1"))

        sig2 = _Signal("XAUUSD", "sig-2")
        p2 = tracker.preview(sig2)
        tracker.reserve_signal(sig2, p2.planned_risk_r)
        tracker.mark_trade_opened(_Trade("t-2", "XAUUSD", "sig-2"))

        # concurrent = 2 = limit → rejected
        preview3 = tracker.preview(_Signal("US500", "sig-3"))

    assert not preview3.approved
    assert "concurrent limit" in preview3.reason


# ── Test 3b — budget exhaustion (without hitting concurrent limit) ─────────────

def test_budget_exhausted_after_loss():
    """After one loss the budget shrinks; a subsequent trade consumes remaining budget,
    leaving too little for the next signal."""
    # Use max_concurrent_positions=3 so the concurrent check doesn't fire first.
    group = ClusterGroupConfig(
        name="test_cluster",
        symbols=("US100", "US500", "XAUUSD"),
        max_same_day_loss_r=1.5,
        max_concurrent_positions=3,
        max_same_day_losses=5,
        after_first_loss_risk_multiplier=0.5,
        min_trade_risk_multiplier=0.25,
    )
    tracker = ClusterRiskTracker(
        config=ClusterRiskConfig(enabled=True, groups=(group,)),
        engine_tz=UTC,
    )
    day = date(2026, 1, 6)
    with _fixed_today(day):
        # First trade: 1.0R → loss → realized_loss_r = 1.0
        sig1 = _Signal("US100", "sig-1")
        p1 = tracker.preview(sig1)
        tracker.reserve_signal(sig1, p1.planned_risk_r)
        tracker.mark_trade_opened(_Trade("t-1", "US100", "sig-1"))
        tracker.mark_trade_closed(_Trade("t-1", "US100", "sig-1", realized_rr=-1.0))

        # Second trade: loss_count=1 → base_mult=0.5, remaining=0.5 → approved at 0.5R
        sig2 = _Signal("US500", "sig-2")
        p2 = tracker.preview(sig2)
        assert p2.approved
        tracker.reserve_signal(sig2, p2.planned_risk_r)
        tracker.mark_trade_opened(_Trade("t-2", "US500", "sig-2"))

        # Third trade: pending/open = 0.5R → remaining = 0, planned = min(0.5, 0) = 0 < 0.25 → rejected
        preview3 = tracker.preview(_Signal("XAUUSD", "sig-3"))

    assert not preview3.approved
    assert "budget exhausted" in preview3.reason


# ── Test 4 — after first loss, next trade is at 0.5x ─────────────────────────

def test_after_first_loss_reduces_risk():
    tracker = _make_tracker()
    day = date(2026, 1, 6)
    with _fixed_today(day):
        sig1 = _Signal("US100", "sig-1")
        p1 = tracker.preview(sig1)
        tracker.reserve_signal(sig1, p1.planned_risk_r)
        tracker.mark_trade_opened(_Trade("t-1", "US100", "sig-1"))
        tracker.mark_trade_closed(_Trade("t-1", "US100", "sig-1", realized_rr=-1.0))

        preview = tracker.preview(_Signal("US500", "sig-2"))

    assert preview.approved
    assert preview.risk_multiplier == pytest.approx(0.5)


# ── Test 5 — after two losses cluster is fully blocked ────────────────────────

def test_two_losses_block_cluster():
    tracker = _make_tracker()
    day = date(2026, 1, 6)
    with _fixed_today(day):
        for i in range(2):
            sig = _Signal("US100", f"sig-{i}")
            p = tracker.preview(sig)
            tracker.reserve_signal(sig, p.planned_risk_r)
            tracker.mark_trade_opened(_Trade(f"t-{i}", "US100", f"sig-{i}"))
            tracker.mark_trade_closed(
                _Trade(f"t-{i}", "US100", f"sig-{i}", realized_rr=-1.0)
            )

        preview = tracker.preview(_Signal("XAUUSD", "sig-x"))

    assert not preview.approved
    assert "2/2 losses" in preview.reason


# ── Test 6 — non-cluster symbol is unaffected ─────────────────────────────────

def test_non_cluster_symbol_unaffected():
    tracker = _make_tracker()
    day = date(2026, 1, 6)
    with _fixed_today(day):
        preview = tracker.preview(_Signal("EURUSD", "sig-1"))
    assert preview.approved
    assert preview.risk_multiplier == 1.0
    assert preview.cluster_name is None


# ── Test 7 — different TF pair still matches the same cluster ─────────────────

def test_different_tf_still_in_cluster():
    """Symbol-only matching: US100 regardless of timeframe is in the cluster."""
    tracker = _make_tracker()
    day = date(2026, 1, 6)
    with _fixed_today(day):
        # First open a 1.0R US100 trade
        sig1 = _Signal("US100", "sig-1")
        p1 = tracker.preview(sig1)
        tracker.reserve_signal(sig1, p1.planned_risk_r)
        tracker.mark_trade_opened(_Trade("t-1", "US100", "sig-1"))

        # Now another US100 signal (simulating a different TF pair) — still cluster-scoped
        preview = tracker.preview(_Signal("US100", "sig-2"))

    # Concurrent limit is 2, budget remaining is 0.5
    assert preview.approved
    assert preview.risk_multiplier == pytest.approx(0.5)


# ── Test 8 — wins release budget (do not count as losses) ─────────────────────

def test_win_releases_budget_does_not_consume_loss_r():
    tracker = _make_tracker()
    day = date(2026, 1, 6)
    with _fixed_today(day):
        sig1 = _Signal("US100", "sig-1")
        p1 = tracker.preview(sig1)
        tracker.reserve_signal(sig1, p1.planned_risk_r)
        tracker.mark_trade_opened(_Trade("t-1", "US100", "sig-1"))
        # Win — should free open_r and not increment loss_count
        tracker.mark_trade_closed(_Trade("t-1", "US100", "sig-1", realized_rr=2.5))

        stats = tracker.stats()

    assert stats["index_gold_cluster"]["loss_count"] == 0
    assert stats["index_gold_cluster"]["realized_loss_r"] == 0.0
    # open_r freed — budget is fully available again
    assert stats["index_gold_cluster"]["open_r"] == 0.0


# ── Test 9 — max_concurrent_positions blocks before budget check ──────────────

def test_concurrent_limit_blocks_third_trade():
    tracker = _make_tracker()
    day = date(2026, 1, 6)
    with _fixed_today(day):
        for i, sym in enumerate(["US100", "US500"]):
            sig = _Signal(sym, f"sig-{i}")
            p = tracker.preview(sig)
            tracker.reserve_signal(sig, p.planned_risk_r)
            tracker.mark_trade_opened(_Trade(f"t-{i}", sym, f"sig-{i}"))

        # Concurrent = 2 = limit → blocked before budget check
        preview = tracker.preview(_Signal("XAUUSD", "sig-x"))

    assert not preview.approved
    assert "concurrent limit" in preview.reason


# ── Test 10 — disabled tracker passes everything through ─────────────────────

def test_disabled_tracker_approves_all():
    config = ClusterRiskConfig(enabled=False, groups=(_GROUP,))
    tracker = ClusterRiskTracker(config=config, engine_tz=UTC)
    day = date(2026, 1, 6)
    with _fixed_today(day):
        preview = tracker.preview(_Signal("US100"))
    assert preview.approved
    assert preview.risk_multiplier == 1.0


# ── Test 11 — reservation released on signal cancel ──────────────────────────

def test_release_signal_removes_pending():
    tracker = _make_tracker()
    day = date(2026, 1, 6)
    with _fixed_today(day):
        sig = _Signal("US100", "sig-1")
        tracker.reserve_signal(sig, 1.0)
        assert tracker.stats()["index_gold_cluster"]["pending_count"] == 1

        tracker.release_signal(sig)
        assert tracker.stats()["index_gold_cluster"]["pending_count"] == 0


# ── Test 12 — daily reset clears state on new day ─────────────────────────────

def test_daily_reset_on_new_day():
    tracker = _make_tracker()
    day1 = date(2026, 1, 6)
    day2 = date(2026, 1, 7)

    with _fixed_today(day1):
        sig = _Signal("US100", "sig-1")
        p = tracker.preview(sig)
        tracker.reserve_signal(sig, p.planned_risk_r)
        tracker.mark_trade_opened(_Trade("t-1", "US100", "sig-1"))
        tracker.mark_trade_closed(_Trade("t-1", "US100", "sig-1", realized_rr=-1.0))

    # New day — state should reset
    with _fixed_today(day2):
        preview = tracker.preview(_Signal("US100", "sig-2"))

    assert preview.approved
    assert preview.risk_multiplier == 1.0
    stats = tracker.stats()
    assert stats["index_gold_cluster"]["loss_count"] == 0
    assert stats["index_gold_cluster"]["realized_loss_r"] == 0.0
