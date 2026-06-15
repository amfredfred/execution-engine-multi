from pathlib import Path
from unittest.mock import MagicMock, call, patch

from manager.app.event_hub import EngineEventHub
from manager.app.models import AgentRegistration, AgentStatus
from manager.app.operations import OperationRunner
from manager.app.process_supervisor import ProcessSupervisor
from manager.app.reconciliation import RestartReconciler, _pid_is_alive
from manager.app.registry import AgentRegistry
from manager.app.service import ManagerRuntime
from src.runtime.contracts import EngineCommandType, EngineEvent, EngineEventType


def _agent(tmp_path: Path) -> AgentRegistration:
    return AgentRegistration(
        agent_id="agent-0",
        display_name="Test Agent",
        status=AgentStatus.STOPPED,
        desired_status="running",
        config_path=str(tmp_path / "config.yaml"),
        data_dir=str(tmp_path),
        terminal_path="terminal64.exe",
        mt5_login=12345678,
        mt5_server="Broker-Demo",
        monitoring_port=8081,
        symbols=["XAUUSD"],
        created_at=1,
        updated_at=1,
        last_seen_at=None,
        pid=None,
    )


def test_delete_agent_removes_registration(tmp_path: Path) -> None:
    registry = AgentRegistry(str(tmp_path / "manager"))
    registry.init()
    registry.upsert_agent(_agent(tmp_path))

    registry.delete_agent("agent-0")

    assert registry.get_agent("agent-0") is None
    assert registry.list_agents() == []


def test_agent_identity_and_port_allocations_are_unique(tmp_path: Path) -> None:
    registry = AgentRegistry(str(tmp_path / "manager"))
    registry.init()

    first = registry.allocate_agent_identity()
    second = registry.allocate_agent_identity()

    assert first == ("agent-0", 8081)
    assert second == ("agent-1", 8082)


def test_crash_count_uses_rolling_window(tmp_path: Path) -> None:
    registry = AgentRegistry(str(tmp_path / "manager"))
    registry.init()
    registry.upsert_agent(_agent(tmp_path))

    with patch("manager.app.registry._now_ms", side_effect=[1, 400_001]):
        assert registry.increment_crash_count("agent-0", window_ms=300_000) == 1
        assert registry.increment_crash_count("agent-0", window_ms=300_000) == 1


def test_remove_operation_forgets_runtime_state_and_deprovisions() -> None:
    registry = MagicMock()
    supervisor = MagicMock()
    supervisor.is_alive.return_value = False
    provisioner = MagicMock()
    event_hub = MagicMock()
    runner = OperationRunner(
        registry, supervisor, MagicMock(), provisioner, event_hub,
    )

    runner._execute("remove", "agent-0", {})

    supervisor.stop_with_escalation.assert_called_once()
    event_hub.forget_engine.assert_called_once_with("agent-0")
    provisioner.deprovision.assert_called_once_with("agent-0")


def test_stop_operation_requests_graceful_worker_shutdown() -> None:
    registry = MagicMock()
    supervisor = MagicMock()
    event_hub = MagicMock()
    event_hub.submit_command.return_value = "command-1"
    runner = OperationRunner(
        registry, supervisor, MagicMock(), MagicMock(), event_hub,
    )

    runner._execute("stop", "agent-0", {})

    event_hub.submit_command.assert_called_once()
    supervisor.stop_with_escalation.assert_called_once_with(
        "agent-0", graceful_requested=True
    )


def test_stop_escalates_from_graceful_to_terminate_to_force() -> None:
    supervisor = ProcessSupervisor.__new__(ProcessSupervisor)
    supervisor.is_alive = MagicMock(return_value=True)
    supervisor.terminate = MagicMock()

    supervisor.stop_with_escalation(
        "agent-0", graceful_requested=True, graceful_timeout=0, terminate_timeout=0
    )

    assert supervisor.terminate.call_args_list == [
        call("agent-0", force=False),
        call("agent-0", force=True),
    ]


def test_windows_access_denied_pid_probe_is_treated_as_alive() -> None:
    with patch("manager.app.reconciliation.os.kill", side_effect=PermissionError):
        assert _pid_is_alive(1234) is True


def test_agent_snapshot_adopts_survivor_after_manager_restart() -> None:
    registry = MagicMock()
    registry.get_agent.return_value = _agent(Path("."))
    event_hub = EngineEventHub(registry, "token")
    event_hub._handle_event(EngineEvent(
        engine_id="agent-0",
        sequence=1,
        event_type=EngineEventType.WORKER_READY,
    ))

    registry.set_agent_status.assert_called_once_with(
        "agent-0", AgentStatus.RUNNING, pid=None,
    )


