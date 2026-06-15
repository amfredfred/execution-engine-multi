"""
manager/operations.py — Serialised, idempotent agent operations.

All mutating actions (start/stop/provision/remove) flow through here
so concurrent GUI clicks cannot produce double-starts.
"""

from __future__ import annotations

import logging
import secrets
import time
import threading
from collections.abc import Callable
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from manager.app.models import AgentStatus, OperationRecord
from src.runtime.contracts import EngineCommandType

if TYPE_CHECKING:
    from manager.app.event_hub import EngineEventHub
    from manager.app.desired_state import DesiredStateSupervisor
    from manager.app.process_supervisor import ProcessSupervisor
    from manager.app.provisioning import AgentProvisioner
    from manager.app.registry import AgentRegistry

logger = logging.getLogger(__name__)
_MAX_PENDING_OPERATIONS = 100


class OperationRunner:
    def __init__(
        self,
        registry: "AgentRegistry",
        supervisor: "ProcessSupervisor",
        desired: "DesiredStateSupervisor",
        provisioner: "AgentProvisioner",
        event_hub: "EngineEventHub",
        on_agent_changed: "Callable[[str], None] | None" = None,
    ) -> None:
        self._registry    = registry
        self._supervisor  = supervisor
        self._desired     = desired
        self._provisioner = provisioner
        self._event_hub   = event_hub
        self._on_changed  = on_agent_changed or (lambda _: None)
        self._agent_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._executor    = ThreadPoolExecutor(max_workers=4, thread_name_prefix="op")
        self._pending_slots = threading.BoundedSemaphore(_MAX_PENDING_OPERATIONS)
        self._registry.recover_interrupted_operations()

    def submit(
        self,
        op_type: str,
        agent_id: str,
        payload: dict | None = None,
        op_id: str | None = None,
    ) -> str:
        op_id = op_id or secrets.token_hex(8)
        payload = payload or {}

        # Idempotency: if this op_id already completed, return it
        existing = self._registry.get_operation(op_id)
        if existing and existing.status == "completed":
            return op_id

        op = OperationRecord(
            op_id=op_id,
            agent_id=agent_id,
            op_type=op_type,
            status="pending",
            created_at=int(time.time() * 1000),
        )
        self._registry.upsert_operation(op)
        if not self._pending_slots.acquire(blocking=False):
            raise RuntimeError("Operation queue is full")
        try:
            self._executor.submit(self._run_op, op, payload)
        except Exception:
            self._pending_slots.release()
            raise
        return op_id

    def _run_op(self, op: OperationRecord, payload: dict) -> None:
        try:
            lock = self._agent_locks[op.agent_id]
            with lock:
                self._registry.upsert_operation(
                    OperationRecord(**{**op.__dict__, "status": "running"})
                )
                error: str | None = None
                try:
                    self._execute(op.op_type, op.agent_id, payload)
                except Exception as exc:
                    error = str(exc)
                    logger.error("Operation %s/%s failed: %s", op.op_type, op.agent_id, exc)

                final_status = "failed" if error else "completed"
                self._registry.upsert_operation(OperationRecord(
                    op_id=op.op_id,
                    agent_id=op.agent_id,
                    op_type=op.op_type,
                    status=final_status,
                    created_at=op.created_at,
                    completed_at=int(time.time() * 1000),
                    error=error,
                ))
        finally:
            self._pending_slots.release()

    def _execute(self, op_type: str, agent_id: str, payload: dict) -> None:
        if op_type == "start":
            self._registry.set_desired_status(agent_id, "running")
            # DesiredStateSupervisor will spawn on its next tick,
            # but we can trigger immediately for responsiveness
            self._supervisor.spawn(agent_id)

        elif op_type == "stop":
            self._registry.set_desired_status(agent_id, "stopped")
            command_id = self._event_hub.submit_command(
                agent_id,
                EngineCommandType.STOP,
                {},
            )
            self._supervisor.stop_with_escalation(
                agent_id,
                graceful_requested=bool(command_id),
            )

        elif op_type == "force_stop":
            self._registry.set_desired_status(agent_id, "stopped")
            self._supervisor.terminate(agent_id, force=True)

        elif op_type == "remove":
            self._registry.set_desired_status(agent_id, "stopped")
            self._supervisor.stop_with_escalation(
                agent_id,
                graceful_requested=bool(self._event_hub.submit_command(
                    agent_id, EngineCommandType.STOP, {}
                )),
            )
            if self._supervisor.is_alive(agent_id):
                raise RuntimeError(f"Cannot deprovision live worker {agent_id}")
            self._event_hub.forget_engine(agent_id)
            self._provisioner.deprovision(agent_id)
            self._on_changed(agent_id)

        elif op_type == "provision":
            reg = self._provisioner.provision(**payload)
            self._on_changed(reg.agent_id)

        elif op_type == "reset_crash_loop":
            self._desired.reset_crash_loop(agent_id)

        else:
            raise ValueError(f"Unknown op_type: {op_type!r}")
