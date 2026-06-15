import json
import socket
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.manager.config_revisions import ConfigRevisionService
from src.manager.event_hub import EngineEventHub
from src.manager.models import AgentRegistration, AgentStatus
from src.manager.registry import AgentRegistry
from src.runtime.contracts import (
    EngineCommand,
    EngineCommandType,
    EngineEvent,
    EngineEventType,
)
from src.worker.event_client import WorkerEventClient


def _registration() -> AgentRegistration:
    return AgentRegistration(
        agent_id="engine-1",
        display_name="Engine 1",
        status=AgentStatus.STARTING,
        desired_status="running",
        config_path="config.yaml",
        data_dir="data",
        terminal_path="terminal64.exe",
        mt5_login=123,
        mt5_server="Broker",
        monitoring_port=8081,
        symbols=["XAUUSD"],
        created_at=1,
        updated_at=1,
        last_seen_at=None,
        pid=44,
    )


def test_command_and_event_contracts_round_trip() -> None:
    command = EngineCommand(
        engine_id="engine-1",
        command_type=EngineCommandType.SIGNAL_DELIVER,
        payload={"signal": {"id": "signal-1"}},
    )
    event = EngineEvent(
        engine_id="engine-1",
        sequence=2,
        event_type=EngineEventType.ENGINE_SNAPSHOT,
        payload={"balance": 1000},
    )

    assert EngineCommand.from_wire(command.to_wire()) == command
    assert EngineEvent.from_wire(event.to_wire()) == event


def test_event_hub_receives_snapshot_and_sends_command() -> None:
    registry = MagicMock()
    registry.get_agent.return_value = _registration()
    hub = EngineEventHub(registry, "secret", port=0)
    hub.start()
    port = hub._server.server_address[1]
    client = socket.create_connection(("127.0.0.1", port), timeout=3)
    reader = client.makefile("r", encoding="utf-8")
    writer = client.makefile("w", encoding="utf-8")

    def send(event: EngineEvent) -> None:
        writer.write(json.dumps(event.to_wire()) + "\n")
        writer.flush()

    send(EngineEvent("engine-1", 1, EngineEventType.WORKER_HELLO, {"token": "secret"}))
    send(EngineEvent("engine-1", 2, EngineEventType.WORKER_READY, {"pid": 44}))
    send(EngineEvent(
        "engine-1",
        3,
        EngineEventType.ENGINE_SNAPSHOT,
        {"status": "RUNNING", "balance": 1000, "telemetry": {"metrics": {"balance": 1000}}},
    ))
    deadline = time.time() + 2
    while hub.get_snapshot("engine-1") is None and time.time() < deadline:
        time.sleep(0.01)

    assert hub.get_snapshot("engine-1").balance == 1000
    assert hub.deliver_signal("engine-1", {"id": "signal-1"})
    command = EngineCommand.from_wire(json.loads(reader.readline()))
    assert command.command_type == EngineCommandType.SIGNAL_DELIVER
    assert command.payload["signal"]["id"] == "signal-1"
    client.close()
    hub.stop()


def test_config_revision_is_persisted_before_controlled_restart(tmp_path: Path) -> None:
    registry = AgentRegistry(str(tmp_path / "manager"))
    registry.init()
    reg = _registration()
    reg.data_dir = str(tmp_path / "engine")
    registry.upsert_agent(reg)
    config_store = MagicMock()
    config_store.preview_agent_config.return_value = {"engine": {"storage_path": reg.data_dir}}
    supervisor = MagicMock()
    service = ConfigRevisionService(registry, config_store, supervisor)

    with patch.object(service, "_validate"):
        result = service.apply("engine-1", {"risk": {"max_losing_streak": 4}})

    assert result["revision"] == 1
    config_store.write_config_document.assert_called_once()
    supervisor.terminate.assert_called_once_with("engine-1")
    assert registry.latest_desired_config_revision("engine-1") == 1


def test_worker_execution_event_outbox_survives_disconnect_until_ack(tmp_path: Path) -> None:
    client = WorkerEventClient(
        "engine-1",
        "127.0.0.1",
        8871,
        "secret",
        MagicMock(),
        123,
        "Broker",
        str(tmp_path),
    )

    client.emit_execution_event("trade.opened", {"trade_id": "trade-1"})
    with __import__("sqlite3").connect(client._outbox_path) as conn:
        event_id = conn.execute("SELECT event_id FROM event_outbox").fetchone()[0]

    client._handle_command(EngineCommand(
        engine_id="engine-1",
        command_type=EngineCommandType.EVENT_ACK,
        payload={"event_id": event_id},
    ))

    with __import__("sqlite3").connect(client._outbox_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM event_outbox").fetchone()[0] == 0


def test_manager_worker_event_deduplication_survives_restart(tmp_path: Path) -> None:
    registry = AgentRegistry(str(tmp_path / "manager"))
    registry.init()

    assert not registry.worker_event_processed("event-1")
    registry.record_worker_event("event-1", "engine-1")
    assert registry.worker_event_processed("event-1")

    reopened = AgentRegistry(str(tmp_path / "manager"))
    reopened.init()
    assert reopened.worker_event_processed("event-1")
