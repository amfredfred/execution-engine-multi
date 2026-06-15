"""
manager/app/gateway_connector.py — Manager's WebSocket session with the Apex Quant cloud gateway.

The manager connects as engine_id='AQM-{MachineGuid}', activates with the manager's
activation key, then sends manager.agent.snapshot messages so that managed
worker agents appear in the cloud dashboard.

Wire protocol (mirrors src/signals/consumer.py):
  SEND  {"event": "engine.hello",      "data": {"protocol_version": "1.0", "message_id": "...", "sent_at": "...", "payload": {...}}}
  RECV  {"event": "protocol.accepted", "data": {"message_id": "<echoed hello_id>"}}
  SEND  {"event": "activation.request","data": {..., "payload": {...}}}
  RECV  {"event": "activation.accepted","data": {"engine_id": "...", ...}}
  SEND  {"event": "room.subscribe",    "data": {..., "payload": {"engine_id": ..., "symbols": [...], "ttl_seconds": 3600}}}
  SEND  {"event": "engine.heartbeat",  "data": {..., "payload": {"engine_id": ..., "status": "running", ...}}}  every 30 s
  SEND  {"event": "manager.agent.snapshot", "data": {..., "payload": {...}}}  per agent
"""

from __future__ import annotations

import json
import logging
import platform
import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Callable
from uuid import uuid4

from src.infra.websocket import WebSocketClient

from manager.app.models import AgentSnapshot

if TYPE_CHECKING:
    from manager.app.registry import AgentRegistry
    from manager.app.secrets import ManagerSecretStore

logger = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL = 30.0   # seconds
_ROOM_TTL_SECONDS   = 3600


