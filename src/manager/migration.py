"""
manager/migration.py — One-time migration of a pre-manager single-agent config.

If the registry has 0 agents AND the legacy config.yaml exists at the
well-known path, import it as agent-0 so existing installs upgrade
to multi-agent without reconfiguration.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from src.manager.provisioning import AgentProvisioner
from src.manager.registry import AgentRegistry

logger = logging.getLogger(__name__)

_MIGRATION_KEY = "migration_v1_complete"


class LegacySingleAgentMigration:
    def __init__(
        self,
        registry: AgentRegistry,
        provisioner: AgentProvisioner,
        legacy_config_path: str,
    ) -> None:
        self._registry           = registry
        self._provisioner        = provisioner
        self._legacy_config_path = legacy_config_path

    def run_if_needed(self) -> bool:
        """
        Returns True if migration was performed, False if skipped.
        Idempotent — safe to call on every manager start.
        """
        if self._registry.load_device_state(_MIGRATION_KEY) == "true":
            return False

        agents = self._registry.list_agents()
        if agents:
            self._registry.save_device_state(_MIGRATION_KEY, "true")
            return False

        legacy_path = Path(self._legacy_config_path)
        if not legacy_path.exists():
            return False

        logger.info("Migrating legacy config from %s", legacy_path)
        try:
            self._migrate(legacy_path)
            self._registry.save_device_state(_MIGRATION_KEY, "true")
            logger.info("Legacy migration complete — agent-0 provisioned")
            return True
        except Exception as exc:
            logger.error("Legacy migration failed: %s", exc)
            return False

    def _migrate(self, config_path: Path) -> None:
        with open(config_path, "r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}

        mt5     = cfg.get("mt5", {})
        gateway = cfg.get("gateway", {})

        mt5_login    = mt5.get("login") or 0
        mt5_password = mt5.get("password") or ""
        mt5_server   = mt5.get("server") or ""
        terminal_path = mt5.get("path") or ""
        symbols      = gateway.get("symbols") or ["XAUUSD"]
        activation_key = gateway.get("activation_key") or ""

        # Always store the master activation key at manager level when available
        if activation_key:
            from src.manager.secrets import ManagerSecretStore
            secrets = ManagerSecretStore(self._registry)
            existing = secrets.get_activation_key()
            if not existing:
                secrets.set_activation_key(activation_key)

        # In multi-agent fresh installs the legacy config has no MT5 credentials
        # (only the activation key from onboarding).  Skip agent-0 creation so
        # users start with a clean fleet and add agents manually.
        if not mt5_login or not mt5_server or not terminal_path:
            logger.info(
                "Legacy config has no MT5 credentials — storing activation key "
                "but skipping auto-creation of agent-0"
            )
            return

        # Build config overrides from the legacy file (strip secrets)
        overrides = {k: v for k, v in cfg.items() if k not in ("mt5", "gateway")}

        self._provisioner.provision(
            display_name="Migrated Agent",
            terminal_path=terminal_path,
            mt5_login=int(mt5_login),
            mt5_password=mt5_password,
            mt5_server=mt5_server,
            symbols=symbols,
            config_overrides=overrides,
        )
