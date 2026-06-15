"""
manager/signal_router.py — Receives consensus signals from Signal Manager → fans out to agents.

The manager connects to the Signal Manager's internal GatewayServer (port 8765 by
default), receives consensus-validated signals via InternalSignalClient, and
delivers each signal to every running agent worker whose broker and symbol match.

Signal-to-agent routing:
  Signal payload carries `broker` (e.g. "exness") set by the Signal Manager.
  Agent registration carries `mt5_server` (e.g. "Exness-MT5-Real4").
  _broker_matches() does normalised lowercase-alphanumeric substring matching.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from src.core.event_bus import EventBus
from src.core.event_types import Events
from manager.app.models import AgentStatus
from src.signals.internal_client import InternalSignalClient

if TYPE_CHECKING:
    from manager.app.event_hub import EngineEventHub
    from manager.app.models import AgentRegistration
    from manager.app.registry import AgentRegistry

logger = logging.getLogger(__name__)


class ManagerSignalRouter:
    def __init__(
        self,
        registry: "AgentRegistry",
        event_hub: "EngineEventHub",
        signal_ws_url: str,
        signal_ws_token: str = "",
    ) -> None:
        self._registry = registry
        self._event_hub = event_hub
        self._signal_ws_url = signal_ws_url
        self._signal_ws_token = signal_ws_token
        self._event_bus: EventBus | None = None
        self._client: InternalSignalClient | None = None
        self._current_symbols: set[str] = set()

    def start(self, active_agents: list["AgentRegistration"]) -> None:
        self._event_bus = EventBus()
        symbols = self._union_symbols(active_agents)
        self._current_symbols = symbols

        if not self._signal_ws_url:
            logger.warning(
                "ManagerSignalRouter: signal_ws_url is not configured — "
                "no signals will be received. Set signal.ws_url in manager config."
            )
            return

        self._client = InternalSignalClient(self._event_bus)
        self._event_bus.on(Events.SIGNAL_TRIGGERED, self._on_signal)
        self._client.start(
            symbols=list(symbols),
            ws_url=self._signal_ws_url,
            ws_token=self._signal_ws_token,
        )
        logger.info(
            "ManagerSignalRouter started: url=%s symbols=%s",
            self._signal_ws_url, sorted(symbols),
        )

    def stop(self) -> None:
        if self._client:
            self._client.stop()
            self._client = None
        logger.info("ManagerSignalRouter stopped")

    def refresh_rooms(self, active_agents: list["AgentRegistration"]) -> None:
        """Recompute the subscribed symbol set when agents are added or removed."""
        new_symbols = self._union_symbols(active_agents)
        if new_symbols == self._current_symbols:
            return

        logger.info(
            "ManagerSignalRouter: symbol set changed %s → %s",
            sorted(self._current_symbols), sorted(new_symbols),
        )
        self._current_symbols = new_symbols

        if self._client is not None:
            self._client.update_symbols(list(new_symbols))
        elif self._signal_ws_url and new_symbols:
            # First agent added after start — create the client now.
            assert self._event_bus is not None
            self._client = InternalSignalClient(self._event_bus)
            self._event_bus.on(Events.SIGNAL_TRIGGERED, self._on_signal)
            self._client.start(
                symbols=list(new_symbols),
                ws_url=self._signal_ws_url,
                ws_token=self._signal_ws_token,
            )

    # ── Internal ──────────────────────────────────────────────────────────────

    def _on_signal(self, signal) -> None:
        """Fan out a triggered signal to all RUNNING agents whose broker+symbol match."""
        agents = self._registry.list_agents()
        signal_dict = signal.to_dict() if hasattr(signal, "to_dict") else {}
        delivered = 0
        eligible = 0

        for reg in agents:
            if reg.status != AgentStatus.RUNNING:
                continue
            agent_symbols = {s.upper() for s in (reg.symbols or [])}
            if signal.symbol.upper() not in agent_symbols:
                continue
            if signal.broker and not _broker_matches(signal.broker, reg.mt5_server):
                continue
            eligible += 1
            sent = self._event_hub.deliver_signal(reg.agent_id, signal_dict)
            if sent:
                delivered += 1
                logger.debug(
                    "Signal %s → agent %s (%s)",
                    signal.id, reg.agent_id, reg.mt5_server,
                )
            else:
                logger.warning(
                    "Signal %s: agent %s not connected via IPC — signal dropped",
                    signal.id, reg.agent_id,
                )

        broker = signal.broker or "(any)"
        direction = (
            signal.direction.value
            if hasattr(signal.direction, "value")
            else str(signal.direction)
        )
        logger.info(
            "Signal %s %s %s broker=%s → %d/%d agent(s)",
            signal.id, signal.symbol, direction, broker, delivered, eligible,
        )

    @staticmethod
    def _union_symbols(agents: list["AgentRegistration"]) -> set[str]:
        result: set[str] = set()
        for agent in agents:
            for s in (agent.symbols or []):
                result.add(s.upper())
        return result


def _broker_matches(broker: str, mt5_server: str | None) -> bool:
    """Fuzzy-match a signal broker name against an MT5 server label.

    Signal Manager uses profile names like "exness", "fundednext".
    MT5 server strings look like "Exness-MT5-Real4", "FundedNext-Server".
    Both are normalised to lowercase alphanumerics before substring matching.
    """
    norm_broker = re.sub(r"[^a-z0-9]", "", broker.lower())
    norm_server = re.sub(r"[^a-z0-9]", "", (mt5_server or "").lower())
    return bool(norm_broker and norm_broker in norm_server)
