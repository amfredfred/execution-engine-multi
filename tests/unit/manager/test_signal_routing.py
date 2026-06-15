from unittest.mock import MagicMock

from src.domain.signal_interface import InboundSignal
from src.core.event_bus import EventBus
from src.core.event_types import Events
from manager.app.models import AgentRegistration, AgentStatus
from manager.app.signal_router import ManagerSignalRouter
from src.runtime.contracts import EngineCommand, EngineCommandType
from src.worker.event_client import WorkerEventClient


def _signal_payload(broker: str = "fbs") -> dict:
    return {
        "id": "signal-route-001",
        "symbol": "XAUUSD",
        "broker": broker,
        "direction": "LONG",
        "status": "TRIGGERED",
        "entryPrice": 3342.5,
        "stopLoss": 3334.0,
        "tp1": 3351.0,
        "tp2": 3360.0,
        "riskRewardRatio": 2.0,
        "riskPips": 8.5,
        "htfRange": {
            "rangeHigh": 3350.0,
            "rangeLow": 3320.0,
            "bosDirection": "BULLISH",
            "timestamp": 1,
            "brokenAt": 2,
            "tpLevel": 3360.0,
        },
        "rejectionCandle": {
            "open": 3340.0,
            "high": 3344.0,
            "low": 3338.0,
            "close": 3342.5,
            "timestamp": 3,
            "wickRatio": 0.6,
            "pattern": "HAMMER",
            "wickTip": 3338.0,
        },
        "createdAt": 4,
        "triggeredAt": 4,
    }


def _agent(agent_id: str, server: str, status=AgentStatus.RUNNING, symbols=None):
    return AgentRegistration(
        agent_id=agent_id,
        display_name=agent_id,
        status=status,
        desired_status="running",
        config_path="config.yaml",
        data_dir="data",
        terminal_path="terminal64.exe",
        mt5_login=123,
        mt5_server=server,
        monitoring_port=8081,
        symbols=symbols or ["XAUUSD"],
        created_at=0,
        updated_at=0,
        last_seen_at=0,
        pid=1,
    )


def test_signal_wire_round_trip_preserves_broker_and_execution_fields() -> None:
    signal = InboundSignal.from_dict(_signal_payload())

    forwarded = signal.to_dict()
    reparsed = InboundSignal.from_dict(forwarded)

    assert forwarded["entryPrice"] == 3342.5
    assert "entry_price" not in forwarded
    assert reparsed.id == signal.id
    assert reparsed.broker == "fbs"
    assert reparsed.symbol == "XAUUSD"


def test_manager_routes_only_to_running_matching_broker_and_symbol() -> None:
    registry = MagicMock()
    registry.list_agents.return_value = [
        _agent("fbs-running", "FBS-Real"),
        _agent("exness-running", "Exness-MT5Real"),
        _agent("fbs-stopped", "FBS-Real", status=AgentStatus.STOPPED),
        _agent("fbs-wrong-symbol", "FBS-Real", symbols=["EURUSD"]),
    ]
    event_hub = MagicMock()
    event_hub.deliver_signal.return_value = True
    router = ManagerSignalRouter(registry, event_hub, "key", "wss://gateway", "1.0")

    router._on_signal(InboundSignal.from_dict(_signal_payload("fbs")))

    event_hub.deliver_signal.assert_called_once()
    agent_id, forwarded = event_hub.deliver_signal.call_args.args
    assert agent_id == "fbs-running"
    assert forwarded["broker"] == "fbs"
    assert forwarded["entryPrice"] == 3342.5


def test_worker_accepts_signal_command_for_execution(tmp_path) -> None:
    container = MagicMock()
    container.event_bus = EventBus()
    received = []
    container.event_bus.on(Events.SIGNAL_TRIGGERED, received.append)
    client = WorkerEventClient(
        "agent-0", "127.0.0.1", 8871, "token", container, 123, "FBS-Real", str(tmp_path),
    )

    client._handle_command(EngineCommand(
        engine_id="agent-0",
        command_type=EngineCommandType.SIGNAL_DELIVER,
        payload={"signal": InboundSignal.from_dict(_signal_payload()).to_dict()},
    ))

    assert len(received) == 1
    assert received[0].id == "signal-route-001"
    assert received[0].broker == "fbs"


