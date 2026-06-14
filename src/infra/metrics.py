"""
In-process metrics collector.

Tracks counters and gauges. Thread-safe via a threading.Lock.
Persists to SQLite every FLUSH_INTERVAL_SEC seconds and restores
on startup so counters survive engine restarts.

Replace with prometheus_client for production scraping.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import TYPE_CHECKING, Dict, Optional

if TYPE_CHECKING:
    from src.infra.db import Database

logger = logging.getLogger(__name__)

FLUSH_INTERVAL_SEC = 30


class Metrics:
    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._counters: Dict[str, int] = defaultdict(int)
        self._gauges: Dict[str, float] = {}
        self._db: Optional["Database"] = None
        self._flush_timer: Optional[threading.Timer] = None

    # ── DB wiring ─────────────────────────────────────────────────────────

    def init_db(self, db: "Database") -> None:
        """
        Call once from bootstrap after Database.init().
        Restores persisted counters/gauges then starts the periodic flush.
        """
        self._db = db
        self._restore()
        self._schedule_flush()
        logger.info("Metrics DB persistence enabled")

    def _restore(self) -> None:
        if not self._db:
            return
        try:
            counters, gauges = self._db.load_metrics()
            with self._lock:
                for k, v in counters.items():
                    self._counters[k] = v
                self._gauges.update(gauges)
            logger.info(
                "Metrics restored from DB",
                extra={"counters": len(counters), "gauges": len(gauges)},
            )
        except Exception:
            logger.exception("Metrics: failed to restore from DB")

    def flush(self) -> None:
        """Persist current snapshot to DB. Called periodically and on shutdown."""
        if not self._db:
            return
        try:
            snap = self.snapshot()
            self._db.save_metrics(snap["counters"], snap["gauges"])
        except Exception:
            logger.exception("Metrics: failed to flush to DB")

    def _schedule_flush(self) -> None:
        self._flush_timer = threading.Timer(FLUSH_INTERVAL_SEC, self._tick)
        self._flush_timer.daemon = True
        self._flush_timer.start()

    def _tick(self) -> None:
        self.flush()
        self._schedule_flush()  # reschedule

    def stop(self) -> None:
        """Cancel the flush timer and do a final flush. Call from bootstrap shutdown."""
        if self._flush_timer:
            self._flush_timer.cancel()
        self.flush()

    # ── Core API (unchanged) ──────────────────────────────────────────────

    def increment(self, name: str, by: int = 1) -> None:
        with self._lock:
            self._counters[name] += by

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = value

    def counter(self, name: str) -> int:
        with self._lock:
            return self._counters[name]

    def gauge(self, name: str) -> float:
        with self._lock:
            return self._gauges.get(name, 0.0)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
            }

    def log_snapshot(self) -> None:
        logger.info("Metrics snapshot", extra=self.snapshot())


metrics = Metrics()









