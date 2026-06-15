"""Manager-owned IPC hub for all isolated execution workers."""

from __future__ import annotations

import json
import logging
import socket
import socketserver
import threading
import time
from typing import Callable

from manager.app.models import AgentSnapshot, AgentStatus
from manager.app.registry import AgentRegistry
from src.runtime.contracts import (
    EngineCommand,
    EngineCommandType,
    EngineEvent,
    EngineEventType,
)

logger = logging.getLogger(__name__)


class _ThreadingServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class EngineEventHub:
    def __init__(self, registry: AgentRegistry, token: str, port: int = 8871) -> None:
        self._registry = registry
        self._token = token
        self._port = port
        self._snapshots: dict[str, AgentSnapshot] = {}
        self._connections: dict[str, tuple[socket.socket, object]] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._server: _ThreadingServer | None = None
        self._thread: threading.Thread | None = None
        self._on_snapshot: Callable[[str, AgentSnapshot], None] = lambda *_: None
        self._on_execution_event: Callable[[str, str, dict], None] = lambda *_: None
        self._on_worker_ready: Callable[[str], None] = lambda *_: None

    def start(self) -> None:
        hub = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                engine_id: str | None = None
                try:
                    hello = EngineEvent.from_wire(json.loads(self.rfile.readline()))
                    if hello.event_type != EngineEventType.WORKER_HELLO:
                        return
                    if hello.payload.get("token") != hub._token:
                        logger.warning("EngineEventHub rejected worker with invalid token")
                        return
                    engine_id = hello.engine_id
                    with hub._lock:
                        hub._connections[engine_id] = (self.request, self.wfile)
                    for raw in self.rfile:
                        hub._handle_event(EngineEvent.from_wire(json.loads(raw)))
                except Exception as exc:
                    logger.debug("Worker IPC connection ended for %s: %s", engine_id, exc)
                finally:
                    if engine_id:
                        with hub._lock:
                            current = hub._connections.get(engine_id)
                            if current and current[0] is self.request:
                                hub._connections.pop(engine_id, None)

        self._server = _ThreadingServer(("127.0.0.1", self._port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="engine-event-hub",
            daemon=True,
        )
        self._thread.start()
        logger.info("EngineEventHub listening on 127.0.0.1:%d", self._port)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def get_snapshot(self, engine_id: str) -> AgentSnapshot | None:
        with self._lock:
            return self._snapshots.get(engine_id)

    def get_all_snapshots(self) -> dict[str, AgentSnapshot]:
        with self._lock:
            return dict(self._snapshots)

    def forget_engine(self, engine_id: str) -> None:
        with self._lock:
            self._snapshots.pop(engine_id, None)
            self._connections.pop(engine_id, None)

    def set_event_callbacks(
        self,
        on_snapshot: Callable[[str, AgentSnapshot], None],
        on_execution_event: Callable[[str, str, dict], None],
    ) -> None:
        self._on_snapshot = on_snapshot
        self._on_execution_event = on_execution_event

    def set_worker_ready_callback(self, callback: Callable[[str], None]) -> None:
        self._on_worker_ready = callback

    def send_command(
        self,
        engine_id: str,
        command_type: EngineCommandType,
        payload: dict | None = None,
    ) -> bool:
        command = EngineCommand(
            engine_id=engine_id,
            command_type=command_type,
            payload=payload or {},
        )
        with self._lock:
            connection = self._connections.get(engine_id)
        if not connection:
            return False
        try:
            _, writer = connection
            with self._write_lock:
                writer.write((json.dumps(command.to_wire(), separators=(",", ":")) + "\n").encode())
                writer.flush()
            return True
        except Exception as exc:
            logger.debug("Command send failed for %s: %s", engine_id, exc)
            return False

    def deliver_signal(self, engine_id: str, signal_value: dict) -> bool:
        return self.send_command(
            engine_id,
            EngineCommandType.SIGNAL_DELIVER,
            {"signal": signal_value},
        )

    def _handle_event(self, event: EngineEvent) -> None:
        engine_id = event.engine_id
        if event.event_type == EngineEventType.WORKER_READY:
            reg = self._registry.get_agent(engine_id)
            if reg and reg.desired_status == "running":
                pid = event.payload.get("pid")
                self._registry.set_agent_status(
                    engine_id,
                    AgentStatus.RUNNING,
                    pid=int(pid) if pid else reg.pid,
                )
                self._registry.reset_crash_count(engine_id)
            self._registry.touch_last_seen(engine_id)
            self._on_worker_ready(engine_id)
        elif event.event_type == EngineEventType.ENGINE_SNAPSHOT:
            snapshot = _parse_snapshot(engine_id, event.payload)
            with self._lock:
                self._snapshots[engine_id] = snapshot
            self._registry.touch_last_seen(engine_id)
            self._on_snapshot(engine_id, snapshot)
        elif event.event_type == EngineEventType.EXECUTION_EVENT:
            event_type = str(event.payload.get("event_type") or "unknown")
            data = event.payload.get("data")
            if not self._registry.worker_event_processed(event.event_id):
                self._on_execution_event(
                    engine_id,
                    event_type,
                    data if isinstance(data, dict) else {},
                )
                self._registry.record_worker_event(event.event_id, engine_id)
            self.send_command(
                engine_id,
                EngineCommandType.EVENT_ACK,
                {"event_id": event.event_id},
            )


def _parse_snapshot(engine_id: str, payload: dict) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=engine_id,
        status=AgentStatus(payload.get("status", "STOPPED")),
        mt5_connected=bool(payload.get("mt5_connected")),
        mt5_login=payload.get("mt5_login"),
        mt5_server=payload.get("mt5_server"),
        balance=payload.get("balance"),
        equity=payload.get("equity"),
        open_trades=int(payload.get("open_trades", 0)),
        gateway_connected=bool(payload.get("manager_connected")),
        uptime_sec=int(payload.get("uptime_sec", 0)),
        observed_at=int(payload.get("observed_at", time.time() * 1000)),
        telemetry=payload.get("telemetry") if isinstance(payload.get("telemetry"), dict) else {},
    )
