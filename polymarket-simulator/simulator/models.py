"""Data models — exact Polymarket order types and lifecycle states.

Order types per Polymarket docs:
  GTC  — Good-Til-Cancelled: rests on book until filled or cancelled
  GTD  — Good-Til-Date: rests until expiration timestamp, filled, or cancelled
  FOK  — Fill-Or-Kill: must fill entirely and immediately, or cancel whole order
  FAK  — Fill-And-Kill: fill what's available immediately, cancel unfilled remainder

Order statuses per Polymarket CLOB:
  LIVE      — resting on the book
  MATCHED   — matched, sent to executor for on-chain submission
  CANCELED  — cancelled by user, expiration, or system
  DELAYED   — marketable order in sports market, 3s matching delay

Trade states (post-match):
  MATCHED   → MINED → CONFIRMED (success)
  MATCHED   → RETRYING → FAILED (permanent failure)
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    GTC = "GTC"   # Good-Til-Cancelled — rests on book
    GTD = "GTD"   # Good-Til-Date — rests until expiration
    FOK = "FOK"   # Fill-Or-Kill — all or nothing, immediate
    FAK = "FAK"   # Fill-And-Kill — partial fill ok, immediate


class OrderStatus(str, Enum):
    LIVE = "LIVE"                   # resting on the book
    MATCHED = "MATCHED"             # fully filled
    PARTIALLY_FILLED = "PARTIALLY_FILLED"  # GTC/GTD partial (still resting)
    CANCELED = "CANCELED"           # cancelled / expired / killed
    DELAYED = "DELAYED"             # sports market 3s delay


class TradeStatus(str, Enum):
    MATCHED = "MATCHED"       # trade sent to executor
    MINED = "MINED"           # tx included in block
    CONFIRMED = "CONFIRMED"   # polygon finality
    RETRYING = "RETRYING"     # on-chain tx failed, retrying
    FAILED = "FAILED"         # permanent failure


class PriceLevel(BaseModel):
    price: float
    size: float


class OrderBookSnapshot(BaseModel):
    token_id: str
    bids: list[PriceLevel] = []   # sorted descending by price
    asks: list[PriceLevel] = []   # sorted ascending by price
    timestamp_ms: int = 0

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid_price(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return self.best_bid or self.best_ask

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None


class OrderEvent(BaseModel):
    """A single state change in an order's lifecycle — for full timeline tracking."""
    event_type: str       # PLACEMENT, FILL, PARTIAL_FILL, CANCELLATION, EXPIRATION, QUEUE_UPDATE
    status: OrderStatus
    timestamp: float = Field(default_factory=time.time)
    detail: str = ""      # human-readable description
    fill_price: Optional[float] = None
    fill_size: Optional[float] = None
    queue_ahead: Optional[float] = None


class SimOrder(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    token_id: str
    token_name: str = ""
    side: Side
    order_type: OrderType
    price: Optional[float] = None     # None only for FOK/FAK market orders
    size: float
    filled_size: float = 0.0
    avg_fill_price: float = 0.0
    status: OrderStatus = OrderStatus.LIVE
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    expiration: Optional[float] = None  # GTD expiration (unix timestamp)

    # Full order timeline — every state change
    timeline: list[OrderEvent] = []

    @property
    def remaining_size(self) -> float:
        return self.size - self.filled_size

    def add_event(self, event_type: str, status: OrderStatus, detail: str = "",
                  fill_price: float = None, fill_size: float = None,
                  queue_ahead: float = None):
        self.timeline.append(OrderEvent(
            event_type=event_type,
            status=status,
            detail=detail,
            fill_price=fill_price,
            fill_size=fill_size,
            queue_ahead=queue_ahead,
        ))
        self.status = status
        self.updated_at = time.time()


class SimFill(BaseModel):
    order_id: str
    token_id: str
    token_name: str = ""
    side: Side
    price: float
    size: float
    notional: float = 0.0
    timestamp: float = Field(default_factory=time.time)
    trade_status: TradeStatus = TradeStatus.CONFIRMED  # simulated = instant confirm

    def model_post_init(self, __context):
        if self.notional == 0.0:
            self.notional = self.price * self.size


class SnipeEvent(BaseModel):
    token_id: str
    token_name: str = ""
    side: str
    price: float
    size_appeared: float
    size_disappeared: float
    duration_ms: float
    timestamp: float = Field(default_factory=time.time)


class MarketInfo(BaseModel):
    condition_id: str = ""
    question: str = ""
    slug: str = ""
    token_ids: list[str] = []
    outcome_names: list[str] = []
    active: bool = True
    image: str = ""

    @property
    def token_to_name(self) -> dict[str, str]:
        return dict(zip(self.token_ids, self.outcome_names))


class MarketSearchResult(BaseModel):
    slug: str
    question: str
    active: bool
    volume: float = 0.0
    liquidity: float = 0.0
    outcomes: list[str] = []
