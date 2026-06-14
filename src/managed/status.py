"""
managed/status.py — Build the AgentSnapshot payload sent to the manager.

In managed mode the agent has no direct gateway connection, so
gateway_connected reflects whether the AgentChannel WS to the manager
is alive (not the upstream gateway WS).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.app.container import AppContainer
    from src.managed.client import ManagedAgentClient

_ACCOUNT_CACHE_TTL = 3.0   # seconds


def build_managed_snapshot(
    container: "AppContainer",
    agent_id: str,
    started_at: float,
    managed_client: "ManagedAgentClient",
) -> dict:
    """Return a dict matching AgentSnapshot fields."""
    mt5_connected = False
    mt5_login     = None
    mt5_server    = None
    balance       = None
    equity        = None

    try:
        if container.mt5_client.is_connected():
            mt5_connected = True
            acct = _cached_account_info(container)
            if acct:
                mt5_login  = acct.login
                mt5_server = acct.server
                balance    = acct.balance
                equity     = acct.equity
    except Exception:
        pass

    open_trades = 0
    try:
        open_trades = len(container.position_store.get_open_trades())
    except Exception:
        pass

    return {
        "agent_id":          agent_id,
        "status":            "RUNNING",
        "mt5_connected":     mt5_connected,
        "mt5_login":         mt5_login,
        "mt5_server":        mt5_server,
        "balance":           balance,
        "equity":            equity,
        "open_trades":       open_trades,
        "gateway_connected": managed_client.is_channel_connected(),
        "uptime_sec":        int(time.time() - started_at),
        "observed_at":       int(time.time() * 1000),
    }


# ── Simple in-process cache to avoid hammering MT5 every 2 s ──────────────────

_account_cache: dict = {}


def _cached_account_info(container: "AppContainer"):
    now = time.time()
    if _account_cache.get("expires", 0) > now:
        return _account_cache.get("data")
    try:
        acct = container.mt5_positions.get_account_info()
        _account_cache["data"]    = acct
        _account_cache["expires"] = now + _ACCOUNT_CACHE_TTL
        return acct
    except Exception:
        return None
