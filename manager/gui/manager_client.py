"""
src/gui/manager_client.py — HTTP client for the LocalManagerApi.

Polls GET /agents every 3 s using stdlib urllib.request (no extra deps).
Reads the bearer token from the api_token.txt file written by the manager.
Submits operations (start/stop/provision) in background threads.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 3.0          # seconds
_MANAGER_BASE  = "http://127.0.0.1:8870"
_TOKEN_PATH    = (
    Path(os.environ.get("PROGRAMDATA", "C:/ProgramData"))
    / "Apex Quantel" / "manager" / "api_token.txt"
)


class ManagerClient:
    """
    Background HTTP poller for the manager REST API.

    Callbacks fire on the polling thread — callers must schedule Tkinter
    updates with app.after() or queue them.
    """

    def __init__(
        self,
        on_agents: Callable[[dict], None],
        on_error:  Callable[[str], None] | None = None,
        base_url:  str = _MANAGER_BASE,
        token_path: Path = _TOKEN_PATH,
    ) -> None:
        self._on_agents   = on_agents
        self._on_error    = on_error
        self._base_url    = base_url
        self._token_path  = token_path
        self._token:  str = ""
        self._stop    = threading.Event()
        self._thread: threading.Thread | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._poll_loop, name="manager-client", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def is_reachable(self) -> bool:
        """Best-effort connectivity check (sync, call from a background thread)."""
        try:
            self._refresh_token()
            if not self._token:
                return False
            url = self._base_url + "/agents"
            req = urllib.request.Request(
                url,
                method="GET",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=3) as _:
                return True
        except Exception:
            return False

    # ── Operations ────────────────────────────────────────────────────────────

    def get_agents(self, on_done: Callable[[dict], None]) -> None:
        """GET /agents in a background thread — call after mutations to refresh immediately."""
        def _run():
            try:
                on_done(self._get("/agents"))
            except Exception:
                on_done({"agents": []})
        threading.Thread(target=_run, daemon=True).start()

    def submit_operation(
        self,
        agent_id: str,
        action: str,
        on_done: Callable[[str | None], None] | None = None,
    ) -> None:
        """POST /agents/{id}/{action} in a background thread."""
        def _run():
            try:
                result = self._post(f"/agents/{agent_id}/{action}", {})
                op_id = result.get("op_id")
                if on_done:
                    on_done(op_id)
            except Exception as exc:
                logger.error("Operation %s/%s failed: %s", agent_id, action, exc)
                if on_done:
                    on_done(None)
        threading.Thread(target=_run, daemon=True).start()

    def provision_agent(
        self,
        payload: dict,
        on_done: Callable[[str | None], None] | None = None,
    ) -> None:
        """POST /agents in a background thread."""
        def _run():
            try:
                result = self._post("/agents", payload)
                op_id = result.get("op_id")
                if on_done:
                    on_done(op_id)
            except Exception as exc:
                logger.error("Provision failed: %s", exc)
                if on_done:
                    on_done(None)
        threading.Thread(target=_run, daemon=True).start()

    def delete_agent(
        self,
        agent_id: str,
        on_done: Callable[[str | None], None] | None = None,
    ) -> None:
        """DELETE /agents/{id} in a background thread."""
        def _run():
            try:
                result = self._delete(f"/agents/{agent_id}")
                op_id = result.get("op_id")
                if on_done:
                    on_done(op_id)
            except Exception as exc:
                logger.error("Delete agent %s failed: %s", agent_id, exc)
                if on_done:
                    on_done(None)
        threading.Thread(target=_run, daemon=True).start()

    def get_terminals(self, on_done: Callable[[list], None]) -> None:
        """GET /terminals in a background thread."""
        def _run():
            try:
                result = self._get("/terminals")
                on_done(result.get("terminals", []))
            except Exception as exc:
                logger.debug("GET /terminals failed: %s", exc)
                on_done([])
        threading.Thread(target=_run, daemon=True).start()

    def send_agent_command(
        self,
        agent_id: str,
        command: str,
        payload: dict | None = None,
        on_done: Callable[[bool, str | None], None] | None = None,
    ) -> None:
        """POST /agents/{id}/command in a background thread."""
        def _run():
            body = {"command": command, **(payload or {})}
            try:
                result = self._post(f"/agents/{agent_id}/command", body)
                if on_done:
                    on_done(bool(result.get("ok")), None)
            except Exception as exc:
                if on_done:
                    on_done(False, str(exc))
        threading.Thread(target=_run, daemon=True).start()

    def get_agent_logs(
        self,
        agent_id: str,
        lines: int = 200,
        on_done: Callable[[list[str]], None] | None = None,
    ) -> None:
        """GET /agents/{id}/logs in a background thread."""
        def _run():
            try:
                result = self._get(f"/agents/{agent_id}/logs?lines={lines}")
                if on_done:
                    on_done(result.get("lines", []))
            except Exception:
                if on_done:
                    on_done([])
        threading.Thread(target=_run, daemon=True).start()

    def get_license_info(self, on_done: Callable[[dict], None]) -> None:
        """GET cached manager license details in a background thread."""
        self._get_license_request("GET", "/license", {}, on_done)

    def preflight_license(self, activation_key: str, on_done: Callable[[dict], None]) -> None:
        """Verify a supplied key, or the stored manager key when blank."""
        self._get_license_request(
            "POST", "/license/preflight", {"activation_key": activation_key}, on_done,
        )

    def set_license_key(self, activation_key: str, on_done: Callable[[dict], None]) -> None:
        """Verify and save a replacement manager license key."""
        self._get_license_request(
            "POST", "/license", {"activation_key": activation_key}, on_done,
        )

    def _get_license_request(
        self,
        method: str,
        path: str,
        body: dict,
        on_done: Callable[[dict], None],
    ) -> None:
        def _run():
            try:
                result = self._request(method, path, body if method == "POST" else None)
                on_done(result)
            except Exception as exc:
                logger.warning("%s %s failed: %s", method, path, exc)
                on_done({"valid": False, "symbols": [], "error": str(exc)})
        threading.Thread(target=_run, daemon=True).start()

    # ── Internal polling loop ─────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._refresh_token()
                if self._token:
                    data = self._get("/agents")
                    self._on_agents(data)
            except Exception as exc:
                logger.debug("Manager poll failed: %s", exc)
                if self._on_error:
                    self._on_error(str(exc))
            self._stop.wait(_POLL_INTERVAL)

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _refresh_token(self) -> None:
        try:
            self._token = self._token_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            self._token = ""

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        if not self._token:
            self._refresh_token()
        url = self._base_url + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type":  "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())

    def patch_agent_config(
        self,
        agent_id: str,
        patch: dict,
        on_done: Callable[[dict], None] | None = None,
    ) -> None:
        """PATCH /agents/{id}/config — merge patch into agent config, auto-restarts agent."""
        def _run():
            try:
                result = self._patch(f"/agents/{agent_id}/config", patch)
                if on_done:
                    on_done(result)
            except Exception as exc:
                if on_done:
                    on_done({"error": str(exc)})
        threading.Thread(target=_run, daemon=True).start()

    def _get(self, path: str) -> dict:
        return self._request("GET", path)

    def _post(self, path: str, body: dict) -> dict:
        return self._request("POST", path, body)

    def _patch(self, path: str, body: dict) -> dict:
        return self._request("PATCH", path, body)

    def _delete(self, path: str) -> dict:
        return self._request("DELETE", path)
