"""Central application state — wires all components together.

Data flow:
  Polymarket WS → orderbook → level_change → GTC queue + sniping + event_recorder
  Polymarket WS → orderbook → record_fill (last_trade_price) → GTC queue consumption → fills
  Polymarket WS → raw messages → event_recorder (every event, nanosecond timestamps)
  Data API poll → wallet_tracker → wallet stats + event_recorder
  Wallet book manager → subset books from specific wallet sets

All live data comes from Polymarket directly:
  - WS for real-time book + trades (primary — nanosecond recorded)
  - REST data-api for wallet-attributed trades (5s poll)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Optional

from fastapi import WebSocket

from .config import BOOK_BROADCAST_INTERVAL, DEFAULT_CASH
from .event_recorder import EventRecorder
from .market_ws import MarketWebSocket
from .models import (
    MarketInfo,
    OrderBookSnapshot,
    SimFill,
)
from .order_manager import OrderManager
from .orderbook import OrderBook
from .position_tracker import PositionTracker
from .sniping_detector import SnipingDetector
from .wallet_book import WalletBookManager
from .wallet_tracker import WalletTracker

log = logging.getLogger(__name__)


class AppState:
    def __init__(self, recordings_dir: str = "recordings"):
        self.market_info: Optional[MarketInfo] = None
        self.books: dict[str, OrderBook] = {}
        self.position = PositionTracker(initial_cash=DEFAULT_CASH)
        self.order_manager = OrderManager(self.position)
        self.sniping_detector = SnipingDetector()
        self.wallet_tracker = WalletTracker()
        self.wallet_book_mgr = WalletBookManager()
        self.event_recorder = EventRecorder(output_dir=recordings_dir)
        self.market_ws: Optional[MarketWebSocket] = None

        # UI WebSocket clients
        self.ws_clients: set[WebSocket] = set()

        # Throttling
        self._book_dirty = False
        self._wallet_dirty = False
        self._wallet_book_dirty = False
        self._broadcast_task: Optional[asyncio.Task] = None

        # Event log
        self.event_log: deque[dict] = deque(maxlen=500)

    async def switch_market(self, market_info: MarketInfo):
        """Switch to a new market — reset everything, reconnect WS."""
        log.info("Switching to market: %s", market_info.question)

        # Stop existing
        if self.market_ws:
            self.market_ws.stop()
        self.wallet_tracker.reset()
        self.wallet_book_mgr.stop_all()
        self.event_recorder.stop()

        self.market_info = market_info

        # Start recording
        self.event_recorder.start(market_info.slug, market_info.question)

        # Reset books
        self.books = {tid: OrderBook(tid) for tid in market_info.token_ids}

        # Reset position and orders
        self.position = PositionTracker(initial_cash=DEFAULT_CASH)
        self.order_manager = OrderManager(self.position)
        self.order_manager.on_fill(self._on_fill)

        # Setup sniping detector
        self.sniping_detector = SnipingDetector()
        self.sniping_detector.set_token_names(market_info.token_to_name)

        # Setup wallet book manager
        self.wallet_book_mgr = WalletBookManager()
        self.wallet_book_mgr.set_token_ids(market_info.token_ids)

        # Wire orderbook events
        for book in self.books.values():
            book.on_update(lambda: self._on_book_update())

            # GTC queue + event recording for level changes
            book.on_level_change(
                lambda tid, side, price, old_s, new_s:
                    self._on_level_change(tid, side, price, old_s, new_s)
            )

            # GTC queue + event recording for trades
            book.on_trade(
                lambda tid, price, size, side:
                    self._on_trade(tid, price, size, side)
            )

        # Start market WS with raw message recording
        self.market_ws = MarketWebSocket(
            self.books,
            on_update=self._on_ws_update,
            on_raw_message=self._on_raw_ws_message,
        )
        self.market_ws.start(market_info.token_ids)

        # Start wallet tracker (REST trades with wallet attribution)
        if market_info.condition_id:
            self.wallet_tracker = WalletTracker()
            self.wallet_tracker.on_new_trades(self._on_new_market_trades)
            self.wallet_tracker.start(market_info.condition_id, poll_interval=5.0)

        # Start broadcast loop
        if self._broadcast_task:
            self._broadcast_task.cancel()
        self._broadcast_task = asyncio.create_task(self._broadcast_loop())

        await self.broadcast("market_changed", {
            "question": market_info.question,
            "slug": market_info.slug,
            "outcomes": market_info.outcome_names,
            "token_ids": market_info.token_ids,
            "condition_id": market_info.condition_id,
        })

    def _on_raw_ws_message(self, raw_text: str):
        """Record every raw WS message for post-match analysis."""
        self.event_recorder.record_raw_ws(raw_text)

    def _on_level_change(self, token_id: str, side: str, price: float,
                          old_size: float, new_size: float):
        """Handle level changes — forward to GTC matcher + record."""
        self.order_manager.on_level_change(token_id, side, price, old_size, new_size)
        self.event_recorder.record_level_transition(token_id, side, price, old_size, new_size)

    def _on_trade(self, token_id: str, price: float, size: float, side: str):
        """Handle trades — forward to GTC matcher + record."""
        self.order_manager.on_trade(token_id, price, size, side)
        self.event_recorder.record_trade(token_id, price, size, side)

    def _on_book_update(self):
        """Synchronous callback from orderbook — mark dirty, process sniping."""
        self._book_dirty = True

        for book in self.books.values():
            new_snipes = self.sniping_detector.process_transitions(book)
            for snipe in new_snipes:
                self._log_event("snipe", {
                    "token_name": snipe.token_name,
                    "side": snipe.side,
                    "price": snipe.price,
                    "size": snipe.size_disappeared,
                    "duration_ms": snipe.duration_ms,
                })
                # Record cancel/snipe to event recorder
                self.event_recorder.record_cancel_detected(
                    snipe.token_id, snipe.side, snipe.price,
                    snipe.size_disappeared,
                    had_fill=False,
                )

    async def _on_ws_update(self):
        pass

    def _on_new_market_trades(self, trades: list[dict]):
        """Callback from wallet tracker — new real trades detected."""
        self._wallet_dirty = True
        for t in trades:
            self._log_event("market_trade", t)
            self.event_recorder.record_rest_trade(t)

    async def _broadcast_loop(self):
        """Throttled broadcast of all live data to UI."""
        wallet_book_counter = 0
        while True:
            await asyncio.sleep(BOOK_BROADCAST_INTERVAL)

            # Check GTD expirations
            expired = self.order_manager.check_expirations()
            for o in expired:
                await self.broadcast("order_update", {
                    "id": o.id, "status": o.status.value,
                    "order_type": o.order_type.value,
                })

            if self._book_dirty:
                self._book_dirty = False
                snapshots = self.get_book_snapshots()
                for tid, snap in snapshots.items():
                    await self.broadcast("book", {
                        "token_id": tid,
                        "token_name": self.market_info.token_to_name.get(tid, "") if self.market_info else "",
                        "bids": [{"price": l.price, "size": l.size} for l in snap.bids[:30]],
                        "asks": [{"price": l.price, "size": l.size} for l in snap.asks[:30]],
                        "best_bid": snap.best_bid,
                        "best_ask": snap.best_ask,
                        "mid_price": snap.mid_price,
                        "spread": snap.spread,
                    })
                await self.broadcast("position", self.position.to_dict(snapshots))

                queue_info = self.order_manager.get_queue_info()
                if queue_info:
                    await self.broadcast("queue_info", queue_info)

            if self._wallet_dirty:
                self._wallet_dirty = False
                await self.broadcast("wallet_update", {
                    "summary": self.wallet_tracker.get_summary(),
                    "top_wallets": self.wallet_tracker.get_top_wallets(20),
                    "watched": self.wallet_tracker.get_watched_wallets(),
                    "recent_trades": self.wallet_tracker.get_recent_trades(20),
                })

            # Wallet subset books — broadcast every 10 cycles (1 second)
            wallet_book_counter += 1
            if wallet_book_counter >= 10 and self.wallet_book_mgr.subsets:
                wallet_book_counter = 0
                if self.market_info and self.market_info.token_ids:
                    tid = self.market_info.token_ids[0]  # primary token
                    snapshots = self.wallet_book_mgr.get_all_snapshots(tid)
                    if snapshots:
                        await self.broadcast("wallet_books", snapshots)

    def _on_fill(self, fill: SimFill):
        self._log_event("fill", {
            "order_id": fill.order_id,
            "token_name": fill.token_name,
            "side": fill.side.value,
            "price": fill.price,
            "size": fill.size,
            "notional": fill.notional,
        })
        asyncio.ensure_future(self.broadcast("fill", {
            "order_id": fill.order_id,
            "token_name": fill.token_name,
            "side": fill.side.value,
            "price": round(fill.price, 4),
            "size": round(fill.size, 2),
            "notional": round(fill.notional, 2),
            "timestamp": fill.timestamp,
        }))

    def get_book_snapshots(self) -> dict[str, OrderBookSnapshot]:
        return {tid: book.snapshot() for tid, book in self.books.items()}

    def _log_event(self, event_type: str, data: dict):
        self.event_log.append({
            "type": event_type,
            "data": data,
            "timestamp": time.time(),
        })

    async def broadcast(self, msg_type: str, data):
        if not self.ws_clients:
            return
        msg = json.dumps({"type": msg_type, "data": data})
        dead = set()
        for ws in self.ws_clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        self.ws_clients -= dead
