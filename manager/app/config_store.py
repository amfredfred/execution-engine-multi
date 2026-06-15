"""
manager/config_store.py — Writes/reads per-agent config.yaml files.

Secrets (mt5.password, gateway.activation_key) are NEVER written to disk.
They are injected as env vars at subprocess spawn time.
"""

from __future__ import annotations

import copy
import logging
import re
from pathlib import Path

import yaml

from manager.app.models import AgentRegistration
from manager.app.registry import AgentRegistry
from manager.app.secrets import ManagerSecretStore

logger = logging.getLogger(__name__)

# Fields that must never appear in written YAML
_REDACT_PATHS = [
    ("mt5", "password"),
    ("gateway", "activation_key"),
    ("gateway", "signal_hmac_secret"),
]


class AgentConfigStore:
    def __init__(self, secrets: ManagerSecretStore, registry: AgentRegistry) -> None:
        self._secrets = secrets
        self._registry = registry

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
                "engine_id": _make_engine_id(
                    self._registry.get_or_create_device_name(),
                    self._registry.get_or_create_device_id(),
                    reg.agent_id,
                ),
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
        }

        if user_overrides:
            base = _deep_merge(base, user_overrides)
        base.setdefault("mt5", {})["magic"] = _magic_for_agent(reg.agent_id)

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
        merged = self.preview_agent_config(reg, patch)
        self.write_config_document(reg, merged)

    def preview_agent_config(self, reg: AgentRegistration, patch: dict) -> dict:
        merged = _deep_merge(self.read_agent_config(reg), patch)
        # Re-scrub after merge
        for section, key in _REDACT_PATHS:
            if section in merged and key in merged[section]:
                merged[section][key] = ""
        return merged

    def write_config_document(self, reg: AgentRegistration, document: dict) -> None:
        document.setdefault("mt5", {})["magic"] = _magic_for_agent(reg.agent_id)
        config_path = Path(reg.data_dir) / "config.yaml"
        with open(config_path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(document, fh, default_flow_style=False, allow_unicode=True)


def _make_engine_id(hostname: str, device_id: str, agent_id: str) -> str:
    """Build a stable, unique engine_id for this agent on this machine.

    Format: <hostname-slug>-<uuid6>-<agent_id>
    Example: desktop-abc123-a1b2c3-agent-0

    The hostname slug is lowercase alphanumeric + hyphens, capped at 16 chars
    so the full ID stays readable in logs. The 6-char UUID prefix makes it
    globally unique even when two machines share the same hostname.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", hostname.lower()).strip("-")[:16]
    uid6 = device_id.replace("-", "")[:6]
    return f"{slug}-{uid6}-{agent_id}"


def _magic_for_agent(agent_id: str) -> int:
    numeric = re.search(r"(\d+)$", agent_id)
    if numeric:
        return 8_850_000 + int(numeric.group(1))
    return 8_000_000 + int.from_bytes(agent_id.encode("utf-8"), "little") % 999_999


def _deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = copy.deepcopy(v)
    return result
