"""
Thread-safe in-memory store for Trade objects.

All mutations return a fresh copy so callers never hold stale references.
MT5 is the source of truth — no disk persistence needed.
"""

from __future__ import annotations

import copy
import threading
from typing import Dict, List, Optional

from src.domain.trade import Trade, TradeStatus
from src.utils.time import now_ms


class PositionStore:
    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._trades: Dict[str, Trade] = {}

    # ── Write ─────────────────────────────────────────────────────────────

    def add(self, trade: Trade) -> None:
        with self._lock:
            self._trades[trade.id] = copy.deepcopy(trade)

    def update(self, trade_id: str, **kwargs) -> Optional[Trade]:
        """Apply keyword-argument patches to a trade and return the updated copy."""
        with self._lock:
            trade = self._trades.get(trade_id)
            if trade is None:
                return None
            for key, val in kwargs.items():
                setattr(trade, key, val)
            trade.updated_at = now_ms()
            self._trades[trade_id] = trade
            return copy.deepcopy(trade)

    def remove(self, trade_id: str) -> None:
        """Remove a trade from the store. Called when a trade closes."""
        with self._lock:
            self._trades.pop(trade_id, None)

    def hydrate(self, trades: List[Trade]) -> None:
        """Bulk-load on startup from persistent storage."""
        with self._lock:
            for t in trades:
                self._trades[t.id] = copy.deepcopy(t)

    # ── Read ──────────────────────────────────────────────────────────────

    def get(self, trade_id: str) -> Optional[Trade]:
        with self._lock:
            t = self._trades.get(trade_id)
            return copy.deepcopy(t) if t else None

    def get_by_signal_id(self, signal_id: str) -> Optional[Trade]:
        with self._lock:
            for t in self._trades.values():
                if t.signal_id == signal_id:
                    return copy.copy(t)
            return None

    def get_by_ticket(self, ticket: int) -> Optional[Trade]:
        with self._lock:
            for t in self._trades.values():
                if t.entry_ticket == ticket:
                    return copy.copy(t)
            return None

    def get_open_trades(self) -> List[Trade]:
        with self._lock:
            return [
                copy.copy(t)
                for t in self._trades.values()
                if t.status in (TradeStatus.OPEN, TradeStatus.PARTIALLY_CLOSED)
            ]

    def get_all(self) -> List[Trade]:
        with self._lock:
            return [copy.copy(t) for t in self._trades.values()]

    def size(self) -> int:
        with self._lock:
            return len(self._trades)









