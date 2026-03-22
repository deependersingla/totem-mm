"""Detect flash/snipe orders — orders that appear and disappear quickly."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Optional

from .config import SNIPE_THRESHOLD_MS, SNIPE_BUFFER_SIZE
from .models import SnipeEvent
from .orderbook import LevelTransition, OrderBook


class SnipingDetector:
    """Tracks order book level transitions to detect flash orders."""

    def __init__(self):
        # Key: (token_id, side, price) -> {appeared_at, size_appeared}
        self._pending_appearances: dict[tuple, dict] = {}
        self.events: deque[SnipeEvent] = deque(maxlen=SNIPE_BUFFER_SIZE)
        self._token_names: dict[str, str] = {}

    def set_token_names(self, mapping: dict[str, str]):
        self._token_names = mapping

    def process_transitions(self, book: OrderBook):
        """Process new transitions from an order book and detect snipes."""
        now = time.time()
        new_events = []

        for t in book.transitions:
            key = (t.token_id, t.side, t.price)

            if t.new_size > t.old_size:
                # Liquidity appeared
                self._pending_appearances[key] = {
                    "appeared_at": t.timestamp,
                    "size_appeared": t.new_size - t.old_size,
                }
            elif t.new_size < t.old_size and key in self._pending_appearances:
                # Liquidity disappeared — check if it's a snipe
                appearance = self._pending_appearances.pop(key)
                duration_ms = (t.timestamp - appearance["appeared_at"]) * 1000

                # Only flag as snipe if the disappearance wasn't a fill
                if duration_ms < SNIPE_THRESHOLD_MS and not book.recent_fill_at_price(t.price, t.side):
                    disappeared = t.old_size - t.new_size
                    event = SnipeEvent(
                        token_id=t.token_id,
                        token_name=self._token_names.get(t.token_id, ""),
                        side=t.side,
                        price=t.price,
                        size_appeared=appearance["size_appeared"],
                        size_disappeared=disappeared,
                        duration_ms=duration_ms,
                        timestamp=t.timestamp,
                    )
                    self.events.append(event)
                    new_events.append(event)

        # Clean up stale pending appearances (older than 5s)
        stale_cutoff = now - 5.0
        stale_keys = [k for k, v in self._pending_appearances.items()
                      if v["appeared_at"] < stale_cutoff]
        for k in stale_keys:
            del self._pending_appearances[k]

        return new_events

    def recent_events(self, seconds: float = 300) -> list[SnipeEvent]:
        cutoff = time.time() - seconds
        return [e for e in self.events if e.timestamp > cutoff]

    def summary(self, seconds: float = 300) -> dict:
        recent = self.recent_events(seconds)
        if not recent:
            return {"count": 0, "total_notional": 0, "avg_duration_ms": 0}
        total_notional = sum(e.price * e.size_disappeared for e in recent)
        avg_dur = sum(e.duration_ms for e in recent) / len(recent)
        return {
            "count": len(recent),
            "total_notional": round(total_notional, 2),
            "avg_duration_ms": round(avg_dur, 1),
        }
