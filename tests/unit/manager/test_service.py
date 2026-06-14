from pathlib import Path

from src.manager.service import ManagerRuntime


def test_runtime_bootstraps_fresh_registry_before_loading_secrets(tmp_path: Path) -> None:
    storage_path = tmp_path / "manager"

    runtime = ManagerRuntime(
        storage_path=str(storage_path),
        agents_data_dir=str(tmp_path / "agents"),
    )

    assert (storage_path / "registry.db").is_file()
    assert runtime.secrets.get_channel_token()
    assert runtime.secrets.get_api_token()