def _manager_engine_id() -> str:
    """Return a stable, machine-unique manager engine ID: AQM-{MachineGuid}."""
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
        ) as key:
            guid, _ = winreg.QueryValueEx(key, "MachineGuid")
            return f"AQM-{guid.strip()}"
    except Exception:
        pass
    import uuid
    return f"AQM-{uuid.UUID(int=uuid.getnode()).hex}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class GatewayConnector:
    """Maintains the manager's WebSocket session with the cloud gateway."""

    def __init__(
        self,
        gateway_ws_url: str,
        secrets: "ManagerSecretStore",
        registry: "AgentRegistry",
        engine_version: str = "0.1.0",
        on_signal: Callable[[dict], None] | None = None,
    ) -> None:
        self._gateway_ws_url = gateway_ws_url
        self._secrets = secrets
        self._registry = registry
        self._engine_version = engine_version
        self._on_signal = on_signal
        self._engine_id = _manager_engine_id()

        self._client: WebSocketClient | None = None
        self._activated = threading.Event()
        self._stop_event = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._metrics_thread: threading.Thread | None = None
        self._heartbeat_sequence = 0

        # Correlate protocol.accepted → hello_message_id
        self._hello_message_id: str | None = None
        self._activation_message_id: str | None = None

        self._snapshots: dict[str, AgentSnapshot] = {}
        self._snapshots_lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if not self._gateway_ws_url:
            logger.warning(
                "GatewayConnector: no gateway WS URL — cloud dashboard disabled"
            )
            return
        self._stop_event.clear()
        self._client = WebSocketClient(
            url=self._gateway_ws_url,
            on_message=self._on_message,
            on_connected=self._on_connected,
            on_disconnected=self._on_disconnected,
        )
        self._client.start()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="gateway-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()
        self._metrics_thread = threading.Thread(
            target=self._metrics_loop,
            name="gateway-metrics",
            daemon=True,
        )
        self._metrics_thread.start()
        logger.info("GatewayConnector started: %s (engine_id=%s)", self._gateway_ws_url, self._engine_id)

    def stop(self) -> None:
        self._stop_event.set()
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=5)
        if self._metrics_thread:
            self._metrics_thread.join(timeout=5)
        if self._client:
            self._client.stop()
            self._client = None
        logger.info("GatewayConnector stopped")

    def push_agent_snapshot(self, agent_id: str, snapshot: "AgentSnapshot") -> None:
        """Called by the event hub whenever a worker sends a metrics snapshot."""
        with self._snapshots_lock:
            self._snapshots[agent_id] = snapshot
        if self._activated.is_set():
            self._send_agent_snapshot(agent_id, snapshot)

    def forget_agent(self, agent_id: str) -> None:
        """Called when an agent is deprovisioned."""
        with self._snapshots_lock:
            self._snapshots.pop(agent_id, None)

    def is_connected(self) -> bool:
        return (
            self._client is not None
            and self._client.is_connected()
            and self._activated.is_set()
        )

    # ── Protocol handlers ─────────────────────────────────────────────────────

    def _on_connected(self) -> None:
        self._activated.clear()
        self._hello_message_id = None
        self._activation_message_id = None
        self._heartbeat_sequence = 0
        self._hello_message_id = self._send("engine.hello", {
            "engine_id": self._engine_id,
            "engine_version": self._engine_version,
            "protocol_versions": ["1.0"],
            "started_at": _utc_now(),
            "accounts": [],
        })

    def _on_disconnected(self) -> None:
        self._activated.clear()

    def _on_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(msg, dict):
            return

        event = msg.get("event", "")
        data  = msg.get("data") or {}

        if event == "protocol.accepted":
            if data.get("message_id") == self._hello_message_id:
                self._request_activation()

        elif event == "activation.accepted":
            valid_ids = {self._activation_message_id, self._hello_message_id}
            if data.get("message_id") not in valid_ids:
                logger.warning("GatewayConnector: unexpected activation.accepted — ignoring")
                return
            if data.get("engine_id") != self._engine_id:
                logger.error("GatewayConnector: activation.accepted engine_id mismatch")
                return
            logger.info("GatewayConnector: activation accepted (%s)", self._engine_id)
            self._activated.set()
            self._subscribe_rooms()
            self._seed_stub_snapshots()
            self._flush_all_snapshots()

        elif event == "activation.rejected":
            logger.error(
                "GatewayConnector: activation rejected — %s",
                data.get("errors") or data.get("reason") or data,
            )

        elif event == "signal.triggered":
            payload = msg.get("payload") or data.get("payload") or data
            if self._on_signal is not None and payload:
                try:
                    self._on_signal(payload)
                except Exception:
                    logger.exception("GatewayConnector: signal handler error")

        elif event in ("gateway.error", "protocol.rejected"):
            logger.warning("GatewayConnector: %s — %s", event, data)

    def _request_activation(self) -> None:
        activation_key = self._secrets.get_activation_key()
        if not activation_key:
            logger.warning("GatewayConnector: no activation key — cannot activate")
            return
        arch = "arm64" if platform.machine().lower() == "arm64" else "x64"
        self._activation_message_id = self._send("activation.request", {
            "activation_key": activation_key,
            "device_name": f"Manager ({platform.node()})",
            "engine_version": self._engine_version,
            "platform": {"os": "windows", "architecture": arch},
            "mt5_accounts": [],
        })

    def _subscribe_rooms(self) -> None:
        """Join symbol rooms so the gateway routes signals to this manager."""
        agents = self._registry.list_agents()
        symbols = sorted({s.upper() for reg in agents for s in (reg.symbols or [])})
        if not symbols:
            return
        self._send("room.subscribe", {
            "engine_id": self._engine_id,
            "symbols": symbols,
            "ttl_seconds": _ROOM_TTL_SECONDS,
        })
        logger.info("GatewayConnector: subscribed to rooms %s", symbols)

    def _seed_stub_snapshots(self) -> None:
        """Register all provisioned agents in the gateway immediately on activation.

        Workers take a few seconds to start and connect via IPC.  Without this
        seed, the first `_flush_all_snapshots()` call sends nothing and the
        agents don't appear in the cloud dashboard until a worker connects.
        Only seeds agents not already in `_snapshots` (real IPC data takes priority).
        """
        with self._snapshots_lock:
            existing = set(self._snapshots)
        for reg in self._registry.list_agents():
            if reg.agent_id in existing:
                continue
            stub = AgentSnapshot(
                agent_id=reg.agent_id,
                status=reg.status,
                mt5_connected=False,
                mt5_login=reg.mt5_login,
                mt5_server=reg.mt5_server,
                balance=None,
                equity=None,
                open_trades=0,
                gateway_connected=False,
                uptime_sec=0,
                observed_at=int(time.time() * 1000),
                telemetry={},
            )
            with self._snapshots_lock:
                self._snapshots.setdefault(reg.agent_id, stub)

    def _flush_all_snapshots(self) -> None:
        with self._snapshots_lock:
            snapshot_copy = dict(self._snapshots)
        for agent_id, snapshot in snapshot_copy.items():
            self._send_agent_snapshot(agent_id, snapshot)

    def _send_agent_snapshot(self, agent_id: str, snapshot: "AgentSnapshot") -> None:
        reg = self._registry.get_agent(agent_id)
        if reg is None:
            return
        status_str = (
            snapshot.status.value
            if hasattr(snapshot.status, "value")
            else str(snapshot.status)
        )
        self._send("manager.agent.snapshot", {
            "engine_id": agent_id,
            "display_name": reg.display_name,
            "account": {
                "login": reg.mt5_login,
                "server": reg.mt5_server or "",
                "mode": "real",
            },
            "awareness": {
                "status": status_str,
                "terminal_connected": snapshot.mt5_connected,
                "runtime_state": "running" if snapshot.mt5_connected else "starting",
            },
            "metrics": snapshot.telemetry,
        })

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.wait(_HEARTBEAT_INTERVAL):
            if self._activated.is_set():
                self._heartbeat_sequence += 1
                self._send("engine.heartbeat", {
                    "engine_id": self._engine_id,
                    "status": "running",
                    "sequence": self._heartbeat_sequence,
                    "observed_at": _utc_now(),
                })

    def _metrics_loop(self) -> None:
        """Push live agent snapshots every 2 s — mirrors single-agent engine cadence."""
        while not self._stop_event.wait(2.0):
            if self._activated.is_set():
                self._flush_all_snapshots()

    def _send(self, event: str, payload: dict) -> str:
        message_id = str(uuid4())
        frame = {
            "event": event,
            "data": {
                "protocol_version": "1.0",
                "message_id": message_id,
                "sent_at": _utc_now(),
                "payload": payload,
            },
        }
        if self._client:
            self._client.send(json.dumps(frame, separators=(",", ":")))
        return message_id
