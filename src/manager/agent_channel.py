"""
manager/agent_channel.py — WebSocket server for agent ↔ manager communication.

Port 8766.  Each agent subprocess connects here via ManagedAgentClient,
authenticates with a shared token, sends snapshots every 2 s, and receives
commands (pause / resume / stop / emergency_stop) and signal.forward messages.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any

from src.manager.models import AgentSnapshot, AgentStatus
from src.manager.registry import AgentRegistry

logger = logging.getLogger(__name__)

try:
    import websockets
    import websockets.server
    _WS_AVAILABLE = True
except ImportError:
    _WS_AVAILABLE = False
    logger.error("websockets package not installed — AgentChannel disabled")


class AgentChannel:
    def __init__(
        self,
        registry: AgentRegistry,
        token: str,
        port: int = 8766,
    ) -> None:
        self._registry    = registry
        self._token       = token
        self._port        = port
        self._snapshots: dict[str, AgentSnapshot] = {}
        self._connections: dict[str, Any] = {}   # agent_id → websocket
        self._lock        = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: Any = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        if not _WS_AVAILABLE:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="agent-channel",
            daemon=True,
        )
        self._thread.start()
        logger.info("AgentChannel started on port %d", self._port)

    def stop(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)

    # ── Public interface ──────────────────────────────────────────────────

    def get_snapshot(self, agent_id: str) -> AgentSnapshot | None:
        with self._lock:
            return self._snapshots.get(agent_id)

    def get_all_snapshots(self) -> dict[str, AgentSnapshot]:
        with self._lock:
            return dict(self._snapshots)

    def send_command(self, agent_id: str, cmd: str, payload: dict | None = None) -> bool:
        """Send a control command to a connected agent. Thread-safe."""
        return self._send_to_agent(agent_id, {"type": cmd, "payload": payload or {}})

    def forward_signal(self, agent_id: str, signal_dict: dict) -> bool:
        """
        Fast-path signal delivery — does NOT go through OperationRunner.
        Thread-safe via asyncio.run_coroutine_threadsafe.
        """
        return self._send_to_agent(agent_id, {"type": "signal.forward", "payload": signal_dict})

    # ── Internal ──────────────────────────────────────────────────────────

    def _send_to_agent(self, agent_id: str, message: dict) -> bool:
        if not self._loop:
            return False
        with self._lock:
            ws = self._connections.get(agent_id)
        if not ws:
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(
                ws.send(json.dumps(message)), self._loop
            )
            future.result(timeout=3)
            return True
        except Exception as exc:
            logger.debug("Failed to send to agent %s: %s", agent_id, exc)
            return False

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self) -> None:
        async with websockets.server.serve(
            self._handle_connection, "127.0.0.1", self._port
        ):
            await asyncio.Future()   # run forever

    async def _handle_connection(self, websocket) -> None:
        agent_id: str | None = None
        try:
            # First message must be agent.hello
            raw = await asyncio.wait_for(websocket.recv(), timeout=10)
            msg = json.loads(raw)

            if msg.get("type") != "agent.hello":
                await websocket.close(1008, "Expected agent.hello")
                return

            if msg.get("token") != self._token:
                await websocket.close(1008, "Invalid token")
                logger.warning("AgentChannel: rejected connection — bad token")
                return

            agent_id = msg.get("agent_id")
            if not agent_id:
                await websocket.close(1008, "Missing agent_id")
                return

            with self._lock:
                self._connections[agent_id] = websocket

            logger.info("AgentChannel: agent %s connected", agent_id)
            await websocket.send(json.dumps({"type": "channel.ack"}))

            async for raw_msg in websocket:
                await self._on_message(agent_id, raw_msg)

        except asyncio.TimeoutError:
            logger.debug("AgentChannel: handshake timeout")
        except Exception as exc:
            logger.debug("AgentChannel: connection error for %s: %s", agent_id, exc)
        finally:
            if agent_id:
                with self._lock:
                    self._connections.pop(agent_id, None)
                # Mark agent as stopped if it was still RUNNING
                reg = self._registry.get_agent(agent_id)
                if reg and reg.status == AgentStatus.RUNNING:
                    self._registry.set_agent_status(agent_id, AgentStatus.STOPPED, pid=None)
                logger.info("AgentChannel: agent %s disconnected", agent_id)

    async def _on_message(self, agent_id: str, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("type")
        payload  = msg.get("payload", {})

        if msg_type == "agent.snapshot":
            snap = _parse_snapshot(agent_id, payload)
            with self._lock:
                self._snapshots[agent_id] = snap
            # Transition STARTING → RUNNING on first snapshot
            reg = self._registry.get_agent(agent_id)
            if reg and reg.status == AgentStatus.STARTING:
                self._registry.set_agent_status(agent_id, AgentStatus.RUNNING)
                self._registry.reset_crash_count(agent_id)
            self._registry.touch_last_seen(agent_id)

        elif msg_type == "agent.status":
            status_str = payload.get("status")
            if status_str:
                try:
                    status = AgentStatus(status_str)
                    self._registry.set_agent_status(agent_id, status)
                except ValueError:
                    pass


def _parse_snapshot(agent_id: str, payload: dict) -> AgentSnapshot:
    return AgentSnapshot(
        agent_id=agent_id,
        status=AgentStatus(payload.get("status", "STOPPED")),
        mt5_connected=bool(payload.get("mt5_connected")),
        mt5_login=payload.get("mt5_login"),
        mt5_server=payload.get("mt5_server"),
        balance=payload.get("balance"),
        equity=payload.get("equity"),
        open_trades=int(payload.get("open_trades", 0)),
        gateway_connected=bool(payload.get("gateway_connected")),
        uptime_sec=int(payload.get("uptime_sec", 0)),
        observed_at=payload.get("observed_at", int(time.time() * 1000)),
    )
