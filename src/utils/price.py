"""Price and pip utility functions."""

from __future__ import annotations

import math


def pip_size(point: float, digits: int) -> float:
    """
    Return the true pip size from broker-provided symbol metadata.

    Brokers sometimes quote prices with sub-pip precision (an extra decimal
    place), which shifts the pip one digit to the left:

        digits=5  →  0.00001 point  →  0.0001 pip  (standard forex)
        digits=3  →  0.001   point  →  0.01   pip  (JPY pairs)
        digits=4  →  0.0001  point  →  0.0001 pip  (4-digit forex)
        digits=2  →  0.01    point  →  0.01   pip  (JPY / metals)

    Odd digit counts (5, 3) indicate sub-pip quoting — multiply point by 10
    to step back to the conventional pip. Even digit counts are already at
    pip precision, so point is returned as-is.
    """
    if digits in (5, 3):
        return point * 10
    return point


def price_to_pips(price_diff: float, point: float, digits: int) -> float:
    """Convert an absolute price difference to pips."""
    return abs(price_diff) / pip_size(point, digits)


def pips_to_price(pips: float, point: float, digits: int) -> float:
    """Convert a pip count to a price difference."""
    return pips * pip_size(point, digits)


def round_price(price: float, digits: int) -> float:
    """Round a price to the broker's required decimal places."""
    factor = 10**digits
    return math.floor(price * factor + 0.5) / factor


def normalise_lots(
    lots: float,
    lot_step: float,
    lot_min: float,
    lot_max: float,
) -> float:
    """Snap *lots* to the nearest valid lot step and clamp to [lot_min, lot_max]."""
    stepped = math.floor(lots / lot_step) * lot_step
    clamped = max(lot_min, min(stepped, lot_max))
    return round(clamped, 2)


def pip_distance(a: float, b: float, point: float, digits: int) -> float:
    """Absolute pip distance between two prices."""
    return price_to_pips(abs(a - b), point, digits)








