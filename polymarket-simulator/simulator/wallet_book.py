"""Wallet-subset order book — reconstructs an order book from only specific wallets.

Uses the Goldsky subgraph to fetch open orders for specific maker addresses,
then maintains a virtual book showing only those wallets' liquidity.

Multiple subsets can be created simultaneously (e.g. "market makers", "snipers", etc).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Optional

import httpx

from .config import CLOB_API

log = logging.getLogger(__name__)

# Polymarket CLOB open orders endpoint
OPEN_ORDERS_URL = f"{CLOB_API}/data/orders"


class WalletSubsetBook:
    """Tracks open orders for a subset of wallets to build a filtered order book."""

    def __init__(self, name: str, wallet_addresses: list[str], token_ids: list[str]):
        self.name = name
        self.wallets = set(addr.lower() for addr in wallet_addresses)
        self.token_ids = token_ids
        # token_id -> side -> price -> size from these wallets
        self.books: dict[str, dict[str, dict[float, float]]] = {
            tid: {"bids": {}, "asks": {}} for tid in token_ids
        }
        self._poll_task: Optional[asyncio.Task] = None
        self.last_updated = 0.0
        self.total_bid_depth = 0.0
        self.total_ask_depth = 0.0

    def start(self, poll_interval: float = 10.0):
        self.stop()
        self._poll_task = asyncio.create_task(self._poll_loop(poll_interval))

    def stop(self):
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            self._poll_task = None

    async def _poll_loop(self, interval: float):
        while True:
            try:
                await self._refresh()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("Wallet book '%s' poll error: %s", self.name, e)
            await asyncio.sleep(interval)

    async def _refresh(self):
        """Fetch open orders for our wallet set and rebuild the book."""
        # Reset books
        for tid in self.token_ids:
            self.books[tid] = {"bids": {}, "asks": {}}

        async with httpx.AsyncClient(timeout=15) as client:
            for wallet in self.wallets:
                for tid in self.token_ids:
                    try:
                        resp = await client.get(OPEN_ORDERS_URL, params={
                            "market": tid,
                            "maker": wallet,
                        })
                        if resp.status_code != 200:
                            continue
                        orders = resp.json()
                        if not isinstance(orders, list):
                            continue
                        for order in orders:
                            price = float(order.get("price", 0))
                            size_remaining = float(order.get("original_size", 0)) - float(order.get("size_matched", 0))
                            side = order.get("side", "")
                            if size_remaining <= 0 or price <= 0:
                                continue
                            side_key = "bids" if side == "BUY" else "asks"
                            self.books[tid][side_key][price] = self.books[tid][side_key].get(price, 0) + size_remaining
                    except Exception:
                        continue

        # Compute depths
        self.total_bid_depth = sum(
            sum(p * s for p, s in book["bids"].items())
            for book in self.books.values()
        )
        self.total_ask_depth = sum(
            sum(p * s for p, s in book["asks"].items())
            for book in self.books.values()
        )
        self.last_updated = time.time()

    def get_snapshot(self, token_id: str) -> dict:
        """Get sorted book snapshot for a token."""
        book = self.books.get(token_id, {"bids": {}, "asks": {}})
        bids = sorted(book["bids"].items(), key=lambda x: -x[0])
        asks = sorted(book["asks"].items(), key=lambda x: x[0])
        return {
            "name": self.name,
            "token_id": token_id,
            "bids": [{"price": p, "size": s} for p, s in bids[:20]],
            "asks": [{"price": p, "size": s} for p, s in asks[:20]],
            "total_bid_depth": round(self.total_bid_depth, 2),
            "total_ask_depth": round(self.total_ask_depth, 2),
            "wallet_count": len(self.wallets),
            "last_updated": self.last_updated,
        }

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "wallets": list(self.wallets),
            "wallet_count": len(self.wallets),
            "total_bid_depth": round(self.total_bid_depth, 2),
            "total_ask_depth": round(self.total_ask_depth, 2),
            "last_updated": self.last_updated,
        }


class WalletBookManager:
    """Manages multiple wallet-subset order books."""

    def __init__(self):
        self.subsets: dict[str, WalletSubsetBook] = {}
        self._token_ids: list[str] = []

    def set_token_ids(self, token_ids: list[str]):
        self._token_ids = token_ids

    def create_subset(self, name: str, wallets: list[str], poll_interval: float = 10.0) -> WalletSubsetBook:
        """Create a new wallet subset book."""
        if name in self.subsets:
            self.subsets[name].stop()

        subset = WalletSubsetBook(name, wallets, self._token_ids)
        self.subsets[name] = subset
        subset.start(poll_interval)
        return subset

    def remove_subset(self, name: str):
        subset = self.subsets.pop(name, None)
        if subset:
            subset.stop()

    def add_wallet_to_subset(self, name: str, wallet: str):
        subset = self.subsets.get(name)
        if subset:
            subset.wallets.add(wallet.lower())

    def remove_wallet_from_subset(self, name: str, wallet: str):
        subset = self.subsets.get(name)
        if subset:
            subset.wallets.discard(wallet.lower())

    def stop_all(self):
        for subset in self.subsets.values():
            subset.stop()
        self.subsets.clear()

    def get_all_snapshots(self, token_id: str) -> list[dict]:
        return [s.get_snapshot(token_id) for s in self.subsets.values()]

    def list_subsets(self) -> list[dict]:
        return [s.to_dict() for s in self.subsets.values()]
