"""
src/gui/service_controller.py — Task Scheduler control for the AQ Agent.

Uses schtasks.exe (built-in on all Windows versions) to query, start,
stop, and restart the scheduled task registered by install_service.ps1.
"""
from __future__ import annotations

import logging
import subprocess
import threading
from typing import Callable

logger = logging.getLogger(__name__)

TASK_NAME = "AQ Agent"
TASK_PATH = r"\Apex Quantel\AQ Agent"   # full path used by schtasks


# ── Status ────────────────────────────────────────────────────────────────────

class ServiceStatus:
    NOT_INSTALLED = "not_installed"
    STOPPED       = "stopped"
    STARTING      = "starting"
    RUNNING       = "running"
    STOPPING      = "stopping"
    UNKNOWN       = "unknown"


# ── Controller ────────────────────────────────────────────────────────────────

class ServiceController:
    """
    Thin wrapper around schtasks.exe for start / stop / status.

    Replaces the old sc.exe / NSSM service approach. The engine now runs as
    a Task Scheduler task (registered by install_service.ps1) so it executes
    in the user's interactive session and can attach to MT5.

    All blocking calls run in daemon threads so the GUI stays responsive.
    on_status_change(status, detail) is called from those threads — use
    app.after() in the callback to push updates back to the GUI thread.
    """

    def __init__(self) -> None:
        self.on_status_change: Callable[[str, str | None], None] | None = None

    # ── Public API ────────────────────────────────────────────────────────────

    def query(self) -> str:
        """Return current ServiceStatus.* value (synchronous, fast)."""
        try:
            result = subprocess.run(
                ["schtasks", "/Query", "/TN", TASK_PATH, "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=8,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode != 0:
                return ServiceStatus.NOT_INSTALLED

            # CSV row: "TaskName","Next Run Time","Status"
            line = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
            parts = [p.strip('"') for p in line.split('","')]
            status_str = parts[2].upper() if len(parts) >= 3 else ""

            if "RUNNING"  in status_str: return ServiceStatus.RUNNING
            if "READY"    in status_str: return ServiceStatus.STOPPED
            if "DISABLED" in status_str: return ServiceStatus.STOPPED
            if "QUEUED"   in status_str: return ServiceStatus.STARTING
            return ServiceStatus.UNKNOWN

        except Exception as exc:
            logger.debug("schtasks query error: %s", exc)
            return ServiceStatus.UNKNOWN

    def is_installed(self) -> bool:
        return self.query() != ServiceStatus.NOT_INSTALLED

    def start(self) -> None:
        threading.Thread(target=self._do_start, daemon=True).start()

    def stop(self) -> None:
        threading.Thread(target=self._do_stop, daemon=True).start()

    def restart(self) -> None:
        threading.Thread(target=self._do_restart, daemon=True).start()

    def install(self, config_path: str) -> None:
        threading.Thread(
            target=self._do_install, args=(config_path,), daemon=True
        ).start()

    # ── Internals ─────────────────────────────────────────────────────────────

    def _notify(self, status: str, detail: str | None = None) -> None:
        if self.on_status_change:
            try:
                self.on_status_change(status, detail)
            except Exception:
                pass

    def _schtasks(self, *args: str, timeout: int = 30) -> tuple[int, str]:
        try:
            r = subprocess.run(
                ["schtasks", *args],
                capture_output=True, text=True, timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return r.returncode, r.stdout + r.stderr
        except subprocess.TimeoutExpired:
            return -1, "timeout"
        except Exception as exc:
            return -1, str(exc)

    def _do_start(self) -> None:
        self._notify(ServiceStatus.STARTING)
        # Re-enable the task in case it was disabled by a previous stop
        self._schtasks("/Change", "/TN", TASK_PATH, "/ENABLE", timeout=10)
        code, out = self._schtasks("/Run", "/TN", TASK_PATH, timeout=15)
        if code == 0:
            self._notify(ServiceStatus.RUNNING)
        else:
            detail = out.strip().splitlines()[-1][:120] if out.strip() else "unknown error"
            self._notify(ServiceStatus.STOPPED, detail)
            logger.warning("Task start failed: %s", out)

    def _do_stop(self) -> None:
        self._notify(ServiceStatus.STOPPING)
        # Disable the task so it does not auto-restart after being ended
        self._schtasks("/Change", "/TN", TASK_PATH, "/DISABLE", timeout=10)
        code, out = self._schtasks("/End", "/TN", TASK_PATH, timeout=15)
        if code == 0:
            self._notify(ServiceStatus.STOPPED)
        else:
            detail = out.strip().splitlines()[-1][:120] if out.strip() else "unknown error"
            self._notify(ServiceStatus.UNKNOWN, detail)
            logger.warning("Task stop failed: %s", out)

    def _do_restart(self) -> None:
        self._do_stop()
        import time; time.sleep(1)
        self._do_start()

    def _do_install(self, config_path: str) -> None:
        import sys
        from pathlib import Path
        self._notify(ServiceStatus.UNKNOWN, "Installing…")

        script: Path | None = None
        exe_dir = Path(sys.executable).resolve().parent
        for depth in range(6):
            candidate = exe_dir
            for _ in range(depth):
                candidate = candidate.parent
            ps1 = candidate / "install_service.ps1"
            if ps1.exists():
                script = ps1
                break
        if script is None:
            cwd_ps1 = Path("install_service.ps1")
            if cwd_ps1.exists():
                script = cwd_ps1

        if script is None:
            self._notify(
                ServiceStatus.NOT_INSTALLED,
                "install_service.ps1 not found — run it manually.",
            )
            return

        logger.info("Running install_service.ps1: %s", script)
        try:
            r = subprocess.run(
                [
                    "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(script), "-Action", "install",
                ],
                capture_output=True, text=True, timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if r.returncode == 0:
                self._notify(ServiceStatus.STOPPED, "Installed — click Start")
            else:
                detail = (r.stderr or r.stdout).strip().splitlines()[-1][:120]
                self._notify(ServiceStatus.NOT_INSTALLED, detail)
        except Exception as exc:
            self._notify(ServiceStatus.NOT_INSTALLED, str(exc)[:120])
