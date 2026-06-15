"""Boot-time reconciliation for manager and isolated worker state."""

from __future__ import annotations

import logging
import os

from manager.app.models import AgentStatus
from manager.app.process_supervisor import ProcessSupervisor
from manager.app.registry import AgentRegistry

logger = logging.getLogger(__name__)


class RestartReconciler:
    def __init__(
        self,
        registry: AgentRegistry,
        supervisor: ProcessSupervisor,
    ) -> None:
        self._registry = registry
        self._supervisor = supervisor

    def run(self) -> None:
        agents = self._registry.list_agents()
        stale_count = 0
        survivor_count = 0

        for reg in agents:
            if reg.status not in (
                AgentStatus.STARTING,
                AgentStatus.RUNNING,
                AgentStatus.STOPPING,
            ):
                continue

            if reg.pid is None:
                self._registry.set_agent_status(
                    reg.agent_id, AgentStatus.STOPPED, pid=None
                )
                stale_count += 1
                continue

            if _pid_is_alive(reg.pid) and self._supervisor.adopt(reg.agent_id, reg.pid):
                logger.info(
                    "Reconciler: preserving agent %s (pid=%d) for adoption",
                    reg.agent_id,
                    reg.pid,
                )
                self._registry.set_agent_status(
                    reg.agent_id, AgentStatus.STARTING, pid=reg.pid
                )
                survivor_count += 1
                continue

            self._registry.set_agent_status(
                reg.agent_id, AgentStatus.STOPPED, pid=None
            )
            stale_count += 1

        for lease in self._registry.list_terminal_leases():
            owner = self._registry.get_agent(lease.agent_id)
            identity = (
                self._supervisor.process_identity(owner.agent_id, lease.pid)
                if owner and lease.pid is not None
                else False
            )
            if lease.pid is not None and identity is False:
                logger.info(
                    "Reconciler: releasing stale terminal lease %s "
                    "(pid=%d was dead)",
                    lease.terminal_path,
                    lease.pid,
                )
                self._registry.release_terminal_lease(lease.terminal_path)

        logger.info(
            "Reconciliation complete: %d stale engine(s) reset, "
            "%d worker(s) awaiting adoption",
            stale_count,
            survivor_count,
        )


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
