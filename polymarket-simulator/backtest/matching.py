"""Polymarket CLOB matching — replica semantics.

Three responsibilities and no others:

  1. Match new orders against the real book on submit (aggressive portion)
  2. Hold resting GTC/GTD orders with queue-position tracking
  3. On every real trade or book update, decide which resting orders fill
     and emit Fills

Matching never touches the portfolio, never emits events, never knows who the
strategy is. Engine wires those concerns together.

Trade-through policy: if a real aggressor's price would have crossed our
resting maker order, we fill at our limit price (we'd have been at top of
book historically). FIFO within a level. Strategy code is responsible for
sizing — large orders get the same trade-through treatment but at scale this
overstates fills since the historical taker's demand is finite.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Optional

from .book import Book, PriceLevel
from .config import PRICE_EPS, SIZE_EPS
from .enums import OrderStatus, OrderType, Side
from .events import TradeEvent
from .fees import compute_taker_fee, rate_from_captured_bps
from .market import Market
from .orders import Fill, LimitOrder, MarketBuyOrder, MarketSellOrder, Order

log = logging.getLogger(__name__)


@dataclass
class _RestingEntry:
    order: LimitOrder
    queue_ahead: float
    initial_queue: float
    joined_at_ms: int


@dataclass
class SubmissionResult:
    """What the engine returns from a submit. Strategy code can read this
    directly — it doesn't need to wait for callbacks for synchronous fills.
    """
    order: Order
    order_id: str
    fills: list[Fill] = field(default_factory=list)
    rejected: bool = False
    reason: str = ""

    @property
    def total_filled_shares(self) -> float:
        return sum(f.size_shares for f in self.fills)

    @property
    def total_filled_notional(self) -> float:
        return sum(f.size_shares * f.price for f in self.fills)


class MatchingEngine:
    """Per-market CLOB matcher. Owns resting orders and queue tracking only."""

    def __init__(self, market: Market):
        self._market = market
        # token_id → side → list of resting entries (small list; no need for
        # sorted structures at the volumes a single strategy generates)
        self._resting: dict[str, dict[Side, list[_RestingEntry]]] = defaultdict(
            lambda: {Side.BUY: [], Side.SELL: []}
        )
        self._by_id: dict[str, _RestingEntry] = {}

    # ─────────────────────────────────────────────────────────────────
    # Submission
    # ─────────────────────────────────────────────────────────────────

    def submit_limit(
        self, *, order: LimitOrder, book: Book, now_ms: int
    ) -> SubmissionResult:
        order.created_at_ms = now_ms
        order.updated_at_ms = now_ms

        # Self-trade prevention
        if self._would_self_cross_limit(order):
            return self._reject(order, "self_trade_prevention")

        crossable = book.crossable_for(order.side, order.price)
        crossable_size = sum(lv.size for lv in crossable)

        if order.post_only and crossable_size > SIZE_EPS:
            return self._reject(order, "post_only_would_cross")

        fills: list[Fill] = []
        if crossable_size > SIZE_EPS:
            fills = self._sweep_shares(
                order_id=order.id, token_id=order.token_id, side=order.side,
                levels=crossable, max_shares=order.size_shares, now_ms=now_ms,
                captured_rate_bps=None,
            )
            self._apply_to_order(order, fills)

        if order.remaining_shares > SIZE_EPS:
            self._rest(order, book, now_ms)
            order.status = OrderStatus.PARTIALLY_FILLED if fills else OrderStatus.LIVE
        else:
            order.status = OrderStatus.MATCHED

        return SubmissionResult(order=order, order_id=order.id, fills=fills)

    def submit_market_buy(
        self, *, order: MarketBuyOrder, book: Book, now_ms: int
    ) -> SubmissionResult:
        order.created_at_ms = now_ms
        order.updated_at_ms = now_ms

        # No self-cross check for market BUYs against own SELL resters: that
        # IS the desired behavior on a real CLOB; reject as STP.
        if self._has_resting_on(order.token_id, Side.SELL):
            return self._reject(order, "self_trade_prevention")

        crossable = book.crossable_for(Side.BUY, order.slip_limit_price)
        notional_avail = sum(lv.size * lv.price for lv in crossable)

        if order.order_type == OrderType.FOK and notional_avail + PRICE_EPS < order.notional_usdc:
            return self._cancel_immediate(order, "fok_not_fillable")

        fills = self._sweep_notional(
            order_id=order.id, token_id=order.token_id, side=Side.BUY,
            levels=crossable, max_notional=order.notional_usdc, now_ms=now_ms,
            captured_rate_bps=None,
        )
        self._apply_to_order(order, fills)

        if not fills:
            return self._cancel_immediate(order, "no_liquidity")

        # FAK: any unfilled portion is implicitly cancelled
        order.status = OrderStatus.MATCHED if (
            order.order_type == OrderType.FOK
            or sum(f.size_shares * f.price for f in fills) >= order.notional_usdc - PRICE_EPS
        ) else OrderStatus.CANCELED

        return SubmissionResult(order=order, order_id=order.id, fills=fills)

    def submit_market_sell(
        self, *, order: MarketSellOrder, book: Book, now_ms: int
    ) -> SubmissionResult:
        order.created_at_ms = now_ms
        order.updated_at_ms = now_ms

        if self._has_resting_on(order.token_id, Side.BUY):
            return self._reject(order, "self_trade_prevention")

        crossable = book.crossable_for(Side.SELL, order.slip_limit_price)
        shares_avail = sum(lv.size for lv in crossable)

        if order.order_type == OrderType.FOK and shares_avail + SIZE_EPS < order.size_shares:
            return self._cancel_immediate(order, "fok_not_fillable")

        fills = self._sweep_shares(
            order_id=order.id, token_id=order.token_id, side=Side.SELL,
            levels=crossable, max_shares=order.size_shares, now_ms=now_ms,
            captured_rate_bps=None,
        )
        self._apply_to_order(order, fills)

        if not fills:
            return self._cancel_immediate(order, "no_liquidity")

        order.status = OrderStatus.MATCHED if (
            order.order_type == OrderType.FOK
            or order.filled_size >= order.size_shares - SIZE_EPS
        ) else OrderStatus.CANCELED

        return SubmissionResult(order=order, order_id=order.id, fills=fills)

    # ─────────────────────────────────────────────────────────────────
    # Cancel + expiration
    # ─────────────────────────────────────────────────────────────────

    def cancel(self, order_id: str, now_ms: int) -> Optional[LimitOrder]:
        entry = self._by_id.pop(order_id, None)
        if entry is None:
            return None
        side_list = self._resting[entry.order.token_id][entry.order.side]
        try:
            side_list.remove(entry)
        except ValueError:
            pass
        entry.order.status = OrderStatus.CANCELED
        entry.order.updated_at_ms = now_ms
        return entry.order

    def expire_due(self, now_ms: int) -> list[LimitOrder]:
        expired: list[LimitOrder] = []
        for oid, entry in list(self._by_id.items()):
            o = entry.order
            if o.order_type == OrderType.GTD and o.expiration_ms is not None:
                if now_ms >= o.expiration_ms:
                    self.cancel(oid, now_ms)
                    expired.append(o)
        return expired

    def open_orders(self) -> list[LimitOrder]:
        return [e.order for e in self._by_id.values()]

    # ─────────────────────────────────────────────────────────────────
    # Real-trade and book-update handlers
    # ─────────────────────────────────────────────────────────────────

    def on_real_trade(self, trade: TradeEvent) -> list[Fill]:
        """A historical aggressive trade — see if it would have hit any of
        our resting orders first.
        """
        victim_side = Side.SELL if trade.side == Side.BUY else Side.BUY
        entries = self._resting[trade.token_id][victim_side]
        if not entries:
            return []

        # Real fills are maker fills, no fee to taker (us as maker pays 0).
        # The captured trade fee is for the ORIGINAL aggressor; we don't pay.
        sorted_entries = sorted(
            entries,
            key=lambda e: e.order.price if victim_side == Side.SELL else -e.order.price,
        )

        fills: list[Fill] = []
        remaining = trade.size_shares
        for entry in sorted_entries:
            if remaining <= SIZE_EPS:
                break
            order = entry.order
            if order.status not in (OrderStatus.LIVE, OrderStatus.PARTIALLY_FILLED):
                continue

            relation, fill_price = self._relate_to_trade(victim_side, order.price, trade.price)
            if relation == "worse":
                continue

            if relation == "through":
                fill_size = min(order.remaining_shares, remaining)
                entry.queue_ahead = 0.0
            else:  # "same" — FIFO at the same level
                consumed = min(remaining, entry.queue_ahead)
                entry.queue_ahead -= consumed
                remaining_after_queue = remaining - consumed
                if remaining_after_queue <= SIZE_EPS:
                    continue
                fill_size = min(order.remaining_shares, remaining_after_queue)

            if fill_size <= SIZE_EPS:
                continue

            fill = Fill(
                order_id=order.id, token_id=order.token_id,
                side=order.side, price=fill_price, size_shares=fill_size,
                ts_ms=trade.ts_ms, is_maker=True, fee_usdc=0.0,
                matched_against_tx=trade.tx_hash,
            )
            fills.append(fill)
            order.filled_size += fill_size
            order.filled_notional += fill_size * fill_price
            order.updated_at_ms = trade.ts_ms
            remaining -= fill_size

            if order.remaining_shares <= SIZE_EPS:
                order.status = OrderStatus.MATCHED
                self._by_id.pop(order.id, None)
                try:
                    entries.remove(entry)
                except ValueError:
                    pass
            else:
                order.status = OrderStatus.PARTIALLY_FILLED

        return fills

    def on_book_update(self, snapshot) -> None:
        """Reconcile queue position. Visible level shrink ⇒ queue advanced;
        growth is ignored (cannot tell if joiners are ahead or behind).
        """
        for side in (Side.BUY, Side.SELL):
            entries = self._resting[snapshot.token_id][side]
            if not entries:
                continue
            levels = snapshot.bids if side == Side.BUY else snapshot.asks
            size_at_price = {lv.price: lv.size for lv in levels}
            for entry in entries:
                px = entry.order.price
                visible = size_at_price.get(px, 0.0)
                if visible < entry.queue_ahead - PRICE_EPS:
                    entry.queue_ahead = visible

    # ─────────────────────────────────────────────────────────────────
    # Internals
    # ─────────────────────────────────────────────────────────────────

    def _reject(self, order: Order, reason: str) -> SubmissionResult:
        order.status = OrderStatus.REJECTED
        return SubmissionResult(
            order=order, order_id=order.id, rejected=True, reason=reason,
        )

    def _cancel_immediate(self, order: Order, reason: str) -> SubmissionResult:
        order.status = OrderStatus.CANCELED
        return SubmissionResult(order=order, order_id=order.id, reason=reason)

    def _would_self_cross_limit(self, incoming: LimitOrder) -> bool:
        opposite = Side.SELL if incoming.side == Side.BUY else Side.BUY
        for e in self._resting[incoming.token_id][opposite]:
            if incoming.side == Side.BUY and incoming.price >= e.order.price - PRICE_EPS:
                return True
            if incoming.side == Side.SELL and incoming.price <= e.order.price + PRICE_EPS:
                return True
        return False

    def _has_resting_on(self, token_id: str, side: Side) -> bool:
        return len(self._resting[token_id][side]) > 0

    def _rest(self, order: LimitOrder, book: Book, now_ms: int) -> None:
        queue_ahead = book.size_at(order.price, order.side)
        entry = _RestingEntry(
            order=order, queue_ahead=queue_ahead, initial_queue=queue_ahead,
            joined_at_ms=now_ms,
        )
        self._resting[order.token_id][order.side].append(entry)
        self._by_id[order.id] = entry

    def _sweep_shares(
        self, *, order_id: str, token_id: str, side: Side,
        levels: tuple[PriceLevel, ...], max_shares: float, now_ms: int,
        captured_rate_bps: Optional[int],
    ) -> list[Fill]:
        rate = self._fee_rate(captured_rate_bps)
        fills: list[Fill] = []
        budget = max_shares
        for lv in levels:
            if budget <= SIZE_EPS:
                break
            take = min(lv.size, budget)
            if take <= SIZE_EPS:
                break
            fills.append(Fill(
                order_id=order_id, token_id=token_id, side=side,
                price=lv.price, size_shares=take, ts_ms=now_ms,
                is_maker=False,
                fee_usdc=compute_taker_fee(shares=take, price=lv.price, rate=rate),
            ))
            budget -= take
        return fills

    def _sweep_notional(
        self, *, order_id: str, token_id: str, side: Side,
        levels: tuple[PriceLevel, ...], max_notional: float, now_ms: int,
        captured_rate_bps: Optional[int],
    ) -> list[Fill]:
        rate = self._fee_rate(captured_rate_bps)
        fills: list[Fill] = []
        budget = max_notional
        for lv in levels:
            if budget <= PRICE_EPS:
                break
            shares_at_level = min(lv.size, budget / lv.price)
            if shares_at_level <= SIZE_EPS:
                break
            fills.append(Fill(
                order_id=order_id, token_id=token_id, side=side,
                price=lv.price, size_shares=shares_at_level, ts_ms=now_ms,
                is_maker=False,
                fee_usdc=compute_taker_fee(shares=shares_at_level, price=lv.price, rate=rate),
            ))
            budget -= shares_at_level * lv.price
        return fills

    def _fee_rate(self, captured_bps: Optional[int]) -> float:
        captured = rate_from_captured_bps(captured_bps)
        if captured is not None:
            return captured
        return self._market.default_rate()

    def _apply_to_order(self, order: Order, fills: list[Fill]) -> None:
        for f in fills:
            order.filled_size += f.size_shares
            order.filled_notional += f.size_shares * f.price

    @staticmethod
    def _relate_to_trade(
        victim_side: Side, my_price: float, trade_price: float
    ) -> tuple[str, float]:
        """Return ('through'|'same'|'worse', fill_price)."""
        if victim_side == Side.SELL:    # my SELL vs BUY aggressor
            if my_price < trade_price - PRICE_EPS:
                return "through", my_price
            if abs(my_price - trade_price) <= PRICE_EPS:
                return "same", trade_price
            return "worse", 0.0
        else:                            # my BUY vs SELL aggressor
            if my_price > trade_price + PRICE_EPS:
                return "through", my_price
            if abs(my_price - trade_price) <= PRICE_EPS:
                return "same", trade_price
            return "worse", 0.0
