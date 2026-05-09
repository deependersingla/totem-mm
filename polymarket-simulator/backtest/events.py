"""Engine → strategy events.

Every event has `ts_ms` so dispatch order is unambiguous. `Event` is the
union type the engine passes to a strategy's `on_event`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Union

from .book import BookSnapshot
from .enums import CricketSignal, Side
from .orders import Fill


@dataclass
class BookEvent:
    snapshot: BookSnapshot

    @property
    def ts_ms(self) -> int:
        return self.snapshot.ts_ms

    @property
    def token_id(self) -> str:
        return self.snapshot.token_id


@dataclass
class TradeEvent:
    """A real taker trade observed on the CLOB. `side` is the aggressor side."""
    token_id: str
    ts_ms: int
    side: Side
    price: float
    size_shares: float
    taker_wallet: str = ""
    tx_hash: str = ""
    captured_fee_rate_bps: Optional[int] = None  # if present, authoritative


@dataclass
class CricketEvent:
    ts_ms: int
    signal: CricketSignal
    runs: Optional[int] = None
    wickets: Optional[int] = None
    overs: str = ""
    score_str: str = ""
    innings: Optional[int] = None


@dataclass
class FillEvent:
    fill: Fill

    @property
    def ts_ms(self) -> int:
        return self.fill.ts_ms


@dataclass
class AckEvent:
    order_id: str
    ts_ms: int


@dataclass
class RejectEvent:
    order_id: str
    ts_ms: int
    reason: str


@dataclass
class CancelEvent:
    order_id: str
    ts_ms: int
    reason: str = ""   # "user" | "expiration" | "immediate"


Event = Union[
    BookEvent, TradeEvent, CricketEvent,
    FillEvent, AckEvent, RejectEvent, CancelEvent,
]
