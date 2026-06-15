from unittest.mock import MagicMock

import json
import time

from src.domain.signal_interface import InboundSignal
from src.core.event_bus import EventBus
from src.core.event_types import Events
from manager.app.models import AgentRegistration, AgentStatus
from manager.app.registry import AgentRegistry
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
        "createdAt": int(time.time() * 1000),
        "triggeredAt": int(time.time() * 1000),
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
    queued = []
    registry.queue_signal_delivery.side_effect = (
        lambda signal_id, agent_id, payload, expires_at: queued.append({
            "signal_id": signal_id, "agent_id": agent_id,
            "payload_json": json.dumps(payload), "command_id": None,
            "attempts": 0, "expires_at": expires_at,
        })
    )
    registry.list_due_signal_deliveries.side_effect = lambda _now: list(queued)
    event_hub.submit_command.return_value = "command-1"
    router = ManagerSignalRouter(registry, event_hub, "wss://gateway", "key")

    router._on_signal(InboundSignal.from_dict(_signal_payload("fbs")))

    event_hub.submit_command.assert_called_once()
    agent_id, _, payload = event_hub.submit_command.call_args.args
    forwarded = payload["signal"]
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


def test_worker_persistently_deduplicates_signal_delivery(tmp_path) -> None:
    container = MagicMock()
    container.event_bus = EventBus()
    received = []
    container.event_bus.on(Events.SIGNAL_TRIGGERED, received.append)
    command = EngineCommand(
        engine_id="agent-0",
        command_type=EngineCommandType.SIGNAL_DELIVER,
        payload={"signal": InboundSignal.from_dict(_signal_payload()).to_dict()},
    )
    first = WorkerEventClient(
        "agent-0", "127.0.0.1", 8871, "token", container,
        123, "FBS-Real", str(tmp_path),
    )
    first._handle_command(command)
    restarted = WorkerEventClient(
        "agent-0", "127.0.0.1", 8871, "token", container,
        123, "FBS-Real", str(tmp_path),
    )
    restarted._handle_command(command)

    assert [signal.id for signal in received] == ["signal-route-001"]


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
    queued = []
    registry.queue_signal_delivery.side_effect = (
        lambda signal_id, agent_id, payload, expires_at: queued.append({
            "signal_id": signal_id, "agent_id": agent_id,
            "payload_json": json.dumps(payload), "command_id": None,
            "attempts": 0, "expires_at": expires_at,
        })
    )
    registry.list_due_signal_deliveries.side_effect = lambda _now: list(queued)

    def _deliver(agent_id: str, _command_type, payload: dict) -> str:
        assert agent_id == "fbs-agent"
        worker._handle_command(EngineCommand(
            engine_id=agent_id,
            command_type=EngineCommandType.SIGNAL_DELIVER,
            payload=payload,
        ))
        return "command-1"

    event_hub.submit_command.side_effect = _deliver
    router = ManagerSignalRouter(registry, event_hub, "wss://gateway", "key")

    router._on_signal(InboundSignal.from_dict(_signal_payload("fbs")))

    assert [signal.id for signal in execution_events] == ["signal-route-001"]


def test_manager_refreshes_signal_manager_subscription_when_symbols_change() -> None:
    router = ManagerSignalRouter(MagicMock(), MagicMock(), "wss://signal-manager", "key")
    router._current_symbols = {"XAUUSD"}
    router._client = MagicMock()

    router.refresh_rooms([_agent("fbs-agent", "FBS-Real", symbols=["XAUUSD", "EURUSD"])])

    router._client.update_symbols.assert_called_once()
    assert set(router._client.update_symbols.call_args.args[0]) == {"XAUUSD", "EURUSD"}


def test_manager_does_not_resubscribe_when_symbols_are_unchanged() -> None:
    router = ManagerSignalRouter(MagicMock(), MagicMock(), "wss://signal-manager", "key")
    router._current_symbols = {"XAUUSD"}
    router._client = MagicMock()

    router.refresh_rooms([_agent("fbs-agent", "FBS-Real", symbols=["XAUUSD"])])

    router._client.update_symbols.assert_not_called()


def test_durable_signal_delivery_retries_and_records_acceptance(tmp_path) -> None:
    registry = AgentRegistry(str(tmp_path / "manager"))
    registry.init()
    event_hub = MagicMock()
    event_hub.submit_command.return_value = "command-1"
    router = ManagerSignalRouter(registry, event_hub, "", "")
    payload = _signal_payload()
    now = int(time.time() * 1000)

    registry.queue_signal_delivery("signal-1", "agent-1", payload, now + 60_000)
    registry.queue_signal_delivery("signal-1", "agent-1", payload, now + 60_000)
    router._process_due_deliveries()

    delivery = registry.get_signal_delivery("signal-1", "agent-1")
    assert delivery["status"] == "sent"
    assert delivery["attempts"] == 1
    registry.record_command("command-1", "agent-1", "signal.deliver", "sent")
    registry.complete_command("command-1", "completed")
    registry.update_signal_delivery(
        "signal-1", "agent-1", status="sent", next_attempt_at=0
    )
    router._process_due_deliveries()

    assert registry.get_signal_delivery("signal-1", "agent-1")["status"] == "accepted"
    reopened = AgentRegistry(str(tmp_path / "manager"))
    reopened.init()
    assert reopened.get_signal_delivery("signal-1", "agent-1")["status"] == "accepted"


def test_stale_queued_signal_expires_without_delivery(tmp_path) -> None:
    registry = AgentRegistry(str(tmp_path / "manager"))
    registry.init()
    event_hub = MagicMock()
    router = ManagerSignalRouter(registry, event_hub, "", "")
    registry.queue_signal_delivery("signal-1", "agent-1", {}, 1)

    router._process_due_deliveries()

    assert registry.get_signal_delivery("signal-1", "agent-1")["status"] == "expired"
    event_hub.submit_command.assert_not_called()


def test_manager_forwards_worker_execution_events_upstream() -> None:
    router = ManagerSignalRouter(MagicMock(), MagicMock(), "", "")
    router._client = MagicMock()
    router._client.send_execution_event.return_value = True

    router.forward_execution_event("agent-1", "trade.opened", {"trade_id": "trade-1"})

    router._client.send_execution_event.assert_called_once_with(
        "agent-1", "trade.opened", {"trade_id": "trade-1"}
    )
