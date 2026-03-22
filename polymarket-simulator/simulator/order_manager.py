"""Order lifecycle — exact Polymarket semantics with full timeline tracking.

Every order gets a timeline of events:
  PLACEMENT → [RESTING] → [QUEUE_UPDATE...] → [PARTIAL_FILL...] → FILL/CANCELLATION/EXPIRATION

GTC/GTD: Check for aggressive fill, then rest with queue tracking
FOK: Must fill entirely or cancel — all or nothing
FAK: Fill what's available, cancel rest
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Optional

from .matching_engine import GTCMatcher, apply_fills, sweep_book, total_available
from .models import (
    OrderBookSnapshot,
    OrderStatus,
    OrderType,
    Side,
    SimFill,
    SimOrder,
)
from .position_tracker import PositionTracker

log = logging.getLogger(__name__)


class OrderManager:
    def __init__(self, position: PositionTracker):
        self.position = position
        self.orders: dict[str, SimOrder] = {}
        self.gtc_matcher = GTCMatcher()
        self.fills: deque[SimFill] = deque(maxlen=1000)
        self._fill_callbacks: list = []

    def on_fill(self, cb):
        self._fill_callbacks.append(cb)

    def _emit_fills(self, fills: list[SimFill]):
        for fill in fills:
            self.fills.append(fill)
            self.position.on_fill(fill)
            for cb in self._fill_callbacks:
                cb(fill)

    def place_order(
        self,
        token_id: str,
        token_name: str,
        side: Side,
        order_type: OrderType,
        size: float,
        price: Optional[float],
        book: OrderBookSnapshot,
        expiration: Optional[float] = None,
    ) -> tuple[SimOrder, list[SimFill]]:

        order = SimOrder(
            token_id=token_id, token_name=token_name, side=side,
            order_type=order_type, price=price, size=size,
            expiration=expiration,
        )
        self.orders[order.id] = order

        # Validate
        if order_type in (OrderType.GTC, OrderType.GTD) and price is None:
            order.add_event("PLACEMENT", OrderStatus.CANCELED, "Limit order requires price")
            return order, []

        # Log placement
        exp_str = f", exp={expiration}" if expiration else ""
        price_str = f"{price:.4f}" if price else "MKT"
        order.add_event(
            "PLACEMENT", OrderStatus.LIVE,
            f"{side.value} {order_type.value} {size:.0f} @ {price_str}{exp_str}",
        )

        fills: list[SimFill] = []

        if order_type == OrderType.FOK:
            fills = self._execute_fok(order, book)
        elif order_type == OrderType.FAK:
            fills = self._execute_fak(order, book)
        elif order_type in (OrderType.GTC, OrderType.GTD):
            fills = self._execute_gtc(order, book)

        if fills:
            self._emit_fills(fills)

        log.info("Order %s: %s %s %.0f @ %s → %s (filled %.0f)",
                 order.id, side.value, order_type.value, size,
                 f"{price:.4f}" if price else "MKT",
                 order.status.value, order.filled_size)

        return order, fills

    def _execute_fok(self, order: SimOrder, book: OrderBookSnapshot) -> list[SimFill]:
        """FOK: must fill entirely or cancel. All or nothing."""
        available = total_available(order, book)
        if available < order.size - 0.001:
            order.add_event("CANCELLATION", OrderStatus.CANCELED,
                            f"FOK: only {available:.0f} available, need {order.size:.0f}")
            return []

        fills = sweep_book(order, book)
        filled_total = sum(f.size for f in fills)
        if filled_total < order.size - 0.001:
            order.add_event("CANCELLATION", OrderStatus.CANCELED,
                            f"FOK: sweep got {filled_total:.0f}, need {order.size:.0f}")
            return []

        apply_fills(order, fills)
        order.add_event("FILL", OrderStatus.MATCHED,
                        f"FOK filled {order.filled_size:.0f} @ {order.avg_fill_price:.4f}")
        return fills

    def _execute_fak(self, order: SimOrder, book: OrderBookSnapshot) -> list[SimFill]:
        """FAK: fill what's available, cancel remainder."""
        fills = sweep_book(order, book)
        apply_fills(order, fills)

        if order.filled_size >= order.size - 0.001:
            order.add_event("FILL", OrderStatus.MATCHED,
                            f"FAK fully filled {order.filled_size:.0f} @ {order.avg_fill_price:.4f}")
        elif order.filled_size > 0:
            order.add_event("PARTIAL_FILL", OrderStatus.MATCHED,
                            f"FAK filled {order.filled_size:.0f}/{order.size:.0f} @ {order.avg_fill_price:.4f}, remainder killed")
        else:
            order.add_event("CANCELLATION", OrderStatus.CANCELED,
                            "FAK: nothing available at price")

        return fills

    def _execute_gtc(self, order: SimOrder, book: OrderBookSnapshot) -> list[SimFill]:
        """GTC/GTD: aggressive portion fills immediately, remainder rests with queue."""
        fills: list[SimFill] = []

        # Check if our price crosses the book (aggressive)
        is_aggressive = False
        if order.price is not None:
            if order.side == Side.BUY and book.best_ask and order.price >= book.best_ask:
                is_aggressive = True
            elif order.side == Side.SELL and book.best_bid and order.price <= book.best_bid:
                is_aggressive = True

        if is_aggressive:
            fills = sweep_book(order, book)
            apply_fills(order, fills)
            if fills:
                filled = sum(f.size for f in fills)
                order.add_event("AGGRESSIVE_FILL", OrderStatus.LIVE if order.remaining_size > 0.001 else OrderStatus.MATCHED,
                                f"Aggressive: swept {filled:.0f} @ avg {order.avg_fill_price:.4f}")

        # Rest remaining size with queue position
        if order.remaining_size > 0.001:
            order.status = OrderStatus.LIVE
            self.gtc_matcher.add_order(order, book)

        if order.filled_size >= order.size - 0.001:
            order.add_event("FILL", OrderStatus.MATCHED,
                            f"Fully filled {order.filled_size:.0f} @ {order.avg_fill_price:.4f}")

        return fills

    def cancel_order(self, order_id: str) -> Optional[SimOrder]:
        order = self.orders.get(order_id)
        if not order:
            return None
        if order.status in (OrderStatus.MATCHED, OrderStatus.CANCELED):
            return order

        unfilled = order.remaining_size
        order.add_event("CANCELLATION", OrderStatus.CANCELED,
                        f"User cancel, unfilled={unfilled:.0f}")
        self.gtc_matcher.remove_order(order_id)
        return order

    # ── WS event handlers ──────────────────────────────────────────

    def on_trade(self, token_id: str, price: float, size: float, side: str) -> list[SimFill]:
        fills = self.gtc_matcher.on_trade(token_id, price, size, side)
        if fills:
            self._emit_fills(fills)
        return fills

    def on_level_change(self, token_id: str, side_str: str, price: float,
                        old_size: float, new_size: float):
        self.gtc_matcher.on_level_change(token_id, side_str, price, old_size, new_size)

    def check_expirations(self) -> list[SimOrder]:
        return self.gtc_matcher.check_expirations()

    # ── Queries ────────────────────────────────────────────────────

    def get_open_orders(self) -> list[SimOrder]:
        return [o for o in self.orders.values()
                if o.status in (OrderStatus.LIVE, OrderStatus.PARTIALLY_FILLED)]

    def get_all_orders(self, limit: int = 50) -> list[SimOrder]:
        return sorted(self.orders.values(), key=lambda o: o.created_at, reverse=True)[:limit]

    def get_recent_fills(self, limit: int = 50) -> list[SimFill]:
        return list(self.fills)[-limit:]

    def get_queue_info(self) -> list[dict]:
        return self.gtc_matcher.get_queue_info()

    def get_order_timeline(self, order_id: str) -> list[dict]:
        order = self.orders.get(order_id)
        if not order:
            return []
        return [{
            "event_type": e.event_type,
            "status": e.status.value,
            "timestamp": e.timestamp,
            "detail": e.detail,
            "fill_price": e.fill_price,
            "fill_size": e.fill_size,
            "queue_ahead": e.queue_ahead,
        } for e in order.timeline]
