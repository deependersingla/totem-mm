"""All enums — Side, OrderType, OrderStatus, CricketSignal, MarketCategory."""

from __future__ import annotations

from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    GTC = "GTC"
    GTD = "GTD"
    FAK = "FAK"
    FOK = "FOK"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    LIVE = "LIVE"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    MATCHED = "MATCHED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


class MarketCategory(str, Enum):
    SPORTS = "SPORTS"
    CRYPTO = "CRYPTO"
    POLITICS = "POLITICS"
    FINANCE = "FINANCE"
    ECONOMICS = "ECONOMICS"
    OTHER = "OTHER"


class CricketSignal(str, Enum):
    """Ball-by-ball events captured into cricket_events.signal_type.

    Captured values are single chars: "W" (wicket), "0"/"1"/"2"/"4"/"6"
    (runs), or "?" (unknown / parsing failure). UNKNOWN preserves anything
    we did not anticipate so strategies can ignore it without a crash.
    """
    WICKET = "W"
    DOT = "0"
    SINGLE = "1"
    DOUBLE = "2"
    BOUNDARY_4 = "4"
    SIX = "6"
    UNKNOWN = "?"

    @classmethod
    def parse(cls, raw: str) -> "CricketSignal":
        try:
            return cls(raw)
        except ValueError:
            return cls.UNKNOWN
