"""
manager/signal_router.py — Single gateway WS connection with signal fan-out.

The manager holds ONE SignalConsumer connected to the gateway on behalf
of all agents.  On signal.triggered the signal is forwarded to every
RUNNING agent that subscribes to that symbol via AgentChannel.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.core.event_bus import EventBus
from src.core.event_types import Events
from src.manager.models import AgentStatus

if TYPE_CHECKING:
    from src.manager.agent_channel import AgentChannel
    from src.manager.models import AgentRegistration
    from src.manager.registry import AgentRegistry
    from src.signals.consumer import SignalConsumer

logger = logging.getLogger(__name__)


class ManagerSignalRouter:
    def __init__(
        self,
        registry: "AgentRegistry",
        channel: "AgentChannel",
        activation_key: str,
        gateway_ws_url: str,
        engine_version: str,
    ) -> None:
        self._registry      = registry
        self._channel       = channel
        self._activation_key = activation_key
        self._gateway_ws_url = gateway_ws_url
        self._engine_version = engine_version
        self._event_bus: EventBus | None = None
        self._consumer: "SignalConsumer | None" = None
        self._current_symbols: set[str] = set()

    def start(self, active_agents: list["AgentRegistration"]) -> None:
        from src.signals.consumer import SignalConsumer
        from src.signals.signal_validator import SignalValidator

        self._event_bus = EventBus()
        symbols = self._union_symbols(active_agents)
        self._current_symbols = symbols

        if not symbols:
            logger.info("ManagerSignalRouter: no symbols yet — will connect when agents are added")
            return

        self._start_consumer(symbols)

    def stop(self) -> None:
        if self._consumer:
            self._consumer.stop()
            self._consumer = None
        logger.info("ManagerSignalRouter stopped")

    def refresh_rooms(self, active_agents: list["AgentRegistration"]) -> None:
        """Called when agents are added or removed — recompute symbol union."""
        new_symbols = self._union_symbols(active_agents)
        if new_symbols == self._current_symbols:
            return

        logger.info(
            "ManagerSignalRouter: symbol set changed %s → %s",
            self._current_symbols, new_symbols,
        )
        self._current_symbols = new_symbols

        if self._consumer is None and new_symbols:
            self._start_consumer(new_symbols)
            return

        if self._consumer and new_symbols != self._current_symbols:
            # Re-subscribe with new symbol set
            try:
                self._consumer._symbols = list(new_symbols)
                self._consumer._subscribe()
            except Exception as exc:
                logger.warning("Failed to refresh gateway room subscription: %s", exc)

    # ── Internal ──────────────────────────────────────────────────────────

    def _start_consumer(self, symbols: set[str]) -> None:
        from src.signals.consumer import SignalConsumer
        from src.signals.signal_validator import SignalValidator

        assert self._event_bus is not None

        validator = SignalValidator()
        self._consumer = SignalConsumer(
            event_bus=self._event_bus,
            validator=validator,
            ws_url=self._gateway_ws_url,
            activation_key=self._activation_key,
            symbols=list(symbols),
            engine_id="manager",
            engine_version=self._engine_version,
            room_ttl_seconds=600,
            account_login="manager",
        )

        # Fan out signals to agents
        self._event_bus.on(Events.SIGNAL_TRIGGERED, self._on_signal)
        # Lifecycle events back to gateway (already done inside SignalConsumer)

        self._consumer.start()
        logger.info(
            "ManagerSignalRouter: gateway WS started for symbols %s", sorted(symbols)
        )

    def _on_signal(self, signal) -> None:
        """Fan out a triggered signal to all RUNNING agents subscribed to its symbol."""
        agents = self._registry.list_agents()
        signal_dict = signal.to_dict() if hasattr(signal, "to_dict") else _signal_to_dict(signal)
        delivered = 0

        for reg in agents:
            if reg.status != AgentStatus.RUNNING:
                continue
            # Normalise symbol comparison
            agent_symbols = {s.upper() for s in (reg.symbols or [])}
            if signal.symbol.upper() not in agent_symbols:
                continue

            sent = self._channel.forward_signal(reg.agent_id, signal_dict)
            if sent:
                delivered += 1
                logger.debug(
                    "Signal %s forwarded to agent %s", signal.id, reg.agent_id
                )
            else:
                logger.warning(
                    "Could not forward signal %s to agent %s (not connected)",
                    signal.id, reg.agent_id,
                )

        logger.info(
            "Signal %s (%s) delivered to %d/%d eligible agent(s)",
            signal.id, signal.symbol, delivered,
            sum(1 for a in agents if a.status == AgentStatus.RUNNING and
                signal.symbol.upper() in {s.upper() for s in (a.symbols or [])}),
        )

    @staticmethod
    def _union_symbols(agents: list["AgentRegistration"]) -> set[str]:
        result: set[str] = set()
        for agent in agents:
            for s in (agent.symbols or []):
                result.add(s.upper())
        return result


def _signal_to_dict(signal) -> dict:
    """Fallback serialisation for InboundSignal if to_dict() not available."""
    import dataclasses
    if dataclasses.is_dataclass(signal):
        return dataclasses.asdict(signal)
    return {}
