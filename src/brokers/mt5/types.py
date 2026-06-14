"""
MT5 order type constants and result containers.

Using the MetaTrader5 Python package constants directly would couple the
rest of the code to MT5.  These thin wrappers keep broker details isolated.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


# ── MT5 order type codes ──────────────────────────────────────────────────────
# Matches MetaTrader5.ORDER_TYPE_* values
class Mt5OrderType(IntEnum):
    BUY             = 0
    SELL            = 1
    BUY_LIMIT       = 2
    SELL_LIMIT      = 3
    BUY_STOP        = 4
    SELL_STOP       = 5

# MT5 position type codes
class Mt5PositionType(IntEnum):
    BUY  = 0
    SELL = 1

# MT5 trade action codes
class Mt5TradeAction(IntEnum):
    DEAL    = 1   # market order
    PENDING = 5   # pending order
    SLTP    = 6   # modify SL/TP
    CLOSE_BY= 10  # close by opposite

# MT5 return codes
MT5_RETCODE_DONE    = 10009
MT5_RETCODE_PLACED  = 10008
MT5_RETCODE_INVALID_STOPS = 10016

@dataclass(frozen=True)
class OrderResult:
    """Returned by mt5_orders after a successful order execution."""
    ticket:         int
    executed_price: float
    volume:         float
    retcode:        int
    comment:        str


@dataclass(frozen=True)
class ModifyResult:
    retcode: int
    comment: str









