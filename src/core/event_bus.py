"""
Synchronous, typed event bus.

Wraps a simple listener registry.  All listeners on a given event are called
in registration order.  Exceptions in listeners are caught and logged so one
bad handler cannot break the pipeline.

For async-heavy use-cases, swap the `emit` implementation for asyncio-aware
dispatch without changing the public API.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Callable, Dict, List

logger = logging.getLogger("EventBus")
Listener = Callable[..., None]


class EventBus:
    def __init__(self) -> None:
        self._listeners: Dict[str, List[Listener]] = defaultdict(list)
        self._wildcard: List[Callable[[str, Any], None]] = []

    # ── Registration ──────────────────────────────────────────────────────

    def on(self, event: str, listener: Listener) -> None:
        """Subscribe *listener* to *event*."""
        self._listeners[event].append(listener)

    def once(self, event: str, listener: Listener) -> None:
        """Subscribe *listener* to fire once, then auto-remove."""

        def _wrapper(payload: Any) -> None:
            listener(payload)
            self._listeners[event].remove(_wrapper)

        self._listeners[event].append(_wrapper)

    def off(self, event: str, listener: Listener) -> None:
        """Unsubscribe *listener* from *event* (no-op if not found)."""
        try:
            self._listeners[event].remove(listener)
        except ValueError:
            pass

    def on_any(self, listener: Callable[[str, Any], None]) -> None:
        """Subscribe to ALL events — useful for audit logging and metrics."""
        self._wildcard.append(listener)

    # ── Emission ──────────────────────────────────────────────────────────

    def emit(self, event: str, payload: Any = None) -> None:
        logger.debug("emit %s", event)

        for listener in list(self._listeners[event]):
            try:
                listener(payload)
            except Exception:
                logger.exception("Listener error on event '%s'", event)

        for wildcard in self._wildcard:
            try:
                wildcard(event, payload)
            except Exception:
                logger.exception("Wildcard listener error on event '%s'", event)

    def remove_all(self, event: str | None = None) -> None:
        if event:
            self._listeners.pop(event, None)
        else:
            self._listeners.clear()
            self._wildcard.clear()









