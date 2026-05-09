"""Strategy interface — reactive ABC + scheduled helper.

Two ways in:

  Strategy             — override on_event (or the typed `on_book` /
                         `on_trade` / `on_cricket` / `on_fill` /
                         `on_cancel` / `on_reject` shims)

  ScheduledStrategy    — pre-built schedule of (ts_ms, OrderSpec) tuples;
                         submitted at the right tick automatically

Both go through the same `StrategyContext` for submit/cancel/state queries.
The context is the only handle a strategy has into the engine; it is small
and exposes only public methods.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Protocol

from .book import Book
from .enums import OrderType, Side
from .events import (
    AckEvent, BookEvent, CancelEvent, CricketEvent, Event,
    FillEvent, RejectEvent, TradeEvent,
)
from .market import Market
from .orders import LimitOrder, MarketBuyOrder, MarketSellOrder, Order
from .portfolio import PortfolioSnapshot


# ── StrategyContext: the only API surface a strategy sees ────────────


class StrategyContext(Protocol):
    """Engine-provided handle. Strategies must not access engine internals."""

    def submit_limit(
        self,
        *,
        token_id: str,
        side: Side,
        size_shares: float,
        price: float,
        order_type: OrderType = OrderType.GTC,
        post_only: bool = False,
        expiration_ms: Optional[int] = None,
        client_tag: str = "",
    ) -> "SubmissionResult": ...

    def submit_market_buy(
        self,
        *,
        token_id: str,
        notional_usdc: float,
        order_type: OrderType = OrderType.FAK,
        slip_limit_price: Optional[float] = None,
        client_tag: str = "",
    ) -> "SubmissionResult": ...

    def submit_market_sell(
        self,
        *,
        token_id: str,
        size_shares: float,
        order_type: OrderType = OrderType.FAK,
        slip_limit_price: Optional[float] = None,
        client_tag: str = "",
    ) -> "SubmissionResult": ...

    def cancel(self, order_id: str) -> bool: ...

    def seed_position(self, token_id: str, shares: float, avg_cost: float = 0.0) -> None:
        """Seed an initial position. Call only from `on_start`."""

    def book(self, token_id: str) -> Book: ...

    def position(self, token_id: str) -> float: ...

    def open_orders(self) -> list[LimitOrder]: ...

    def pnl(self) -> PortfolioSnapshot: ...

    def now_ms(self) -> int: ...

    def market(self) -> Market: ...


# ── Reactive Strategy ABC ────────────────────────────────────────────


class Strategy(ABC):
    """Reactive base class. Override `on_event` for everything-in-one, or
    override the typed shims for clarity. Default shims are no-ops.
    """

    def on_start(self, ctx: StrategyContext) -> None: ...
    def on_end(self, ctx: StrategyContext) -> None: ...

    def on_book(self, evt: BookEvent, ctx: StrategyContext) -> None: ...
    def on_trade(self, evt: TradeEvent, ctx: StrategyContext) -> None: ...
    def on_cricket(self, evt: CricketEvent, ctx: StrategyContext) -> None: ...
    def on_fill(self, evt: FillEvent, ctx: StrategyContext) -> None: ...
    def on_ack(self, evt: AckEvent, ctx: StrategyContext) -> None: ...
    def on_reject(self, evt: RejectEvent, ctx: StrategyContext) -> None: ...
    def on_cancel(self, evt: CancelEvent, ctx: StrategyContext) -> None: ...

    def on_event(self, evt: Event, ctx: StrategyContext) -> None:
        """Default dispatcher to the typed shims. Override for full control."""
        if isinstance(evt, BookEvent):
            self.on_book(evt, ctx)
        elif isinstance(evt, TradeEvent):
            self.on_trade(evt, ctx)
        elif isinstance(evt, CricketEvent):
            self.on_cricket(evt, ctx)
        elif isinstance(evt, FillEvent):
            self.on_fill(evt, ctx)
        elif isinstance(evt, AckEvent):
            self.on_ack(evt, ctx)
        elif isinstance(evt, RejectEvent):
            self.on_reject(evt, ctx)
        elif isinstance(evt, CancelEvent):
            self.on_cancel(evt, ctx)


# ── Scheduled Strategy helper ────────────────────────────────────────


@dataclass
class ScheduledOrder:
    """One row of an ad-hoc schedule. Exactly one of {limit, market_buy,
    market_sell} should be set."""
    ts_ms: int
    token_id: str
    kind: str                # "limit" | "market_buy" | "market_sell"
    side: Optional[Side] = None
    size_shares: float = 0.0
    notional_usdc: float = 0.0
    price: Optional[float] = None
    order_type: OrderType = OrderType.GTC
    post_only: bool = False
    expiration_ms: Optional[int] = None
    slip_limit_price: Optional[float] = None
    client_tag: str = ""


class ScheduledStrategy(Strategy):
    """Submits a pre-defined sequence of orders at their scheduled times.

    Usage:
        ScheduledStrategy([
            ScheduledOrder(ts_ms=..., token_id=..., kind="limit",
                           side=Side.BUY, size_shares=100, price=0.45),
            ScheduledOrder(ts_ms=..., kind="market_buy",
                           token_id=..., notional_usdc=10),
        ])
    """

    def __init__(self, schedule: list[ScheduledOrder]):
        self._schedule = sorted(schedule, key=lambda o: o.ts_ms)
        self._idx = 0

    def _drain_due(self, ctx: StrategyContext) -> None:
        now = ctx.now_ms()
        # Advance the cursor BEFORE submitting. submit() emits AckEvent,
        # which re-enters on_event → _drain_due; without this, the same
        # spec would be re-submitted recursively.
        while self._idx < len(self._schedule):
            spec = self._schedule[self._idx]
            if spec.ts_ms > now:
                break
            self._idx += 1
            self._submit_one(spec, ctx)

    @staticmethod
    def _submit_one(spec: ScheduledOrder, ctx: StrategyContext):
        if spec.kind == "limit":
            return ctx.submit_limit(
                token_id=spec.token_id, side=spec.side,
                size_shares=spec.size_shares, price=spec.price,
                order_type=spec.order_type, post_only=spec.post_only,
                expiration_ms=spec.expiration_ms, client_tag=spec.client_tag,
            )
        if spec.kind == "market_buy":
            return ctx.submit_market_buy(
                token_id=spec.token_id, notional_usdc=spec.notional_usdc,
                order_type=spec.order_type, slip_limit_price=spec.slip_limit_price,
                client_tag=spec.client_tag,
            )
        if spec.kind == "market_sell":
            return ctx.submit_market_sell(
                token_id=spec.token_id, size_shares=spec.size_shares,
                order_type=spec.order_type, slip_limit_price=spec.slip_limit_price,
                client_tag=spec.client_tag,
            )
        raise ValueError(f"unknown scheduled order kind: {spec.kind!r}")

    # Drain on every event so submissions fire as soon as their ts_ms passes
    def on_event(self, evt, ctx):
        super().on_event(evt, ctx)
        self._drain_due(ctx)

    def on_start(self, ctx):
        self._drain_due(ctx)
