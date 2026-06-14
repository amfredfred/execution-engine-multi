"""
manager/provisioning.py — Agent lifecycle provisioning.

Creates/removes per-agent directories, config files, secrets, and terminal leases.
Pre-flight slot check against the gateway before provisioning.
"""

from __future__ import annotations

import logging
import socket
import time
import urllib.request
import urllib.error
import json
import uuid
from pathlib import Path

from src.manager.config_store import AgentConfigStore
from src.manager.models import AgentRegistration, AgentStatus
from src.manager.registry import AgentRegistry
from src.manager.secrets import ManagerSecretStore
from src.manager.terminal_discovery import TerminalDiscovery

logger = logging.getLogger(__name__)

_BASE_MONITORING_PORT = 8081


class SlotLimitError(Exception):
    """Raised when the license has no remaining agent slots."""


class LicensePreflightError(Exception):
    """Raised when a license key cannot be verified with the gateway."""


class AgentProvisioner:
    def __init__(
        self,
        registry: AgentRegistry,
        secrets: ManagerSecretStore,
        config_store: AgentConfigStore,
        discovery: TerminalDiscovery,
        agents_data_dir: str,
        gateway_http_url: str = "",
    ) -> None:
        self._registry        = registry
        self._secrets         = secrets
        self._config_store    = config_store
        self._discovery       = discovery
        self._agents_data_dir = Path(agents_data_dir)
        self._gateway_http_url = gateway_http_url.rstrip("/")

    def provision(
        self,
        display_name: str,
        terminal_path: str,
        mt5_login: int,
        mt5_password: str,
        mt5_server: str,
        symbols: list[str],
        config_overrides: dict | None = None,
    ) -> AgentRegistration:
        # 1. Pre-flight slot check
        self._check_slot_available()

        now = int(time.time() * 1000)

        # 2. Allocate IDs
        agent_id = self._next_agent_id()
        port     = self._next_port()
        data_dir = str(self._agents_data_dir / agent_id)

        # 3. Create data directory
        Path(data_dir).mkdir(parents=True, exist_ok=True)

        # 4. Build registration
        reg = AgentRegistration(
            agent_id=agent_id,
            display_name=display_name,
            status=AgentStatus.PROVISIONED,
            desired_status="stopped",
            config_path=str(Path(data_dir) / "config.yaml"),
            data_dir=data_dir,
            terminal_path=terminal_path,
            mt5_login=mt5_login,
            mt5_server=mt5_server,
            monitoring_port=port,
            symbols=symbols,
            created_at=now,
            updated_at=now,
            last_seen_at=None,
            pid=None,
        )

        # 5. Write redacted config.yaml
        self._config_store.write_agent_config(reg, config_overrides)

        # 6. Store per-agent secrets
        self._secrets.set_secret(agent_id, "mt5_password", mt5_password)

        # 7. Acquire terminal lease
        if terminal_path:
            acquired = self._registry.acquire_terminal_lease(terminal_path, agent_id)
            if not acquired:
                # Lease already held by another agent — warn but continue
                logger.warning(
                    "Terminal %s is already leased; provisioning %s anyway",
                    terminal_path, agent_id,
                )

        # 8. Persist agent
        self._registry.upsert_agent(reg)
        self._registry.emit_event("agent.provisioned", agent_id, {"display_name": display_name})

        logger.info("Provisioned agent %s (%s) on port %d", agent_id, display_name, port)
        return reg

    def get_license_info(self, force: bool = False) -> dict:
        """
        Return license metadata from the gateway (slots, symbols).
        Caches the result in device_state for 10 minutes to avoid
        hammering the gateway on every AddAgent page load.
        """
        import json
        import time

        _CACHE_TTL = 600   # seconds

        raw = self._registry.load_device_state("license_info")
        if raw and not force:
            try:
                cached = json.loads(raw)
                if time.time() - cached.get("_cached_at", 0) < _CACHE_TTL:
                    return cached
            except Exception:
                pass

        activation_key = self._secrets.get_activation_key()
        if not activation_key:
            return {
                "configured": False,
                "valid": False,
                "symbols": [],
                "error": "No manager license key configured",
            }
        if not self._gateway_http_url:
            return {
                "configured": True,
                "valid": False,
                "symbols": [],
                "error": "Gateway HTTP URL is not configured",
            }

        try:
            return self.preflight_license(activation_key, cache=True)
        except Exception as exc:
            logger.warning("License info fetch failed: %s", exc)
            return {
                "configured": True,
                "valid": False,
                "symbols": [],
                "error": str(exc),
            }

    def preflight_license(self, activation_key: str, cache: bool = False) -> dict:
        """Verify a license key without changing the stored manager key."""
        activation_key = activation_key.strip()
        if not activation_key:
            raise LicensePreflightError("License key is required")
        if not self._gateway_http_url:
            raise LicensePreflightError("Gateway HTTP URL is not configured")

        try:
            body = json.dumps({"activation_key": activation_key}).encode()
            req = urllib.request.Request(
                f"{self._gateway_http_url}/activation/preflight",
                data=body,
                headers={"Content-Type": "application/json", "User-Agent": "AQAgent/1.0"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("message")
            except Exception:
                detail = None
            raise LicensePreflightError(detail or f"Gateway returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise LicensePreflightError(f"Gateway unreachable: {exc.reason}") from exc
        except Exception as exc:
            raise LicensePreflightError(f"License preflight failed: {exc}") from exc

        data["configured"] = bool(self._secrets.get_activation_key())
        data["error"] = None if data.get("valid") else "License key is invalid"
        if cache and data.get("valid"):
            data["_cached_at"] = time.time()
            self._registry.save_device_state("license_info", json.dumps(data))
        return data

    def set_activation_key(self, activation_key: str) -> dict:
        """Verify and persist a replacement manager license key."""
        info = self.preflight_license(activation_key)
        if not info.get("valid"):
            raise LicensePreflightError("License key is invalid")

        self._secrets.set_activation_key(activation_key.strip())
        info["configured"] = True
        info["_cached_at"] = time.time()
        self._registry.save_device_state("license_info", json.dumps(info))
        return info

    def deprovision(self, agent_id: str) -> None:
        reg = self._registry.get_agent(agent_id)
        if not reg:
            return

        # Release terminal lease
        self._registry.release_agent_leases(agent_id)

        # Remove per-agent secrets
        self._secrets.delete_agent_secrets(agent_id)

        # Mark as STOPPED (preserve history row)
        self._registry.set_agent_status(agent_id, AgentStatus.STOPPED, pid=None)
        self._registry.set_desired_status(agent_id, "stopped")
        self._registry.emit_event("agent.deprovisioned", agent_id, {})

        logger.info("Deprovisioned agent %s", agent_id)

    def assign_terminal(self, agent_id: str, terminal_path: str) -> None:
        """Reassign the terminal lease for an existing agent."""
        reg = self._registry.get_agent(agent_id)
        if not reg:
            raise ValueError(f"Agent {agent_id} not found")

        # Release old lease if any
        if reg.terminal_path:
            self._registry.release_terminal_lease(reg.terminal_path)

        acquired = self._registry.acquire_terminal_lease(terminal_path, agent_id)
        if not acquired:
            raise ValueError(f"Terminal {terminal_path} is already leased")

        # Update registration
        updated = AgentRegistration(
            **{**reg.__dict__, "terminal_path": terminal_path, "updated_at": int(time.time() * 1000)}
        )
        self._registry.upsert_agent(updated)

    # ── Private helpers ───────────────────────────────────────────────────

    def _check_slot_available(self) -> None:
        if not self._gateway_http_url:
            return   # no gateway URL configured — skip check

        activation_key = self._secrets.get_activation_key()
        if not activation_key:
            raise SlotLimitError("No activation key configured at manager level")

        current_count = len(self._registry.list_agents())

        try:
            body = json.dumps({
                "activation_key": activation_key,
                "device_count":   current_count,
            }).encode()
            req = urllib.request.Request(
                f"{self._gateway_http_url}/licenses/slots/check",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            if not data.get("allowed", False):
                max_slots  = data.get("max_slots", "?")
                used_slots = data.get("used_slots", current_count)
                raise SlotLimitError(
                    f"License allows {max_slots} agent(s); {used_slots} already in use"
                )
        except SlotLimitError:
            raise
        except urllib.error.URLError as exc:
            logger.warning("Slot check failed (gateway unreachable): %s — proceeding", exc)
        except Exception as exc:
            logger.warning("Slot check error: %s — proceeding", exc)

    def _next_agent_id(self) -> str:
        existing = {a.agent_id for a in self._registry.list_agents()}
        for i in range(1000):
            candidate = f"agent-{i}"
            if candidate not in existing:
                return candidate
        return f"agent-{uuid.uuid4().hex[:8]}"

    def _next_port(self) -> int:
        used = {a.monitoring_port for a in self._registry.list_agents()}
        port = _BASE_MONITORING_PORT
        while port < 9000:
            if port not in used and _port_available(port):
                return port
            port += 1
        raise RuntimeError("No available monitoring port found in range 8081-8999")


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False
