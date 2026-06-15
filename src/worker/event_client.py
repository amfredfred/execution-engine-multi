"""Worker-side client for the canonical manager command/event IPC protocol."""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import sqlite3
import threading
import time
from typing import TYPE_CHECKING

from src.domain.signal_interface import InboundSignal
from src.runtime.contracts import (
    EngineCommand,
    EngineCommandType,
    EngineEvent,
    EngineEventType,
)

if TYPE_CHECKING:
    from src.app.container import AppContainer

logger = logging.getLogger(__name__)

_SNAPSHOT_INTERVAL = 2.0
_RECONNECT_DELAYS = [2, 5, 10, 15, 30]


class WorkerEventClient:
    def __init__(
        self,
        engine_id: str,
        manager_host: str,
        manager_port: int,
        token: str,
        container: "AppContainer",
        account_login: int | None,
        account_server: str | None,
        storage_path: str,
    ) -> None:
        self.engine_id = engine_id
        self._host = manager_host
        self._port = manager_port
        self._token = token
        self._container = container
        self._account_login = account_login
        self._account_server = account_server
        self._outbox_path = os.path.join(storage_path, "worker-events.db")
        self._started_at = time.time()
        self._stop_event = threading.Event()
        self._connected = threading.Event()
        self._send_lock = threading.Lock()
        self._sequence = 0
        self._socket: socket.socket | None = None
        self._writer = None
        self._thread: threading.Thread | None = None
        self._init_outbox()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"worker-events-{self.engine_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._close_connection()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def emit_execution_event(self, event_type: str, data: dict) -> None:
        self._emit(
            EngineEventType.EXECUTION_EVENT,
            {"event_type": event_type, "data": data},
        )

    def _run_loop(self) -> None:
        attempt = 0
        while not self._stop_event.is_set():
            try:
                self._connect_and_read()
                attempt = 0
            except Exception as exc:
                logger.debug("Manager IPC disconnected: %s", exc)
            finally:
                self._close_connection()
            if self._stop_event.is_set():
                break
            delay = _RECONNECT_DELAYS[min(attempt, len(_RECONNECT_DELAYS) - 1)]
            attempt += 1
            self._stop_event.wait(delay)

    def _connect_and_read(self) -> None:
        sock = socket.create_connection((self._host, self._port), timeout=10)
        sock.settimeout(None)
        self._socket = sock
        self._writer = sock.makefile("w", encoding="utf-8", newline="\n")
        reader = sock.makefile("r", encoding="utf-8", newline="\n")
        self._connected.set()
        self._emit(EngineEventType.WORKER_HELLO, {"token": self._token, "pid": os.getpid()})
        self._emit(EngineEventType.WORKER_READY, {"pid": os.getpid()})
        self._replay_outbox()
        snapshot_thread = threading.Thread(
            target=self._snapshot_loop,
            name=f"worker-snapshot-{self.engine_id}",
            daemon=True,
        )
        snapshot_thread.start()
        for line in reader:
            if self._stop_event.is_set():
                break
            try:
                self._handle_command(EngineCommand.from_wire(json.loads(line)))
            except Exception as exc:
                logger.warning("Rejected manager command: %s", exc)

    def _snapshot_loop(self) -> None:
        while not self._stop_event.wait(_SNAPSHOT_INTERVAL) and self._connected.is_set():
            try:
                self._emit(EngineEventType.ENGINE_SNAPSHOT, self._build_snapshot())
            except Exception as exc:
                logger.debug("Snapshot emit failed: %s", exc)
                break

    def _build_snapshot(self) -> dict:
        telemetry = (
            self._container.ui_bridge.build_remote_snapshot()
            if self._container.ui_bridge
            else {}
        )
        account = telemetry.get("metrics", {}) if isinstance(telemetry, dict) else {}
        return {
            "status": "RUNNING",
            "mt5_connected": bool(telemetry.get("connected")),
            "mt5_login": self._account_login,
            "mt5_server": self._account_server,
            "balance": account.get("balance"),
            "equity": account.get("equity"),
            "open_trades": len(self._container.position_store.get_open_trades()),
            "manager_connected": self.is_connected(),
            "uptime_sec": int(time.time() - self._started_at),
            "observed_at": int(time.time() * 1000),
            "telemetry": telemetry,
        }

    def _handle_command(self, command: EngineCommand) -> None:
        if command.engine_id != self.engine_id:
            return
        try:
            if command.command_type == EngineCommandType.SIGNAL_DELIVER:
                signal_value = command.payload.get("signal", command.payload)
                self._container.event_bus.emit(
                    "signal.triggered",
                    InboundSignal.from_dict(signal_value),
                )
            elif command.command_type == EngineCommandType.PAUSE:
                self._container.signal_queue.pause()
            elif command.command_type == EngineCommandType.RESUME:
                self._container.signal_queue.resume()
            elif command.command_type == EngineCommandType.EMERGENCY_STOP:
                self._container.signal_queue.pause()
                self._container.position_manager.emergency_close_all()
            elif command.command_type == EngineCommandType.STOP:
                os.kill(os.getpid(), signal.SIGTERM)
            elif command.command_type == EngineCommandType.CONFIG_APPLY:
                raise ValueError("Config revision requires controlled worker restart")
            elif command.command_type == EngineCommandType.EVENT_ACK:
                self._ack_event(str(command.payload["event_id"]))
                return
            self._emit(EngineEventType.COMMAND_ACK, {"command_id": command.command_id})
        except Exception as exc:
            self._emit(
                EngineEventType.COMMAND_REJECTED,
                {"command_id": command.command_id, "error": str(exc)},
            )

    def _emit(self, event_type: EngineEventType, payload: dict) -> None:
        self._sequence += 1
        event = EngineEvent(
            engine_id=self.engine_id,
            sequence=self._sequence,
            event_type=event_type,
            payload=payload,
        )
        if event_type == EngineEventType.EXECUTION_EVENT:
            self._persist_event(event)
        self._send_event(event)

    def _send_event(self, event: EngineEvent) -> None:
        with self._send_lock:
            if not self._writer:
                return
            self._writer.write(json.dumps(event.to_wire(), separators=(",", ":")) + "\n")
            self._writer.flush()

    def _init_outbox(self) -> None:
        os.makedirs(os.path.dirname(self._outbox_path), exist_ok=True)
        with sqlite3.connect(self._outbox_path) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS event_outbox (
                   event_id TEXT PRIMARY KEY,
                   envelope_json TEXT NOT NULL,
                   created_at INTEGER NOT NULL
                )"""
            )

    def _persist_event(self, event: EngineEvent) -> None:
        with sqlite3.connect(self._outbox_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO event_outbox VALUES (?,?,?)",
                (event.event_id, json.dumps(event.to_wire()), event.occurred_at),
            )

    def _replay_outbox(self) -> None:
        with sqlite3.connect(self._outbox_path) as conn:
            rows = conn.execute(
                "SELECT envelope_json FROM event_outbox ORDER BY created_at",
            ).fetchall()
        for (raw,) in rows:
            self._send_event(EngineEvent.from_wire(json.loads(raw)))

    def _ack_event(self, event_id: str) -> None:
        with sqlite3.connect(self._outbox_path) as conn:
            conn.execute("DELETE FROM event_outbox WHERE event_id=?", (event_id,))

    def _close_connection(self) -> None:
        self._connected.clear()
        writer, sock = self._writer, self._socket
        self._writer = None
        self._socket = None
        try:
            if writer:
                writer.close()
        except Exception:
            pass
        try:
            if sock:
                sock.close()
        except Exception:
            pass
