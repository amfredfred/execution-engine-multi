"""
src/gui/ws_client.py — WebSocket client for the UIBridge.

Uses websocket-client (synchronous). Reconnects automatically with
exponential back-off so the GUI stays live during engine restarts.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)


class WSClient:
    """
    Connects to ws://localhost:{port} (an agent UIBridge) and delivers
    messages to the GUI via thread-safe callbacks.

    Callbacks fire on the ws-client thread — callers must use widget.after()
    to schedule any Tkinter updates.
    """

    def __init__(
        self,
        url: str,
        on_message: Callable[[dict], None] | None = None,
        on_connect: Callable[[], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
    ) -> None:
        self.url = url
        self._on_message    = on_message
        self._on_connect    = on_connect
        self._on_disconnect = on_disconnect
        self._ws            = None
        self._thread: threading.Thread | None = None
        self._stop = False

    def start(self) -> None:
        self._stop = False
        self._thread = threading.Thread(
            target=self._run_loop, name="agent-ws-client", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        ws = self._ws
        if ws:
            try:
                ws.close()
            except Exception:
                pass

    def send(self, msg_type: str, payload: dict) -> None:
        ws = self._ws
        if ws:
            try:
                ws.send(json.dumps({"type": msg_type, "payload": payload}))
            except Exception as exc:
                logger.warning("WSClient.send failed: %s", exc)

    def _run_loop(self) -> None:
        import websocket  # websocket-client

        delay = 2.0
        while not self._stop:
            try:
                ws = websocket.WebSocketApp(
                    self.url,
                    on_open=self._on_open,
                    on_message=self._on_msg,
                    on_close=self._on_close,
                    on_error=self._on_error,
                )
                self._ws = ws
                ws.run_forever(ping_interval=15, ping_timeout=5)
                delay = 2.0
            except Exception as exc:
                logger.debug("WSClient connection error: %s", exc)

            if not self._stop:
                time.sleep(delay)
                delay = min(delay * 1.5, 15.0)

    def _on_open(self, ws) -> None:
        logger.debug("WSClient connected to %s", self.url)
        if self._on_connect:
            self._on_connect()

    def _on_msg(self, ws, raw: str) -> None:
        try:
            msg = json.loads(raw)
            if self._on_message:
                self._on_message(msg)
        except Exception as exc:
            logger.warning("WSClient parse error: %s", exc)

    def _on_close(self, ws, code, msg) -> None:
        logger.debug("WSClient disconnected (code=%s)", code)
        if self._on_disconnect:
            self._on_disconnect()

    def _on_error(self, ws, error) -> None:
        logger.debug("WSClient error: %s", error)
