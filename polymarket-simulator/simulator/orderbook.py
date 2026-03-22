"""L2 order book maintenance — mirrors Polymarket WS data.

Emits level-change events for GTC queue adjustment and sniping detection.
Tracks last_trade_price events for cancel/fill disambiguation.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Callable, Optional

from .config import SNIPE_BUFFER_SIZE
from .models import OrderBookSnapshot, PriceLevel


class LevelTransition:
    """Records a price-level change for sniping detection."""
    __slots__ = ("timestamp", "token_id", "side", "price", "old_size", "new_size")

    def __init__(self, timestamp: float, token_id: str, side: str,
                 price: float, old_size: float, new_size: float):
        self.timestamp = timestamp
        self.token_id = token_id
        self.side = side
        self.price = price
        self.old_size = old_size
        self.new_size = new_size


class OrderBook:
    """Maintains L2 book for a single token."""

    def __init__(self, token_id: str):
        self.token_id = token_id
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.timestamp_ms: int = 0
        self.transitions: deque[LevelTransition] = deque(maxlen=SNIPE_BUFFER_SIZE)

        # Recent fills from last_trade_price events
        self._recent_fills: list[dict] = []
        self._fill_window = 2.0

        # Callbacks
        self._on_update: list[Callable[[], None]] = []
        self._on_level_change: list[Callable[[str, str, float, float, float], None]] = []
        self._on_trade: list[Callable[[str, float, float, str], None]] = []

    def on_update(self, cb: Callable[[], None]):
        self._on_update.append(cb)

    def on_level_change(self, cb: Callable[[str, str, float, float, float], None]):
        """Register callback: cb(token_id, side, price, old_size, new_size)."""
        self._on_level_change.append(cb)

    def on_trade(self, cb: Callable[[str, float, float, str], None]):
        """Register callback: cb(token_id, price, size, side)."""
        self._on_trade.append(cb)

    def _fire_update(self):
        for cb in self._on_update:
            cb()

    def _fire_level_change(self, side: str, price: float, old_size: float, new_size: float):
        for cb in self._on_level_change:
            cb(self.token_id, side, price, old_size, new_size)

    def _fire_trade(self, price: float, size: float, side: str):
        for cb in self._on_trade:
            cb(self.token_id, price, size, side)

    # ── Snapshots ──────────────────────────────────────────────────

    def snapshot(self) -> OrderBookSnapshot:
        sorted_bids = sorted(self.bids.items(), key=lambda x: -x[0])
        sorted_asks = sorted(self.asks.items(), key=lambda x: x[0])
        return OrderBookSnapshot(
            token_id=self.token_id,
            bids=[PriceLevel(price=p, size=s) for p, s in sorted_bids],
            asks=[PriceLevel(price=p, size=s) for p, s in sorted_asks],
            timestamp_ms=self.timestamp_ms,
        )

    # ── Apply updates ──────────────────────────────────────────────

    def apply_snapshot(self, bids: list[dict], asks: list[dict]):
        """Replace entire book from a 'book' event."""
        self.bids.clear()
        self.asks.clear()
        for b in bids:
            p, s = float(b.get("price", 0)), float(b.get("size", 0))
            if s > 0:
                self.bids[p] = s
        for a in asks:
            p, s = float(a.get("price", 0)), float(a.get("size", 0))
            if s > 0:
                self.asks[p] = s
        self.timestamp_ms = int(time.time() * 1000)
        self._fire_update()

    def apply_price_changes(self, changes: list[dict]):
        """Incremental update — emits level-change events for GTC queue tracking."""
        now = time.time()
        for change in changes:
            price = float(change.get("price", 0))
            new_size = float(change.get("size", 0))
            side_str = change.get("side", "")

            side_dict = self.bids if side_str == "BUY" else self.asks
            old_size = side_dict.get(price, 0.0)

            if abs(new_size - old_size) > 0.001:
                # Record for sniping
                self.transitions.append(LevelTransition(
                    timestamp=now, token_id=self.token_id, side=side_str,
                    price=price, old_size=old_size, new_size=new_size,
                ))
                # Notify GTC matcher about level changes
                self._fire_level_change(side_str, price, old_size, new_size)

            if new_size <= 0:
                side_dict.pop(price, None)
            else:
                side_dict[price] = new_size

        self.timestamp_ms = int(time.time() * 1000)
        self._fire_update()

    # ── Trade recording ────────────────────────────────────────────

    def record_fill(self, price: float, size: float, side: str):
        """Process a last_trade_price event.

        This is critical for:
        1. Cancel vs fill disambiguation (sniping detector)
        2. GTC order queue consumption (realistic matching)
        """
        now = time.time()
        self._recent_fills.append({
            "time": now, "price": price, "size": size, "side": side,
        })
        cutoff = now - self._fill_window * 2
        self._recent_fills = [f for f in self._recent_fills if f["time"] > cutoff]

        # Fire trade callback — this drives GTC fills
        self._fire_trade(price, size, side)

    def recent_fill_at_price(self, price: float, side: str) -> bool:
        cutoff = time.time() - self._fill_window
        for fill in reversed(self._recent_fills):
            if fill["time"] < cutoff:
                break
            if abs(fill["price"] - price) < 0.0001 and fill["side"] == side:
                return True
        return False
