"""
manager/api.py — LocalManagerApi: REST server on port 8870.

Uses stdlib http.server.ThreadingHTTPServer — no extra dependencies.
All routes require Authorization: Bearer <token>.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, TYPE_CHECKING
from urllib.parse import urlparse, parse_qs

from manager.app.secrets import _dpapi_encrypt

if TYPE_CHECKING:
    from manager.app.event_hub import EngineEventHub
    from manager.app.config_revisions import ConfigRevisionService
    from manager.app.operations import OperationRunner
    from manager.app.provisioning import AgentProvisioner
    from manager.app.registry import AgentRegistry
    from manager.app.terminal_discovery import TerminalDiscovery

logger = logging.getLogger(__name__)

_VERSION = "0.1.0"
_MAX_BODY_BYTES = 1_048_576
_MAX_LOG_LINES = 2_000
_MAX_CONCURRENT_REQUESTS = 32


class _BoundedThreadingHTTPServer(ThreadingHTTPServer):
    def __init__(self, *args, **kwargs):
        self._request_slots = threading.BoundedSemaphore(_MAX_CONCURRENT_REQUESTS)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address) -> None:
        if not self._request_slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


class LocalManagerApi:
    def __init__(
        self,
        registry: "AgentRegistry",
        ops: "OperationRunner",
        provisioner: "AgentProvisioner",
        discovery: "TerminalDiscovery",
        event_hub: "EngineEventHub",
        config_revisions: "ConfigRevisionService",
        token: str,
        port: int = 8870,
        storage_path: str = "",
        on_activation_key_changed: Callable[[str], None] | None = None,
        on_api_token_rotated: Callable[[str], None] | None = None,
        health_provider: Callable[[], dict] | None = None,
    ) -> None:
        self._registry    = registry
        self._ops         = ops
        self._provisioner = provisioner
        self._discovery   = discovery
        self._event_hub   = event_hub
        self._config_revisions = config_revisions
        self._token       = token
        self._port        = port
        self._storage_path = storage_path
        self._on_activation_key_changed = on_activation_key_changed
        self._on_api_token_rotated = on_api_token_rotated
        self._health_provider = health_provider
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        # Write token to file so GUI can read it
        if self._storage_path:
            token_path = Path(self._storage_path) / "api_token.txt"
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(_dpapi_encrypt(self._token), encoding="utf-8")

        handler = self._make_handler()
        self._server = _BoundedThreadingHTTPServer(("127.0.0.1", self._port), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="manager-api",
            daemon=True,
        )
        self._thread.start()
        logger.info("LocalManagerApi started on port %d", self._port)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()

    def _make_handler(self) -> type:
        registry    = self._registry
        ops         = self._ops
        provisioner = self._provisioner
        discovery   = self._discovery
        event_hub   = self._event_hub
        config_revisions = self._config_revisions
        api         = self
        on_activation_key_changed = self._on_activation_key_changed
        on_api_token_rotated = self._on_api_token_rotated

        class Handler(BaseHTTPRequestHandler):
            def setup(self) -> None:
                super().setup()
                self.request.settimeout(15)

            def log_message(self, fmt, *args):
                logger.debug("API %s - %s", self.address_string(), fmt % args)

            def _auth(self) -> bool:
                auth = self.headers.get("Authorization", "")
                if auth == f"Bearer {api._token}":
                    return True
                self._send(401, {"error": "Unauthorized"})
                return False

            def _body(self) -> dict:
                try:
                    length = int(self.headers.get("Content-Length", 0))
                except ValueError as exc:
                    raise ValueError("Invalid Content-Length") from exc
                if not length:
                    return {}
                if length < 0 or length > _MAX_BODY_BYTES:
                    raise ValueError(f"Request body exceeds {_MAX_BODY_BYTES} bytes")
                value = json.loads(self.rfile.read(length))
                if not isinstance(value, dict):
                    raise ValueError("Request body must be a JSON object")
                return value

            def _send(self, code: int, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _path_parts(self) -> list[str]:
                return [p for p in urlparse(self.path).path.split("/") if p]

            def do_GET(self):
                if not self._auth():
                    return
                parts = self._path_parts()
                query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                allowed_query = {"lines"} if (
                    len(parts) == 3 and parts[0] == "agents" and parts[2] == "logs"
                ) else set()
                unexpected = set(query) - allowed_query
                if unexpected:
                    self._send(400, {"error": f"Unexpected query parameters: {sorted(unexpected)}"})
                    return

                if not parts or parts == ["health"]:
                    health = event_hub.health_report()
                    if api._health_provider:
                        health = api._health_provider()
                    self._send(
                        200 if health["ok"] else 503,
                        {"version": _VERSION, **health},
                    )

                elif parts == ["agents"]:
                    agents = registry.list_agents()
                    snapshots = event_hub.get_all_snapshots()
                    result = []
                    for reg in agents:
                        snap = snapshots.get(reg.agent_id)
                        d = {
                            "agent_id":        reg.agent_id,
                            "display_name":    reg.display_name,
                            "status":          reg.status.value,
                            "desired_status":  reg.desired_status,
                            "mt5_login":       reg.mt5_login,
                            "mt5_server":      reg.mt5_server,
                            "monitoring_port": reg.monitoring_port,
                            "symbols":         reg.symbols,
                            "terminal_path":   reg.terminal_path,
                            "crash_count":     reg.crash_count,
                            "error_message":   reg.error_message,
                            "created_at":      reg.created_at,
                        }
                        if snap:
                            d.update({
                                "mt5_connected":     snap.mt5_connected,
                                "balance":           snap.balance,
                                "equity":            snap.equity,
                                "open_trades":       snap.open_trades,
                                "gateway_connected": snap.gateway_connected,
                                "uptime_sec":        snap.uptime_sec,
                                "observed_at":       snap.observed_at,
                            })
                        result.append(d)
                    self._send(200, {"agents": result})

                elif len(parts) == 2 and parts[0] == "agents":
                    reg = registry.get_agent(parts[1])
                    if not reg:
                        self._send(404, {"error": "Agent not found"})
                        return
                    snap = event_hub.get_snapshot(parts[1])
                    d = {"agent_id": reg.agent_id, "status": reg.status.value}
                    if snap:
                        d["snapshot"] = snap.__dict__
                    self._send(200, d)

                elif len(parts) == 3 and parts[0] == "agents" and parts[2] == "logs":
                    reg = registry.get_agent(parts[1])
                    if not reg:
                        self._send(404, {"error": "Agent not found"})
                        return
                    qs = parse_qs(urlparse(self.path).query)
                    try:
                        lines = int((qs.get("lines") or ["200"])[0])
                    except ValueError:
                        self._send(400, {"error": "lines must be an integer"})
                        return
                    if lines < 1 or lines > _MAX_LOG_LINES:
                        self._send(
                            400,
                            {"error": f"lines must be between 1 and {_MAX_LOG_LINES}"},
                        )
                        return
                    log_lines = _tail_agent_log(reg.agent_id, lines)
                    self._send(200, {"agent_id": reg.agent_id, "lines": log_lines})

                elif parts == ["terminals"]:
                    terminals = discovery.scan()
                    self._send(200, {"terminals": [
                        {"path": t.path, "name": t.name, "state": t.state, "leased_to": t.leased_to}
                        for t in terminals
                    ]})

                elif parts == ["license"]:
                    self._send(200, provisioner.get_license_info())

                elif len(parts) == 2 and parts[0] == "operations":
                    op = registry.get_operation(parts[1])
                    if not op:
                        self._send(404, {"error": "Operation not found"})
                        return
                    self._send(200, {
                        "op_id": op.op_id, "status": op.status,
                        "error": op.error, "completed_at": op.completed_at,
                    })

                elif len(parts) == 2 and parts[0] == "commands":
                    outcome = registry.get_command_outcome(parts[1])
                    if not outcome:
                        self._send(404, {"error": "Command not found"})
                        return
                    self._send(200, outcome)

                else:
                    self._send(404, {"error": "Not found"})

            def do_POST(self):
                try:
                    self._do_POST()
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._send(400, {"error": str(exc)})

            def _do_POST(self):
                if not self._auth():
                    return
                if urlparse(self.path).query:
                    raise ValueError("Query parameters are not allowed for this endpoint")
                parts = self._path_parts()

                if parts == ["agents"]:
                    body = _validate_provision_body(self._body())
                    op_id = ops.submit("provision", "__new__", payload=body)
                    self._send(202, {"op_id": op_id})

                elif parts == ["license", "preflight"]:
                    body = self._body()
                    activation_key = str(body.get("activation_key") or "").strip()
                    try:
                        if activation_key:
                            info = provisioner.preflight_license(activation_key)
                        else:
                            info = provisioner.get_license_info(force=True)
                        self._send(200, info)
                    except Exception as exc:
                        self._send(400, {"valid": False, "symbols": [], "error": str(exc)})

                elif parts == ["auth", "rotate"]:
                    replacement = secrets.token_hex(32)
                    api._token = replacement
                    if on_api_token_rotated:
                        on_api_token_rotated(replacement)
                    if api._storage_path:
                        token_path = Path(api._storage_path) / "api_token.txt"
                        token_path.write_text(_dpapi_encrypt(replacement), encoding="utf-8")
                    self._send(200, {"rotated": True})

                elif parts == ["license"]:
                    body = self._body()
                    activation_key = str(body.get("activation_key") or "").strip()
                    try:
                        info = provisioner.set_activation_key(activation_key)
                        if on_activation_key_changed:
                            on_activation_key_changed(activation_key)
                        self._send(200, info)
                    except Exception as exc:
                        self._send(400, {"valid": False, "symbols": [], "error": str(exc)})

                elif len(parts) == 3 and parts[0] == "agents":
                    agent_id = parts[1]
                    action   = parts[2]
                    reg = registry.get_agent(agent_id)
                    if not reg:
                        self._send(404, {"error": "Agent not found"})
                        return

                    if action == "command":
                        body = self._body()
                        command_id, err = _dispatch_agent_command(
                            event_hub, agent_id, body
                        )
                        if command_id:
                            self._send(202, {
                                "ok": True,
                                "command_id": command_id,
                                "status": "sent",
                            })
                        else:
                            self._send(400, {"error": err or "command failed"})
                        return

                    op_map = {
                        "start":            "start",
                        "stop":             "stop",
                        "force-stop":       "force_stop",
                        "reset-crash-loop": "reset_crash_loop",
                    }
                    op_type = op_map.get(action)
                    if not op_type:
                        self._send(404, {"error": f"Unknown action: {action}"})
                        return

                    op_id = ops.submit(op_type, agent_id)
                    self._send(202, {"op_id": op_id})

                else:
                    self._send(404, {"error": "Not found"})

            def do_DELETE(self):
                if not self._auth():
                    return
                if urlparse(self.path).query:
                    self._send(400, {"error": "Query parameters are not allowed for this endpoint"})
                    return
                parts = self._path_parts()
                if len(parts) == 2 and parts[0] == "agents":
                    agent_id = parts[1]
                    reg = registry.get_agent(agent_id)
                    if not reg:
                        self._send(404, {"error": "Agent not found"})
                        return
                    op_id = ops.submit("remove", agent_id)
                    self._send(202, {"op_id": op_id})
                else:
                    self._send(404, {"error": "Not found"})

            def do_PATCH(self):
                try:
                    self._do_PATCH()
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._send(400, {"error": str(exc)})

            def _do_PATCH(self):
                if not self._auth():
                    return
                if urlparse(self.path).query:
                    raise ValueError("Query parameters are not allowed for this endpoint")
                parts = self._path_parts()
                if len(parts) == 3 and parts[0] == "agents" and parts[2] == "config":
                    agent_id = parts[1]
                    reg = registry.get_agent(agent_id)
                    if not reg:
                        self._send(404, {"error": "Agent not found"})
                        return
                    try:
                        result = config_revisions.apply(agent_id, self._body())
                        self._send(202, result)
                    except Exception as exc:
                        self._send(400, {"error": str(exc)})
                else:
                    self._send(404, {"error": "Not found"})

        return Handler


# ── Module-level helpers ──────────────────────────────────────────────────────

def _tail_agent_log(agent_id: str, max_lines: int) -> list[str]:
    """Read the last max_lines lines from the agent's log file."""
    import os
    from src.config.settings import ManagerConfig
    cfg = ManagerConfig.defaults()
    log_path = Path(cfg.agents_data_dir) / agent_id / "logs" / "engine.log"
    if not log_path.exists():
        for name in ("engine.log", "stdout.log", "apex.log"):
            candidate = Path(cfg.agents_data_dir) / agent_id / "logs" / name
            if candidate.exists():
                log_path = candidate
                break
        else:
            return []
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            return [line.rstrip() for line in deque(fh, maxlen=max_lines)]
    except OSError:
        return []


