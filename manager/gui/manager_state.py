"""
src/gui/manager_state.py — Observable state for the multi-agent fleet view.

Decoupled from any HTTP client: the caller feeds it API response dicts and
it notifies subscribers.  Two events:
    "agents"         — agent list updated (payload: list[AgentCardState])
    "agent_selected" — user drilled into an agent (payload: agent_id, monitoring_port)
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class AgentCardState:
    agent_id:        str
    display_name:    str
    status:          str            # PROVISIONED / STARTING / RUNNING / STOPPED / CRASH_LOOP / ERROR
    desired_status:  str
    mt5_login:       int | None
    mt5_server:      str | None
    monitoring_port: int
    symbols:         list[str]
    crash_count:     int = 0
    error_message:   str | None = None
    # Live fields (present only when agent has sent a snapshot)
    mt5_connected:     bool  = False
    balance:           float | None = None
    equity:            float | None = None
    open_trades:       int   = 0
    gateway_connected: bool  = False
    uptime_sec:        int   = 0


class ManagerAppState:
    def __init__(self) -> None:
        self._lock        = threading.Lock()
        self._callbacks:  dict[str, list[Callable]] = {}
        self.agents:      list[AgentCardState] = []
        self.selected_agent_id:   str | None = None
        self.selected_agent_port: int | None = None

    # ── Subscription ──────────────────────────────────────────────────────────

    def subscribe(self, event: str, callback: Callable) -> None:
        self._callbacks.setdefault(event, []).append(callback)

    def _notify(self, event: str, **kwargs: Any) -> None:
        for cb in self._callbacks.get(event, []):
            try:
                cb(**kwargs)
            except Exception:
                pass

    # ── Mutations ─────────────────────────────────────────────────────────────

    def apply(self, api_response: dict) -> None:
        """Parse GET /agents response and emit 'agents' event."""
        raw_agents = api_response.get("agents", [])
        cards: list[AgentCardState] = []
        for a in raw_agents:
            cards.append(AgentCardState(
                agent_id        = a.get("agent_id", ""),
                display_name    = a.get("display_name", ""),
                status          = a.get("status", "UNKNOWN"),
                desired_status  = a.get("desired_status", ""),
                mt5_login       = a.get("mt5_login"),
                mt5_server      = a.get("mt5_server"),
                monitoring_port = a.get("monitoring_port", 8081),
                symbols         = a.get("symbols", []),
                crash_count     = a.get("crash_count", 0),
                error_message   = a.get("error_message"),
                mt5_connected   = bool(a.get("mt5_connected", False)),
                balance         = a.get("balance"),
                equity          = a.get("equity"),
                open_trades     = int(a.get("open_trades", 0)),
                gateway_connected = bool(a.get("gateway_connected", False)),
                uptime_sec      = int(a.get("uptime_sec", 0)),
            ))
        with self._lock:
            self.agents = cards
        self._notify("agents", agents=cards)

    def select_agent(self, agent_id: str, monitoring_port: int) -> None:
        with self._lock:
            self.selected_agent_id   = agent_id
            self.selected_agent_port = monitoring_port
        self._notify("agent_selected", agent_id=agent_id, monitoring_port=monitoring_port)

    def clear_selection(self) -> None:
        with self._lock:
            self.selected_agent_id   = None
            self.selected_agent_port = None
        self._notify("fleet_restored")

    def get_agent(self, agent_id: str) -> AgentCardState | None:
        with self._lock:
            return next((a for a in self.agents if a.agent_id == agent_id), None)
