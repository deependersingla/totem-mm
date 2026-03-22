"""Records every WS event with nanosecond timestamps for post-match analysis.

Stores: every order book change, every trade, every cancel, every fill — all raw
events from the Polymarket WebSocket that won't be available after a market closes.

Data is written to JSONL files (one JSON object per line) for efficient append
and post-processing.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional


class EventRecorder:
    """Appends raw WS events to a JSONL file with nanosecond timestamps."""

    def __init__(self, output_dir: str = "recordings"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._file = None
        self._file_path: Optional[Path] = None
        self._event_count = 0
        self._started_at: Optional[float] = None
        self.recording = False

    def start(self, slug: str, market_question: str = ""):
        """Start recording for a market."""
        self.stop()
        ts = time.strftime("%Y%m%d_%H%M%S")
        safe_slug = slug.replace("/", "_")[:80]
        filename = f"{ts}_{safe_slug}.jsonl"
        self._file_path = self.output_dir / filename
        self._file = open(self._file_path, "a")
        self._event_count = 0
        self._started_at = time.time()
        self.recording = True

        # Write header event
        self._write_event("recording_start", {
            "slug": slug,
            "question": market_question,
            "started_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        })

    def stop(self):
        if self._file:
            if self.recording:
                self._write_event("recording_stop", {
                    "total_events": self._event_count,
                    "duration_seconds": time.time() - (self._started_at or 0),
                })
            self._file.close()
            self._file = None
        self.recording = False

    def record_raw_ws(self, raw_text: str):
        """Record a raw WS message exactly as received."""
        if not self.recording:
            return
        self._write_event("ws_raw", {"payload": raw_text})

    def record_book_snapshot(self, token_id: str, bids: list, asks: list):
        """Record a full book snapshot."""
        if not self.recording:
            return
        self._write_event("book_snapshot", {
            "token_id": token_id,
            "bid_levels": len(bids),
            "ask_levels": len(asks),
            "bids": bids[:50],  # top 50 levels
            "asks": asks[:50],
        })

    def record_price_change(self, token_id: str, changes: list):
        """Record incremental price changes — every order add/modify/cancel."""
        if not self.recording:
            return
        self._write_event("price_change", {
            "token_id": token_id,
            "changes": changes,
        })

    def record_trade(self, token_id: str, price: float, size: float, side: str):
        """Record a last_trade_price event — every matched fill."""
        if not self.recording:
            return
        self._write_event("trade", {
            "token_id": token_id,
            "price": price,
            "size": size,
            "side": side,
        })

    def record_level_transition(self, token_id: str, side: str, price: float,
                                 old_size: float, new_size: float):
        """Record every level size change with old/new — detect orders added/removed."""
        if not self.recording:
            return
        delta = new_size - old_size
        event_subtype = "order_added" if delta > 0 else "order_removed"
        self._write_event(event_subtype, {
            "token_id": token_id,
            "side": side,
            "price": price,
            "old_size": old_size,
            "new_size": new_size,
            "delta": round(delta, 6),
        })

    def record_cancel_detected(self, token_id: str, side: str, price: float,
                                size: float, had_fill: bool):
        """Record when we detect a probable cancel (size decrease without matching fill)."""
        if not self.recording:
            return
        self._write_event("cancel_detected", {
            "token_id": token_id,
            "side": side,
            "price": price,
            "size_removed": size,
            "had_matching_fill": had_fill,
        })

    def record_rest_trade(self, trade_data: dict):
        """Record a trade from the REST API with wallet attribution."""
        if not self.recording:
            return
        self._write_event("rest_trade", trade_data)

    def _write_event(self, event_type: str, data: dict):
        if not self._file:
            return
        # time.time_ns() gives nanosecond precision
        record = {
            "ts_ns": time.time_ns(),
            "ts": time.time(),
            "type": event_type,
            **data,
        }
        self._file.write(json.dumps(record) + "\n")
        self._file.flush()
        self._event_count += 1

    def get_status(self) -> dict:
        return {
            "recording": self.recording,
            "file": str(self._file_path) if self._file_path else None,
            "event_count": self._event_count,
            "duration_seconds": round(time.time() - self._started_at, 1) if self._started_at else 0,
        }