def test_manager_to_matching_agent_execution_event_pipeline(tmp_path) -> None:
    container = MagicMock()
    container.event_bus = EventBus()
    execution_events = []
    container.event_bus.on(Events.SIGNAL_TRIGGERED, execution_events.append)
    worker = WorkerEventClient(
        "fbs-agent", "127.0.0.1", 8871, "token", container, 123, "FBS-Real", str(tmp_path),
    )

    registry = MagicMock()
    registry.list_agents.return_value = [
        _agent("fbs-agent", "FBS-Real"),
        _agent("exness-agent", "Exness-MT5Real"),
    ]
    event_hub = MagicMock()

    def _deliver(agent_id: str, payload: dict) -> bool:
        assert agent_id == "fbs-agent"
        worker._handle_command(EngineCommand(
            engine_id=agent_id,
            command_type=EngineCommandType.SIGNAL_DELIVER,
            payload={"signal": payload},
        ))
        return True

    event_hub.deliver_signal.side_effect = _deliver
    router = ManagerSignalRouter(registry, event_hub, "key", "wss://gateway", "1.0")

    router._on_signal(InboundSignal.from_dict(_signal_payload("fbs")))

    assert [signal.id for signal in execution_events] == ["signal-route-001"]


def test_manager_publishes_agent_snapshot_as_virtual_execution_source() -> None:
    registry = MagicMock()
    registry.get_agent.return_value = _agent("fbs-agent", "FBS-Real")
    router = ManagerSignalRouter(registry, MagicMock(), "key", "wss://gateway", "1.0")
    router._consumer = MagicMock()
    snapshot = MagicMock(
        mt5_login=123,
        mt5_server="FBS-Real",
        mt5_connected=True,
        status=AgentStatus.RUNNING,
        balance=1000.0,
        equity=999.0,
        open_trades=1,
        uptime_sec=60,
        observed_at=1234,
    )

    router.publish_agent_snapshot("fbs-agent", snapshot)

    event, payload = router._consumer._send.call_args.args
    assert event == "manager.agent.snapshot"
    assert payload["engine_id"] == "execution-123"
    assert payload["account"]["server"] == "FBS-Real"
    assert payload["metrics"]["metrics"]["open_trades"] == 1


def test_manager_publishes_full_child_telemetry_snapshot() -> None:
    registry = MagicMock()
    registry.get_agent.return_value = _agent("fbs-agent", "FBS-Real")
    router = ManagerSignalRouter(registry, MagicMock(), "key", "wss://gateway", "1.0")
    router._consumer = MagicMock()
    telemetry = {
        "connected": True,
        "engine": {"status": "RUNNING"},
        "metrics": {"balance": 1000.0, "daily_pnl": 25.0},
        "trades": [{"symbol": "XAUUSD"}],
        "riskGuards": [{"id": "guard1"}],
        "signals": [{"id": "signal-1"}],
    }
    snapshot = MagicMock(
        mt5_login=123,
        mt5_server="FBS-Real",
        mt5_connected=True,
        status=AgentStatus.RUNNING,
        telemetry=telemetry,
    )

    router.publish_agent_snapshot("fbs-agent", snapshot)

    _, payload = router._consumer._send.call_args.args
    assert payload["metrics"] == telemetry


def test_manager_attributes_child_execution_event_to_virtual_source() -> None:
    registry = MagicMock()
    registry.get_agent.return_value = _agent("fbs-agent", "FBS-Real")
    router = ManagerSignalRouter(registry, MagicMock(), "key", "wss://gateway", "1.0")
    router._consumer = MagicMock()

    router.publish_agent_event("fbs-agent", "trade.opened", {"symbol": "XAUUSD"})

    router._consumer._send.assert_called_once_with(
        "execution.event",
        {
            "engine_id": "execution-123",
            "event_type": "trade.opened",
            "data": {"symbol": "XAUUSD"},
        },
    )
