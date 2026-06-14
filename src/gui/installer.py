"""
src/gui/installer.py — High-level service install / uninstall helpers.

Wraps service_controller and install_service.ps1 with user-friendly
methods that return plain-English results.  Never throws; always
returns (ok: bool, message: str).
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)

Result = Tuple[bool, str]   # (success, human-readable message)


class InstallerService:
    """
    Manages Windows service registration for the Apex Quantel engine.

    All blocking operations run in daemon threads.
    on_result(ok, message) is called when the operation completes.
    """

    def __init__(self) -> None:
        self.on_result: Optional[Callable[[bool, str], None]] = None

    # ── Public API ────────────────────────────────────────────────────────────

    def install_async(self, config_path: str) -> None:
        """Install the service in a background thread."""
        threading.Thread(
            target=self._do_install,
            args=(config_path,),
            daemon=True,
        ).start()

    def uninstall_async(self) -> None:
        threading.Thread(target=self._do_uninstall, daemon=True).start()

    def reinstall_async(self, config_path: str) -> None:
        threading.Thread(
            target=self._do_reinstall,
            args=(config_path,),
            daemon=True,
        ).start()

    def is_installed(self) -> bool:
        from src.gui.service_controller import ServiceController, ServiceStatus
        return ServiceController().query() != ServiceStatus.NOT_INSTALLED

    # ── Install script location ───────────────────────────────────────────────

    @staticmethod
    def find_install_script() -> Optional[Path]:
        """
        Locate install_service.ps1 by walking up from the running EXE.
        Returns None if not found.
        """
        exe_dir = Path(sys.executable).parent
        for depth in range(7):
            candidate = exe_dir
            for _ in range(depth):
                candidate = candidate.parent
            ps1 = candidate / "install_service.ps1"
            if ps1.exists():
                return ps1
        # CWD fallback
        cwd_ps1 = Path("install_service.ps1")
        if cwd_ps1.exists():
            return cwd_ps1
        return None

    # ── Internals ─────────────────────────────────────────────────────────────

    def _do_install(self, config_path: str) -> None:
        script = self.find_install_script()
        if script is None:
            self._notify(
                False,
                "The AQ Agent installer script (install_service.ps1) was not found "
                "in this installation. Rebuild the installer package or reinstall "
                "AQ Agent.",
            )
            return

        logger.info("Running install_service.ps1: %s", script)
        ok, msg = self._run_ps1(script, "install")
        if ok:
            self._notify(True, "AQ Agent installed successfully. Click Start to begin.")
        else:
            self._notify(False, f"Installation failed: {msg}")

    def _do_uninstall(self) -> None:
        script = self.find_install_script()
        if script is None:
            # Try sc.exe directly as fallback
            ok, msg = self._sc_uninstall()
            self._notify(ok, msg)
            return
        ok, msg = self._run_ps1(script, "uninstall")
        self._notify(ok, "AQ Agent removed." if ok else f"Uninstall failed: {msg}")

    def _do_reinstall(self, config_path: str) -> None:
        script = self.find_install_script()
        if script is None:
            self._notify(
                False,
                "The engine installer script was not found. Cannot reinstall.",
            )
            return
        ok, msg = self._run_ps1(script, "update")
        if ok:
            self._notify(True, "AQ Agent updated. Restarting…")
        else:
            self._notify(False, f"Reinstall failed: {msg}")

    def _run_ps1(self, script: Path, action: str) -> Result:
        """Run install_service.ps1 elevated, return (ok, message)."""
        # Encode the call so we can run via RunAs elevation
        inner_cmd = (
            f"powershell -NoProfile -ExecutionPolicy Bypass "
            f"-File \"{script}\" -Action {action}"
        )
        import base64
        enc = base64.b64encode(inner_cmd.encode("utf-16-le")).decode("ascii")
        try:
            proc = subprocess.run(
                [
                    "powershell", "-NoProfile",
                    "-Command",
                    f"Start-Process powershell -Verb RunAs -Wait "
                    f"-ArgumentList '-NoProfile -EncodedCommand {enc}'",
                ],
                capture_output=True, text=True, timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            # Check if service now exists
            from src.gui.service_controller import ServiceController, ServiceStatus
            status = ServiceController().query()
            if action == "uninstall":
                ok = status == ServiceStatus.NOT_INSTALLED
            else:
                ok = status != ServiceStatus.NOT_INSTALLED
            detail = (proc.stderr or proc.stdout).strip()
            return ok, detail[-200:] if detail else "Done."
        except subprocess.TimeoutExpired:
            return False, "Installation timed out. Try running install_service.ps1 manually as Administrator."
        except FileNotFoundError:
            return False, "PowerShell was not found. Please install PowerShell."
        except Exception as exc:
            return False, str(exc)[:200]

    def _sc_uninstall(self) -> Result:
        try:
            from src.gui.service_controller import TASK_PATH
            subprocess.run(
                ["schtasks", "/Delete", "/TN", TASK_PATH, "/F"],
                capture_output=True, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return True, "Task removed."
        except Exception as exc:
            return False, str(exc)

    def _notify(self, ok: bool, message: str) -> None:
        if self.on_result:
            try:
                self.on_result(ok, message)
            except Exception:
                pass
