"""
manager/models.py — Pure dataclasses for the multi-agent manager layer.
No business logic; only data definitions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class AgentStatus(str, Enum):
    PROVISIONED = "PROVISIONED"
    STARTING    = "STARTING"
    RUNNING     = "RUNNING"
    DEGRADED    = "DEGRADED"
    STOPPING    = "STOPPING"
    STOPPED     = "STOPPED"
    CRASH_LOOP  = "CRASH_LOOP"
    ERROR       = "ERROR"


@dataclass
class AgentRegistration:
    agent_id:        str
    display_name:    str
    status:          AgentStatus
    desired_status:  str            # "running" | "stopped"
    config_path:     str            # abs path to per-agent config.yaml
    data_dir:        str            # abs path to per-agent data directory
    terminal_path:   str | None
    mt5_login:       int | None
    mt5_server:      str | None
    monitoring_port: int            # UIBridge port (8081, 8082, ...)
    symbols:         list[str]      # e.g. ["XAUUSD"] — used for signal routing
    created_at:      int            # epoch ms
    updated_at:      int
    last_seen_at:    int | None     # last worker event or snapshot
    pid:             int | None     # current OS pid if RUNNING/STARTING
    crash_count:     int = 0
    last_crash_at:   int | None = None
    error_message:   str | None = None


@dataclass
class TerminalLease:
    terminal_path: str
    agent_id:      str
    leased_at:     int
    pid:           int | None


@dataclass
class TerminalInfo:
    path:      str
    name:      str   # display name derived from parent directory
    state:     str   # "available" | "managed_running" | "managed_stopped" | "running_unmanaged"
    leased_to: str | None = None   # agent_id if managed


@dataclass
class AgentSnapshot:
    agent_id:          str
    status:            AgentStatus
    mt5_connected:     bool
    mt5_login:         int | None
    mt5_server:        str | None
    balance:           float | None
    equity:            float | None
    open_trades:       int
    gateway_connected: bool   # True = worker IPC connection to manager is alive
    uptime_sec:        int
    observed_at:       int    # epoch ms when snapshot was built
    telemetry:         dict


@dataclass
class OperationRecord:
    op_id:        str
    agent_id:     str
    op_type:      str   # "start" | "stop" | "force_stop" | "remove" | "provision" | "reset_crash_loop"
    status:       str   # "pending" | "running" | "completed" | "failed"
    created_at:   int
    completed_at: int | None = None
    error:        str | None = None
