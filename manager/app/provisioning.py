"""
manager/provisioning.py — Agent lifecycle provisioning.

Creates/removes per-agent directories, config files, secrets, and terminal leases.
Pre-flight slot check against the gateway before provisioning.
"""

from __future__ import annotations

import logging
import time
import urllib.request
import urllib.error
import json
import shutil
import secrets as _secrets
from pathlib import Path

from manager.app.config_store import AgentConfigStore
from manager.app.models import AgentRegistration, AgentStatus
from manager.app.registry import AgentRegistry
from manager.app.secrets import ManagerSecretStore
from manager.app.terminal_discovery import TerminalDiscovery

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
        agent_id, port = self._registry.allocate_agent_identity(_BASE_MONITORING_PORT)
        data_dir = str(self._agents_data_dir / agent_id)

        data_path = Path(data_dir)
        lease_acquired = False
        agent_persisted = False
        secret_persisted = False
        try:
            data_path.mkdir(parents=True, exist_ok=False)

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

            self._config_store.write_agent_config(reg, config_overrides)

            self._secrets.set_secret(agent_id, "mt5_password", mt5_password)
            secret_persisted = True
            self._secrets.set_secret(agent_id, "ipc_token", _secrets.token_hex(32))

            if terminal_path:
                lease_acquired = self._registry.acquire_terminal_lease(
                    terminal_path, agent_id
                )
                if not lease_acquired:
                    raise ValueError(f"Terminal {terminal_path} is already leased")

            self._registry.upsert_agent(reg)
            agent_persisted = True
            self._registry.emit_event(
                "agent.provisioned", agent_id, {"display_name": display_name}
            )
        except Exception:
            if agent_persisted:
                self._registry.delete_agent(agent_id)
            if lease_acquired:
                self._registry.release_agent_leases(agent_id)
            if secret_persisted:
                self._secrets.delete_agent_secrets(agent_id)
            self._registry.release_agent_allocation(agent_id)
            shutil.rmtree(data_path, ignore_errors=True)
            raise

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

        data_path = Path(reg.data_dir)
        if data_path.exists():
            archive_dir = self._agents_data_dir / "archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            target = archive_dir / f"{agent_id}-{int(time.time())}"
            shutil.move(str(data_path), str(target))

        # Operations and manager events retain the historical audit trail.
        self._registry.delete_agent(agent_id)
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
            raise SlotLimitError("Gateway HTTP URL is required for slot verification")

        activation_key = self._secrets.get_activation_key()
        if not activation_key:
            raise SlotLimitError("No activation key configured at manager level")

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
            if not data.get("valid", False):
                raise SlotLimitError("License key is not valid or has expired")
            available = data.get("available_devices", 1)
            if available <= 0:
                max_dev  = data.get("max_devices", "?")
                used_dev = data.get("used_devices", "?")
                raise SlotLimitError(
                    f"License allows {max_dev} agent(s); {used_dev} already in use"
                )
        except SlotLimitError:
            raise
        except urllib.error.HTTPError as exc:
            # 429 = rate limited — surface it clearly; other HTTP errors fail open.
            if exc.code == 429:
                raise SlotLimitError(
                    "Rate limit exceeded on slot check — wait a few minutes and try again"
                ) from exc
            logger.warning(
                "Slot check returned HTTP %s — proceeding with provisioning", exc.code
            )
        except urllib.error.URLError as exc:
            logger.warning("Slot check unreachable (%s) — proceeding with provisioning", exc.reason)
        except Exception as exc:
            logger.warning("Slot check failed (%s) — proceeding with provisioning", exc)
