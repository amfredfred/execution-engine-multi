import json
import socket
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from manager.app.config_revisions import ConfigRevisionService
from manager.app.event_hub import EngineEventHub, _validate_event_sequence
from manager.app.models import AgentRegistration, AgentStatus
from manager.app.registry import AgentRegistry
from src.runtime.contracts import (
    EngineCommand,
    EngineCommandType,
    EngineEvent,
    EngineEventType,
    MAX_ENVELOPE_AGE_MS,
    MAX_WIRE_BYTES,
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


def test_event_hub_rejects_unregistered_worker() -> None:
    registry = MagicMock()
    registry.get_agent.return_value = None
    hub = EngineEventHub(registry, "secret", port=0)
    hub.start()
    port = hub._server.server_address[1]
    client = socket.create_connection(("127.0.0.1", port), timeout=3)
    writer = client.makefile("w", encoding="utf-8")

    hello = EngineEvent("unknown", 1, EngineEventType.WORKER_HELLO, {"token": "secret"})
    writer.write(json.dumps(hello.to_wire()) + "\n")
    writer.flush()

    deadline = time.time() + 1
    while hub._connections and time.time() < deadline:
        time.sleep(0.01)

    assert "unknown" not in hub._connections
    client.close()
    hub.stop()


def test_event_hub_rejects_another_workers_credential() -> None:
    registry = MagicMock()
    registry.get_agent.return_value = _registration()
    tokens = {"engine-1": "token-1", "engine-2": "token-2"}
    hub = EngineEventHub(
        registry, "fallback", port=0, token_resolver=tokens.get
    )
    hub.start()
    port = hub._server.server_address[1]
    client = socket.create_connection(("127.0.0.1", port), timeout=3)
    hello = EngineEvent(
        "engine-1", 1, EngineEventType.WORKER_HELLO, {"token": "token-2"}
    )
    client.sendall((json.dumps(hello.to_wire()) + "\n").encode())
    client.settimeout(2)

    assert client.recv(1) == b""
    assert "engine-1" not in hub._connections
    client.close()
    hub.stop()


def test_event_hub_rejects_worker_identity_spoof() -> None:
    registry = MagicMock()
    registry.get_agent.return_value = _registration()
    hub = EngineEventHub(registry, "secret", port=0)
    hub.start()
    port = hub._server.server_address[1]
    client = socket.create_connection(("127.0.0.1", port), timeout=3)
    writer = client.makefile("w", encoding="utf-8")

    hello = EngineEvent("engine-1", 1, EngineEventType.WORKER_HELLO, {"token": "secret"})
    spoofed = EngineEvent(
        "engine-2",
        2,
        EngineEventType.ENGINE_SNAPSHOT,
        {"status": "RUNNING", "balance": 1000},
    )
    writer.write(json.dumps(hello.to_wire()) + "\n")
    writer.write(json.dumps(spoofed.to_wire()) + "\n")
    writer.flush()

    deadline = time.time() + 1
    while "engine-1" in hub._connections and time.time() < deadline:
        time.sleep(0.01)

    assert hub.get_snapshot("engine-2") is None
    assert "engine-1" not in hub._connections
    client.close()
    hub.stop()


def test_event_hub_replaces_and_rejects_superseded_connection() -> None:
    registry = MagicMock()
    registry.get_agent.return_value = _registration()
    hub = EngineEventHub(registry, "secret", port=0)
    hub.start()
    port = hub._server.server_address[1]

    first = socket.create_connection(("127.0.0.1", port), timeout=3)
    first_writer = first.makefile("w", encoding="utf-8")
    hello = EngineEvent("engine-1", 1, EngineEventType.WORKER_HELLO, {"token": "secret"})
    first_writer.write(json.dumps(hello.to_wire()) + "\n")
    first_writer.flush()

    deadline = time.time() + 1
    while "engine-1" not in hub._connections and time.time() < deadline:
        time.sleep(0.01)
    old_server_connection = hub._connections["engine-1"][0]

    second = socket.create_connection(("127.0.0.1", port), timeout=3)
    second_writer = second.makefile("w", encoding="utf-8")
    second_writer.write(json.dumps(hello.to_wire()) + "\n")
    second_writer.flush()

    deadline = time.time() + 1
    while (
        hub._connections.get("engine-1", (None,))[0] is old_server_connection
        and time.time() < deadline
    ):
        time.sleep(0.01)

    assert hub._connections["engine-1"][0] is not old_server_connection
    first.settimeout(1)
    assert first.recv(1) == b""
    with pytest.raises(ValueError, match="superseded worker connection"):
        hub._handle_authenticated_event(
            "engine-1",
            old_server_connection,
            EngineEvent("engine-1", 2, EngineEventType.ENGINE_SNAPSHOT),
        )

    first.close()
    second.close()
    hub.stop()


def test_event_hub_rejects_oversized_envelope() -> None:
    registry = MagicMock()
    hub = EngineEventHub(registry, "secret", port=0)
    hub.start()
    port = hub._server.server_address[1]
    client = socket.create_connection(("127.0.0.1", port), timeout=3)

    client.sendall(b"x" * (MAX_WIRE_BYTES + 1))
    client.settimeout(2)

    assert client.recv(1) == b""
    assert not hub._connections
    client.close()
    hub.stop()


def test_event_hub_closes_idle_worker_connection() -> None:
    registry = MagicMock()
    registry.get_agent.return_value = _registration()
    with patch("manager.app.event_hub._WORKER_IDLE_TIMEOUT_SEC", 0.1):
        hub = EngineEventHub(registry, "secret", port=0)
        hub.start()
        port = hub._server.server_address[1]
        client = socket.create_connection(("127.0.0.1", port), timeout=3)
        hello = EngineEvent("engine-1", 1, EngineEventType.WORKER_HELLO, {"token": "secret"})
        client.sendall((json.dumps(hello.to_wire()) + "\n").encode())
        client.settimeout(2)

        assert client.recv(1) == b""
        assert "engine-1" not in hub._connections
        client.close()
        hub.stop()


def test_event_hub_uses_independent_worker_write_locks() -> None:
    registry = MagicMock()
    registry.get_agent.return_value = _registration()
    hub = EngineEventHub(registry, "secret", port=0)
    hub.start()
    port = hub._server.server_address[1]
    first = socket.create_connection(("127.0.0.1", port), timeout=3)
    second = socket.create_connection(("127.0.0.1", port), timeout=3)
    first_hello = EngineEvent("engine-1", 1, EngineEventType.WORKER_HELLO, {"token": "secret"})
    second_hello = EngineEvent("engine-2", 1, EngineEventType.WORKER_HELLO, {"token": "secret"})
    first.sendall((json.dumps(first_hello.to_wire()) + "\n").encode())
    second.sendall((json.dumps(second_hello.to_wire()) + "\n").encode())

    deadline = time.time() + 1
    while len(hub._connections) < 2 and time.time() < deadline:
        time.sleep(0.01)

    assert hub._connections["engine-1"][2] is not hub._connections["engine-2"][2]
    first.close()
    second.close()
    hub.stop()


def test_event_hub_rejects_oversized_command() -> None:
    hub = EngineEventHub(MagicMock(), "secret")
    writer = MagicMock()
    hub._connections["engine-1"] = (
        MagicMock(),
        writer,
        __import__("threading").Lock(),
        1,
    )

    assert not hub.send_command(
        "engine-1",
        EngineCommandType.SIGNAL_DELIVER,
        {"signal": {"value": "x" * MAX_WIRE_BYTES}},
    )
    writer.write.assert_not_called()


def test_event_hub_rejects_replayed_event_sequence() -> None:
    last_sequence = 5
    replayed = EngineEvent("engine-1", 5, EngineEventType.ENGINE_SNAPSHOT)

    with pytest.raises(ValueError, match="sequence must increase"):
        _validate_event_sequence(last_sequence, replayed)


def test_event_hub_allows_durable_execution_event_replay() -> None:
    replayed = EngineEvent(
        "engine-1",
        2,
        EngineEventType.EXECUTION_EVENT,
        occurred_at=int(time.time() * 1000) - MAX_ENVELOPE_AGE_MS - 1,
    )

    assert _validate_event_sequence(5, replayed) == 5


def test_event_hub_rejects_stale_lifecycle_event() -> None:
    stale = EngineEvent(
        "engine-1",
        6,
        EngineEventType.WORKER_READY,
        occurred_at=int(time.time() * 1000) - MAX_ENVELOPE_AGE_MS - 1,
    )

    with pytest.raises(ValueError, match="stale"):
        _validate_event_sequence(5, stale)


def test_worker_rejects_stale_command(tmp_path: Path) -> None:
    container = MagicMock()
    client = WorkerEventClient(
        "engine-1",
        "127.0.0.1",
        8871,
        "secret",
        container,
        123,
        "Broker",
        str(tmp_path),
    )
    client._emit = MagicMock()
    stale = EngineCommand(
        engine_id="engine-1",
        command_type=EngineCommandType.PAUSE,
        issued_at=int(time.time() * 1000) - MAX_ENVELOPE_AGE_MS - 1,
    )

    client._handle_command(stale)

    container.signal_queue.pause.assert_not_called()
    client._emit.assert_called_once()
    assert client._emit.call_args.args[0] == EngineEventType.COMMAND_REJECTED


def test_worker_rejects_incompatible_command_revision(tmp_path: Path) -> None:
    container = MagicMock()
    client = WorkerEventClient(
        "engine-1",
        "127.0.0.1",
        8871,
        "secret",
        container,
        123,
        "Broker",
        str(tmp_path),
        config_revision=3,
    )
    client._emit = MagicMock()

    client._handle_command(EngineCommand(
        engine_id="engine-1",
        command_type=EngineCommandType.PAUSE,
        config_revision=2,
    ))

    container.signal_queue.pause.assert_not_called()
    client._emit.assert_called_once()
    assert client._emit.call_args.args[0] == EngineEventType.COMMAND_REJECTED
    assert "does not match worker revision 3" in client._emit.call_args.args[1]["error"]


def test_worker_snapshot_reports_starting_degraded_and_running(tmp_path: Path) -> None:
    container = MagicMock()
    container.runtime_ready = __import__("threading").Event()
    container.runtime_error = None
    container.mt5_positions.get_account_info.side_effect = RuntimeError("offline")
    container.mt5_client.is_connected.return_value = False
    container.position_store.get_open_trades.return_value = []
    container.loss_tracker.stats.return_value = {}
    client = WorkerEventClient(
        "engine-1", "127.0.0.1", 8871, "secret", container,
        123, "Broker", str(tmp_path),
    )

    assert client._build_snapshot()["status"] == "STARTING"
    container.runtime_error = "MT5 offline"
    assert client._build_snapshot()["status"] == "DEGRADED"
    container.runtime_ready.set()
    assert client._build_snapshot()["status"] == "RUNNING"


def test_event_hub_rejects_incompatible_worker_revision() -> None:
    registry = MagicMock()
    registry.get_agent.return_value = _registration()
    registry.current_config_revision.return_value = 2
    hub = EngineEventHub(registry, "secret", port=0)
    hub.start()
    port = hub._server.server_address[1]
    client = socket.create_connection(("127.0.0.1", port), timeout=3)
    hello = EngineEvent(
        "engine-1",
        1,
        EngineEventType.WORKER_HELLO,
        {"token": "secret"},
        config_revision=1,
    )
    client.sendall((json.dumps(hello.to_wire()) + "\n").encode())
    client.settimeout(2)

    assert client.recv(1) == b""
    assert "engine-1" not in hub._connections
    client.close()
    hub.stop()


def test_event_hub_stamps_commands_with_connected_worker_revision() -> None:
    hub = EngineEventHub(MagicMock(), "secret")
    writer = MagicMock()
    hub._connections["engine-1"] = (
        MagicMock(),
        writer,
        __import__("threading").Lock(),
        7,
    )

    assert hub.send_command("engine-1", EngineCommandType.PAUSE)

    command = EngineCommand.from_wire(json.loads(writer.write.call_args.args[0]))
    assert command.config_revision == 7


def test_event_hub_rejects_event_revision_change_after_authentication() -> None:
    hub = EngineEventHub(MagicMock(), "secret")
    connection = MagicMock()
    hub._connections["engine-1"] = (
        connection,
        MagicMock(),
        __import__("threading").Lock(),
        4,
    )
    event = EngineEvent(
        "engine-1",
        2,
        EngineEventType.ENGINE_SNAPSHOT,
        config_revision=5,
    )

    with pytest.raises(ValueError, match="does not match authenticated revision 4"):
        hub._handle_authenticated_event("engine-1", connection, event)


def test_worker_stopped_event_updates_registry() -> None:
    registry = MagicMock()
    hub = EngineEventHub(registry, "secret")

    hub._handle_event(EngineEvent(
        "engine-1", 3, EngineEventType.WORKER_STOPPED, {"pid": 12}
    ))

    registry.set_agent_status.assert_called_once_with(
        "engine-1", AgentStatus.STOPPED, pid=None
    )


def test_event_hub_health_rejects_missing_and_stale_workers() -> None:
    registry = MagicMock()
    reg = _registration()
    registry.list_agents.return_value = [reg]
    hub = EngineEventHub(registry, "secret")

    assert hub.health_report()["unhealthy_workers"][0]["reason"] == "not_connected"
    hub._connections["engine-1"] = (
        MagicMock(), MagicMock(), __import__("threading").Lock(), 1,
    )
    hub._snapshots["engine-1"] = __import__(
        "manager.app.models", fromlist=["AgentSnapshot"]
    ).AgentSnapshot(
        "engine-1", AgentStatus.RUNNING, True, 123, "Broker",
        1000, 1000, 0, True, 10, 1, {},
    )

    assert hub.health_report()["unhealthy_workers"][0]["reason"] == "stale_snapshot"


def test_tracked_command_outcome_is_persisted_and_completed(tmp_path: Path) -> None:
    registry = AgentRegistry(str(tmp_path / "manager"))
    registry.init()
    hub = EngineEventHub(registry, "secret")
    writer = MagicMock()
    hub._connections["engine-1"] = (
        MagicMock(),
        writer,
        __import__("threading").Lock(),
        1,
    )

    command_id = hub.submit_command("engine-1", EngineCommandType.PAUSE)

    assert registry.get_command_outcome(command_id)["status"] == "sent"
    hub._handle_event(EngineEvent(
        "engine-1",
        2,
        EngineEventType.COMMAND_ACK,
        {"command_id": command_id},
    ))
    assert registry.get_command_outcome(command_id)["status"] == "completed"

    reopened = AgentRegistry(str(tmp_path / "manager"))
    reopened.init()
    assert reopened.get_command_outcome(command_id)["status"] == "completed"


def test_worker_rejects_malformed_command_payload(tmp_path: Path) -> None:
    container = MagicMock()
    client = WorkerEventClient(
        "engine-1", "127.0.0.1", 8871, "secret", container,
        123, "Broker", str(tmp_path),
    )
    client._emit = MagicMock()

    client._handle_command(EngineCommand(
        engine_id="engine-1",
        command_type=EngineCommandType.CLOSE_TRADE,
        payload={},
    ))

    container.position_manager.close_trade.assert_not_called()
    assert client._emit.call_args.args[0] == EngineEventType.COMMAND_REJECTED
    assert "trade_id" in client._emit.call_args.args[1]["error"]


def test_worker_executes_dedicated_close_trade_command(tmp_path: Path) -> None:
    container = MagicMock()
    client = WorkerEventClient(
        "engine-1", "127.0.0.1", 8871, "secret", container,
        123, "Broker", str(tmp_path),
    )
    client._emit = MagicMock()

    client._handle_command(EngineCommand(
        engine_id="engine-1",
        command_type=EngineCommandType.CLOSE_TRADE,
        payload={"trade_id": "trade-1"},
    ))

    container.position_manager.close_trade.assert_called_once_with("trade-1")
    assert client._emit.call_args.args[0] == EngineEventType.COMMAND_ACK


def test_worker_stop_command_requests_graceful_shutdown(tmp_path: Path) -> None:
    requested = MagicMock()
    client = WorkerEventClient(
        "engine-1", "127.0.0.1", 8871, "secret", MagicMock(),
        123, "Broker", str(tmp_path), on_stop_requested=requested,
    )
    client._emit = MagicMock()

    client._handle_command(EngineCommand(
        engine_id="engine-1",
        command_type=EngineCommandType.STOP,
    ))

    requested.assert_called_once_with()
    assert client._emit.call_args.args[0] == EngineEventType.COMMAND_ACK


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
    assert registry.current_config_revision("engine-1") == 1


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
