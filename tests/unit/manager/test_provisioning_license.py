import json
from unittest.mock import MagicMock, patch

import pytest

from manager.app.provisioning import (
    AgentProvisioner,
    LicensePreflightError,
    SlotLimitError,
)


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
    assert result["authoritative"] is False
    assert result["symbols"] == []
    assert "No manager license key" in result["error"]


@patch("manager.app.provisioning.urllib.request.urlopen")
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
    assert result["authoritative"] is True
    assert result["symbols"] == ["XAUUSD", "EURUSD"]
    secrets.set_activation_key.assert_not_called()


@patch("manager.app.provisioning.urllib.request.urlopen")
def test_set_activation_key_only_persists_verified_key(mock_urlopen) -> None:
    provisioner, registry, secrets = _provisioner()
    mock_urlopen.return_value = _response({"valid": True, "symbols": ["US100"]})

    result = provisioner.set_activation_key("  TR-NEW-LICENSE-KEY-12345  ")

    secrets.set_activation_key.assert_called_once_with("TR-NEW-LICENSE-KEY-12345")
    registry.save_device_state.assert_called_once()
    assert result["configured"] is True


@patch("manager.app.provisioning.urllib.request.urlopen")
def test_set_activation_key_rejects_invalid_key(mock_urlopen) -> None:
    provisioner, _, secrets = _provisioner()
    mock_urlopen.return_value = _response({"valid": False})

    with pytest.raises(LicensePreflightError, match="invalid"):
        provisioner.set_activation_key("TR-BAD-LICENSE-KEY-123456")

    secrets.set_activation_key.assert_not_called()


def test_slot_verification_fails_closed_without_gateway() -> None:
    provisioner, _, _ = _provisioner()
    provisioner._gateway_http_url = ""

    with pytest.raises(SlotLimitError, match="required"):
        provisioner._check_slot_available()


@patch("manager.app.provisioning.urllib.request.urlopen")
def test_slot_verification_passes_when_slots_available(mock_urlopen) -> None:
    provisioner, _, _ = _provisioner()
    mock_urlopen.return_value = _response({
        "valid": True, "max_devices": 3, "used_devices": 1, "available_devices": 2,
    })

    provisioner._check_slot_available()  # must not raise


@patch("manager.app.provisioning.urllib.request.urlopen")
def test_slot_verification_blocks_when_no_slots_available(mock_urlopen) -> None:
    provisioner, _, _ = _provisioner()
    mock_urlopen.return_value = _response({
        "valid": True, "max_devices": 1, "used_devices": 1, "available_devices": 0,
    })

    with pytest.raises(SlotLimitError, match="1 agent"):
        provisioner._check_slot_available()


@patch("manager.app.provisioning.urllib.request.urlopen")
def test_slot_verification_blocks_when_key_invalid(mock_urlopen) -> None:
    provisioner, _, _ = _provisioner()
    mock_urlopen.return_value = _response({"valid": False})

    with pytest.raises(SlotLimitError, match="not valid"):
        provisioner._check_slot_available()


@patch("manager.app.provisioning.urllib.request.urlopen")
def test_slot_verification_blocks_on_429(mock_urlopen) -> None:
    provisioner, _, _ = _provisioner()
    mock_urlopen.side_effect = __import__("urllib.error").error.HTTPError(
        url="", code=429, msg="Too Many Requests", hdrs={}, fp=None,
    )

    with pytest.raises(SlotLimitError, match="Rate limit"):
        provisioner._check_slot_available()


@patch("manager.app.provisioning.urllib.request.urlopen")
def test_slot_verification_fails_open_when_gateway_is_unreachable(mock_urlopen) -> None:
    provisioner, _, _ = _provisioner()
    mock_urlopen.side_effect = __import__("urllib.error").error.URLError("offline")

    provisioner._check_slot_available()  # must not raise


@patch("manager.app.provisioning.urllib.request.urlopen")
def test_slot_verification_fails_open_on_http_5xx(mock_urlopen) -> None:
    provisioner, _, _ = _provisioner()
    mock_urlopen.side_effect = __import__("urllib.error").error.HTTPError(
        url="", code=503, msg="Service Unavailable", hdrs={}, fp=None,
    )

    provisioner._check_slot_available()  # must not raise


def test_provision_rolls_back_directory_secret_and_lease_on_failure(tmp_path) -> None:
    registry = MagicMock()
    registry.acquire_terminal_lease.return_value = True
    secrets = MagicMock()
    config_store = MagicMock()
    config_store.write_agent_config.side_effect = RuntimeError("write failed")
    provisioner = AgentProvisioner(
        registry, secrets, config_store, MagicMock(), str(tmp_path), "https://gateway"
    )
    provisioner._check_slot_available = MagicMock()
    registry.allocate_agent_identity.return_value = ("agent-0", 8081)

    with pytest.raises(RuntimeError, match="write failed"):
        provisioner.provision(
            "Agent", "terminal.exe", 123, "password", "Broker", ["XAUUSD"]
        )

    assert not (tmp_path / "agent-0").exists()
    secrets.set_secret.assert_not_called()
    registry.upsert_agent.assert_not_called()
    registry.release_agent_allocation.assert_called_once_with("agent-0")
