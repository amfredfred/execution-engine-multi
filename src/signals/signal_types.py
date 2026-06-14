"""
Signal type guards and event classification sets.
"""

from __future__ import annotations

from src.domain.signal_interface import SignalEventName

# Events that trigger a new trade attempt
SIGNAL_TRIGGER_EVENTS: frozenset[str] = frozenset(
    {
        SignalEventName.TRIGGERED.value,
    }
)

# Events that indicate a signal has closed (informational only)
SIGNAL_CLOSE_EVENTS: frozenset[str] = frozenset(
    {
        SignalEventName.TP1_HIT.value,
        SignalEventName.TP2_HIT.value,
        SignalEventName.SL_HIT.value,
        SignalEventName.INVALIDATED.value,
        SignalEventName.EXPIRED.value,
    }
)


def is_valid_signal_dict(obj: object) -> bool:
    """
    Lightweight structural check on a raw payload dict before full parsing.
    """
    if not isinstance(obj, dict):
        return False
    required = {
        "id",
        "symbol",
        "direction",
        "status",
        "entryPrice",
        "stopLoss",
        "tp1",
        "tp2",
        "riskRewardRatio",
        "htfRange",
        "rejectionCandle",
    }
    return required.issubset(obj.keys())









