"""
Routes signals to the appropriate adapter by symbol or direction.
Falls back to a passthrough adapter if no match is registered.
"""

from __future__ import annotations

import logging
from typing import Dict

from .adapter import PassthroughAdapter, SignalAdapter
from src.domain.signal_interface import InboundSignal

logger = logging.getLogger(__name__)


class StrategyRouter:
    def __init__(self) -> None:
        self._adapters: Dict[str, SignalAdapter] = {}
        self._fallback: SignalAdapter = PassthroughAdapter()

    def register(self, key: str, adapter: SignalAdapter) -> None:
        """Register an adapter by symbol (e.g. "EUR/USD") or direction."""
        self._adapters[key] = adapter
        logger.info("StrategyRouter: registered adapter", extra={"key": key})

    def route(self, signal: InboundSignal) -> InboundSignal:
        adapter = (
            self._adapters.get(signal.resolved_symbol)
            or self._adapters.get(signal.direction.value)
            or self._fallback
        )
        adapted = adapter.adapt(signal)
        if adapted is not signal:
            logger.debug(
                "StrategyRouter: signal adapted",
                extra={"signal_id": signal.id, "adapter": type(adapter).__name__},
            )
        return adapted
