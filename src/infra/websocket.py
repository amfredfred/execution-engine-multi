"""
Resilient WebSocket client with exponential-backoff reconnection.

Runs in a dedicated daemon thread.  Call `start()` once; the client
reconnects automatically on drop.  Pass `on_message` to receive frames.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
import urllib.parse
from typing import TYPE_CHECKING

import websocket  # websocket-client

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


def _ipv4_url(url: str) -> tuple[str, dict]:
    """
    Resolve the hostname in a wss:// URL to its IPv4 address so the
    connection bypasses any broken IPv6 path on the host network.

    Returns (rewritten_url, sslopt) where sslopt carries the original
    hostname as server_hostname so Cloudflare/SNI still works.
    Falls back to the original URL silently if resolution fails.
    """
    parsed = urllib.parse.urlparse(url)
    hostname = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme in ("wss", "https") else 80)
    try:
        addrs = socket.getaddrinfo(hostname, port, socket.AF_INET, socket.SOCK_STREAM)
        if addrs:
            ipv4 = addrs[0][4][0]
            netloc = f"{ipv4}:{port}"
            ipv4_url = parsed._replace(netloc=netloc).geturl()
            return ipv4_url, {"server_hostname": hostname}
    except OSError:
        pass
    return url, {}


class WebSocketClient:
    def __init__(
        self,
        url: str,
        on_message: Callable[[str], None],
        on_connected: Callable[[], None] | None = None,
        on_disconnected: Callable[[], None] | None = None,
        reconnect_delay: float = 2.0,
        max_reconnect_delay: float = 30.0,
        ping_interval: float = 20.0,
    ) -> None:
        self._url               = url
        self._on_message        = on_message
        self._on_connected      = on_connected or (lambda: None)
        self._on_disconnected   = on_disconnected or (lambda: None)
        self._reconnect_delay   = reconnect_delay
        self._max_reconnect     = max_reconnect_delay
        self._ping_interval     = ping_interval

        self._ws:      websocket.WebSocketApp | None = None
        self._stopped  = threading.Event()
        self._thread:  threading.Thread | None = None
        self._current_delay = reconnect_delay

    # ── Public API ────────────────────────────────────────────────────────

    def start(self) -> None:
        logger.info("WebSocketClient starting, url=%s", self._url)
        self._stopped.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        logger.info("WebSocketClient stopping")
        self._stopped.set()
        if self._ws:
            self._ws.close()

    def send(self, data: str) -> bool:
        if self._ws:
            try:
                self._ws.send(data)
                return True
            except Exception:
                logger.warning("WebSocketClient.send failed")
        return False

    # ── Internal loop ─────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        while not self._stopped.is_set():
            # Resolve to IPv4 so a broken IPv6 path doesn't cause a ~21s
            # timeout on every reconnect attempt (Cloudflare serves both;
            # many ISPs have broken IPv6 routing).
            conn_url, sslopt = _ipv4_url(self._url)
            self._ws = websocket.WebSocketApp(
                conn_url,
                on_open=self._handle_open,
                on_message=self._handle_message,
                on_error=self._handle_error,
                on_close=self._handle_close,
            )
            self._ws.run_forever(
                ping_interval=int(self._ping_interval),
                sslopt=sslopt if sslopt else None,
                # Restore the original hostname in the HTTP Host header so
                # Cloudflare can route the request correctly (the URL contains
                # a raw IPv4 address to bypass broken IPv6 routing).
                host=sslopt.get("server_hostname") if sslopt else None,
            )

            if self._stopped.is_set():
                break

            logger.info(
                "WebSocketClient reconnecting in %.1fs", self._current_delay
            )
            time.sleep(self._current_delay)
            self._current_delay = min(
                self._current_delay * 2, self._max_reconnect
            )

    def _handle_open(self, ws: websocket.WebSocketApp) -> None:
        logger.info("WebSocketClient connected: %s", self._url)
        self._current_delay = self._reconnect_delay   # reset backoff
        self._on_connected()

    def _handle_message(self, ws: websocket.WebSocketApp, message: str) -> None:
        try:
            self._on_message(message)
        except Exception:
            logger.exception("WebSocketClient message handler error")

    def _handle_error(self, ws: websocket.WebSocketApp, error: Exception) -> None:
        logger.error("WebSocketClient error: %s", error)

    def _handle_close(
        self,
        ws: websocket.WebSocketApp,
        close_status_code: int | None,
        close_msg: str | None,
    ) -> None:
        logger.warning(
            "WebSocketClient closed: code=%s msg=%s", close_status_code, close_msg
        )
        self._on_disconnected()









