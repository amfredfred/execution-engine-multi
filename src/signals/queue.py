"""
Signal queue — decouples the WebSocket ingestion thread from execution.

The WebSocket thread puts signals onto the queue and returns immediately.
A single worker thread drains the queue one signal at a time, guaranteeing
sequential execution and making re-entrance physically impossible.

Deduplication:
    If a signal for the same symbol is already waiting in the queue, the
    new signal is dropped. There is no value in executing a stale signal
    for a symbol that already has a pending one — the market has moved.

Queue depth:
    Bounded to MAX_QUEUE_SIZE (default 50). If the queue is full, incoming
    signals are dropped with a warning. This prevents unbounded memory growth
    under a flood of signals.
"""

from __future__ import annotations

from dataclasses import replace
import logging
import queue
import threading
from typing import Callable, Optional, Set

from src.domain.signal_interface import InboundSignal
from src.utils.time import now_ms

logger = logging.getLogger(__name__)

MAX_QUEUE_SIZE = 50


class SignalQueue:
    """
    Thread-safe signal queue with per-symbol deduplication.

    Usage:
        sq = SignalQueue(on_signal=execution_engine.execute)
        sq.start()
        sq.put(signal)   # called from WebSocket thread — non-blocking
        sq.stop()
    """

    def __init__(self, on_signal: Callable[[InboundSignal], None]) -> None:
        self._on_signal: Callable[[InboundSignal], None] = on_signal
        self._queue: queue.Queue[InboundSignal] = queue.Queue(maxsize=MAX_QUEUE_SIZE)
        self._queued_symbols: Set[str] = set()
        self._symbols_lock: threading.Lock = threading.Lock()
        self._stopped: threading.Event = threading.Event()
        self._paused: threading.Event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        self._stopped.clear()
        self._thread = threading.Thread(
            target=self._worker,
            name="signal-queue-worker",
            daemon=True,
        )
        self._thread.start()
        logger.info("SignalQueue worker started")

    def stop(self) -> None:
        self._stopped.set()
        # Unblock the worker if it's waiting on an empty queue
        try:
            self._queue.put_nowait(None)  # type: ignore[arg-type]  — sentinel
        except queue.Full:
            pass
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("SignalQueue worker stopped")

    # ── Producer (WebSocket thread) ───────────────────────────────────────

    def put(self, signal: InboundSignal) -> None:
        """
        Enqueue a signal. Non-blocking — never stalls the WebSocket thread.

        Drops the signal if:
          - Same symbol already queued (deduplication)
          - Queue is full (flood protection)
        """
        with self._symbols_lock:
            signal = replace(signal, queued_at=signal.queued_at or now_ms())
            if signal.resolved_symbol in self._queued_symbols:
                logger.debug(
                    "SignalQueue: dropped — symbol already queued",
                    extra={"signal_id": signal.id, "symbol": signal.resolved_symbol},
                )
                return

            try:
                self._queue.put_nowait(signal)
                self._queued_symbols.add(signal.resolved_symbol)
                logger.debug(
                    "SignalQueue: enqueued",
                    extra={
                        "signal_id": signal.id,
                        "symbol": signal.resolved_symbol,
                        "depth": self._queue.qsize(),
                    },
                )
            except queue.Full:
                logger.warning(
                    "SignalQueue: queue full — signal dropped",
                    extra={"signal_id": signal.id, "symbol": signal.resolved_symbol},
                )

    def pause(self) -> None:
        self._paused.set()
        logger.info("SignalQueue paused")

    def resume(self) -> None:
        self._paused.clear()
        logger.info("SignalQueue resumed")

    def is_paused(self) -> bool:
        return self._paused.is_set()

    def depth(self) -> int:
        return self._queue.qsize()

    # ── Consumer (worker thread) ──────────────────────────────────────────

    def _worker(self) -> None:
        logger.info("SignalQueue worker running")
        while not self._stopped.is_set():
            try:
                signal = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if signal is None:  # sentinel — stop requested
                break

            # Block here while paused — signal stays consumed from queue but we
            # wait before executing it, so the queue doesn't fill up.
            while self._paused.is_set() and not self._stopped.is_set():
                self._paused.wait(timeout=0.5)

            if self._stopped.is_set():
                break

            try:
                self._on_signal(signal)
            except Exception:
                logger.exception(
                    "SignalQueue: unhandled error processing signal",
                    extra={"signal_id": signal.id, "symbol": signal.resolved_symbol},
                )
            finally:
                # Release symbol reservation only after execution completes so a
                # second signal for the same symbol cannot enter the queue while
                # the first one is still being executed.
                with self._symbols_lock:
                    self._queued_symbols.discard(signal.resolved_symbol)
                self._queue.task_done()
