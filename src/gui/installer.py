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

    def install_manager_async(self) -> None:
        """Register and start the AQ Manager scheduled task in a background thread."""
        threading.Thread(target=self._do_install_manager, daemon=True).start()

    def reinstall_manager_async(self) -> None:
        """Re-register the AQ Manager task (update action) in a background thread."""
        threading.Thread(target=lambda: self._do_manager_action("update", "AQ Manager reinstalled."), daemon=True).start()

    def uninstall_manager_async(self) -> None:
        """Remove the AQ Manager scheduled task in a background thread."""
        threading.Thread(target=lambda: self._do_manager_action("uninstall", "AQ Manager uninstalled."), daemon=True).start()

    def start_manager_task(self) -> Result:
        """Start the registered AQ Manager task (non-elevated)."""
        try:
            r = subprocess.run(
                ["schtasks", "/Run", "/TN", r"\Apex Quantel\AQ Manager"],
                capture_output=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return r.returncode == 0, "Manager started." if r.returncode == 0 else r.stderr.decode(errors="replace").strip()
        except Exception as exc:
            return False, str(exc)

    def stop_manager_task(self) -> Result:
        """Stop the running AQ Manager task (non-elevated)."""
        try:
            r = subprocess.run(
                ["schtasks", "/End", "/TN", r"\Apex Quantel\AQ Manager"],
                capture_output=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return r.returncode == 0, "Manager stopped." if r.returncode == 0 else r.stderr.decode(errors="replace").strip()
        except Exception as exc:
            return False, str(exc)

    # ── Install script location ───────────────────────────────────────────────

    @staticmethod
    def find_manager_script() -> Optional[Path]:
        """Locate install_manager.ps1 by walking up from the running EXE."""
        exe_dir = Path(sys.executable).parent
        for depth in range(7):
            candidate = exe_dir
            for _ in range(depth):
                candidate = candidate.parent
            ps1 = candidate / "install_manager.ps1"
            if ps1.exists():
                return ps1
        cwd_ps1 = Path("install_manager.ps1")
        if cwd_ps1.exists():
            return cwd_ps1
        return None

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

    def _do_install_manager(self) -> None:
        self._do_manager_action("install", "AQ Manager installed and started.")

    def _do_manager_action(self, action: str, success_msg: str) -> None:
        script = self.find_manager_script()
        if script is None:
            self._notify(
                False,
                "The AQ Manager installer script (install_manager.ps1) was not found. "
                "Rebuild the installer package or reinstall AQ Agent.",
            )
            return
        logger.info("Running install_manager.ps1 -Action %s: %s", action, script)
        ok, msg = self._run_manager_ps1(script, action)
        if ok:
            self._notify(True, success_msg)
        else:
            self._notify(False, f"Manager operation failed: {msg}")

    def _run_manager_ps1(self, script: Path, action: str = "install") -> Result:
        """Run install_manager.ps1 elevated via RunAs, return (ok, detail)."""
        inner_cmd = (
            f"powershell -NoProfile -ExecutionPolicy Bypass "
            f"-File \"{script}\" -Action {action}"
        )
        import base64
        enc = base64.b64encode(inner_cmd.encode("utf-16-le")).decode("ascii")
        try:
            subprocess.run(
                [
                    "powershell", "-NoProfile",
                    "-Command",
                    f"Start-Process powershell -Verb RunAs -Wait "
                    f"-ArgumentList '-NoProfile -EncodedCommand {enc}'",
                ],
                capture_output=True, text=True, timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            # Script ran — let the poll loop decide if the Manager came up.
            return True, "Manager task registered. Waiting for Manager to start…"
        except subprocess.TimeoutExpired:
            return False, "Installation timed out. Try running install_manager.ps1 manually as Administrator."
        except FileNotFoundError:
            return False, "PowerShell was not found. Please install PowerShell."
        except Exception as exc:
            return False, str(exc)[:200]

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
