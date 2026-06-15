import json
from unittest.mock import MagicMock, patch

import pytest

from manager.app.provisioning import AgentProvisioner, LicensePreflightError


def _provisioner(activation_key: str | None = "TR-VALID-LICENSE-KEY-1234"):
    registry = MagicMock()
    registry.load_device_state.return_value = None
    secrets = MagicMock()
    secrets.get_activation_key.return_value = activation_key
    provisioner = AgentProvisioner(
        registry=registry,
        secrets=secrets,
        config_store=MagicMock(),
        discovery=MagicMock(),
        agents_data_dir="agents",
        gateway_http_url="https://gateway.example",
    )
    return provisioner, registry, secrets


def _response(payload: dict):
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return response


def test_get_license_info_reports_missing_manager_key() -> None:
    provisioner, _, _ = _provisioner(activation_key=None)

    result = provisioner.get_license_info()

    assert result["configured"] is False
    assert result["symbols"] == []
    assert "No manager license key" in result["error"]


@patch("src.manager.provisioning.urllib.request.urlopen")
def test_preflight_returns_gateway_symbols_without_saving(mock_urlopen) -> None:
    provisioner, _, secrets = _provisioner()
    mock_urlopen.return_value = _response({
        "valid": True,
        "max_devices": 3,
        "available_devices": 2,
        "symbols": ["XAUUSD", "EURUSD"],
    })

    result = provisioner.preflight_license("TR-NEW-LICENSE-KEY-12345")

    assert result["valid"] is True
    assert result["symbols"] == ["XAUUSD", "EURUSD"]
    secrets.set_activation_key.assert_not_called()


@patch("src.manager.provisioning.urllib.request.urlopen")
def test_set_activation_key_only_persists_verified_key(mock_urlopen) -> None:
    provisioner, registry, secrets = _provisioner()
    mock_urlopen.return_value = _response({"valid": True, "symbols": ["US100"]})

    result = provisioner.set_activation_key("  TR-NEW-LICENSE-KEY-12345  ")

    secrets.set_activation_key.assert_called_once_with("TR-NEW-LICENSE-KEY-12345")
    registry.save_device_state.assert_called_once()
    assert result["configured"] is True


@patch("src.manager.provisioning.urllib.request.urlopen")
def test_set_activation_key_rejects_invalid_key(mock_urlopen) -> None:
    provisioner, _, secrets = _provisioner()
    mock_urlopen.return_value = _response({"valid": False})

    with pytest.raises(LicensePreflightError, match="invalid"):
        provisioner.set_activation_key("TR-BAD-LICENSE-KEY-123456")

    secrets.set_activation_key.assert_not_called()
