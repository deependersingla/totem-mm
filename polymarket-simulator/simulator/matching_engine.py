"""Matching engine — exact Polymarket order type semantics.

GTC: Rests on book. Aggressive portion sweeps immediately, remainder rests with
     queue position tracking. Fills only via last_trade_price events consuming queue.

GTD: Same as GTC but auto-cancels at expiration timestamp.

FOK: Must fill entirely and immediately against the real book, or cancel whole order.
     All-or-nothing. No resting.

FAK: Fill what's available immediately against the real book, cancel unfilled remainder.
     Partial fills ok. No resting.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .models import (
    OrderBookSnapshot,
    OrderStatus,
    OrderType,
    Side,
    SimFill,
    SimOrder,
)

log = logging.getLogger(__name__)


# ── Book sweeping (aggressive matching) ────────────────────────────────

def sweep_book(order: SimOrder, book: OrderBookSnapshot, max_size: float = None) -> list[SimFill]:
    """Sweep the book for aggressive fills.
    BUY: walk asks lowest-first. SELL: walk bids highest-first.
    """
    fills: list[SimFill] = []
    remaining = max_size if max_size is not None else order.remaining_size
    if remaining <= 0:
        return fills

    levels = book.asks if order.side == Side.BUY else book.bids

    for level in levels:
        if remaining <= 0:
            break
        if order.price is not None:
            if order.side == Side.BUY and level.price > order.price:
                break
            if order.side == Side.SELL and level.price < order.price:
                break

        fill_size = min(remaining, level.size)
        fills.append(SimFill(
            order_id=order.id,
            token_id=order.token_id,
            token_name=order.token_name,
            side=order.side,
            price=level.price,
            size=fill_size,
        ))
        remaining -= fill_size

    return fills


def apply_fills(order: SimOrder, fills: list[SimFill]):
    """Update order state after fills."""
    total_filled = sum(f.size for f in fills)
    total_notional = sum(f.price * f.size for f in fills)

    if total_filled > 0:
        prev_notional = order.avg_fill_price * order.filled_size
        order.filled_size += total_filled
        order.avg_fill_price = (prev_notional + total_notional) / order.filled_size

    order.updated_at = time.time()


def total_available(order: SimOrder, book: OrderBookSnapshot) -> float:
    """Calculate total size available at or better than order price."""
    levels = book.asks if order.side == Side.BUY else book.bids
    total = 0.0
    for level in levels:
        if order.price is not None:
            if order.side == Side.BUY and level.price > order.price:
                break
            if order.side == Side.SELL and level.price < order.price:
                break
        total += level.size
    return total


# ── GTC Queue Position Tracker ─────────────────────────────────────────

@dataclass
class QueueEntry:
    order: SimOrder
    price: float
    queue_ahead: float
    initial_queue: float
    joined_at: float = field(default_factory=time.time)


class GTCMatcher:
    """Queue-position-aware matching for resting GTC/GTD orders."""

    def __init__(self):
        self.entries: dict[str, QueueEntry] = {}

    def add_order(self, order: SimOrder, book_snapshot: OrderBookSnapshot):
        if order.price is None:
            return

        # Find queue ahead at our price level
        queue_ahead = 0.0
        side_levels = book_snapshot.bids if order.side == Side.BUY else book_snapshot.asks
        for level in side_levels:
            if abs(level.price - order.price) < 0.00001:
                queue_ahead = level.size
                break

        entry = QueueEntry(
            order=order,
            price=order.price,
            queue_ahead=queue_ahead,
            initial_queue=queue_ahead,
        )
        self.entries[order.id] = entry

        order.add_event("RESTING", OrderStatus.LIVE,
                        f"Resting at {order.price:.4f}, queue_ahead={queue_ahead:.0f}",
                        queue_ahead=queue_ahead)

        log.info("GTC %s: %s %s %.0f @ %.4f — queue=%.0f",
                 order.id, order.side.value, order.token_name,
                 order.remaining_size, order.price, queue_ahead)

    def remove_order(self, order_id: str):
        self.entries.pop(order_id, None)

    def on_trade(self, token_id: str, price: float, size: float, side: str) -> list[SimFill]:
        """Process last_trade_price — consume queue, generate fills."""
        all_fills: list[SimFill] = []

        for oid, entry in list(self.entries.items()):
            order = entry.order
            if order.token_id != token_id:
                continue
            if order.status not in (OrderStatus.LIVE, OrderStatus.PARTIALLY_FILLED):
                continue

            # BUY resting bid filled by aggressive SELL, SELL resting ask filled by aggressive BUY
            if order.side == Side.BUY and side != "SELL":
                continue
            if order.side == Side.SELL and side != "BUY":
                continue

            if order.side == Side.BUY:
                if price > entry.price + 0.00001:
                    continue
                trade_through = price < entry.price - 0.00001
            else:
                if price < entry.price - 0.00001:
                    continue
                trade_through = price > entry.price + 0.00001

            if trade_through:
                fill_size = order.remaining_size
                entry.queue_ahead = 0
            else:
                consumed = min(size, entry.queue_ahead)
                entry.queue_ahead -= consumed
                leftover = size - consumed
                if leftover <= 0:
                    # Queue update event
                    order.add_event("QUEUE_UPDATE", order.status,
                                    f"Queue consumed {consumed:.0f}, ahead={entry.queue_ahead:.0f}",
                                    queue_ahead=entry.queue_ahead)
                    continue
                fill_size = min(leftover, order.remaining_size)

            if fill_size <= 0:
                continue

            fill = SimFill(
                order_id=order.id, token_id=order.token_id,
                token_name=order.token_name, side=order.side,
                price=entry.price, size=fill_size,
            )
            all_fills.append(fill)
            apply_fills(order, [fill])

            if order.remaining_size <= 0.001:
                order.add_event("FILL", OrderStatus.MATCHED,
                                f"Fully filled {order.filled_size:.0f} @ {order.avg_fill_price:.4f}",
                                fill_price=entry.price, fill_size=fill_size)
                self.entries.pop(oid, None)
            else:
                order.add_event("PARTIAL_FILL", OrderStatus.PARTIALLY_FILLED,
                                f"Partial {fill_size:.0f} @ {entry.price:.4f}, remaining={order.remaining_size:.0f}",
                                fill_price=entry.price, fill_size=fill_size,
                                queue_ahead=entry.queue_ahead)

            log.info("GTC %s: filled %.0f @ %.4f (queue=%.0f, through=%s)",
                     order.id, fill_size, entry.price, entry.queue_ahead, trade_through)

        return all_fills

    def on_level_change(self, token_id: str, side_str: str, price: float,
                        old_size: float, new_size: float):
        """Cancels at our price reduce queue proportionally."""
        if new_size >= old_size:
            return
        removed = old_size - new_size

        for entry in self.entries.values():
            order = entry.order
            if order.token_id != token_id or abs(entry.price - price) > 0.00001:
                continue
            if order.side == Side.BUY and side_str != "BUY":
                continue
            if order.side == Side.SELL and side_str != "SELL":
                continue
            if entry.queue_ahead <= 0:
                continue

            if old_size > 0:
                fraction = removed / old_size
                reduction = fraction * entry.queue_ahead
                entry.queue_ahead = max(0, entry.queue_ahead - reduction)

    def check_expirations(self) -> list[SimOrder]:
        """Check GTD orders for expiration. Returns expired orders."""
        now = time.time()
        expired = []
        for oid, entry in list(self.entries.items()):
            order = entry.order
            if order.order_type == OrderType.GTD and order.expiration and now >= order.expiration:
                order.add_event("EXPIRATION", OrderStatus.CANCELED,
                                f"GTD expired at {order.expiration}")
                self.entries.pop(oid, None)
                expired.append(order)
        return expired

    def get_queue_info(self) -> list[dict]:
        info = []
        for oid, entry in self.entries.items():
            o = entry.order
            info.append({
                "order_id": oid,
                "token_name": o.token_name,
                "side": o.side.value,
                "order_type": o.order_type.value,
                "price": entry.price,
                "size": o.size,
                "filled": round(o.filled_size, 1),
                "remaining": round(o.remaining_size, 1),
                "queue_ahead": round(entry.queue_ahead, 1),
                "initial_queue": round(entry.initial_queue, 1),
                "queue_pct": round(
                    (entry.queue_ahead / entry.initial_queue * 100)
                    if entry.initial_queue > 0 else 0, 1
                ),
                "expiration": o.expiration,
            })
        return info
