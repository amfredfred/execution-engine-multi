"""
manager/service.py — ManagerRuntime: top-level orchestrator.

Wires all manager components and owns their lifecycle.
Analogous to src/app/bootstrap.py in the single-agent engine.
"""

from __future__ import annotations

import logging
import secrets as _secrets
import threading
import time
import urllib.request
from pathlib import Path

from manager.app.api import LocalManagerApi
from manager.app.gateway_connector import GatewayConnector
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

        # ── Gateway reachability + WebSocket session ──────────────────────
        self._gateway_http_url: str = gateway_http_url.rstrip("/")
        self._gateway_reachable: bool | None = None   # None = not yet checked
        self._gateway_check_lock = threading.Lock()
        # Convert https://host → wss://host/engine
        gw_ws_url = (
            self._gateway_http_url
            .replace("https://", "wss://")
            .replace("http://", "ws://")
            + "/engine"
            if self._gateway_http_url else ""
        )
        # Only route gateway signals through GatewayConnector when there is no
        # direct Signal Manager subscription; otherwise ManagerSignalRouter
        # already handles them and wiring both would cause duplicate delivery.
        gw_signal_cb = (
            self._on_gateway_signal if not signal_ws_url else None
        )
        self.gateway_connector = GatewayConnector(
            gateway_ws_url=gw_ws_url,
            secrets=self.secrets,
            registry=self.registry,
            engine_version=engine_version,
            on_signal=gw_signal_cb,
        )

        # ── Signal Router ─────────────────────────────────────────────────
        self.signal_router = ManagerSignalRouter(
            registry=self.registry,
            event_hub=self.event_hub,
            signal_ws_url=signal_ws_url,
            signal_ws_token=signal_ws_token,
        )
        self.event_hub.set_event_callbacks(
            on_snapshot=self.gateway_connector.push_agent_snapshot,
            on_execution_event=self.gateway_connector.push_execution_event,
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
        self._migrate_activation_key()
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
            self.gateway_connector.start()
            started.append(self.gateway_connector)
            threading.Thread(
                target=self._gateway_check_loop,
                name="gateway-check",
                daemon=True,
            ).start()
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
        self.gateway_connector.stop()
        self.api.stop()
        self.event_hub.stop()
        logger.info("ManagerRuntime stopped")

    def _license_monitor_loop(self) -> None:
        while not self._license_stop.wait(60):
            try:
                info = self.provisioner.get_license_info(force=True)
            except Exception as exc:
                info = {
                    "valid": False,
                    "authoritative": False,
                    "error": str(exc),
                }
            self._apply_license_info(info)

    def _apply_license_info(self, info: dict) -> None:
        if info.get("valid"):
            return
        reason = info.get("error") or "license invalid or expired"
        if not info.get("authoritative"):
            logger.warning(
                "License verification unavailable; leaving agents running: %s",
                reason,
            )
            return
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
        if self.registry.get_agent(agent_id) is None:
            self.gateway_connector.forget_agent(agent_id)

    def _on_activation_key_changed(self, activation_key: str) -> None:
        """Called when the manager license key changes (used for provisioning only)."""
        logger.info("Manager activation key updated")

    def _on_gateway_signal(self, payload: dict) -> None:
        """Called by GatewayConnector when a signal arrives via the gateway WS.

        This path is only active when Signal Manager is not configured; it acts
        as a fallback so the manager still receives signals routed by the gateway.
        """
        try:
            self.signal_router.handle_gateway_signal(payload)
        except Exception:
            logger.exception("Gateway signal dispatch failed")

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
        with self._gateway_check_lock:
            gw_reachable = self._gateway_reachable
        gw_connected = self.gateway_connector.is_connected()
        return {
            "ok": ok,
            "registry": {"ok": registry_ok},
            "ipc": {"ok": ipc_ok},
            "signal_manager": signal_manager,
            "gateway": {
                "url": self._gateway_http_url,
                "reachable": gw_reachable,
                "connected": gw_connected,
            },
            **workers,
        }

    def _gateway_check_loop(self) -> None:
        """Background thread: probe gateway HTTP reachability every 30 s."""
        while not self._license_stop.wait(0):
            reachable = self._probe_gateway()
            with self._gateway_check_lock:
                self._gateway_reachable = reachable
            if self._license_stop.wait(30):
                break

    def _probe_gateway(self) -> bool:
        if not self._gateway_http_url:
            return False
        try:
            req = urllib.request.Request(
                self._gateway_http_url,
                method="HEAD",
                headers={"User-Agent": "AQAgent/1.0"},
            )
            with urllib.request.urlopen(req, timeout=5):
                pass
            return True
        except urllib.error.HTTPError:
            return True   # Got an HTTP response — server is up
        except Exception:
            return False

    def _migrate_activation_key(self) -> None:
        """One-time migration: read activation_key from config.yaml into DPAPI secret store."""
        if self.secrets.get_activation_key():
            return
        config_path = Path(self.registry.storage_path).parent / "config.yaml"
        if not config_path.exists():
            return
        try:
            import yaml
            with open(config_path, "r", encoding="utf-8") as fh:
                cfg = yaml.safe_load(fh) or {}
            key = str(cfg.get("gateway", {}).get("activation_key") or "").strip()
            if len(key) < 16:
                return
            self.secrets.set_activation_key(key)
            cfg.setdefault("gateway", {})["activation_key"] = ""
            tmp = config_path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                yaml.safe_dump(cfg, fh, default_flow_style=False, allow_unicode=True)
            tmp.replace(config_path)
            logger.info("Migrated activation key from config.yaml to secret store")
        except Exception:
            logger.exception("Failed to migrate activation key from config.yaml")

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