def test_restart_reconciler_preserves_live_worker_for_adoption(tmp_path: Path) -> None:
    registry = MagicMock()
    registry.list_agents.return_value = [_agent(tmp_path)]
    registry.list_agents.return_value[0].status = AgentStatus.RUNNING
    registry.list_agents.return_value[0].pid = 1234
    registry.list_terminal_leases.return_value = []
    supervisor = MagicMock()

    with patch("manager.app.reconciliation._pid_is_alive", return_value=True):
        RestartReconciler(registry, supervisor).run()

    supervisor.kill_orphan.assert_not_called()
    registry.set_agent_status.assert_called_once_with(
        "agent-0", AgentStatus.STARTING, pid=1234,
    )


def test_manager_start_rolls_back_started_components() -> None:
    runtime = ManagerRuntime.__new__(ManagerRuntime)
    runtime.reconciler = MagicMock()
    runtime.registry = MagicMock()
    runtime.secrets = MagicMock()
    runtime.secrets.get_activation_key.return_value = "already-set"
    runtime.event_hub = MagicMock()
    runtime.event_hub.get_all_snapshots.return_value = {}
    runtime.signal_router = MagicMock()
    runtime.api = MagicMock()
    runtime.desired = MagicMock()
    runtime.api.start.side_effect = RuntimeError("bind failed")

    with __import__("pytest").raises(RuntimeError, match="bind failed"):
        runtime.start()

    runtime.signal_router.stop.assert_called_once()
    runtime.event_hub.stop.assert_called_once()
    runtime.registry.enforce_retention.assert_called_once()


def test_manager_stops_workers_before_ipc_hub() -> None:
    runtime = ManagerRuntime.__new__(ManagerRuntime)
    calls = []
    runtime.desired = MagicMock()
    runtime.signal_router = MagicMock()
    runtime.api = MagicMock()
    runtime.event_hub = MagicMock()
    runtime.event_hub.get_all_snapshots.return_value = {}
    runtime.gateway_connector = MagicMock()
    runtime._stop_all_workers = lambda: calls.append("workers")
    runtime.event_hub.stop.side_effect = lambda: calls.append("hub")

    runtime.stop()

    assert calls == ["workers", "hub"]


def test_manager_refuses_shutdown_with_open_positions_unless_forced() -> None:
    runtime = ManagerRuntime.__new__(ManagerRuntime)
    runtime.event_hub = MagicMock()
    runtime.event_hub.get_all_snapshots.return_value = {
        "agent-1": MagicMock(open_trades=1)
    }

    with __import__("pytest").raises(RuntimeError, match="Unsafe shutdown refused"):
        runtime.stop()


def test_manager_health_includes_registry_ipc_signal_manager_and_workers() -> None:
    import threading
    runtime = ManagerRuntime.__new__(ManagerRuntime)
    runtime.registry = MagicMock()
    runtime.registry.health_check.return_value = True
    runtime.event_hub = MagicMock()
    runtime.event_hub._server = MagicMock()
    runtime.event_hub.health_report.return_value = {
        "ok": True, "unhealthy_workers": []
    }
    runtime.signal_router = MagicMock()
    runtime.signal_router.health_report.return_value = {
        "ok": True, "configured": True, "connected": True
    }
    runtime._gateway_http_url = "https://apex-gateway.example"
    runtime._gateway_reachable = True
    runtime._gateway_check_lock = threading.Lock()
    from manager.app.gateway_connector import GatewayConnector
    runtime.gateway_connector = MagicMock(spec=GatewayConnector)
    runtime.gateway_connector.is_connected.return_value = True

    report = runtime.health_report()

    assert report["ok"] is True
    assert report["registry"]["ok"] is True
    assert report["ipc"]["ok"] is True
    assert report["signal_manager"]["connected"] is True
    assert report["gateway"]["reachable"] is True
    assert report["gateway"]["connected"] is True


def test_license_verification_failure_does_not_pause_running_agents() -> None:
    runtime = ManagerRuntime.__new__(ManagerRuntime)
    runtime.registry = MagicMock()
    runtime.registry.list_agents.return_value = [MagicMock(desired_status="running")]
    runtime.event_hub = MagicMock()

    runtime._apply_license_info({
        "valid": False,
        "authoritative": False,
        "error": "Gateway unreachable",
    })

    runtime.event_hub.submit_command.assert_not_called()


def test_authoritative_invalid_license_pauses_running_agents() -> None:
    runtime = ManagerRuntime.__new__(ManagerRuntime)
    runtime.registry = MagicMock()
    runtime.registry.list_agents.return_value = [
        MagicMock(agent_id="agent-1", desired_status="running"),
        MagicMock(agent_id="agent-2", desired_status="stopped"),
    ]
    runtime.event_hub = MagicMock()

    runtime._apply_license_info({
        "valid": False,
        "authoritative": True,
        "error": "License key is invalid",
    })

    runtime.event_hub.submit_command.assert_called_once_with(
        "agent-1", EngineCommandType.PAUSE, {},
    )
