"""
Unit test: AgentConfigStore must never write secrets to disk.

Confirms that mt5.password, gateway.activation_key, and
gateway.signal_hmac_secret are always blank in the written YAML.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from src.manager.config_store import AgentConfigStore
from src.manager.models import AgentRegistration, AgentStatus


def _make_reg(tmp_path: Path, agent_id: str = "agent-0") -> AgentRegistration:
    data_dir = str(tmp_path / agent_id)
    Path(data_dir).mkdir(parents=True)
    now = 0
    return AgentRegistration(
        agent_id=agent_id,
        display_name="Test",
        status=AgentStatus.PROVISIONED,
        desired_status="running",
        config_path=str(Path(data_dir) / "config.yaml"),
        data_dir=data_dir,
        terminal_path="C:/MT5/terminal64.exe",
        mt5_login=12345,
        mt5_server="TestBroker",
        monitoring_port=8081,
        symbols=["XAUUSD"],
        created_at=now,
        updated_at=now,
        last_seen_at=None,
        pid=None,
    )


@pytest.fixture
def store():
    return AgentConfigStore(secrets=MagicMock())


class TestConfigStoreSecrets:

    def test_no_mt5_password_in_yaml(self, store, tmp_path):
        reg = _make_reg(tmp_path)
        store.write_agent_config(reg)

        written = yaml.safe_load(Path(reg.config_path).read_text(encoding="utf-8"))
        password = (written.get("mt5") or {}).get("password", "")
        assert not password, f"mt5.password must be blank, got: {password!r}"

    def test_no_activation_key_in_yaml(self, store, tmp_path):
        reg = _make_reg(tmp_path)
        store.write_agent_config(reg)

        written = yaml.safe_load(Path(reg.config_path).read_text(encoding="utf-8"))
        key = (written.get("gateway") or {}).get("activation_key", "")
        assert not key, f"gateway.activation_key must be blank, got: {key!r}"

    def test_signal_hmac_secret_redacted_from_overrides(self, store, tmp_path):
        reg = _make_reg(tmp_path)
        store.write_agent_config(reg, user_overrides={"gateway": {"signal_hmac_secret": "super-secret"}})

        written = yaml.safe_load(Path(reg.config_path).read_text(encoding="utf-8"))
        secret = (written.get("gateway") or {}).get("signal_hmac_secret", "")
        assert not secret, f"gateway.signal_hmac_secret must be redacted, got: {secret!r}"

    def test_password_redacted_even_from_overrides(self, store, tmp_path):
        reg = _make_reg(tmp_path)
        store.write_agent_config(reg, user_overrides={"mt5": {"password": "leaked!"}})

        written = yaml.safe_load(Path(reg.config_path).read_text(encoding="utf-8"))
        password = (written.get("mt5") or {}).get("password", "")
        assert not password, f"mt5.password must be redacted even from overrides, got: {password!r}"

    def test_storage_path_set_to_agent_data_dir(self, store, tmp_path):
        reg = _make_reg(tmp_path)
        store.write_agent_config(reg)

        written = yaml.safe_load(Path(reg.config_path).read_text(encoding="utf-8"))
        sp = (written.get("engine") or {}).get("storage_path", "")
        assert sp == reg.data_dir, f"storage_path must equal data_dir, got: {sp!r}"

    def test_monitoring_port_written(self, store, tmp_path):
        reg = _make_reg(tmp_path)
        store.write_agent_config(reg)

        written = yaml.safe_load(Path(reg.config_path).read_text(encoding="utf-8"))
        port = (written.get("engine") or {}).get("monitoring_port")
        assert port == reg.monitoring_port, f"monitoring_port must match reg, got: {port}"

    def test_symbols_written_to_gateway(self, store, tmp_path):
        reg = _make_reg(tmp_path)
        store.write_agent_config(reg)

        written = yaml.safe_load(Path(reg.config_path).read_text(encoding="utf-8"))
        symbols = (written.get("gateway") or {}).get("symbols", [])
        assert symbols == reg.symbols

    def test_update_config_redacts_password(self, store, tmp_path):
        reg = _make_reg(tmp_path)
        store.write_agent_config(reg)

        # Attempt to patch in a password via update
        store.update_agent_config(reg, {"mt5": {"password": "should-be-gone"}})

        written = yaml.safe_load(Path(reg.config_path).read_text(encoding="utf-8"))
        password = (written.get("mt5") or {}).get("password", "")
        assert not password, f"update must redact password too, got: {password!r}"
