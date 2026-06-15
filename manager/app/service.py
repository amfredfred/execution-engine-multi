"""
manager/service.py — ManagerRuntime: top-level orchestrator.

Wires all manager components and owns their lifecycle.
Analogous to src/app/bootstrap.py in the single-agent engine.
"""

from __future__ import annotations

import logging
import secrets as _secrets
import threading
from pathlib import Path

from manager.app.api import LocalManagerApi
from manager.app.models import AgentStatus
from manager.app.config_store import AgentConfigStore
from manager.app.config_revisions import ConfigRevisionService
from manager.app.desired_state import DesiredStateSupervisor
from manager.app.event_hub import EngineEventHub
from manager.app.operations import OperationRunner
from manager.app.process_supervisor import ProcessSupervisor
from manager.app.provisioning import AgentProvisioner
from manager.app.reconciliation import RestartReconciler
from manager.app.registry import AgentRegistry
from manager.app.secrets import ManagerSecretStore
from manager.app.signal_router import ManagerSignalRouter
from manager.app.terminal_discovery import TerminalDiscovery
from src.runtime.contracts import EngineCommandType

logger = logging.getLogger(__name__)


class ManagerRuntime:
    def __init__(
        self,
        storage_path: str,
        agents_data_dir: str,
        api_port: int = 8870,
        ipc_port: int = 8871,
        signal_ws_url: str = "ws://127.0.0.1:8765",
        signal_ws_token: str = "",
        gateway_http_url: str = "",
        engine_version: str = "0.1.0",
    ) -> None:
        # ── Registry + Secrets ────────────────────────────────────────────
        self.registry = AgentRegistry(storage_path)
        self.registry.init()
        self.secrets  = ManagerSecretStore(self.registry)

        # ── Config / Discovery / Provisioning ─────────────────────────────
        self.config_store = AgentConfigStore(self.secrets, self.registry)
        self.discovery    = TerminalDiscovery(self.registry)
        self.provisioner  = AgentProvisioner(
            registry=self.registry,
            secrets=self.secrets,
            config_store=self.config_store,
            discovery=self.discovery,
            agents_data_dir=agents_data_dir,
            gateway_http_url=gateway_http_url,
        )

        # ── Agent Channel ─────────────────────────────────────────────────
        ipc_token = self._load_or_create_ipc_token()
        self.event_hub = EngineEventHub(
            registry=self.registry,
            token=ipc_token,
            port=ipc_port,
            token_resolver=lambda agent_id: self.secrets.get_secret(
                agent_id, "ipc_token"
            ) or ipc_token,
        )

        # ── Process Supervisor + Desired State ────────────────────────────
        src_root = str(Path(__file__).parents[2])   # execution-engine-multi/
        self.supervisor = ProcessSupervisor(
            registry=self.registry,
            secrets=self.secrets,
            src_root=src_root,
            ipc_port=ipc_port,
            on_agent_stopped=self._on_agent_stopped,
        )
        self.desired = DesiredStateSupervisor(
            registry=self.registry,
            supervisor=self.supervisor,
        )
        self.config_revisions = ConfigRevisionService(
            self.registry, self.config_store, self.supervisor,
        )

        # ── Operations ────────────────────────────────────────────────────
        self.ops = OperationRunner(
            registry=self.registry,
            supervisor=self.supervisor,
            desired=self.desired,
            provisioner=self.provisioner,
            event_hub=self.event_hub,
            on_agent_changed=self._on_agent_changed,
        )

        # ── Signal Router ─────────────────────────────────────────────────
        self.signal_router = ManagerSignalRouter(
            registry=self.registry,
            event_hub=self.event_hub,
            signal_ws_url=signal_ws_url,
            signal_ws_token=signal_ws_token,
        )
        self.event_hub.set_event_callbacks(
            on_snapshot=lambda agent_id, snap: None,
            on_execution_event=self.signal_router.forward_execution_event,
        )
        self.event_hub.set_worker_ready_callback(self.config_revisions.worker_ready)

        # ── REST API ──────────────────────────────────────────────────────
        api_token = self._load_or_create_api_token(storage_path)
        self.api = LocalManagerApi(
            registry=self.registry,
            ops=self.ops,
            provisioner=self.provisioner,
            discovery=self.discovery,
            event_hub=self.event_hub,
            config_revisions=self.config_revisions,
            token=api_token,
            port=api_port,
            storage_path=storage_path,
            on_activation_key_changed=self._on_activation_key_changed,
            on_api_token_rotated=self.secrets.set_api_token,
            health_provider=self.health_report,
        )

        # ── Reconciliation + Migration ────────────────────────────────────
        self.reconciler = RestartReconciler(self.registry, self.supervisor)
        self._license_stop = threading.Event()
        self._license_thread: threading.Thread | None = None

    def start(self) -> None:
        logger.info("ManagerRuntime starting")
        started = []
        try:
            self.reconciler.run()
            self.registry.enforce_retention()
            self.event_hub.start()
            started.append(self.event_hub)
            active_agents = self.registry.list_agents()
            self.signal_router.start(active_agents)
            started.append(self.signal_router)
            self.api.start()
            started.append(self.api)
            self.desired.start()
            started.append(self.desired)
            self._license_thread = threading.Thread(
                target=self._license_monitor_loop,
                name="license-monitor",
                daemon=True,
            )
            self._license_thread.start()
        except Exception:
            logger.exception("ManagerRuntime startup failed; rolling back")
            for component in reversed(started):
                try:
                    component.stop()
                except Exception:
                    logger.exception("Manager startup rollback failed")
            raise

        logger.info("ManagerRuntime online")

    def stop(self, force: bool = False) -> None:
        safe, reason = self.can_shutdown()
        if not safe and not force:
            raise RuntimeError(reason)
        logger.info("ManagerRuntime stopping")
        license_stop = getattr(self, "_license_stop", None)
        if license_stop:
            license_stop.set()
        license_thread = getattr(self, "_license_thread", None)
        if license_thread:
            license_thread.join(timeout=5)
        self.desired.stop()
        self._stop_all_workers()
        self.signal_router.stop()
        self.api.stop()
        self.event_hub.stop()
        logger.info("ManagerRuntime stopped")

    def _license_monitor_loop(self) -> None:
        while not self._license_stop.wait(60):
            try:
                info = self.provisioner.get_license_info(force=True)
            except Exception as exc:
                info = {"valid": False, "error": str(exc)}
            if info.get("valid"):
                continue
            reason = info.get("error") or "license invalid or expired"
            logger.error("License enforcement paused new entries: %s", reason)
            for reg in self.registry.list_agents():
                if reg.desired_status == "running":
                    self.event_hub.submit_command(
                        reg.agent_id, EngineCommandType.PAUSE, {}
                    )

    def can_shutdown(self) -> tuple[bool, str]:
        exposed = [
            f"{agent_id} ({snapshot.open_trades} open)"
            for agent_id, snapshot in self.event_hub.get_all_snapshots().items()
            if snapshot.open_trades > 0
        ]
        if exposed:
            return False, "Unsafe shutdown refused; open positions: " + ", ".join(exposed)
        return True, ""

    def _stop_all_workers(self) -> None:
        """Terminate all running agent subprocesses before the manager exits."""
        for reg in self.registry.list_agents():
            if reg.status in (
                AgentStatus.RUNNING,
                AgentStatus.STARTING,
                AgentStatus.STOPPING,
            ):
                try:
                    self.supervisor.terminate(reg.agent_id, force=True)
                    logger.info("Stopped agent worker %s", reg.agent_id)
                except Exception as exc:
                    logger.warning("Could not stop agent %s: %s", reg.agent_id, exc)

    # ── Event callbacks ───────────────────────────────────────────────────

    def _on_agent_stopped(self, agent_id: str) -> None:
        """Called by ProcessSupervisor watcher when an agent subprocess exits."""
        self.desired.notify_crashed(agent_id)

    def _on_agent_changed(self, agent_id: str) -> None:
        """Called by OperationRunner after provision/deprovision."""
        active_agents = self.registry.list_agents()
        self.signal_router.refresh_rooms(active_agents)

    def _on_activation_key_changed(self, activation_key: str) -> None:
        """Called when the manager license key changes (used for provisioning only)."""
        logger.info("Manager activation key updated")

    def health_report(self) -> dict:
        registry_ok = False
        try:
            registry_ok = self.registry.health_check()
        except Exception:
            logger.exception("Registry health check failed")
        workers = self.event_hub.health_report()
        signal_manager = self.signal_router.health_report()
        ipc_ok = self.event_hub._server is not None
        ok = registry_ok and ipc_ok and workers["ok"] and signal_manager["ok"]
        return {
            "ok": ok,
            "registry": {"ok": registry_ok},
            "ipc": {"ok": ipc_ok},
            "signal_manager": signal_manager,
            **workers,
        }

    # ── Token bootstrap ───────────────────────────────────────────────────

    def _load_or_create_ipc_token(self) -> str:
        token = self.secrets.get_ipc_token()
        if not token:
            token = _secrets.token_hex(32)
            self.secrets.set_ipc_token(token)
        return token

    def _load_or_create_api_token(self, storage_path: str) -> str:
        token = self.secrets.get_api_token()
        if not token:
            token = _secrets.token_hex(32)
            self.secrets.set_api_token(token)
        return token
