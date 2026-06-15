from pathlib import Path
from unittest.mock import MagicMock, patch

from src.manager.event_hub import EngineEventHub
from src.manager.models import AgentRegistration, AgentStatus
from src.manager.operations import OperationRunner
from src.manager.reconciliation import RestartReconciler, _pid_is_alive
from src.manager.registry import AgentRegistry
from src.runtime.contracts import EngineEvent, EngineEventType


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


def test_remove_operation_forgets_runtime_state_and_deprovisions() -> None:
    registry = MagicMock()
    supervisor = MagicMock()
    provisioner = MagicMock()
    event_hub = MagicMock()
    runner = OperationRunner(
        registry, supervisor, MagicMock(), provisioner, event_hub,
    )

    runner._execute("remove", "agent-0", {})

    supervisor.terminate.assert_called_once_with("agent-0", force=True)
    event_hub.forget_engine.assert_called_once_with("agent-0")
    provisioner.deprovision.assert_called_once_with("agent-0")


def test_windows_access_denied_pid_probe_is_treated_as_alive() -> None:
    with patch("src.manager.reconciliation.os.kill", side_effect=PermissionError):
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

    with patch("src.manager.reconciliation._pid_is_alive", return_value=True):
        RestartReconciler(registry, supervisor).run()

    supervisor.kill_orphan.assert_not_called()
    registry.set_agent_status.assert_called_once_with(
        "agent-0", AgentStatus.STARTING, pid=1234,
    )
