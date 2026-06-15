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
    MAX_WIRE_BYTES,
    validate_envelope_timestamp,
)

logger = logging.getLogger(__name__)
_WORKER_IDLE_TIMEOUT_SEC = 15.0
_STALE_SNAPSHOT_MS = 10_000


class _ThreadingServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class EngineEventHub:
    def __init__(
        self,
        registry: AgentRegistry,
        token: str,
        port: int = 8871,
        token_resolver: Callable[[str], str | None] | None = None,
    ) -> None:
        self._registry = registry
        self._token = token
        self._token_resolver = token_resolver
        self._port = port
        self._snapshots: dict[str, AgentSnapshot] = {}
        self._connections: dict[
            str,
            tuple[socket.socket, object, threading.Lock, int],
        ] = {}
        self._lock = threading.Lock()
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
                    self.request.settimeout(_WORKER_IDLE_TIMEOUT_SEC)
                    hello = EngineEvent.from_wire(json.loads(_read_wire_line(self.rfile)))
                    validate_envelope_timestamp(hello.occurred_at)
                    if hello.event_type != EngineEventType.WORKER_HELLO:
                        return
                    expected_token = (
                        hub._token_resolver(hello.engine_id)
                        if hub._token_resolver
                        else hub._token
                    )
                    if not expected_token or hello.payload.get("token") != expected_token:
                        logger.warning("EngineEventHub rejected worker with invalid token")
                        return
                    engine_id = hello.engine_id
                    if hub._registry.get_agent(engine_id) is None:
                        logger.warning(
                            "EngineEventHub rejected unregistered worker %s",
                            engine_id,
                        )
                        return
                    expected_revision = int(
                        hub._registry.current_config_revision(engine_id)
                    )
                    if hello.config_revision != expected_revision:
                        logger.warning(
                            "EngineEventHub rejected worker %s with config revision %d; expected %d",
                            engine_id,
                            hello.config_revision,
                            expected_revision,
                        )
                        return
                    with hub._lock:
                        previous = hub._connections.get(engine_id)
                        hub._connections[engine_id] = (
                            self.request,
                            self.wfile,
                            threading.Lock(),
                            hello.config_revision,
                        )
                    if previous and previous[0] is not self.request:
                        try:
                            previous[0].shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        previous[0].close()
                    last_sequence = hello.sequence
                    while raw := _read_wire_line(self.rfile):
                        event = EngineEvent.from_wire(json.loads(raw))
                        last_sequence = _validate_event_sequence(last_sequence, event)
                        hub._handle_authenticated_event(
                            engine_id,
                            self.request,
                            event,
                        )
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

    def health_report(self) -> dict:
        now = int(time.time() * 1000)
        unhealthy: list[dict] = []
        for reg in self._registry.list_agents():
            if reg.desired_status != "running":
                continue
            with self._lock:
                connected = reg.agent_id in self._connections
                snapshot = self._snapshots.get(reg.agent_id)
            reason = None
            if not connected:
                reason = "not_connected"
            elif snapshot is None or now - snapshot.observed_at > _STALE_SNAPSHOT_MS:
                reason = "stale_snapshot"
            elif snapshot.status != AgentStatus.RUNNING:
                reason = snapshot.status.value.lower()
            if reason:
                unhealthy.append({"agent_id": reg.agent_id, "reason": reason})
        return {"ok": not unhealthy, "unhealthy_workers": unhealthy}

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
        return self._send_command(
            EngineCommand(
                engine_id=engine_id,
                command_type=command_type,
                payload=payload or {},
            ),
            track=False,
        )

    def submit_command(
        self,
        engine_id: str,
        command_type: EngineCommandType,
        payload: dict | None = None,
    ) -> str | None:
        command = EngineCommand(
            engine_id=engine_id,
            command_type=command_type,
            payload=payload or {},
        )
        return command.command_id if self._send_command(command, track=True) else None

    def _send_command(self, command: EngineCommand, *, track: bool) -> bool:
        engine_id = command.engine_id
        with self._lock:
            connection = self._connections.get(engine_id)
        if not connection:
            if track:
                self._registry.record_command(
                    command.command_id,
                    engine_id,
                    str(command.command_type),
                    "not_connected",
                )
            return False
        try:
            _, writer, write_lock, config_revision = connection
            command = EngineCommand(
                command_id=command.command_id,
                engine_id=engine_id,
                command_type=command.command_type,
                payload=command.payload,
                config_revision=config_revision,
            )
            wire = (json.dumps(command.to_wire(), separators=(",", ":")) + "\n").encode()
            if len(wire) > MAX_WIRE_BYTES:
                logger.warning(
                    "Command rejected for %s: envelope exceeds %d bytes",
                    engine_id,
                    MAX_WIRE_BYTES,
                )
                return False
            if track:
                self._registry.record_command(
                    command.command_id,
                    engine_id,
                    str(command.command_type),
                    "sent",
                )
            with write_lock:
                writer.write(wire)
                writer.flush()
            return True
        except Exception as exc:
            if track:
                self._registry.complete_command(command.command_id, "send_failed", str(exc))
            logger.debug("Command send failed for %s: %s", engine_id, exc)
            return False

    def deliver_signal(self, engine_id: str, signal_value: dict) -> bool:
        return self.send_command(
            engine_id,
            EngineCommandType.SIGNAL_DELIVER,
            {"signal": signal_value},
        )

    def _handle_authenticated_event(
        self,
        authenticated_engine_id: str,
        connection: socket.socket,
        event: EngineEvent,
    ) -> None:
        if event.engine_id != authenticated_engine_id:
            raise ValueError(
                f"Worker {authenticated_engine_id} attempted to publish as {event.engine_id}"
            )
        with self._lock:
            current = self._connections.get(authenticated_engine_id)
        if not current or current[0] is not connection:
            raise ValueError(
                f"Rejected event from superseded worker connection {authenticated_engine_id}"
            )
        if event.config_revision != current[3]:
            raise ValueError(
                f"Worker event config revision {event.config_revision} does not match "
                f"authenticated revision {current[3]}"
            )
        self._handle_event(event)

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
        elif event.event_type == EngineEventType.WORKER_STOPPED:
            self._registry.set_agent_status(engine_id, AgentStatus.STOPPED, pid=None)
            self._registry.touch_last_seen(engine_id)
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
        elif event.event_type in {
            EngineEventType.COMMAND_ACK,
            EngineEventType.COMMAND_REJECTED,
        }:
            command_id = str(event.payload.get("command_id") or "")
            if not command_id:
                raise ValueError(f"{event.event_type} requires command_id")
            rejected = event.event_type == EngineEventType.COMMAND_REJECTED
            self._registry.complete_command(
                command_id,
                "rejected" if rejected else "completed",
                str(event.payload.get("error") or "") if rejected else None,
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


def _read_wire_line(reader) -> bytes:
    raw = reader.readline(MAX_WIRE_BYTES + 1)
    if not raw:
        return b""
    if len(raw) > MAX_WIRE_BYTES:
        raise ValueError(f"IPC envelope exceeds {MAX_WIRE_BYTES} bytes")
    if not raw.endswith(b"\n"):
        raise ValueError("IPC envelope must end with a newline")
    return raw


def _validate_event_sequence(last_sequence: int, event: EngineEvent) -> int:
    if event.event_type == EngineEventType.EXECUTION_EVENT:
        return max(last_sequence, event.sequence)
    validate_envelope_timestamp(event.occurred_at)
    if event.sequence <= last_sequence:
        raise ValueError(
            f"Worker event sequence must increase: {event.sequence} <= {last_sequence}"
        )
    return event.sequence
