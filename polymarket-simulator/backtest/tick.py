"""Tick-size inference from observed book prices.

Polymarket tick size is a property of the market and changes mid-match. Rather
than hardcode it, we infer it from the most recent book snapshot:

    if any visible price has 4 decimal places  → tick = 0.0001
    if any visible price has 3 decimal places  → tick = 0.001
    if any visible price has 2 decimal places  → tick = 0.01
    otherwise                                  → tick = 0.1

A coarser tick implies prices that are also valid under finer ticks (every
multiple of 0.01 is also a multiple of 0.001), so we bind the inference to
the *finest* precision we have actually observed.
"""

from __future__ import annotations

from typing import Iterable

from .config import PRICE_EPS, VALID_TICKS


def _decimal_places(price: float) -> int:
    """Number of significant decimal places. Conservative: trims trailing
    zeros so 0.50 returns 1, 0.5234 returns 4. Capped at 4 (Polymarket max).
    """
    if not (0 < price < 1):
        return 0
    # Six decimal places is enough resolution to distinguish all valid
    # Polymarket ticks (finest is 0.0001 = 4 dp). Pad and rstrip zeros.
    s = f"{price:.6f}".rstrip("0").rstrip(".")
    if "." not in s:
        return 0
    return min(len(s.split(".", 1)[1]), 4)


def infer_tick_from_prices(prices: Iterable[float]) -> float:
    """Return the finest Polymarket tick consistent with the given prices.

    `prices` is typically every visible level (bid + ask) from one snapshot.
    Empty input returns the coarsest tick (0.1) as a permissive default.
    """
    max_dp = 0
    for p in prices:
        dp = _decimal_places(p)
        if dp > max_dp:
            max_dp = dp

    if max_dp >= 4:
        return 0.0001
    if max_dp == 3:
        return 0.001
    if max_dp == 2:
        return 0.01
    return 0.1


def is_multiple_of_tick(price: float, tick: float) -> bool:
    """True if `price` is on the tick grid (within float tolerance)."""
    if tick not in VALID_TICKS:
        raise ValueError(f"invalid tick size {tick!r}; must be one of {VALID_TICKS}")
    ratio = price / tick
    return abs(ratio - round(ratio)) < PRICE_EPS / tick
