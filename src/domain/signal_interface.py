"""
Inbound signal types — mirror of the Signal Engine's TradeSignal payload.

These dataclasses represent the data that arrives over the WebSocket from
the Signal Engine.  They are the boundary types: nothing outside the
`signals/` package should depend on raw dicts.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional
from src.utils.symbol import normalise_symbol
from src.utils.time import now_ms


# ── Enums ──────────────────────────────────────────────────────────────────────


class SignalDirection(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class SignalStatus(str, Enum):
    PENDING = "PENDING"
    TRIGGERED = "TRIGGERED"
    TP1_HIT = "TP1_HIT"
    TP2_HIT = "TP2_HIT"
    SL_HIT = "SL_HIT"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


class BosDirection(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


class CandlePattern(str, Enum):
    SHOOTING_STAR = "SHOOTING_STAR"
    HAMMER = "HAMMER"
    CRT_BUY = "CRT_BUY"
    CRT_SELL = "CRT_SELL"


class SignalEventName(str, Enum):
    PENDING = "signal.pending"
    TRIGGERED = "signal.triggered"
    TP1_HIT = "signal.tp1_hit"
    TP2_HIT = "signal.tp2_hit"
    SL_HIT = "signal.sl_hit"
    INVALIDATED = "signal.invalidated"
    EXPIRED = "signal.expired"
    UPDATED = "signal.updated"


# ── Sub-structures ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HtfRange:
    range_high: float
    range_low: float
    bos_direction: BosDirection
    timestamp: int
    broken_at: int
    tp_level: float
    midpoint: float
    height: float
    htf_candle_open: int
    htf_candle_close: int

    @classmethod
    def from_dict(cls, d: dict) -> HtfRange:
        return cls(
            range_high=d["rangeHigh"],
            range_low=d["rangeLow"],
            bos_direction=BosDirection(d["bosDirection"]),
            timestamp=d["timestamp"],
            broken_at=d.get("brokenAt") or 0,
            tp_level=d.get("tpLevel") or 0.0,
            midpoint=d.get("midpoint") or (d["rangeHigh"] + d["rangeLow"]) / 2,
            height=d.get("height") or (d["rangeHigh"] - d["rangeLow"]),
            htf_candle_open=d.get("htfCandleOpen") or 0,
            htf_candle_close=d.get("htfCandleClose") or 0,
        )


@dataclass(frozen=True)
class LtfRange:
    range_high: float
    range_low: float
    timestamp: int
    direction: SignalDirection
    sl_level: float

    @classmethod
    def from_dict(cls, d: dict) -> LtfRange:
        return cls(
            range_high=d["rangeHigh"],
            range_low=d["rangeLow"],
            timestamp=d["timestamp"],
            direction=SignalDirection(d["direction"]),
            sl_level=d["slLevel"],
        )


@dataclass(frozen=True)
class RejectionCandle:
    open: float
    high: float
    low: float
    close: float
    timestamp: int
    wick_ratio: float
    pattern: CandlePattern
    wick_tip: float

    @classmethod
    def from_dict(cls, d: dict) -> RejectionCandle:
        return cls(
            open=d["open"],
            high=d["high"],
            low=d["low"],
            close=d["close"],
            timestamp=d["timestamp"],
            wick_ratio=d["wickRatio"],
            pattern=CandlePattern(d["pattern"]),
            wick_tip=d["wickTip"],
        )


# ── Top-level signal ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InboundSignal:
    """
    A fully-deserialised signal from the Signal Engine.
    Immutable so it can safely be passed between components.
    """

    id: str
    symbol: str
    direction: SignalDirection
    status: SignalStatus
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    risk_reward_ratio: float
    risk_pips: float
    htf_range: HtfRange
    rejection_candle: RejectionCandle
    created_at: int
    htf_interval: str = ""
    ltf_interval: str = ""
    broker: str = ""
    ltf_range: Optional[LtfRange] = None
    pending_at: Optional[int] = None
    triggered_at: Optional[int] = None
    tp1_hit_at: Optional[int] = None
    tp2_hit_at: Optional[int] = None
    sl_hit_at: Optional[int] = None
    invalidated_at: Optional[int] = None
    expired_at: Optional[int] = None
    closed_at: Optional[int] = None
    outcome: Optional[str] = None
    realized_rr: Optional[float] = None
    close_price: Optional[float] = None
    resolved_symbol: Optional[str] = None  # broker-resolved symbol, set at consumer level
    setup_candle_open_at: Optional[int] = None
    setup_candle_close_at: Optional[int] = None
    detected_at: Optional[int] = None
    emitted_at: Optional[int] = None
    received_at: Optional[int] = None
    queued_at: Optional[int] = None
    execution_started_at: Optional[int] = None
    order_sent_at: Optional[int] = None
    order_filled_at: Optional[int] = None
    trade_opened_at: Optional[int] = None

    @classmethod
    def from_dict(cls, d: dict) -> InboundSignal:
        received_at = _optional_int(d, "receivedAt", "received_at") or now_ms()
        emitted_at = _optional_int(d, "emittedAt", "emitted_at") or received_at
        detected_at = (
            _optional_int(d, "detectedAt", "detected_at", "triggeredAt", "triggered_at")
            or emitted_at
        )
        rejection = d.get("rejectionCandle") or d.get("rejection_candle") or {}
        setup_candle_open_at = (
            _optional_int(d, "setupCandleOpenAt", "setup_candle_open_at")
            or _optional_int(rejection, "timestamp")
        )
        setup_candle_close_at = _optional_int(
            d,
            "setupCandleCloseAt",
            "setup_candle_close_at",
            "rejectionCandleCloseAt",
            "rejection_candle_close_at",
        ) or _optional_int(rejection, "closeAt", "close_at")
        triggered_at = _optional_int(d, "triggeredAt", "triggered_at")
        if setup_candle_close_at is None:
            setup_candle_close_at = triggered_at
        return cls(
            id=d["id"],
            symbol=normalise_symbol(d["symbol"]),
            direction=SignalDirection(d["direction"]),
            status=SignalStatus(d["status"]),
            entry_price=d["entryPrice"],
            stop_loss=d["stopLoss"],
            tp1=d["tp1"],
            tp2=d["tp2"],
            risk_reward_ratio=d["riskRewardRatio"],
            risk_pips=d["riskPips"],
            htf_range=HtfRange.from_dict(d["htfRange"]),
            rejection_candle=RejectionCandle.from_dict(d["rejectionCandle"]),
            created_at=d["createdAt"],
            htf_interval=str(d.get("htfInterval") or d.get("htf_interval") or ""),
            ltf_interval=str(d.get("ltfInterval") or d.get("ltf_interval") or ""),
            broker=str(d.get("broker") or ""),
            ltf_range=LtfRange.from_dict(d["ltfRange"]) if d.get("ltfRange") else None,
            pending_at=d.get("pendingAt"),
            triggered_at=triggered_at,
            tp1_hit_at=d.get("tp1HitAt"),
            tp2_hit_at=d.get("tp2HitAt"),
            sl_hit_at=d.get("slHitAt"),
            invalidated_at=d.get("invalidatedAt"),
            expired_at=d.get("expiredAt"),
            closed_at=d.get("closedAt"),
            outcome=d.get("outcome"),
            realized_rr=d.get("realizedRR"),
            close_price=d.get("closePrice"),
            setup_candle_open_at=setup_candle_open_at,
            setup_candle_close_at=setup_candle_close_at,
            detected_at=detected_at,
            emitted_at=emitted_at,
            received_at=received_at,
            queued_at=_optional_int(d, "queuedAt", "queued_at"),
            execution_started_at=_optional_int(d, "executionStartedAt", "execution_started_at"),
            order_sent_at=_optional_int(d, "orderSentAt", "order_sent_at"),
            order_filled_at=_optional_int(d, "orderFilledAt", "order_filled_at"),
            trade_opened_at=_optional_int(d, "tradeOpenedAt", "trade_opened_at"),
        )

    def to_dict(self) -> dict:
        """Serialize using the legacy signal.triggered wire contract."""
        result = {
            "id": self.id,
            "symbol": self.symbol,
            "direction": self.direction.value,
            "status": self.status.value,
            "entryPrice": self.entry_price,
            "stopLoss": self.stop_loss,
            "tp1": self.tp1,
            "tp2": self.tp2,
            "riskRewardRatio": self.risk_reward_ratio,
            "riskPips": self.risk_pips,
            "htfInterval": self.htf_interval,
            "ltfInterval": self.ltf_interval,
            "broker": self.broker,
            "htfRange": {
                "rangeHigh": self.htf_range.range_high,
                "rangeLow": self.htf_range.range_low,
                "bosDirection": self.htf_range.bos_direction.value,
                "timestamp": self.htf_range.timestamp,
                "brokenAt": self.htf_range.broken_at,
                "tpLevel": self.htf_range.tp_level,
                "midpoint": self.htf_range.midpoint,
                "height": self.htf_range.height,
                "htfCandleOpen": self.htf_range.htf_candle_open,
                "htfCandleClose": self.htf_range.htf_candle_close,
            },
            "rejectionCandle": {
                "open": self.rejection_candle.open,
                "high": self.rejection_candle.high,
                "low": self.rejection_candle.low,
                "close": self.rejection_candle.close,
                "timestamp": self.rejection_candle.timestamp,
                "wickRatio": self.rejection_candle.wick_ratio,
                "pattern": self.rejection_candle.pattern.value,
                "wickTip": self.rejection_candle.wick_tip,
            },
            "createdAt": self.created_at,
            "pendingAt": self.pending_at,
            "triggeredAt": self.triggered_at,
            "tp1HitAt": self.tp1_hit_at,
            "tp2HitAt": self.tp2_hit_at,
            "slHitAt": self.sl_hit_at,
            "invalidatedAt": self.invalidated_at,
            "expiredAt": self.expired_at,
            "closedAt": self.closed_at,
            "outcome": self.outcome,
            "realizedRR": self.realized_rr,
            "closePrice": self.close_price,
            "setupCandleOpenAt": self.setup_candle_open_at,
            "setupCandleCloseAt": self.setup_candle_close_at,
            "detectedAt": self.detected_at,
            "emittedAt": self.emitted_at,
            "receivedAt": self.received_at,
            "queuedAt": self.queued_at,
            "executionStartedAt": self.execution_started_at,
            "orderSentAt": self.order_sent_at,
            "orderFilledAt": self.order_filled_at,
            "tradeOpenedAt": self.trade_opened_at,
        }
        if self.ltf_range:
            result["ltfRange"] = {
                "rangeHigh": self.ltf_range.range_high,
                "rangeLow": self.ltf_range.range_low,
                "timestamp": self.ltf_range.timestamp,
                "direction": self.ltf_range.direction.value,
                "slLevel": self.ltf_range.sl_level,
            }
        return result


def _optional_int(d: dict, *keys: str) -> Optional[int]:
    for key in keys:
        value = d.get(key)
        if value is None:
            continue
        return int(value)
    return None



