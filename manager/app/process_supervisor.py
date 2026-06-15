"""
manager/process_supervisor.py — Spawn and kill agent subprocesses.

Does NOT make lifecycle decisions — that is DesiredStateSupervisor's job.
This module only executes spawn/terminate instructions.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from manager.app.models import AgentStatus
from manager.app.registry import AgentRegistry
from manager.app.secrets import ManagerSecretStore

logger = logging.getLogger(__name__)


class ProcessSupervisor:
    def __init__(
        self,
        registry: AgentRegistry,
        secrets: ManagerSecretStore,
        src_root: str,
        ipc_port: int = 8871,
        on_agent_stopped: Callable[[str], None] | None = None,
    ) -> None:
        self._registry  = registry
        self._secrets   = secrets
        self._src_root  = src_root
        self._ipc_port  = ipc_port
        self._on_stopped = on_agent_stopped or (lambda _: None)
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    def spawn(self, agent_id: str) -> None:
        reg = self._registry.get_agent(agent_id)
        if not reg:
            raise ValueError(f"Agent {agent_id} not found in registry")

        with self._lock:
            if agent_id in self._procs and self._procs[agent_id].poll() is None:
                logger.warning("Agent %s is already running (pid=%d)", agent_id, self._procs[agent_id].pid)
                return

        # Build environment: copy current env + inject secrets
        env = os.environ.copy()

        mt5_password = self._secrets.get_secret(agent_id, "mt5_password") or ""
        activation_key = self._secrets.get_activation_key() or ""
        ipc_token = self._secrets.get_ipc_token() or ""

        env["MT5_PASSWORD"]         = mt5_password
        env["APEX_ACTIVATION_KEY"]  = activation_key
        env["ENGINE_IPC_TOKEN"]     = ipc_token
        env["ENGINE_IPC_PORT"]      = str(self._ipc_port)

        # Open log file for subprocess stdout/stderr
        logs_dir = Path(reg.data_dir) / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = logs_dir / "engine.log"
        log_fh   = open(log_path, "a", encoding="utf-8")

        cmd = [sys.executable, "-m", "src", "--agent", agent_id, reg.config_path]

        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW

        proc = subprocess.Popen(
            cmd,
            cwd=self._src_root,
            env=env,
            stdout=log_fh,
            stderr=log_fh,
            creationflags=creationflags,
        )

        with self._lock:
            self._procs[agent_id] = proc

        self._registry.set_agent_status(agent_id, AgentStatus.STARTING, pid=proc.pid)
        self._registry.emit_event("agent.spawned", agent_id, {"pid": proc.pid})
        logger.info("Spawned agent %s (pid=%d)", agent_id, proc.pid)

        # Start watcher thread
        t = threading.Thread(
            target=self._watch_process,
            args=(agent_id, proc, log_fh),
            name=f"watcher-{agent_id}",
            daemon=True,
        )
        t.start()

    def terminate(self, agent_id: str, force: bool = False) -> None:
        with self._lock:
            proc = self._procs.get(agent_id)

        if not proc or proc.poll() is not None:
            reg = self._registry.get_agent(agent_id)
            if reg and reg.pid:
                self._terminate_pid(reg.pid, force)
                self._registry.set_agent_status(agent_id, AgentStatus.STOPPED, pid=None)
                return
            logger.debug("Agent %s is not running; skipping terminate", agent_id)
            self._registry.set_agent_status(agent_id, AgentStatus.STOPPED, pid=None)
            return

        self._registry.set_agent_status(agent_id, AgentStatus.STOPPING)
        logger.info("Terminating agent %s (pid=%d, force=%s)", agent_id, proc.pid, force)

        try:
            if force or sys.platform != "win32":
                proc.kill()
            else:
                proc.terminate()
        except ProcessLookupError:
            pass

    def is_alive(self, agent_id: str) -> bool:
        with self._lock:
            proc = self._procs.get(agent_id)
        if proc is not None and proc.poll() is None:
            return True
        reg = self._registry.get_agent(agent_id)
        return bool(reg and reg.pid and _pid_is_alive(reg.pid))

    def get_pid(self, agent_id: str) -> int | None:
        with self._lock:
            proc = self._procs.get(agent_id)
        return proc.pid if proc and proc.poll() is None else None

    def kill_orphan(self, pid: int) -> None:
        """Kill a process by PID — used by reconciliation on startup."""
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True, timeout=10,
                )
            else:
                os.kill(pid, 9)
            logger.info("Killed orphan process pid=%d", pid)
        except Exception as exc:
            logger.debug("Could not kill orphan pid=%d: %s", pid, exc)

    def _terminate_pid(self, pid: int, force: bool) -> None:
        try:
            if sys.platform == "win32":
                command = ["taskkill", "/PID", str(pid)]
                if force:
                    command.insert(1, "/F")
                subprocess.run(command, capture_output=True, timeout=10)
            else:
                os.kill(pid, 9 if force else 15)
        except Exception as exc:
            logger.debug("Could not terminate adopted worker pid=%d: %s", pid, exc)

    # ── Internal ──────────────────────────────────────────────────────────

    def _watch_process(
        self, agent_id: str, proc: subprocess.Popen, log_fh
    ) -> None:
        """Daemon thread per agent — blocks until subprocess exits."""
        exit_code = proc.wait()
        try:
            log_fh.close()
        except Exception:
            pass

        with self._lock:
            self._procs.pop(agent_id, None)

        current = self._registry.get_agent(agent_id)
        if current and current.status not in (AgentStatus.STOPPING, AgentStatus.STOPPED):
            self._registry.set_agent_status(agent_id, AgentStatus.STOPPED, pid=None)

        self._registry.emit_event(
            "agent.stopped", agent_id, {"exit_code": exit_code}
        )
        logger.info("Agent %s exited (exit_code=%d)", agent_id, exit_code)
        self._on_stopped(agent_id)


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
