"""
manager/config_store.py — Writes/reads per-agent config.yaml files.

Secrets (mt5.password, gateway.activation_key) are NEVER written to disk.
They are injected as env vars at subprocess spawn time.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path

import yaml

from src.manager.models import AgentRegistration
from src.manager.secrets import ManagerSecretStore

logger = logging.getLogger(__name__)

# Fields that must never appear in written YAML
_REDACT_PATHS = [
    ("mt5", "password"),
    ("gateway", "activation_key"),
    ("gateway", "signal_hmac_secret"),
]


class AgentConfigStore:
    def __init__(self, secrets: ManagerSecretStore) -> None:
        self._secrets = secrets

    def write_agent_config(
        self,
        reg: AgentRegistration,
        user_overrides: dict | None = None,
    ) -> str:
        """
        Write a redacted config.yaml into reg.data_dir.
        Returns the path of the written file.
        """
        base: dict = {
            "gateway": {
                "activation_key": "",   # injected via APEX_ACTIVATION_KEY env var
                "symbols": reg.symbols,
            },
            "mt5": {
                "login":    reg.mt5_login,
                "password": "",          # injected via MT5_PASSWORD env var
                "server":   reg.mt5_server,
                "path":     reg.terminal_path or "",
            },
            "engine": {
                "storage_path":    reg.data_dir,
                "monitoring_port": reg.monitoring_port,
            },
            "manager": {
                "channel_port": 8871,
            },
        }

        if user_overrides:
            base = _deep_merge(base, user_overrides)

        # Scrub any secrets that crept in via overrides
        for section, key in _REDACT_PATHS:
            if section in base and key in base[section]:
                base[section][key] = ""

        config_path = Path(reg.data_dir) / "config.yaml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(base, fh, default_flow_style=False, allow_unicode=True)

        logger.debug("Wrote agent config to %s", config_path)
        return str(config_path)

    def read_agent_config(self, reg: AgentRegistration) -> dict:
        config_path = Path(reg.data_dir) / "config.yaml"
        if not config_path.exists():
            return {}
        with open(config_path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}

    def update_agent_config(self, reg: AgentRegistration, patch: dict) -> None:
        current = self.read_agent_config(reg)
        merged  = _deep_merge(current, patch)
        # Re-scrub after merge
        for section, key in _REDACT_PATHS:
            if section in merged and key in merged[section]:
                merged[section][key] = ""
        config_path = Path(reg.data_dir) / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(merged, fh, default_flow_style=False, allow_unicode=True)


def _deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = copy.deepcopy(v)
    return result
