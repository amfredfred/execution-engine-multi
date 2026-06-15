from pathlib import Path

import pytest
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


def _write_config(tmp_path: Path, patch: dict) -> Path:
    value = {
        "gateway": {"symbols": ["XAUUSD"]},
        "mt5": {"login": 12345678, "server": "FBS-Demo"},
        "engine": {"storage_path": str(tmp_path), "monitoring_port": 8081},
    }
    for section, values in patch.items():
        value.setdefault(section, {}).update(values)
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(value), encoding="utf-8")
    return path


def test_string_false_is_not_accepted_as_true(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MT5_PASSWORD", "test-password")
    monkeypatch.setenv("APEX_ACTIVATION_KEY", "TR-VALID-TEST-ACTIVATION-KEY")

    with pytest.raises(ValueError, match="must be true or false"):
        AppConfig.from_yaml(_write_config(tmp_path, {"risk": {"no_hedging": "false"}}))


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_non_positive_or_non_finite_poll_interval_is_rejected(
    tmp_path, monkeypatch, value
) -> None:
    monkeypatch.setenv("MT5_PASSWORD", "test-password")
    monkeypatch.setenv("APEX_ACTIVATION_KEY", "TR-VALID-TEST-ACTIVATION-KEY")

    with pytest.raises(ValueError, match="position_poll_interval"):
        AppConfig.from_yaml(
            _write_config(tmp_path, {"engine": {"position_poll_interval": value}})
        )


def test_non_finite_risk_value_is_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MT5_PASSWORD", "test-password")
    monkeypatch.setenv("APEX_ACTIVATION_KEY", "TR-VALID-TEST-ACTIVATION-KEY")

    with pytest.raises(ValueError, match="max_daily_loss_percent"):
        AppConfig.from_yaml(
            _write_config(tmp_path, {"risk": {"max_daily_loss_percent": float("nan")}})
        )
