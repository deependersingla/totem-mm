"""Order types — three constructors, no boolean flags.

LimitOrder        — GTC or GTD; rests on book until matched/cancelled/expired.
MarketBuyOrder    — FAK or FOK; size is USDC notional (Polymarket convention).
MarketSellOrder   — FAK or FOK; size is share count.

Every order has a stable engine-assigned id, a status, and fill accumulators
shared across the three flavours. Strategies build them via the StrategyContext
helpers (`submit_limit`, `submit_market_buy`, `submit_market_sell`), not by
constructing these dataclasses directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Union

from .enums import OrderStatus, OrderType, Side


@dataclass
class _OrderState:
    """Mutable lifecycle state shared by all order kinds."""
    id: str = ""
    token_id: str = ""
    status: OrderStatus = OrderStatus.PENDING
    created_at_ms: int = 0
    updated_at_ms: int = 0
    filled_size: float = 0.0          # always in shares
    filled_notional: float = 0.0
    client_tag: str = ""

    @property
    def avg_fill_price(self) -> float:
        return self.filled_notional / self.filled_size if self.filled_size > 0 else 0.0


@dataclass
class LimitOrder(_OrderState):
    """Resting limit order: GTC or GTD."""
    side: Side = Side.BUY
    size_shares: float = 0.0
    price: float = 0.0
    order_type: OrderType = OrderType.GTC
    post_only: bool = False
    expiration_ms: Optional[int] = None  # required for GTD

    @property
    def remaining_shares(self) -> float:
        return max(0.0, self.size_shares - self.filled_size)


@dataclass
class MarketBuyOrder(_OrderState):
    """Aggressive BUY priced in USDC notional."""
    notional_usdc: float = 0.0
    order_type: OrderType = OrderType.FAK     # FAK or FOK only
    slip_limit_price: Optional[float] = None  # worst price the strategy will pay

    side: Side = field(default=Side.BUY, init=False)


@dataclass
class MarketSellOrder(_OrderState):
    """Aggressive SELL priced in shares."""
    size_shares: float = 0.0
    order_type: OrderType = OrderType.FAK
    slip_limit_price: Optional[float] = None  # worst price the strategy will accept

    side: Side = field(default=Side.SELL, init=False)


Order = Union[LimitOrder, MarketBuyOrder, MarketSellOrder]


@dataclass
class Fill:
    order_id: str
    token_id: str
    side: Side
    price: float
    size_shares: float
    ts_ms: int
    is_maker: bool
    fee_usdc: float = 0.0
    matched_against_tx: str = ""    # tx_hash of the real trade we matched, if any

    @property
    def notional_usdc(self) -> float:
        return self.price * self.size_shares
