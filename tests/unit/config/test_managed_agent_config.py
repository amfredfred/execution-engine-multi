from pathlib import Path

import yaml

from src.config.settings import AppConfig


def test_slim_managed_agent_config_uses_internal_risk_defaults(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({
            "gateway": {"symbols": ["XAUUSD"]},
            "mt5": {
                "login": 12345678,
                "password": "",
                "server": "FBS-Demo",
                "path": "terminal64.exe",
            },
            "engine": {"storage_path": str(tmp_path), "monitoring_port": 8081},
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("MT5_PASSWORD", "test-password")
    monkeypatch.setenv("APEX_ACTIVATION_KEY", "TR-VALID-TEST-ACTIVATION-KEY")

    config = AppConfig.from_yaml(config_path)

    assert config.risk.max_losing_streak == 3
    assert config.risk.max_daily_loss_percent == 2.5
    assert config.risk.max_lot_size == 100.0
    assert config.gateway.engine_id == "execution-12345678"
