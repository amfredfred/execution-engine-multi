from unittest.mock import MagicMock

from src.domain.signal_interface import InboundSignal
from src.core.event_bus import EventBus
from src.core.event_types import Events
from src.managed.client import ManagedAgentClient
from src.manager.models import AgentRegistration, AgentStatus
from src.manager.signal_router import ManagerSignalRouter


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
    channel = MagicMock()
    channel.forward_signal.return_value = True
    router = ManagerSignalRouter(registry, channel, "key", "wss://gateway", "1.0")

    router._on_signal(InboundSignal.from_dict(_signal_payload("fbs")))

    channel.forward_signal.assert_called_once()
    agent_id, forwarded = channel.forward_signal.call_args.args
    assert agent_id == "fbs-running"
    assert forwarded["broker"] == "fbs"
    assert forwarded["entryPrice"] == 3342.5


def test_managed_agent_accepts_forwarded_wire_payload_for_execution() -> None:
    container = MagicMock()
    container.event_bus = EventBus()
    received = []
    container.event_bus.on(Events.SIGNAL_TRIGGERED, received.append)
    client = ManagedAgentClient("agent-0", "ws://localhost:8871", "token", container)

    client._handle_signal_forward(InboundSignal.from_dict(_signal_payload()).to_dict())

    assert len(received) == 1
    assert received[0].id == "signal-route-001"
    assert received[0].broker == "fbs"


def test_manager_to_matching_agent_execution_event_pipeline() -> None:
    container = MagicMock()
    container.event_bus = EventBus()
    execution_events = []
    container.event_bus.on(Events.SIGNAL_TRIGGERED, execution_events.append)
    managed_client = ManagedAgentClient(
        "fbs-agent", "ws://localhost:8871", "token", container,
    )

    registry = MagicMock()
    registry.list_agents.return_value = [
        _agent("fbs-agent", "FBS-Real"),
        _agent("exness-agent", "Exness-MT5Real"),
    ]
    channel = MagicMock()

    def _deliver(agent_id: str, payload: dict) -> bool:
        assert agent_id == "fbs-agent"
        managed_client._handle_signal_forward(payload)
        return True

    channel.forward_signal.side_effect = _deliver
    router = ManagerSignalRouter(registry, channel, "key", "wss://gateway", "1.0")

    router._on_signal(InboundSignal.from_dict(_signal_payload("fbs")))

    assert [signal.id for signal in execution_events] == ["signal-route-001"]
