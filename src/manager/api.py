"""
manager/api.py — LocalManagerApi: REST server on port 8870.

Uses stdlib http.server.ThreadingHTTPServer — no extra dependencies.
All routes require Authorization: Bearer <token>.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    from src.manager.event_hub import EngineEventHub
    from src.manager.config_revisions import ConfigRevisionService
    from src.manager.operations import OperationRunner
    from src.manager.provisioning import AgentProvisioner
    from src.manager.registry import AgentRegistry
    from src.manager.terminal_discovery import TerminalDiscovery

logger = logging.getLogger(__name__)

_VERSION = "0.1.0"


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
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        # Write token to file so GUI can read it
        if self._storage_path:
            token_path = Path(self._storage_path) / "api_token.txt"
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(self._token, encoding="utf-8")

        handler = self._make_handler()
        self._server = ThreadingHTTPServer(("127.0.0.1", self._port), handler)
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
        token       = self._token
        on_activation_key_changed = self._on_activation_key_changed

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                logger.debug("API %s - %s", self.address_string(), fmt % args)

            def _auth(self) -> bool:
                auth = self.headers.get("Authorization", "")
                if auth == f"Bearer {token}":
                    return True
                self._send(401, {"error": "Unauthorized"})
                return False

            def _body(self) -> dict:
                length = int(self.headers.get("Content-Length", 0))
                if not length:
                    return {}
                return json.loads(self.rfile.read(length))

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

                if not parts or parts == ["health"]:
                    self._send(200, {"ok": True, "version": _VERSION})

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

                else:
                    self._send(404, {"error": "Not found"})

            def do_POST(self):
                if not self._auth():
                    return
                parts = self._path_parts()

                if parts == ["agents"]:
                    body = self._body()
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
                if not self._auth():
                    return
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
