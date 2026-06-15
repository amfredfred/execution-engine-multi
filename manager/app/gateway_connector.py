"""
manager/app/gateway_connector.py — Manager's WebSocket session with the Apex Quant cloud gateway.

The manager connects as engine_id='manager-main', activates with the manager's
activation key, then sends manager.agent.snapshot messages so that managed
worker agents appear in the cloud dashboard.

Gateway protocol (engine.gateway.ts):
  → engine.hello        immediately on connect
  ← protocol.accepted   gateway acknowledges
  → activation.request  sent after protocol.accepted
  ← activation.accepted session is live
  → engine.heartbeat    every 20 s
  → manager.agent.snapshot  once per running agent (and on every snapshot update)
"""

from __future__ import annotations

import json
import logging
import platform
import threading
from typing import TYPE_CHECKING, Callable

from src.infra.websocket import WebSocketClient

if TYPE_CHECKING:
    from manager.app.models import AgentSnapshot
    from manager.app.registry import AgentRegistry
    from manager.app.secrets import ManagerSecretStore

logger = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL = 20.0   # seconds


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
    # Non-Windows fallback (MAC-address-based UUID)
    import uuid
    return f"AQM-{uuid.UUID(int=uuid.getnode()).hex}"


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
        logger.info("GatewayConnector started: %s", self._gateway_ws_url)

    def stop(self) -> None:
        self._stop_event.set()
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=5)
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
        """Called when an agent is deprovisioned — stops forwarding its snapshots."""
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
        self._send({"type": "engine.hello", "engine_id": self._engine_id, "accounts": []})

    def _on_disconnected(self) -> None:
        self._activated.clear()

    def _on_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        msg_type = msg.get("type", "")
        if msg_type == "protocol.accepted":
            self._request_activation()
        elif msg_type == "activation.accepted":
            logger.info("GatewayConnector: activation accepted (%s)", self._engine_id)
            self._activated.set()
            self._flush_all_snapshots()
        elif msg_type == "activation.rejected":
            logger.error(
                "GatewayConnector: activation rejected — %s", msg.get("reason", "unknown")
            )
        elif msg_type == "signal.triggered":
            payload = msg.get("payload", msg)
            if self._on_signal is not None:
                try:
                    self._on_signal(payload)
                except Exception:
                    logger.exception("GatewayConnector: signal handler error")
        elif msg_type in ("gateway.error", "error"):
            logger.warning("GatewayConnector: gateway error — %s", msg)

    def _request_activation(self) -> None:
        activation_key = self._secrets.get_activation_key()
        if not activation_key:
            logger.warning(
                "GatewayConnector: no activation key set — cannot activate with gateway"
            )
            return
        self._send({
            "type": "activation.request",
            "activation_key": activation_key,
            "device_name": f"Manager ({platform.node()}) [{self._engine_id}]",
            "engine_version": self._engine_version,
            "platform": {
                "os": platform.system(),
                "arch": platform.machine(),
                "python": platform.python_version(),
            },
            "mt5_accounts": [],
        })

    def _flush_all_snapshots(self) -> None:
        """Send all known agent snapshots immediately after (re)activation."""
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
        self._send({
            "type": "manager.agent.snapshot",
            "engine_id": agent_id,
            "display_name": reg.display_name,
            "account": {
                "login": reg.mt5_login,
                "server": reg.mt5_server or "",
                "mode": "real",
            },
            "awareness": {
                "status": status_str,
                "mt5_connected": snapshot.mt5_connected,
            },
            "metrics": {
                "balance": snapshot.balance,
                "equity": snapshot.equity,
                "open_trades": snapshot.open_trades,
                "uptime_sec": snapshot.uptime_sec,
            },
        })

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.wait(_HEARTBEAT_INTERVAL):
            if self._activated.is_set():
                self._send({"type": "engine.heartbeat"})

    def _send(self, payload: dict) -> None:
        if self._client:
            self._client.send(json.dumps(payload))
