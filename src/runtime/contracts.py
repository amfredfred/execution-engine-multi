"""Typed command and event envelopes shared by manager and engine workers."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4

MAX_WIRE_BYTES = 1_048_576
MAX_ENVELOPE_AGE_MS = 300_000
MAX_CLOCK_SKEW_MS = 60_000


class EngineCommandType(StrEnum):
    SIGNAL_DELIVER = "signal.deliver"
    CLOSE_TRADE = "trade.close"
    PAUSE = "engine.pause"
    RESUME = "engine.resume"
    STOP = "engine.stop"
    EMERGENCY_STOP = "engine.emergency_stop"
    CONFIG_APPLY = "config.apply"
    EVENT_ACK = "event.ack"


class EngineEventType(StrEnum):
    WORKER_HELLO = "worker.hello"
    WORKER_READY = "worker.ready"
    WORKER_STOPPED = "worker.stopped"
    ENGINE_SNAPSHOT = "engine.snapshot"
    EXECUTION_EVENT = "execution.event"
    COMMAND_ACK = "command.ack"
    COMMAND_REJECTED = "command.rejected"


@dataclass(frozen=True)
class EngineCommand:
    engine_id: str
    command_type: EngineCommandType
    payload: dict[str, Any] = field(default_factory=dict)
    command_id: str = field(default_factory=lambda: uuid4().hex)
    config_revision: int = 1
    issued_at: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_wire(self) -> dict[str, Any]:
        return {"kind": "command", **asdict(self)}

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> "EngineCommand":
        if value.get("kind") != "command":
            raise ValueError("Expected command envelope")
        return cls(
            command_id=str(value["command_id"]),
            engine_id=str(value["engine_id"]),
            command_type=EngineCommandType(value["command_type"]),
            config_revision=int(value.get("config_revision", 1)),
            issued_at=int(value["issued_at"]),
            payload=_payload(value),
        )


@dataclass(frozen=True)
class EngineEvent:
    engine_id: str
    sequence: int
    event_type: EngineEventType
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: uuid4().hex)
    config_revision: int = 1
    occurred_at: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_wire(self) -> dict[str, Any]:
        return {"kind": "event", **asdict(self)}

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> "EngineEvent":
        if value.get("kind") != "event":
            raise ValueError("Expected event envelope")
        return cls(
            event_id=str(value["event_id"]),
            engine_id=str(value["engine_id"]),
            sequence=int(value["sequence"]),
            event_type=EngineEventType(value["event_type"]),
            config_revision=int(value.get("config_revision", 1)),
            occurred_at=int(value["occurred_at"]),
            payload=_payload(value),
        )


def _payload(value: dict[str, Any]) -> dict[str, Any]:
    payload = value.get("payload", {})
    if not isinstance(payload, dict):
        raise ValueError("Envelope payload must be an object")
    return payload


def validate_envelope_timestamp(timestamp_ms: int, *, now_ms: int | None = None) -> None:
    now = int(time.time() * 1000) if now_ms is None else now_ms
    if timestamp_ms < now - MAX_ENVELOPE_AGE_MS:
        raise ValueError("Envelope is stale")
    if timestamp_ms > now + MAX_CLOCK_SKEW_MS:
        raise ValueError("Envelope timestamp is too far in the future")


def validate_command_payload(command: EngineCommand) -> None:
    payload = command.payload
    if command.command_type == EngineCommandType.SIGNAL_DELIVER:
        signal = payload.get("signal")
        if not isinstance(signal, dict) or not str(signal.get("id") or "").strip():
            raise ValueError("signal.deliver requires signal object with id")
    elif command.command_type == EngineCommandType.CLOSE_TRADE:
        if not str(payload.get("trade_id") or "").strip():
            raise ValueError("trade.close requires trade_id")
    elif command.command_type == EngineCommandType.EVENT_ACK:
        if not str(payload.get("event_id") or "").strip():
            raise ValueError("event.ack requires event_id")
    elif command.command_type in {
        EngineCommandType.PAUSE,
        EngineCommandType.RESUME,
        EngineCommandType.STOP,
        EngineCommandType.EMERGENCY_STOP,
    }:
        if payload:
            raise ValueError(f"{command.command_type} does not accept a payload")
