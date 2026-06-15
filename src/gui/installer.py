"""Manager task installation and lifecycle controls."""

from __future__ import annotations

import base64
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional, Tuple

Result = Tuple[bool, str]


class InstallerService:
    def __init__(self) -> None:
        self.on_result: Optional[Callable[[bool, str], None]] = None

    def install_manager_async(self) -> None:
        threading.Thread(
            target=lambda: self._manager_action("install", "AQ Manager installed and started."),
            daemon=True,
        ).start()

    def reinstall_manager_async(self) -> None:
        threading.Thread(
            target=lambda: self._manager_action("update", "AQ Manager updated."),
            daemon=True,
        ).start()

    def uninstall_manager_async(self) -> None:
        threading.Thread(
            target=lambda: self._manager_action("uninstall", "AQ Manager uninstalled."),
            daemon=True,
        ).start()

    def start_manager_task(self) -> Result:
        return self._schtasks("/Run", "Manager started.")

    def stop_manager_task(self) -> Result:
        return self._schtasks("/End", "Manager stopped.")

    @staticmethod
    def find_manager_script() -> Optional[Path]:
        exe_dir = Path(sys.executable).parent
        for depth in range(7):
            candidate = exe_dir
            for _ in range(depth):
                candidate = candidate.parent
            script = candidate / "install_manager.ps1"
            if script.exists():
                return script
        script = Path("install_manager.ps1")
        return script if script.exists() else None

    def _manager_action(self, action: str, success_message: str) -> None:
        script = self.find_manager_script()
        if not script:
            self._notify(False, "install_manager.ps1 was not found.")
            return
        command = (
            f'powershell -NoProfile -ExecutionPolicy Bypass -File "{script}" '
            f"-Action {action}"
        )
        encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
        try:
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Start-Process powershell -Verb RunAs -Wait "
                    f"-ArgumentList '-NoProfile -EncodedCommand {encoded}'",
                ],
                capture_output=True,
                text=True,
                timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self._notify(True, success_message)
        except Exception as exc:
            self._notify(False, str(exc))

    @staticmethod
    def _schtasks(action: str, success_message: str) -> Result:
        try:
            result = subprocess.run(
                ["schtasks", action, "/TN", r"\Apex Quantel\AQ Manager"],
                capture_output=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            detail = result.stderr.decode(errors="replace").strip()
            return result.returncode == 0, success_message if result.returncode == 0 else detail
        except Exception as exc:
            return False, str(exc)

    def _notify(self, ok: bool, message: str) -> None:
        if self.on_result:
            self.on_result(ok, message)
