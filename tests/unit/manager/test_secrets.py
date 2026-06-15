from unittest.mock import MagicMock, patch

import pytest

from manager.app.secrets import ManagerSecretStore


def test_encryption_failure_never_falls_back_to_plaintext() -> None:
    registry = MagicMock()
    store = ManagerSecretStore(registry)

    with (
        patch("manager.app.secrets._dpapi_encrypt", side_effect=RuntimeError("DPAPI unavailable")),
        pytest.raises(RuntimeError, match="Refusing to store secret"),
    ):
        store.set_secret("agent-0", "mt5_password", "plain-secret")

    registry._connect.assert_not_called()
