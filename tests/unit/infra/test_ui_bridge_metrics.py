import time
from types import SimpleNamespace

from src.infra.ui_bridge import UIBridge


class _Queue:
    def depth(self) -> int:
        return 0


def _throttle_stats(**overrides) -> dict:
    stats = {
        "enabled": True,
        "engaged": False,
        "multiplier": 1.0,
        "drawdown_r": 0.0,
        "threshold_r": 8.0,
        "release_r": 6.0,
        "window_days": 30,
        "samples": 0,
    }
    stats.update(overrides)
    return stats


def _guards_config() -> SimpleNamespace:
    return SimpleNamespace(
        risk=SimpleNamespace(
            max_daily_loss_percent=2.5,
            max_profit_drawdown_percent=2.0,
            rolling_window_size=2,
            rolling_drawdown_pct=2.0,
            cluster_risk=SimpleNamespace(enabled=False, groups=[]),
        )
    )


def _guards_bridge(throttle_stats: dict) -> UIBridge:
    bridge = UIBridge.__new__(UIBridge)
    bridge._container = SimpleNamespace(
        cluster_tracker=SimpleNamespace(stats=lambda: {}),
        equity_throttle=SimpleNamespace(stats=lambda: throttle_stats),
    )
    return bridge


_LT = {
    "daily_loss_pct": 0.0,
    "equity_drawdown_pct": 0.0,
    "paused": False,
    "pause_reason": "",
}


def test_risk_guards_include_engaged_equity_throttle() -> None:
    bridge = _guards_bridge(
        _throttle_stats(engaged=True, multiplier=0.5, drawdown_r=9.2, samples=120)
    )
    guards = bridge._build_risk_guards(dict(_LT), _guards_config())

    g5 = next(g for g in guards if g["id"] == "guard5")
    assert g5["name"] == "EQUITY THROTTLE"
    assert g5["status"] == "ACTIVE"
    assert g5["unit"] == "R"
    assert g5["current_value"] == 9.2
    assert g5["threshold"] == 8.0
    assert "Sizing at 0.5×" in g5["description"]


def test_risk_guards_equity_throttle_disabled_state() -> None:
    bridge = _guards_bridge(_throttle_stats(enabled=False))
    guards = bridge._build_risk_guards(dict(_LT), _guards_config())

    g5 = next(g for g in guards if g["id"] == "guard5")
    assert g5["status"] == "DISABLED"
    assert "Halves risk" in g5["description"]


class _Repo:
    def __init__(self, trades: list | None = None) -> None:
        self._trades = trades or []

    def load_all(self) -> list:
        return self._trades


def _bridge(trades: list | None = None) -> UIBridge:
    bridge = UIBridge.__new__(UIBridge)
    bridge._container = SimpleNamespace(signal_queue=_Queue(), trade_repo=_Repo(trades))
    return bridge


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        gateway=SimpleNamespace(symbols=["XAUUSD"]),
        risk=SimpleNamespace(
            max_losing_streak=3,
            max_daily_loss_percent=2.0,
        ),
    )


def test_metrics_use_final_trade_outcomes_for_win_rate() -> None:
    metrics = _bridge()._build_metrics_from(
        lt={
            "start_of_day_equity": 5_000.0,
            "daily_loss_pct": 0.0,
            "daily_budget": 100.0,
            "equity_peak": 5_000.0,
            "equity_drawdown_pct": 0.0,
        },
        counters={
            "trades.opened": 3,
            "trades.closed": 3,
            "trades.tp1_hit": 2,
            "trades.tp2_hit": 0,
            "trades.sl_hit": 2,
            "trades.winning": 1,
            "trades.losing": 2,
        },
        gauges={},
        open_trades=[],
        config=_config(),
        account={
            "balance": 4_970.0,
            "equity": 4_970.0,
            "free_margin": 4_970.0,
            "margin": 0.0,
            "margin_level": 0.0,
            "currency": "USD",
        },
    )

    assert metrics["winning_trades"] == 1
    assert metrics["losing_trades"] == 2
    assert metrics["total_trades"] == 3
    assert metrics["win_rate"] == 33.3
    assert metrics["trades_tp1_hit"] == 2
    assert metrics["daily_pnl"] == -30.0


def test_metrics_hydrate_final_outcomes_from_persisted_trades() -> None:
    now_ms = int(time.time() * 1000)
    metrics = _bridge(
        [
            SimpleNamespace(
                status="CLOSED",
                close_reason="TP2_HIT",
                realized_rr=0.0,
                side="BUY",
                entry_price=100.0,
                close_price=110.0,
                closed_at=now_ms,
            ),
            SimpleNamespace(
                status="CLOSED",
                close_reason="MANUAL",
                realized_rr=-0.5,
                side="SELL",
                entry_price=100.0,
                close_price=102.0,
                closed_at=now_ms,
            ),
            SimpleNamespace(
                status="CLOSED",
                close_reason="MANUAL",
                realized_rr=0.8,
                side="BUY",
                entry_price=100.0,
                close_price=99.0,
                closed_at=now_ms,
            ),
        ]
    )._build_metrics_from(
        lt={
            "start_of_day_equity": 5_000.0,
            "daily_loss_pct": 0.0,
            "daily_budget": 100.0,
            "equity_peak": 5_000.0,
            "equity_drawdown_pct": 0.0,
        },
        counters={},
        gauges={},
        open_trades=[],
        config=_config(),
    )

    assert metrics["winning_trades"] == 1
    assert metrics["losing_trades"] == 2
    assert metrics["total_trades"] == 3
    assert metrics["win_rate"] == 33.3


def test_event_queue_drops_oldest_when_full() -> None:
    """BUG-14: the bridge event queue is bounded; overflow drops oldest, not newest."""
    import asyncio

    from src.infra.ui_bridge import _EVENT_QUEUE_MAX

    bridge = _bridge()
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        bridge._queue = asyncio.Queue(maxsize=3)

        for i in range(5):
            bridge._enqueue_event((f"event-{i}", None))

        assert bridge._queue.qsize() == 3
        items = [bridge._queue.get_nowait()[0] for _ in range(3)]
        assert items == ["event-2", "event-3", "event-4"]
    finally:
        asyncio.set_event_loop(None)
        loop.close()
    assert _EVENT_QUEUE_MAX == 500


def test_metrics_show_positive_daily_pnl_from_equity_delta() -> None:
    metrics = _bridge()._build_metrics_from(
        lt={
            "start_of_day_equity": 5_000.0,
            "daily_loss_pct": 0.0,
            "daily_budget": 100.0,
            "equity_peak": 5_040.0,
            "equity_drawdown_pct": 0.0,
        },
        counters={"trades.closed": 1, "trades.winning": 1},
        gauges={},
        open_trades=[],
        config=_config(),
        account={
            "balance": 5_040.0,
            "equity": 5_040.0,
            "free_margin": 5_040.0,
            "margin": 0.0,
            "margin_level": 0.0,
            "currency": "USD",
        },
    )

    assert metrics["daily_pnl"] == 40.0
