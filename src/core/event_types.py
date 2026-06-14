"""
Typed event definitions for the execution engine event bus.

Defines:
  - EventName   — string literal union of all valid event names
  - Payload types — TypedDict for each event's payload
  - EventPayloadMap — maps each event name to its payload type
  - TypedEventBus — thin wrapper around EventBus with typed emit/on

Usage:
    from core.event_types import Events, TypedEventBus, SignalTriggeredPayload

    bus = TypedEventBus(raw_bus)
    bus.emit(Events.TRADE_OPENED, trade)          # type checked
    bus.on(Events.RISK_REJECTED, handler)         # type checked
"""

from __future__ import annotations

from typing import Any, Callable, List, Literal
from typing_extensions import TypedDict

from src.domain.signal_interface import InboundSignal
from src.domain.trade import Trade, TradePlan


# ── Event name literals ───────────────────────────────────────────────────────

# Signal
SIGNAL_RECEIVED = "signal.received"
SIGNAL_VALIDATED = "signal.validated"
SIGNAL_REJECTED = "signal.rejected"
SIGNAL_TRIGGERED = "signal.triggered"
EXECUTION_ATTEMPTED = "execution.attempted"

# Risk
RISK_APPROVED = "risk.approved"
RISK_REJECTED = "risk.rejected"

# Trade lifecycle
TRADE_PLANNED = "trade.planned"
TRADE_OPENED = "trade.opened"
TRADE_TP1_HIT = "trade.tp1_hit"
TRADE_TP2_HIT = "trade.tp2_hit"
TRADE_SL_HIT = "trade.sl_hit"
TRADE_INVALIDATED = "trade.invalidated"
TRADE_EXPIRED = "trade.expired"
TRADE_CLOSED = "trade.closed"
TRADE_ERROR = "trade.error"

# Order
ORDER_CREATED = "order.created"
ORDER_EXECUTED = "order.executed"
ORDER_REJECTED = "order.rejected"

# Broker
BROKER_CONNECTED = "broker.connected"
BROKER_DISCONNECTED = "broker.disconnected"
BROKER_ERROR = "broker.error"

# System
SYSTEM_STARTED = "system.started"
SYSTEM_STOPPING = "system.stopping"
DAILY_RESET = "system.daily_reset"


EventName = Literal[
    "signal.received",
    "signal.validated",
    "signal.rejected",
    "signal.triggered",
    "execution.attempted",
    "risk.approved",
    "risk.rejected",
    "trade.planned",
    "trade.opened",
    "trade.tp1_hit",
    "trade.tp2_hit",
    "trade.sl_hit",
    "trade.invalidated",
    "trade.expired",
    "trade.closed",
    "trade.error",
    "order.created",
    "order.executed",
    "order.rejected",
    "broker.connected",
    "broker.disconnected",
    "broker.error",
    "system.started",
    "system.stopping",
    "system.daily_reset",
]


# ── Payload TypedDicts ────────────────────────────────────────────────────────


class SignalReceivedPayload(TypedDict):
    event: str
    signal: InboundSignal


class SignalRejectedPayload(TypedDict):
    signal: InboundSignal
    reason: List[str]


class SignalTriggeredPayload(TypedDict):
    # payload IS the signal — emitted directly, not wrapped
    pass


class RiskApprovedPayload(TypedDict):
    signal: InboundSignal


class RiskRejectedPayload(TypedDict):
    signal: InboundSignal
    reason: str


class TradePlannedPayload(TypedDict):
    plan: TradePlan


class TradeOpenedPayload(TypedDict):
    # payload IS the Trade object
    pass


class TradeErrorPayload(TypedDict):
    signal: InboundSignal
    reason: str
    message: str


# Trade lifecycle events (TP1, TP2, SL, closed) all emit Trade directly
TradePayload = Trade


# ── Convenience re-export (keeps Events.XYZ import style working) ─────────────


class Events:
    """String constants for all event names. Import this for autocomplete."""

    # Signal
    SIGNAL_RECEIVED: EventName = "signal.received"
    SIGNAL_VALIDATED: EventName = "signal.validated"
    SIGNAL_REJECTED: EventName = "signal.rejected"
    SIGNAL_TRIGGERED: EventName = "signal.triggered"
    EXECUTION_ATTEMPTED: EventName = "execution.attempted"

    # Risk
    RISK_APPROVED: EventName = "risk.approved"
    RISK_REJECTED: EventName = "risk.rejected"

    # Trade lifecycle
    TRADE_PLANNED: EventName = "trade.planned"
    TRADE_OPENED: EventName = "trade.opened"
    TRADE_TP1_HIT: EventName = "trade.tp1_hit"
    TRADE_TP2_HIT: EventName = "trade.tp2_hit"
    TRADE_SL_HIT: EventName = "trade.sl_hit"
    TRADE_INVALIDATED: EventName = "trade.invalidated"
    TRADE_EXPIRED: EventName = "trade.expired"
    TRADE_CLOSED: EventName = "trade.closed"
    TRADE_ERROR: EventName = "trade.error"

    # Order
    ORDER_CREATED: EventName = "order.created"
    ORDER_EXECUTED: EventName = "order.executed"
    ORDER_REJECTED: EventName = "order.rejected"

    # Broker
    BROKER_CONNECTED: EventName = "broker.connected"
    BROKER_DISCONNECTED: EventName = "broker.disconnected"
    BROKER_ERROR: EventName = "broker.error"

    # System
    SYSTEM_STARTED: EventName = "system.started"
    SYSTEM_STOPPING: EventName = "system.stopping"
    DAILY_RESET: EventName = "system.daily_reset"

    @classmethod
    def all(cls) -> List[str]:
        """Return all event name values."""
        return [
            v
            for k, v in vars(cls).items()
            if not k.startswith("_") and isinstance(v, str)
        ]


# ── Payload map — event name → expected payload type ─────────────────────────

EventPayloadMap = {
    SIGNAL_RECEIVED: SignalReceivedPayload,
    SIGNAL_REJECTED: SignalRejectedPayload,
    SIGNAL_TRIGGERED: InboundSignal,
    EXECUTION_ATTEMPTED: InboundSignal,
    RISK_APPROVED: RiskApprovedPayload,
    RISK_REJECTED: RiskRejectedPayload,
    TRADE_PLANNED: TradePlannedPayload,
    TRADE_OPENED: Trade,
    TRADE_TP1_HIT: Trade,
    TRADE_TP2_HIT: Trade,
    TRADE_SL_HIT: Trade,
    TRADE_CLOSED: Trade,
    TRADE_ERROR: TradeErrorPayload,
    BROKER_CONNECTED: None,
    BROKER_DISCONNECTED: None,
    SYSTEM_STARTED: None,
    SYSTEM_STOPPING: None,
    DAILY_RESET: None,
}


# ── Typed listener aliases ────────────────────────────────────────────────────

SignalListener = Callable[[InboundSignal], None]
TradeListener = Callable[[Trade], None]
AnyListener = Callable[[str, Any], None]









