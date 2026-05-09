"""Engine — orchestrates clock, market, books, matching, portfolio, metrics.

Holds the only mutable runtime state. Strategies see only `StrategyContext`,
which is a small public surface (no engine internals leak through it).

Event dispatch order within a tick: book → trade → cricket → strategy.
"""

from __future__ import annotations

import itertools
import logging
from typing import Iterable, Optional

from .book import Book, BookSnapshot
from .enums import OrderStatus, OrderType, Side
from .events import (
    AckEvent, BookEvent, CancelEvent, CricketEvent, Event,
    FillEvent, RejectEvent, TradeEvent,
)
from .market import Market
from .matching import MatchingEngine, SubmissionResult
from .orders import (
    Fill, LimitOrder, MarketBuyOrder, MarketSellOrder, Order,
)
from .portfolio import Portfolio, PortfolioSnapshot
from .metrics import MetricsRecorder
from .strategy import Strategy
from .validators import (
    OrderRejection, validate_limit, validate_market_buy, validate_market_sell,
)

log = logging.getLogger(__name__)


class Engine:
    """Replay-time orchestrator.

    `starting_cash_usdc` is informational only — the engine never enforces
    a cash constraint (cash can go negative; PnL is what matters). Pass it
    if you want the equity curve to start from a meaningful number;
    otherwise the default 0 is fine for pure-strategy backtesting.

    `max_position_shares` is the only optional hard constraint. When set,
    the engine rejects any submission that would push the strategy's
    position on a token beyond this absolute value. None = no cap.
    """

    def __init__(
        self,
        market: Market,
        *,
        portfolio: Optional[Portfolio] = None,
        metrics: Optional[MetricsRecorder] = None,
        starting_cash_usdc: float = 0.0,
        max_position_shares: Optional[float] = None,
    ):
        self.market = market
        self.portfolio = portfolio or Portfolio(starting_cash_usdc)
        self.metrics = metrics or MetricsRecorder()
        self.max_position_shares = max_position_shares
        self.matching = MatchingEngine(market)
        self.books: dict[str, Book] = {tid: Book(tid) for tid in market.token_ids}

        self._now_ms: int = 0
        self._strategy: Optional[Strategy] = None
        self._oid_seq = itertools.count(1)
        # Single context instance reused; engine state is read live.
        self._ctx: Optional[_Ctx] = None

    # ── public API ───────────────────────────────────────────────────

    def register(self, strategy: Strategy) -> None:
        self._strategy = strategy
        self._ctx = _Ctx(self)

    def now_ms(self) -> int:
        return self._now_ms

    def book(self, token_id: str) -> Book:
        return self.books[token_id]

    def run(self, events: Iterable[Event]) -> None:
        if self._strategy is None:
            raise RuntimeError("no strategy registered")

        self._strategy.on_start(self._ctx)
        try:
            for evt in events:
                self._tick_to(evt.ts_ms if hasattr(evt, "ts_ms") else self._now_ms)
                self._dispatch(evt)
        finally:
            self._strategy.on_end(self._ctx)

    # ── submission (called via StrategyContext) ──────────────────────

    def _submit_limit(
        self, *, token_id: str, side: Side, size_shares: float, price: float,
        order_type: OrderType, post_only: bool, expiration_ms: Optional[int],
        client_tag: str,
    ) -> SubmissionResult:
        oid = self._next_oid()
        if self._would_breach_position_cap(token_id, side, size_shares):
            return self._reject_now(
                oid=oid, token_id=token_id, side=side,
                reason=f"position_cap {self.max_position_shares}",
            )
        try:
            validate_limit(
                market=self.market, side=side, size_shares=size_shares,
                price=price, order_type=order_type, post_only=post_only,
                expiration_ms=expiration_ms, now_ms=self._now_ms,
            )
        except OrderRejection as ex:
            return self._reject_now(oid=oid, token_id=token_id, side=side, reason=str(ex))

        order = LimitOrder(
            id=oid, token_id=token_id, side=side, size_shares=size_shares,
            price=price, order_type=order_type, post_only=post_only,
            expiration_ms=expiration_ms, client_tag=client_tag,
        )
        result = self.matching.submit_limit(
            order=order, book=self.books[token_id], now_ms=self._now_ms,
        )
        self._post_submit(result)
        return result

    def _submit_market_buy(
        self, *, token_id: str, notional_usdc: float, order_type: OrderType,
        slip_limit_price: Optional[float], client_tag: str,
    ) -> SubmissionResult:
        oid = self._next_oid()
        try:
            validate_market_buy(
                market=self.market, notional_usdc=notional_usdc,
                order_type=order_type, slip_limit_price=slip_limit_price,
            )
        except OrderRejection as ex:
            return self._reject_now(oid=oid, token_id=token_id, side=Side.BUY, reason=str(ex))

        order = MarketBuyOrder(
            id=oid, token_id=token_id, notional_usdc=notional_usdc,
            order_type=order_type, slip_limit_price=slip_limit_price,
            client_tag=client_tag,
        )
        result = self.matching.submit_market_buy(
            order=order, book=self.books[token_id], now_ms=self._now_ms,
        )
        self._post_submit(result)
        return result

    def _submit_market_sell(
        self, *, token_id: str, size_shares: float, order_type: OrderType,
        slip_limit_price: Optional[float], client_tag: str,
    ) -> SubmissionResult:
        oid = self._next_oid()
        try:
            validate_market_sell(
                market=self.market, size_shares=size_shares,
                order_type=order_type, slip_limit_price=slip_limit_price,
            )
        except OrderRejection as ex:
            return self._reject_now(oid=oid, token_id=token_id, side=Side.SELL, reason=str(ex))

        order = MarketSellOrder(
            id=oid, token_id=token_id, size_shares=size_shares,
            order_type=order_type, slip_limit_price=slip_limit_price,
            client_tag=client_tag,
        )
        result = self.matching.submit_market_sell(
            order=order, book=self.books[token_id], now_ms=self._now_ms,
        )
        self._post_submit(result)
        return result

    def _cancel(self, order_id: str) -> bool:
        order = self.matching.cancel(order_id, self._now_ms)
        if order is None:
            return False
        self._emit(CancelEvent(order_id=order_id, ts_ms=self._now_ms, reason="user"))
        return True

    # ── internals ────────────────────────────────────────────────────

    def _next_oid(self) -> str:
        return f"o{next(self._oid_seq):06d}"

    def _would_breach_position_cap(
        self, token_id: str, side: Side, size_shares: float,
    ) -> bool:
        cap = self.max_position_shares
        if cap is None:
            return False
        current = self.portfolio.position(token_id)
        # Worst case: this whole order fills. Reject if it would push past cap.
        if side == Side.BUY:
            return current + size_shares > cap + 1e-9
        # SELL reduces toward (or past) zero. We don't allow shorting CT
        # anyway; that's enforced inside Portfolio. No cap check needed here.
        return False

    def _reject_now(self, *, oid: str, token_id: str, side: Side, reason: str) -> SubmissionResult:
        # Minimal carrier so the caller's return type is uniform.
        carrier = LimitOrder(
            id=oid, token_id=token_id, side=side, size_shares=0.0, price=0.0,
            status=OrderStatus.REJECTED,
        )
        result = SubmissionResult(order=carrier, order_id=oid, rejected=True, reason=reason)
        self._emit(RejectEvent(order_id=oid, ts_ms=self._now_ms, reason=reason))
        return result

    def _post_submit(self, result: SubmissionResult) -> None:
        if result.rejected:
            self._emit(RejectEvent(
                order_id=result.order_id, ts_ms=self._now_ms, reason=result.reason,
            ))
            return

        self._emit(AckEvent(order_id=result.order_id, ts_ms=self._now_ms))

        for f in result.fills:
            self._apply_fill(f)

        # Any FAK/FOK that ended up cancelled gets a CancelEvent. Limit orders
        # only emit cancels via user request or GTD expiration (handled separately).
        if result.order.status == OrderStatus.CANCELED:
            self._emit(CancelEvent(
                order_id=result.order_id, ts_ms=self._now_ms,
                reason=result.reason or "fak_leftover",
            ))

    def _apply_fill(self, fill: Fill) -> None:
        self.portfolio.apply(fill)
        self.metrics.record_fill(fill)
        self._emit(FillEvent(fill=fill))

    def _tick_to(self, ts_ms: int) -> None:
        if ts_ms < self._now_ms:
            ts_ms = self._now_ms       # ignore out-of-order; never go back
        self._now_ms = ts_ms
        for expired in self.matching.expire_due(ts_ms):
            self._emit(CancelEvent(order_id=expired.id, ts_ms=ts_ms, reason="expiration"))

    def _dispatch(self, evt: Event) -> None:
        if isinstance(evt, BookEvent):
            self._handle_book(evt)
        elif isinstance(evt, TradeEvent):
            self._handle_trade(evt)
        elif isinstance(evt, CricketEvent):
            self._strategy.on_event(evt, self._ctx)

    def _handle_book(self, evt: BookEvent) -> None:
        snap = evt.snapshot
        if snap.token_id not in self.books:
            return
        self.market.observe_tick(list(snap.all_prices()))
        self.books[snap.token_id].apply(snap)
        self.matching.on_book_update(snap)
        self.metrics.record_book(snap)

        marks = {tid: b.mid for tid, b in self.books.items() if b.mid is not None}
        self.metrics.record_portfolio(snap.ts_ms, self.portfolio.snapshot(marks))

        self._strategy.on_event(evt, self._ctx)

    def _handle_trade(self, evt: TradeEvent) -> None:
        for f in self.matching.on_real_trade(evt):
            self._apply_fill(f)
        self._strategy.on_event(evt, self._ctx)

    def _emit(self, evt: Event) -> None:
        if self._strategy is not None:
            self._strategy.on_event(evt, self._ctx)


