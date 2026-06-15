from dataclasses import replace

from src.domain.signal_interface import InboundSignal
from src.signals.queue import SignalQueue
from tests.unit.manager.test_signal_routing import _signal_payload


def test_queue_accepts_distinct_same_symbol_signals() -> None:
    queue = SignalQueue(lambda _: None)
    first = replace(
        InboundSignal.from_dict(_signal_payload()),
        resolved_symbol="XAUUSD",
    )
    second = replace(first, id="signal-route-002")

    queue.put(first)
    queue.put(second)

    assert queue.depth() == 2


def test_queue_deduplicates_exact_signal_id() -> None:
    queue = SignalQueue(lambda _: None)
    signal = replace(
        InboundSignal.from_dict(_signal_payload()),
        resolved_symbol="XAUUSD",
    )

    queue.put(signal)
    queue.put(signal)

    assert queue.depth() == 1
