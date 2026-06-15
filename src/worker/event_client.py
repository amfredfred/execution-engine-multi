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
from collections.abc import Callable

from src.domain.signal_interface import InboundSignal
from src.runtime.contracts import (
    EngineCommand,
    EngineCommandType,
    EngineEvent,
    EngineEventType,
    MAX_WIRE_BYTES,
    validate_command_payload,
    validate_envelope_timestamp,
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
        config_revision: int = 1,
        on_stop_requested: Callable[[], None] | None = None,
    ) -> None:
        self.engine_id = engine_id
        self._host = manager_host
        self._port = manager_port
        self._token = token
        self._container = container
        self._account_login = account_login
        self._account_server = account_server
        self._config_revision = config_revision
        self._on_stop_requested = on_stop_requested
        self._outbox_path = os.path.join(storage_path, "worker-events.db")
        self._started_at = time.time()
        self._stop_event = threading.Event()
        self._connected = threading.Event()
        self._send_lock = threading.Lock()
        self._sequence = 0
        self._socket: socket.socket | None = None
        self._writer = None
        self._thread: threading.Thread | None = None
        self._ready_emitted = False
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
        if self._connected.is_set():
            try:
                self._emit(EngineEventType.WORKER_STOPPED, {"pid": os.getpid()})
            except Exception:
                logger.exception("Failed to emit WORKER_STOPPED")
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
        self._ready_emitted = False
        self._emit(EngineEventType.WORKER_HELLO, {"token": self._token, "pid": os.getpid()})
        self._replay_outbox()
        snapshot_thread = threading.Thread(
            target=self._snapshot_loop,
            name=f"worker-snapshot-{self.engine_id}",
            daemon=True,
        )
        snapshot_thread.start()
        while line := reader.readline(MAX_WIRE_BYTES + 1):
            if self._stop_event.is_set():
                break
            try:
                if len(line.encode("utf-8")) > MAX_WIRE_BYTES:
                    raise ValueError(f"Manager command exceeds {MAX_WIRE_BYTES} bytes")
                if not line.endswith("\n"):
                    raise ValueError("Manager command must end with a newline")
                self._handle_command(EngineCommand.from_wire(json.loads(line)))
            except Exception as exc:
                logger.warning("Rejected manager command: %s", exc)

    def _snapshot_loop(self) -> None:
        while not self._stop_event.wait(_SNAPSHOT_INTERVAL) and self._connected.is_set():
            try:
                if self._container.runtime_ready.is_set() and not self._ready_emitted:
                    self._emit(EngineEventType.WORKER_READY, {"pid": os.getpid()})
                    self._ready_emitted = True
                self._replay_outbox()
                self._emit(EngineEventType.ENGINE_SNAPSHOT, self._build_snapshot())
            except Exception as exc:
                logger.debug("Snapshot emit failed: %s", exc)
                break

    def _build_snapshot(self) -> dict:
        c = self._container
        try:
            telemetry = c.signal_consumer.build_metrics_snapshot()
        except Exception:
            logger.exception("Failed to build canonical execution snapshot")
            telemetry = {}
        try:
            acct = c.mt5_positions.get_account_info()
            balance: float | None = acct.balance
            equity: float | None = acct.equity
            mt5_ok = True
        except Exception:
            balance = None
            equity = None
            mt5_ok = c.mt5_client.is_connected()

        open_trades = c.position_store.get_open_trades()

        status = (
            "RUNNING"
            if c.runtime_ready.is_set()
            else ("DEGRADED" if c.runtime_error else "STARTING")
        )
        return {
            "status": status,
            "mt5_connected": mt5_ok,
            "mt5_login": self._account_login,
            "mt5_server": self._account_server,
            "balance": balance,
            "equity": equity,
            "open_trades": len(open_trades),
            "manager_connected": self.is_connected(),
            "uptime_sec": int(time.time() - self._started_at),
            "observed_at": int(time.time() * 1000),
            "telemetry": telemetry,
        }

    def _handle_command(self, command: EngineCommand) -> None:
        if command.engine_id != self.engine_id:
            return
        try:
            validate_envelope_timestamp(command.issued_at)
            if command.config_revision != self._config_revision:
                raise ValueError(
                    "Command config revision "
                    f"{command.config_revision} does not match worker revision "
                    f"{self._config_revision}"
                )
            validate_command_payload(command)
            if command.command_type == EngineCommandType.SIGNAL_DELIVER:
                signal_value = command.payload.get("signal", command.payload)
                inbound = InboundSignal.from_dict(signal_value)
                if self._accept_signal(inbound.id):
                    self._container.event_bus.emit("signal.triggered", inbound)
            elif command.command_type == EngineCommandType.CLOSE_TRADE:
                self._container.position_manager.close_trade(
                    str(command.payload["trade_id"])
                )
            elif command.command_type == EngineCommandType.PAUSE:
                self._container.signal_queue.pause()
            elif command.command_type == EngineCommandType.RESUME:
                self._container.signal_queue.resume()
            elif command.command_type == EngineCommandType.EMERGENCY_STOP:
                self._container.signal_queue.pause()
                self._container.position_manager.emergency_close_all()
            elif command.command_type == EngineCommandType.STOP:
                if self._on_stop_requested:
                    self._on_stop_requested()
                else:
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
            config_revision=self._config_revision,
        )
        if event_type == EngineEventType.EXECUTION_EVENT:
            self._persist_event(event)
        self._send_event(event)

    def _send_event(self, event: EngineEvent) -> None:
        with self._send_lock:
            if not self._writer:
                return
            wire = json.dumps(event.to_wire(), separators=(",", ":")) + "\n"
            if len(wire.encode("utf-8")) > MAX_WIRE_BYTES:
                raise ValueError(f"Worker event exceeds {MAX_WIRE_BYTES} bytes")
            self._writer.write(wire)
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
            conn.execute(
                """CREATE TABLE IF NOT EXISTS accepted_signals (
                   signal_id TEXT PRIMARY KEY,
                   accepted_at INTEGER NOT NULL
                )"""
            )
            conn.execute(
                "DELETE FROM accepted_signals WHERE accepted_at < ?",
                (int(time.time() * 1000) - 7 * 24 * 60 * 60 * 1000,),
            )
            conn.execute(
                "DELETE FROM event_outbox WHERE created_at < ?",
                (int(time.time() * 1000) - 7 * 24 * 60 * 60 * 1000,),
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

    def _accept_signal(self, signal_id: str) -> bool:
        with sqlite3.connect(self._outbox_path) as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO accepted_signals VALUES (?,?)",
                (signal_id, int(time.time() * 1000)),
            )
        return cursor.rowcount == 1

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
