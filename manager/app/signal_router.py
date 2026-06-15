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
import json
import threading
import time
from typing import TYPE_CHECKING

from src.core.event_bus import EventBus
from src.core.event_types import Events
from manager.app.models import AgentStatus
from src.runtime.contracts import EngineCommandType
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
        self._stop_event = threading.Event()
        self._delivery_thread: threading.Thread | None = None

    def start(self, active_agents: list["AgentRegistration"]) -> None:
        self._stop_event.clear()
        self._delivery_thread = threading.Thread(
            target=self._delivery_loop,
            name="signal-delivery",
            daemon=True,
        )
        self._delivery_thread.start()
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
        self._stop_event.set()
        if self._delivery_thread:
            self._delivery_thread.join(timeout=5)
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

    def handle_gateway_signal(self, payload: dict) -> None:
        """Route a signal received directly from the cloud gateway WS.

        Used only when Signal Manager is not configured; the payload has the
        same shape as a signal.triggered frame from the Signal Manager.
        """
        symbol = str(payload.get("symbol") or "").upper()
        broker = str(payload.get("broker") or "").lower()
        signal_id = str(payload.get("id") or payload.get("signal_id") or "")
        if not symbol:
            logger.warning("Gateway signal has no symbol — ignoring")
            return

        agents = self._registry.list_agents()
        reference_ms = int(payload.get("emitted_at") or payload.get("created_at") or time.time() * 1000)
        eligible = 0
        for reg in agents:
            if reg.status != AgentStatus.RUNNING:
                continue
            if symbol not in {s.upper() for s in (reg.symbols or [])}:
                continue
            if broker and not _broker_matches(broker, reg.mt5_server):
                continue
            eligible += 1
            self._registry.queue_signal_delivery(
                signal_id,
                reg.agent_id,
                payload,
                reference_ms + 120_000,
            )
        logger.info(
            "Gateway signal %s %s broker=%s → %d agent(s)", signal_id, symbol, broker or "(any)", eligible
        )
        self._process_due_deliveries()

    def forward_execution_event(
        self, agent_id: str, event_type: str, data: dict
    ) -> None:
        if not self._client or not self._client.send_execution_event(
            agent_id, event_type, data
        ):
            logger.warning(
                "Execution event %s from %s could not be forwarded",
                event_type,
                agent_id,
            )

    def health_report(self) -> dict:
        configured = bool(self._signal_ws_url)
        connected = bool(self._client and self._client.is_connected())
        return {
            "configured": configured,
            "connected": connected,
            "ok": configured and connected,
        }

    # ── Internal ──────────────────────────────────────────────────────────────

    def _on_signal(self, signal) -> None:
        """Fan out a triggered signal to all RUNNING agents whose broker+symbol match."""
        agents = self._registry.list_agents()
        signal_dict = signal.to_dict() if hasattr(signal, "to_dict") else {}
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
            reference_ms = int(
                getattr(signal, "emitted_at", 0)
                or getattr(signal, "created_at", 0)
                or time.time() * 1000
            )
            self._registry.queue_signal_delivery(
                signal.id,
                reg.agent_id,
                signal_dict,
                reference_ms + 120_000,
            )

        broker = signal.broker or "(any)"
        direction = (
            signal.direction.value
            if hasattr(signal.direction, "value")
            else str(signal.direction)
        )
        logger.info(
            "Signal %s %s %s broker=%s → %d/%d agent(s)",
            signal.id, signal.symbol, direction, broker, 0, eligible,
        )
        if eligible == 0:
            self._registry.record_signal_outcome(
                signal.id,
                "__routing__",
                signal_dict,
                "no_eligible_agent",
            )
        self._process_due_deliveries()

    def _delivery_loop(self) -> None:
        while not self._stop_event.wait(1.0):
            try:
                self._process_due_deliveries()
            except Exception:
                logger.exception("Signal delivery retry loop failed")

    def _process_due_deliveries(self) -> None:
        now = int(time.time() * 1000)
        for delivery in self._registry.list_due_signal_deliveries(now):
            signal_id = delivery["signal_id"]
            agent_id = delivery["agent_id"]
            if now >= int(delivery["expires_at"]):
                self._registry.update_signal_delivery(
                    signal_id, agent_id, status="expired", error="signal expired"
                )
                continue
            command_id = delivery.get("command_id")
            if command_id:
                outcome = self._registry.get_command_outcome(command_id)
                if outcome and outcome["status"] == "completed":
                    self._registry.update_signal_delivery(
                        signal_id, agent_id, status="accepted"
                    )
                    continue
                if outcome and outcome["status"] == "rejected":
                    self._registry.update_signal_delivery(
                        signal_id,
                        agent_id,
                        status="rejected",
                        error=str(outcome.get("error") or ""),
                    )
                    continue
            attempts = int(delivery["attempts"]) + 1
            command_id = self._event_hub.submit_command(
                agent_id,
                EngineCommandType.SIGNAL_DELIVER,
                {"signal": json.loads(delivery["payload_json"])},
            )
            delay_ms = min(30_000, 1000 * (2 ** min(attempts - 1, 5)))
            self._registry.update_signal_delivery(
                signal_id,
                agent_id,
                status="sent" if command_id else "pending",
                command_id=command_id,
                attempts=attempts,
                next_attempt_at=now + delay_ms,
                error="" if command_id else "agent not connected",
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
