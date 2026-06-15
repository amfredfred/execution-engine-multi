"""Typed command and event envelopes shared by manager and engine workers."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4


class EngineCommandType(StrEnum):
    SIGNAL_DELIVER = "signal.deliver"
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
