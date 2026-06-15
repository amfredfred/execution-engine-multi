"""
manager/desired_state.py — Reconciliation loop that keeps actual state
matching desired state with exponential backoff and CRASH_LOOP detection.
"""

from __future__ import annotations

import logging
import threading
import time

from src.manager.models import AgentStatus
from src.manager.process_supervisor import ProcessSupervisor
from src.manager.registry import AgentRegistry

logger = logging.getLogger(__name__)

_BACKOFF_DELAYS  = [5, 10, 20, 60, 120]   # seconds; last repeats
_CRASH_THRESHOLD = 5
_CRASH_WINDOW_S  = 300   # 5 minutes


class DesiredStateSupervisor:
    def __init__(
        self,
        registry: AgentRegistry,
        supervisor: ProcessSupervisor,
    ) -> None:
        self._registry   = registry
        self._supervisor = supervisor
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # agent_id → earliest epoch (seconds) at which restart is allowed
        self._restart_at: dict[str, float] = {}

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop,
            name="desired-state",
            daemon=True,
        )
        self._thread.start()
        logger.info("DesiredStateSupervisor started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def notify_crashed(self, agent_id: str) -> None:
        """Called by ProcessSupervisor watcher when an agent exits unexpectedly."""
        reg = self._registry.get_agent(agent_id)
        if not reg:
            return

        # Only treat as crash if desired=running (deliberate stops are not crashes)
        if reg.desired_status != "running":
            return

        crash_count = self._registry.increment_crash_count(agent_id)
        last_crash  = self._registry.get_agent(agent_id).last_crash_at or 0
        window_start = (time.time() - _CRASH_WINDOW_S) * 1000

        if crash_count >= _CRASH_THRESHOLD and last_crash > window_start:
            self._registry.set_agent_status(
                agent_id, AgentStatus.CRASH_LOOP,
                error=f"Crashed {crash_count} times in {_CRASH_WINDOW_S}s"
            )
            logger.error("Agent %s entered CRASH_LOOP after %d crashes", agent_id, crash_count)
            return

        # Schedule restart with backoff
        delay = _BACKOFF_DELAYS[min(crash_count - 1, len(_BACKOFF_DELAYS) - 1)]
        self._restart_at[agent_id] = time.time() + delay
        logger.warning(
            "Agent %s crashed (count=%d); will restart in %ds",
            agent_id, crash_count, delay,
        )

    def reset_crash_loop(self, agent_id: str) -> None:
        self._registry.reset_crash_count(agent_id)
        self._registry.set_agent_status(agent_id, AgentStatus.STOPPED)
        self._registry.set_desired_status(agent_id, "stopped")
        self._restart_at.pop(agent_id, None)
        logger.info("Agent %s CRASH_LOOP reset", agent_id)

    # ── Loop ──────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop_event.wait(1.0):
            try:
                for reg in self._registry.list_agents():
                    self._reconcile(reg)
            except Exception as exc:
                logger.exception("DesiredStateSupervisor loop error: %s", exc)

    def _reconcile(self, reg) -> None:
        status  = reg.status
        desired = reg.desired_status

        if status == AgentStatus.CRASH_LOOP:
            return   # manual reset required

        if desired == "running":
            if status in (AgentStatus.STOPPED, AgentStatus.PROVISIONED):
                if self._should_restart(reg.agent_id):
                    try:
                        self._supervisor.spawn(reg.agent_id)
                    except Exception as exc:
                        logger.error("Failed to spawn %s: %s", reg.agent_id, exc)
                        self._registry.set_agent_status(
                            reg.agent_id, AgentStatus.ERROR, error=str(exc)
                        )

        elif desired == "stopped":
            if status in (AgentStatus.RUNNING, AgentStatus.STARTING):
                try:
                    self._supervisor.terminate(reg.agent_id)
                except Exception as exc:
                    logger.error("Failed to terminate %s: %s", reg.agent_id, exc)

    def _should_restart(self, agent_id: str) -> bool:
        earliest = self._restart_at.get(agent_id)
        if earliest is None:
            return True
        if time.time() >= earliest:
            self._restart_at.pop(agent_id, None)
            return True
        return False
