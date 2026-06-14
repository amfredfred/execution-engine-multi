"""Broker-side position and account types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class PositionSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Mt5OrderFilling:
    FOK = 0  # mt5.ORDER_FILLING_FOK
    IOC = 1  # mt5.ORDER_FILLING_IOC
    RETURN = 2  # mt5.ORDER_FILLING_RETURN


@dataclass(frozen=True)
class Position:
    """A live position as reported by MT5."""

    ticket: int
    symbol: str
    side: PositionSide
    lots: float
    open_price: float
    current_price: float
    stop_loss: float
    take_profit: float
    swap: float
    profit: float
    open_time: int  # Unix ms
    comment: str
    magic: int


@dataclass(frozen=True)
class AccountInfo:
    login: int
    server: str
    currency: str
    balance: float
    equity: float
    margin: float
    free_margin: float
    margin_level: float
    leverage: int


@dataclass(frozen=True)
class SymbolInfo:
    # Identity
    symbol: str
    description: str
    currency_base: str
    currency_profit: str
    currency_margin: str

    # Price precision
    digits: int
    point: float
    tick_size: float
    tick_value: float

    # Contract
    contract_size: float
    lot_min: float
    lot_max: float
    lot_step: float

    # Current quote
    ask: float
    bid: float
    spread: int
    spread_float: bool

    # Margin & leverage
    margin_initial: float
    margin_maintenance: float
    margin_hedged: float

    # Execution
    filling_mode: int  # bitmask: 1=FOK 2=IOC 4=RETURN
    execution_mode: int  # SYMBOL_TRADE_EXECUTION_*
    trade_mode: int  # SYMBOL_TRADE_MODE_* (disabled/longonly/shortonly/full)

    # Swap
    swap_mode: int
    swap_long: float
    swap_short: float
    swap_rollover3days: int  # day of week triple swap

    # Stops
    stops_level: int  # min distance in points from current price
    freeze_level: int  # freeze distance for pending orders

    # Session
    volume_min: float  # same as lot_min but in units
    volume_max: float
    volume_step: float

    # Optional / not always populated
    expiration_mode: Optional[int] = None  # bitmask of allowed expiration types
    order_mode: Optional[int] = None  # bitmask of allowed order types
    
    @property
    def order_filling_mode(self) -> int:
        if self.filling_mode & 1:
            return Mt5OrderFilling.FOK
        elif self.filling_mode & 2:
            return Mt5OrderFilling.IOC
        else:
            return Mt5OrderFilling.RETURN









