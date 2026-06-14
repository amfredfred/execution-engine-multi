"""
managed/client.py — ManagedAgentClient

WebSocket client that connects from an agent subprocess to the manager's
AgentChannel on port 8871.

Responsibilities:
  • Send agent.hello on connect (token auth)
  • Send agent.status.snapshot every 2 s
  • Receive signal.forward → emit on local EventBus
  • Receive cmd.pause/resume/stop/emergency_stop → execute
  • Receive license.updated → handle (same as SignalConsumer)
  • Non-fatal: agent keeps running if manager WS is down
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from src.app.container import AppContainer

logger = logging.getLogger(__name__)

_SNAPSHOT_INTERVAL = 2.0   # seconds
_RECONNECT_DELAYS  = [2, 5, 10, 15, 30]   # seconds, last repeats


class ManagedAgentClient:
    def __init__(
        self,
        agent_id: str,
        channel_url: str,     # ws://localhost:8871
        token: str,
        container: "AppContainer",
    ) -> None:
        self._agent_id    = agent_id
        self._channel_url = channel_url
        self._token       = token
        self._container   = container
        self._started_at  = time.time()
        self._stop_event  = threading.Event()
        self._connected   = False
        self._thread: threading.Thread | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"managed-client-{self._agent_id}",
            daemon=True,
        )
        self._thread.start()
        logger.info("ManagedAgentClient started → %s", self._channel_url)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def is_channel_connected(self) -> bool:
        return self._connected

    # ── Connection loop ───────────────────────────────────────────────────

    def _run_loop(self) -> None:
        attempt = 0
        while not self._stop_event.is_set():
            try:
                self._connect_and_run()
            except Exception as exc:
                logger.debug("ManagedAgentClient disconnected: %s", exc)
            finally:
                self._connected = False

            if self._stop_event.is_set():
                break

            delay = _RECONNECT_DELAYS[min(attempt, len(_RECONNECT_DELAYS) - 1)]
            attempt += 1
            logger.debug("ManagedAgentClient: reconnecting in %ds", delay)
            self._stop_event.wait(delay)

    def _connect_and_run(self) -> None:
        import websocket as _ws

        ws_app = _ws.WebSocketApp(
            self._channel_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_close=self._on_close,
            on_error=self._on_error,
        )
        ws_app.run_forever(ping_interval=30, ping_timeout=10)

    def _on_open(self, ws) -> None:
        self._connected = True
        ws.send(json.dumps({
            "type":     "agent.hello",
            "agent_id": self._agent_id,
            "token":    self._token,
        }))
        logger.info("ManagedAgentClient: connected to manager channel")
        # Start snapshot thread
        t = threading.Thread(
            target=self._snapshot_loop,
            args=(ws,),
            name="managed-snapshot",
            daemon=True,
        )
        t.start()

    def _on_close(self, ws, close_status_code, close_msg) -> None:
        self._connected = False
        logger.debug("ManagedAgentClient: connection closed (%s)", close_msg)

    def _on_error(self, ws, error) -> None:
        logger.debug("ManagedAgentClient: WS error: %s", error)

    def _on_message(self, ws, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("type")
        payload  = msg.get("payload", {})

        if msg_type == "channel.ack":
            logger.debug("ManagedAgentClient: channel ack received")

        elif msg_type == "signal.forward":
            self._handle_signal_forward(payload)

        elif msg_type == "cmd.pause":
            self._handle_pause()

        elif msg_type == "cmd.resume":
            self._handle_resume()

        elif msg_type == "cmd.stop":
            self._handle_stop()

        elif msg_type == "cmd.emergency_stop":
            self._handle_emergency_stop()

        elif msg_type == "license.updated":
            self._handle_license_updated(payload)

    def _snapshot_loop(self, ws) -> None:
        from src.managed.status import build_managed_snapshot
        while not self._stop_event.is_set() and self._connected:
            try:
                snap = build_managed_snapshot(
                    self._container,
                    self._agent_id,
                    self._started_at,
                    self,
                )
                ws.send(json.dumps({"type": "agent.snapshot", "payload": snap}))
            except Exception as exc:
                logger.debug("Snapshot send failed: %s", exc)
                break
            self._stop_event.wait(_SNAPSHOT_INTERVAL)

    # ── Command handlers ──────────────────────────────────────────────────

    def _handle_signal_forward(self, signal_dict: dict) -> None:
        try:
            from src.core.event_types import Events
            from src.domain.signal_interface import InboundSignal
            signal = InboundSignal.from_dict(signal_dict)
            self._container.event_bus.emit(Events.SIGNAL_TRIGGERED, signal)
            logger.debug("Signal %s forwarded from manager → EventBus", signal.id)
        except Exception as exc:
            logger.error("Failed to handle signal.forward: %s", exc)

    def _handle_pause(self) -> None:
        try:
            if not self._container.signal_queue.is_paused():
                self._container.signal_queue.pause()
                logger.info("Agent paused via manager command")
        except Exception as exc:
            logger.error("Pause command failed: %s", exc)

    def _handle_resume(self) -> None:
        try:
            if self._container.signal_queue.is_paused():
                self._container.signal_queue.resume()
                logger.info("Agent resumed via manager command")
        except Exception as exc:
            logger.error("Resume command failed: %s", exc)

    def _handle_stop(self) -> None:
        logger.info("Stop command received from manager — initiating shutdown")
        os.kill(os.getpid(), signal.SIGTERM)

    def _handle_emergency_stop(self) -> None:
        try:
            logger.warning("Emergency stop received from manager")
            self._container.signal_queue.pause()
            self._container.position_manager.emergency_close_all()
        except Exception as exc:
            logger.error("Emergency stop failed: %s", exc)

    def _handle_license_updated(self, payload: dict) -> None:
        status = payload.get("status", "")
        if status in ("suspended", "revoked", "expired"):
            logger.warning(
                "License status changed to %s — shutting down agent", status
            )
            os.kill(os.getpid(), signal.SIGTERM)
