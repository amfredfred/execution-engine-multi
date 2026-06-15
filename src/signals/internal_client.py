"""
signals/internal_client.py — WebSocket client for Signal Manager's GatewayServer.

Speaks the simple internal protocol (contrast with SignalConsumer which speaks
the complex Apex Quantel external gateway protocol):

  On connect:    → {action: "subscribe", symbols: [...]}
  On signal:     ← {event: "signal.triggered", payload: {..., broker: "..."}}
  On reconnect:  → {action: "subscribe", symbols: [...]}  (automatic)

The Signal Manager handles deduplication, consensus, and authentication upstream,
so this client is intentionally minimal.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from src.core.event_types import Events
from src.domain.signal_interface import InboundSignal
from src.infra.websocket import WebSocketClient

if TYPE_CHECKING:
    from src.core.event_bus import EventBus

logger = logging.getLogger(__name__)


class InternalSignalClient:
    """Receives consensus signals from the Signal Manager's GatewayServer."""

    def __init__(self, event_bus: "EventBus") -> None:
        self._bus = event_bus
        self._ws: WebSocketClient | None = None
        self._symbols: list[str] = []

    def start(self, symbols: list[str], ws_url: str, ws_token: str = "") -> None:
        self._symbols = list(symbols)
        url = ws_url
        if ws_token:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}token={ws_token}"
        self._ws = WebSocketClient(
            url=url,
            on_message=self._handle_message,
            on_connected=self._on_connected,
            on_disconnected=self._on_disconnected,
        )
        self._ws.start()
        logger.info(
            "InternalSignalClient starting: url=%s symbols=%s", ws_url, symbols
        )

    def stop(self) -> None:
        if self._ws:
            self._ws.stop()
            self._ws = None

    def update_symbols(self, symbols: list[str]) -> None:
        """Re-subscribe after the tracked symbol set changes."""
        self._symbols = list(symbols)
        if self._ws:
            self._ws.send(json.dumps({"action": "subscribe", "symbols": symbols}))

    def send_execution_event(self, engine_id: str, event_type: str, data: dict) -> bool:
        if not self._ws:
            return False
        self._ws.send(json.dumps({
            "action": "execution.event",
            "engine_id": engine_id,
            "event_type": event_type,
            "data": data,
        }))
        return True

    def is_connected(self) -> bool:
        return bool(self._ws and self._ws.is_connected())

    # ── Private ───────────────────────────────────────────────────────────────

    def _on_connected(self) -> None:
        logger.info("InternalSignalClient connected to Signal Manager")
        if self._ws and self._symbols:
            self._ws.send(
                json.dumps({"action": "subscribe", "symbols": self._symbols})
            )

    def _on_disconnected(self) -> None:
        logger.warning("InternalSignalClient disconnected from Signal Manager")

    def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("InternalSignalClient: received invalid JSON")
            return

        if not isinstance(msg, dict):
            return

        event = msg.get("event", "")
        payload = msg.get("payload", {})

        if event == "connected":
            logger.info(
                "Signal Manager handshake: clientId=%s supported=%s",
                payload.get("clientId"),
                payload.get("supported_symbols"),
            )
        elif event == "subscribed":
            logger.info(
                "InternalSignalClient: subscribed to %s", payload.get("symbols")
            )
        elif event == "signal.triggered":
            self._process_signal(payload)
        # metrics.snapshot, log.record, etc. are intentionally ignored here.

    def _process_signal(self, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        try:
            signal = InboundSignal.from_dict(payload)
        except Exception:
            logger.exception(
                "InternalSignalClient: failed to deserialise signal payload"
            )
            return

        logger.info(
            "Signal received: id=%s symbol=%s direction=%s broker=%s",
            signal.id, signal.symbol,
            signal.direction.value if hasattr(signal.direction, "value") else signal.direction,
            signal.broker,
        )
        self._bus.emit(Events.SIGNAL_RECEIVED, {"event": "signal.triggered", "signal": signal})
        self._bus.emit(Events.SIGNAL_TRIGGERED, signal)
