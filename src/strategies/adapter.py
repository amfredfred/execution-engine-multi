"""
Signal adapter interface + default passthrough.

Extend this to transform signals before they enter the execution pipeline
(e.g. adjust SL for ATR, add session filters, override lot multipliers).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.domain.signal_interface import InboundSignal


class SignalAdapter(ABC):
    @abstractmethod
    def adapt(self, signal: InboundSignal) -> InboundSignal: ...


class PassthroughAdapter(SignalAdapter):
    """Default: return the signal unchanged."""

    def adapt(self, signal: InboundSignal) -> InboundSignal:
        return signal