def _validate_provision_body(body: dict) -> dict:
    required = {
        "display_name": str,
        "terminal_path": str,
        "mt5_login": int,
        "mt5_password": str,
        "mt5_server": str,
        "symbols": list,
    }
    allowed = {*required, "config_overrides"}
    unexpected = set(body) - allowed
    if unexpected:
        raise ValueError(f"Unexpected provision fields: {sorted(unexpected)}")
    for key, expected in required.items():
        value = body.get(key)
        if not isinstance(value, expected) or (
            expected is str and not value.strip()
        ):
            raise ValueError(f"{key} must be a non-empty {expected.__name__}")
    if body["mt5_login"] <= 0:
        raise ValueError("mt5_login must be positive")
    if not body["symbols"] or not all(
        isinstance(symbol, str) and symbol.strip() for symbol in body["symbols"]
    ):
        raise ValueError("symbols must contain non-empty strings")
    overrides = body.get("config_overrides")
    if overrides is not None and not isinstance(overrides, dict):
        raise ValueError("config_overrides must be an object")
    return body


def _dispatch_agent_command(event_hub, agent_id: str, body: dict):
    """Forward a GUI command to the worker via IPC. Returns (ok, error_or_None)."""
    from src.runtime.contracts import EngineCommandType
    cmd = str(body.get("command", "")).strip()
    _MAP = {
        "pause":          EngineCommandType.PAUSE,
        "resume":         EngineCommandType.RESUME,
        "emergency_stop": EngineCommandType.EMERGENCY_STOP,
    }
    if cmd == "close_trade":
        trade_id = str(body.get("trade_id", "")).strip()
        if not trade_id:
            return False, "trade_id required for close_trade"
        command_id = event_hub.submit_command(
            agent_id,
            EngineCommandType.CLOSE_TRADE,
            {"trade_id": trade_id},
        )
        return command_id, None if command_id else "agent not connected"

    cmd_type = _MAP.get(cmd)
    if not cmd_type:
        return False, f"unknown command: {cmd}"
    command_id = event_hub.submit_command(agent_id, cmd_type, {})
    return command_id, None if command_id else "agent not connected"
