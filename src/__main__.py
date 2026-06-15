"""Apex Quantel entry points: GUI, manager service, and isolated engine worker."""

from __future__ import annotations

import sys


def _get_worker_id() -> str | None:
    try:
        return sys.argv[sys.argv.index("--agent") + 1]
    except (ValueError, IndexError):
        return None


def _worker_main(engine_id: str) -> None:
    import logging
    import os
    import signal
    import threading
    from pathlib import Path

    from src.app.bootstrap import bootstrap, shutdown
    from src.app.container import build_container
    from src.config.settings import AppConfig
    from src.infra.logger import add_file_handler, setup_logging
    from src.utils import time as engine_time
    from src.worker.event_client import WorkerEventClient

    try:
        config_path = sys.argv[sys.argv.index("--agent") + 2]
    except (ValueError, IndexError):
        raise SystemExit("Usage: --agent <engine_id> <config_path>")

    config = AppConfig.from_yaml(config_path)
    setup_logging(config.log_level, config.engine_timezone)
    engine_time.configure(config.engine_timezone)
    add_file_handler(Path(config.storage_path) / "logs", config.engine_timezone)
    logger = logging.getLogger("worker")
    lock_file = _acquire_lock(Path(config.storage_path) / "execution-engine.lock")

    container = build_container(config)
    worker_events = WorkerEventClient(
        engine_id=engine_id,
        manager_host="127.0.0.1",
        manager_port=int(os.environ.get("ENGINE_IPC_PORT", "8871")),
        token=os.environ.get("ENGINE_IPC_TOKEN", ""),
        container=container,
        account_login=config.mt5.login,
        account_server=config.mt5.server,
        storage_path=config.storage_path,
    )
    container.worker_events = worker_events
    container.signal_consumer.set_execution_event_sink(worker_events.emit_execution_event)

    bootstrap(container, config, expose_local_ui=False)
    worker_events.start()

    stop_event = threading.Event()

    def stop(signum: int, _frame) -> None:
        logger.info("Worker %s stopping on %s", engine_id, signal.Signals(signum).name)
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    logger.info("Worker %s ready", engine_id)
    try:
        while not stop_event.wait(1):
            pass
    finally:
        shutdown(container)
        lock_file.close()


def _manager_main() -> None:
    import logging
    import signal
    import threading
    from pathlib import Path
    from zoneinfo import ZoneInfo

    from src.config.settings import ManagerConfig
    from src.infra.logger import add_file_handler, setup_logging
    from src.manager.service import ManagerRuntime

    setup_logging("INFO", ZoneInfo("UTC"))
    config = ManagerConfig.defaults()
    lock_file = _acquire_lock(Path(config.storage_path) / "manager.lock")
    add_file_handler(Path(config.storage_path) / "logs", ZoneInfo("UTC"))
    logger = logging.getLogger("manager")
    runtime = ManagerRuntime(
        storage_path=config.storage_path,
        agents_data_dir=config.agents_data_dir,
        api_port=config.api_port,
        ipc_port=config.ipc_port,
        signal_ws_url=config.signal_ws_url,
        signal_ws_token=config.signal_ws_token,
        gateway_http_url=config.gateway_http_url,
        engine_version=config.engine_version,
    )
    stop_event = threading.Event()

    def stop(signum: int, _frame) -> None:
        logger.info("Manager stopping on %s", signal.Signals(signum).name)
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    runtime.start()
    try:
        while not stop_event.wait(1):
            pass
    finally:
        runtime.stop()
        lock_file.close()


def _gui_main() -> None:
    if sys.platform == "win32":
        import ctypes

        mutex = ctypes.windll.kernel32.CreateMutexW(None, True, "Global\\ApexQuantel_GUI_v1")
        if ctypes.windll.kernel32.GetLastError() == 183:
            hwnd = ctypes.windll.user32.FindWindowW(None, "Apex Quantel")
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 9)
                ctypes.windll.user32.SetForegroundWindow(hwnd)
            ctypes.windll.kernel32.CloseHandle(mutex)
            return
        _gui_main._mutex = mutex  # type: ignore[attr-defined]

    from src.gui.app import ApexTraderGUI, resolve_config_path

    ApexTraderGUI(config_path=resolve_config_path(sys.argv)).mainloop()


def _acquire_lock(path):
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise SystemExit(f"Runtime already active for {path}: {exc}") from exc
    return handle


def main() -> None:
    worker_id = _get_worker_id()
    if worker_id:
        _worker_main(worker_id)
    elif "--manager" in sys.argv:
        _manager_main()
    else:
        _gui_main()


if __name__ == "__main__":
    main()
