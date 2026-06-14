"""
manager/reconciliation.py — Boot-time cleanup.

Runs once before DesiredStateSupervisor starts to clear stale state
left from a previous manager crash or unclean shutdown.
"""

from __future__ import annotations

import logging
import os

from src.manager.models import AgentStatus
from src.manager.process_supervisor import ProcessSupervisor
from src.manager.registry import AgentRegistry

logger = logging.getLogger(__name__)


class RestartReconciler:
    def __init__(
        self,
        registry: AgentRegistry,
        supervisor: ProcessSupervisor,
    ) -> None:
        self._registry   = registry
        self._supervisor = supervisor

    def run(self) -> None:
        agents = self._registry.list_agents()
        stale_count = 0
        orphan_count = 0

        for reg in agents:
            if reg.status not in (AgentStatus.STARTING, AgentStatus.RUNNING, AgentStatus.STOPPING):
                continue

            if reg.pid is None:
                # Status is active but no pid recorded — just reset
                self._registry.set_agent_status(reg.agent_id, AgentStatus.STOPPED, pid=None)
                stale_count += 1
                continue

            if _pid_is_alive(reg.pid):
                # Process is alive but manager just restarted — kill it (orphan)
                logger.info(
                    "Reconciler: killing orphan agent %s (pid=%d)", reg.agent_id, reg.pid
                )
                self._supervisor.kill_orphan(reg.pid)
                orphan_count += 1

            self._registry.set_agent_status(reg.agent_id, AgentStatus.STOPPED, pid=None)
            stale_count += 1

        # Release stale terminal leases whose pids are dead
        for lease in self._registry.list_terminal_leases():
            if lease.pid is not None and not _pid_is_alive(lease.pid):
                logger.info(
                    "Reconciler: releasing stale terminal lease %s (pid=%d was dead)",
                    lease.terminal_path, lease.pid,
                )
                self._registry.release_terminal_lease(lease.terminal_path)

        logger.info(
            "Reconciliation complete: %d stale agent(s) reset, %d orphan(s) killed",
            stale_count, orphan_count,
        )


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