class _Ctx:
    """StrategyContext implementation. Strategies hold this, not Engine."""

    def __init__(self, engine: Engine):
        self._e = engine

    def submit_limit(
        self, *, token_id, side, size_shares, price,
        order_type=OrderType.GTC, post_only=False,
        expiration_ms=None, client_tag="",
    ):
        return self._e._submit_limit(
            token_id=token_id, side=side, size_shares=size_shares,
            price=price, order_type=order_type, post_only=post_only,
            expiration_ms=expiration_ms, client_tag=client_tag,
        )

    def submit_market_buy(
        self, *, token_id, notional_usdc, order_type=OrderType.FAK,
        slip_limit_price=None, client_tag="",
    ):
        return self._e._submit_market_buy(
            token_id=token_id, notional_usdc=notional_usdc,
            order_type=order_type, slip_limit_price=slip_limit_price,
            client_tag=client_tag,
        )

    def submit_market_sell(
        self, *, token_id, size_shares, order_type=OrderType.FAK,
        slip_limit_price=None, client_tag="",
    ):
        return self._e._submit_market_sell(
            token_id=token_id, size_shares=size_shares,
            order_type=order_type, slip_limit_price=slip_limit_price,
            client_tag=client_tag,
        )

    def cancel(self, order_id: str) -> bool:
        return self._e._cancel(order_id)

    def seed_position(self, token_id: str, shares: float, avg_cost: float = 0.0) -> None:
        self._e.portfolio.set_initial_position(token_id, shares, avg_cost)

    def book(self, token_id: str) -> Book:
        return self._e.books[token_id]

    def position(self, token_id: str) -> float:
        return self._e.portfolio.position(token_id)

    def open_orders(self) -> list[LimitOrder]:
        return self._e.matching.open_orders()

    def pnl(self) -> PortfolioSnapshot:
        marks = {tid: b.mid for tid, b in self._e.books.items() if b.mid is not None}
        return self._e.portfolio.snapshot(marks)

    def now_ms(self) -> int:
        return self._e.now_ms()

    def market(self) -> Market:
        return self._e.market
