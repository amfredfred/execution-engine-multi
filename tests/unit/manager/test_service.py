from pathlib import Path

from src.manager.service import ManagerRuntime


def test_runtime_bootstraps_fresh_registry_before_loading_secrets(tmp_path: Path) -> None:
    storage_path = tmp_path / "manager"

    runtime = ManagerRuntime(
        storage_path=str(storage_path),
        agents_data_dir=str(tmp_path / "agents"),
    )

    assert (storage_path / "registry.db").is_file()
    assert runtime.secrets.get_ipc_token()
    assert runtime.secrets.get_api_token()


def test_runtime_starts_one_ipc_hub_and_local_api(tmp_path: Path) -> None:
    runtime = ManagerRuntime(
        storage_path=str(tmp_path / "manager"),
        agents_data_dir=str(tmp_path / "agents"),
        api_port=0,
        ipc_port=0,
    )

    runtime.start()
    try:
        assert runtime.event_hub._server is not None
        assert runtime.api._server is not None
    finally:
        runtime.stop()
