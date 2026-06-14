"""
SQLite-backed trade repository.

Replaces the JSON-file implementation. Stores every trade — open and
closed — in the `trades` table. Consumers see the same interface as before.

The old JSON files in <storage_path>/trades/ are no longer written.
On first run with the new code, open trades are recovered from MT5 via
the normal broker reconciliation path — no migration needed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List, Optional

from src.domain.trade import (
    CloseReason,
    OrderSide,
    Trade,
    TradePlan,
    TradeStatus,
)

if TYPE_CHECKING:
    from src.infra.db import Database

logger = logging.getLogger(__name__)


class TradeRepository:
    def __init__(self, db: "Database") -> None:
        self._db = db

    def init(self) -> None:
        # Database.init() handles schema creation — nothing extra needed here.
        logger.info("TradeRepository initialised (SQLite)")

    # ── Write ─────────────────────────────────────────────────────────────

    def save(self, trade: Trade) -> bool:
        try:
            self._db.upsert_trade(trade)
            return True
        except Exception:
            logger.exception("TradeRepository: failed to save trade %s", trade.id)
            return False

    def delete(self, trade_id: str) -> None:
        """
        Kept for interface compatibility.
        With SQLite we retain closed trades — this is now a no-op.
        Closed trades remain in the DB with their final status.
        """
        pass

    # ── Read ──────────────────────────────────────────────────────────────

    def load(self, trade_id: str) -> Optional[Trade]:
        rows = self._db.load_all_trades_raw()
        for row in rows:
            if row["id"] == trade_id:
                return self._from_row(row)
        return None

    def load_by_ticket(self, ticket: int) -> Optional[Trade]:
        for trade in self.load_open_trades():
            if trade.entry_ticket == ticket:
                return trade
        return None

    def load_open_trades(self) -> List[Trade]:
        rows = self._db.load_open_trades_raw()
        trades = []
        for row in rows:
            t = self._from_row(row)
            if t:
                trades.append(t)
        return trades

    def load_all(self) -> List[Trade]:
        rows = self._db.load_all_trades_raw()
        trades = []
        for row in rows:
            t = self._from_row(row)
            if t:
                trades.append(t)
        return trades

    def load_closed_trades_since(self, ts_ms: int) -> List[dict]:
        """Raw closed-trade rows for equity-throttle hydration (oldest first)."""
        try:
            return self._db.load_closed_trades_since(ts_ms)
        except Exception:
            logger.exception("TradeRepository: failed to load closed trades")
            return []

    # ── Private ───────────────────────────────────────────────────────────

    @staticmethod
    def _from_row(row: dict) -> Optional[Trade]:
        try:
            import json as _json

            plan_d = {}
            if row.get("plan_json"):
                try:
                    plan_d = _json.loads(row["plan_json"])
                except Exception:
                    pass

            plan = TradePlan(
                signal_id=row.get("signal_id", ""),
                symbol=row["symbol"],
                side=OrderSide(row["side"]),
                # Prefer plan_json originals — the trades.stop_loss column is
                # mutated to breakeven after TP1.
                entry_price=plan_d.get("entryPrice") or row.get("entry_price") or 0.0,
                stop_loss=plan_d.get("stopLoss") or row.get("stop_loss") or 0.0,
                tp1=row.get("tp1") or 0.0,
                tp2=row.get("tp2") or 0.0,
                lot_size=plan_d.get("lotSize", 0.0),
                risk_amount=plan_d.get("riskAmount", 0.0),
                risk_percent=plan_d.get("riskPercent", 0.0),
                risk_reward_ratio=plan_d.get("riskRewardRatio", 0.0),
                planned_at=0,
                signal=None,
                risk_multiplier=plan_d.get("riskMultiplier", 1.0),
            )

            return Trade(
                id=row["id"],
                signal_id=row.get("signal_id", ""),
                symbol=row["symbol"],
                side=OrderSide(row["side"]),
                status=TradeStatus(row["status"]),
                plan=plan,
                entry_ticket=row.get("entry_ticket"),
                entry_price=row.get("entry_price"),
                entry_lots=row.get("entry_lots") or 0.0,
                current_lots=row.get("current_lots") or 0.0,
                stop_loss=row.get("stop_loss") or 0.0,
                tp1=row.get("tp1") or 0.0,
                tp2=row.get("tp2") or 0.0,
                tp1_hit=bool(row.get("tp1_hit")),
                tp1_hit_at=row.get("tp1_hit_at"),
                tp2_hit=bool(row.get("tp2_hit")),
                tp2_hit_at=row.get("tp2_hit_at"),
                sl_hit=bool(row.get("sl_hit")),
                sl_hit_at=row.get("sl_hit_at"),
                opened_at=row.get("opened_at"),
                closed_at=row.get("closed_at"),
                close_reason=(
                    CloseReason(row["close_reason"])
                    if row.get("close_reason")
                    else None
                ),
                close_price=row.get("close_price"),
                realized_pnl=row.get("realized_pnl"),
                realized_rr=row.get("realized_rr"),
                created_at=row.get("created_at") or 0,
                updated_at=row.get("updated_at") or 0,
            )
        except Exception:
            logger.exception(
                "TradeRepository: failed to reconstruct Trade from row id=%s",
                row.get("id"),
            )
            return None








