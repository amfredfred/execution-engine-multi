"""
manager/service.py — ManagerRuntime: top-level orchestrator.

Wires all manager components and owns their lifecycle.
Analogous to src/app/bootstrap.py in the single-agent engine.
"""

from __future__ import annotations

import logging
import secrets as _secrets
import sys
from pathlib import Path

from src.manager.agent_channel import AgentChannel
from src.manager.api import LocalManagerApi
from src.manager.config_store import AgentConfigStore
from src.manager.desired_state import DesiredStateSupervisor
from src.manager.migration import LegacySingleAgentMigration
from src.manager.operations import OperationRunner
from src.manager.process_supervisor import ProcessSupervisor
from src.manager.provisioning import AgentProvisioner
from src.manager.reconciliation import RestartReconciler
from src.manager.registry import AgentRegistry
from src.manager.secrets import ManagerSecretStore
from src.manager.signal_router import ManagerSignalRouter
from src.manager.terminal_discovery import TerminalDiscovery

logger = logging.getLogger(__name__)


class ManagerRuntime:
    def __init__(
        self,
        storage_path: str,
        agents_data_dir: str,
        api_port: int = 8765,
        channel_port: int = 8766,
        legacy_config_path: str = "",
        gateway_ws_url: str = "",
        gateway_http_url: str = "",
        engine_version: str = "0.1.0",
    ) -> None:
        # ── Registry + Secrets ────────────────────────────────────────────
        self.registry = AgentRegistry(storage_path)
        self.secrets  = ManagerSecretStore(self.registry)

        # ── Config / Discovery / Provisioning ─────────────────────────────
        self.config_store = AgentConfigStore(self.secrets)
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
        channel_token = self._load_or_create_channel_token()
        self.channel = AgentChannel(
            registry=self.registry,
            token=channel_token,
            port=channel_port,
        )

        # ── Process Supervisor + Desired State ────────────────────────────
        src_root = str(Path(__file__).parents[2])   # execution-engine-multi/
        self.supervisor = ProcessSupervisor(
            registry=self.registry,
            secrets=self.secrets,
            src_root=src_root,
            on_agent_stopped=self._on_agent_stopped,
        )
        self.desired = DesiredStateSupervisor(
            registry=self.registry,
            supervisor=self.supervisor,
            channel=self.channel,
        )

        # ── Operations ────────────────────────────────────────────────────
        self.ops = OperationRunner(
            registry=self.registry,
            supervisor=self.supervisor,
            desired=self.desired,
            provisioner=self.provisioner,
            channel=self.channel,
            on_agent_changed=self._on_agent_changed,
        )

        # ── Signal Router ─────────────────────────────────────────────────
        activation_key = self.secrets.get_activation_key() or ""
        self.signal_router = ManagerSignalRouter(
            registry=self.registry,
            channel=self.channel,
            activation_key=activation_key,
            gateway_ws_url=gateway_ws_url,
            engine_version=engine_version,
        )

        # ── REST API ──────────────────────────────────────────────────────
        api_token = self._load_or_create_api_token(storage_path)
        self.api = LocalManagerApi(
            registry=self.registry,
            ops=self.ops,
            provisioner=self.provisioner,
            discovery=self.discovery,
            channel=self.channel,
            token=api_token,
            port=api_port,
            storage_path=storage_path,
        )

        # ── Reconciliation + Migration ────────────────────────────────────
        self.reconciler = RestartReconciler(self.registry, self.supervisor)
        self.migrator   = LegacySingleAgentMigration(
            registry=self.registry,
            provisioner=self.provisioner,
            legacy_config_path=legacy_config_path,
        )

    def start(self) -> None:
        logger.info("ManagerRuntime starting")

        # 1. Init DB schema
        self.registry.init()

        # 2. Clean up stale state from previous run
        self.reconciler.run()

        # 3. Import legacy single-agent config if this is a fresh install
        self.migrator.run_if_needed()

        # 4. Start AgentChannel WS server
        self.channel.start()

        # 5. Start gateway signal router
        active_agents = self.registry.list_agents()
        self.signal_router.start(active_agents)

        # 6. Start REST API
        self.api.start()

        # 7. Start desired-state reconciliation loop
        self.desired.start()

        logger.info("ManagerRuntime online")

    def stop(self) -> None:
        logger.info("ManagerRuntime stopping")
        self.desired.stop()
        self.signal_router.stop()
        self.api.stop()
        self.channel.stop()
        logger.info("ManagerRuntime stopped")

    # ── Event callbacks ───────────────────────────────────────────────────

    def _on_agent_stopped(self, agent_id: str) -> None:
        """Called by ProcessSupervisor watcher when an agent subprocess exits."""
        self.desired.notify_crashed(agent_id)

    def _on_agent_changed(self, agent_id: str) -> None:
        """Called by OperationRunner after provision/deprovision."""
        active_agents = self.registry.list_agents()
        self.signal_router.refresh_rooms(active_agents)

    # ── Token bootstrap ───────────────────────────────────────────────────

    def _load_or_create_channel_token(self) -> str:
        token = self.secrets.get_channel_token()
        if not token:
            token = _secrets.token_hex(32)
            self.secrets.set_channel_token(token)
        return token

    def _load_or_create_api_token(self, storage_path: str) -> str:
        token = self.secrets.get_api_token()
        if not token:
            token = _secrets.token_hex(32)
            self.secrets.set_api_token(token)
        return token
