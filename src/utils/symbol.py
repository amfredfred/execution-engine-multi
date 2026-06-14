"""
Symbol normalisation utilities.

Brokers and signal providers use inconsistent symbol formats:
    EUR/USD  →  EURUSD
    EUR.USD  →  EURUSD
    eur-usd  →  EURUSD
    BTC/USD  →  BTCUSD
    XAU/USD  →  XAUUSD

Always normalise at the boundary (signal ingest, config load) so all
internal components speak a single consistent format that matches MT5.
"""

from __future__ import annotations

import re


def normalise_symbol(symbol: str) -> str:
    """
    Strip separators and uppercase.

    Removes: / . - _ and whitespace
    Examples:
        'EUR/USD' → 'EURUSD'
        'eur.usd' → 'EURUSD'
        'BTC-USD' → 'BTCUSD'
        'XAU_USD' → 'XAUUSD'
        'EURUSD'  → 'EURUSD'  (already clean, no-op)
    """
    return re.sub(r"[/.\-_\s]", "", symbol).upper()









