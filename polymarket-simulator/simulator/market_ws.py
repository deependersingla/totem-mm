"""WebSocket client for Polymarket market data."""

from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets

from .config import WS_PING_INTERVAL, WS_RECONNECT_BASE, WS_RECONNECT_MAX, WS_URL
from .orderbook import OrderBook

log = logging.getLogger(__name__)


class MarketWebSocket:
    """Connects to Polymarket WS and maintains order books."""

    def __init__(self, books: dict[str, OrderBook], on_update=None, on_raw_message=None):
        self.books = books
        self.on_update = on_update  # async callback after any book update
        self.on_raw_message = on_raw_message  # sync callback for every raw WS message
        self._cancel = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self, token_ids: list[str]):
        """Start (or restart) the WS connection for given token IDs."""
        self.stop()
        self._cancel.clear()
        self._task = asyncio.create_task(self._run(token_ids))

    def stop(self):
        if self._task and not self._task.done():
            self._cancel.set()
            self._task.cancel()
            self._task = None
        self._connected = False

    async def _run(self, token_ids: list[str]):
        backoff = WS_RECONNECT_BASE
        while not self._cancel.is_set():
            try:
                log.info("Connecting to Polymarket WS for %d tokens...", len(token_ids))
                async with websockets.connect(WS_URL, ping_interval=None) as ws:
                    # Subscribe
                    sub_msg = json.dumps({
                        "assets_ids": token_ids,
                        "type": "market",
                    })
                    await ws.send(sub_msg)
                    self._connected = True
                    backoff = WS_RECONNECT_BASE
                    log.info("WS connected and subscribed")

                    # Ping task
                    async def keepalive():
                        while True:
                            await asyncio.sleep(WS_PING_INTERVAL)
                            try:
                                await ws.send("PING")
                            except Exception:
                                return

                    ping_task = asyncio.create_task(keepalive())
                    try:
                        async for raw_msg in ws:
                            if self._cancel.is_set():
                                break
                            if raw_msg == "PONG":
                                continue
                            # Record raw message for event recording
                            if self.on_raw_message:
                                self.on_raw_message(raw_msg)
                            self._process_message(raw_msg)
                    finally:
                        ping_task.cancel()
                        self._connected = False

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                log.warning("WS disconnected: %s — reconnecting in %ds", e, backoff)
                try:
                    await asyncio.wait_for(self._cancel.wait(), timeout=backoff)
                    break  # cancel was set
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, WS_RECONNECT_MAX)

    def _process_message(self, text: str):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return

        events = data if isinstance(data, list) else [data]

        for event in events:
            if not isinstance(event, dict):
                continue

            # Market-level price_changes (no event_type, has price_changes array)
            if "price_changes" in event:
                for change in event["price_changes"]:
                    aid = change.get("asset_id", "")
                    if aid in self.books:
                        self.books[aid].apply_price_changes([change])
                self._notify()
                continue

            asset_id = event.get("asset_id")
            event_type = event.get("event_type") or event.get("type")

            if not asset_id or asset_id not in self.books:
                continue

            book = self.books[asset_id]

            if event_type == "book":
                book.apply_snapshot(
                    event.get("bids", []),
                    event.get("asks", []),
                )
                self._notify()

            elif event_type == "price_change":
                changes = event.get("changes", [])
                if changes:
                    book.apply_price_changes(changes)
                else:
                    # Fallback: bids/asks in event body
                    bid_changes = [
                        {"price": b.get("price", 0), "size": b.get("size", 0), "side": "BUY"}
                        for b in event.get("bids", [])
                    ]
                    ask_changes = [
                        {"price": a.get("price", 0), "size": a.get("size", 0), "side": "SELL"}
                        for a in event.get("asks", [])
                    ]
                    if bid_changes or ask_changes:
                        book.apply_price_changes(bid_changes + ask_changes)
                self._notify()

            elif event_type == "last_trade_price":
                price = float(event.get("price", 0))
                size = float(event.get("size", 0))
                side = event.get("side", "")
                book.record_fill(price, size, side)

    def _notify(self):
        if self.on_update:
            # Schedule the async callback
            try:
                loop = asyncio.get_running_loop()
                loop.call_soon(lambda: asyncio.ensure_future(self.on_update()))
            except RuntimeError:
                pass
