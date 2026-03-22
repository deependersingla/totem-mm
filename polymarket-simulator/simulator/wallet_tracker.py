"""Live wallet tracking — polls Data API for trades with wallet attribution."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from typing import Optional

import httpx

from .config import DATA_API

log = logging.getLogger(__name__)


class WalletStats:
    """Per-wallet aggregated stats."""
    __slots__ = (
        "address", "total_bought", "total_sold", "net_pos", "trade_count",
        "buy_count", "sell_count", "first_seen", "last_seen",
        "max_deployed", "running_deployed", "trades",
    )

    def __init__(self, address: str):
        self.address = address
        self.total_bought = 0.0
        self.total_sold = 0.0
        self.net_pos: dict[str, float] = defaultdict(float)  # outcome -> tokens
        self.trade_count = 0
        self.buy_count = 0
        self.sell_count = 0
        self.first_seen: Optional[str] = None
        self.last_seen: Optional[str] = None
        self.max_deployed = 0.0
        self.running_deployed = 0.0
        self.trades: deque[dict] = deque(maxlen=100)

    @property
    def rotation(self) -> float:
        return (self.total_bought / self.max_deployed) if self.max_deployed > 0 else 0

    @property
    def net_pnl(self) -> float:
        return self.total_sold - self.total_bought

    def to_dict(self) -> dict:
        return {
            "address": self.address,
            "total_bought": round(self.total_bought, 2),
            "total_sold": round(self.total_sold, 2),
            "net_pnl": round(self.net_pnl, 2),
            "net_pos": {k: round(v, 1) for k, v in self.net_pos.items() if abs(v) > 0.1},
            "trade_count": self.trade_count,
            "buy_count": self.buy_count,
            "sell_count": self.sell_count,
            "rotation": round(self.rotation, 1),
            "max_deployed": round(self.max_deployed, 2),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }


class WalletTracker:
    """Tracks wallet activity for a market via REST polling."""

    def __init__(self):
        self.wallets: dict[str, WalletStats] = {}  # address -> stats
        self.watched_addresses: set[str] = set()     # user-specified watchlist
        self.all_trades: deque[dict] = deque(maxlen=2000)
        self._seen_tx: set[str] = set()
        self._condition_id: Optional[str] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._on_new_trades: list = []
        self.total_trade_count = 0
        self.total_volume = 0.0

    def on_new_trades(self, cb):
        self._on_new_trades.append(cb)

    def start(self, condition_id: str, poll_interval: float = 5.0):
        """Start polling for trades."""
        self.stop()
        self._condition_id = condition_id
        self._poll_task = asyncio.create_task(self._poll_loop(poll_interval))

    def stop(self):
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            self._poll_task = None

    def reset(self):
        """Clear all state for a new market."""
        self.stop()
        self.wallets.clear()
        self.all_trades.clear()
        self._seen_tx.clear()
        self.total_trade_count = 0
        self.total_volume = 0.0

    def add_watch(self, address: str):
        self.watched_addresses.add(address.lower())

    def remove_watch(self, address: str):
        self.watched_addresses.discard(address.lower())

    async def _poll_loop(self, interval: float):
        while True:
            try:
                await self._fetch_trades()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("Trade poll error: %s", e)
            await asyncio.sleep(interval)

    async def _fetch_trades(self):
        if not self._condition_id:
            return

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{DATA_API}/trades",
                params={"market": self._condition_id, "limit": 100},
            )
            if resp.status_code != 200:
                return
            trades = resp.json()

        new_trades = []
        for t in trades:
            tx_hash = t.get("transactionHash", "")
            if not tx_hash or tx_hash in self._seen_tx:
                continue
            self._seen_tx.add(tx_hash)

            size = float(t.get("size", 0))
            price = float(t.get("price", 0))
            notional = size * price
            side = t.get("side", "BUY")
            wallet = (t.get("proxyWallet", "") or "").lower()
            outcome = t.get("outcome", t.get("asset", "?"))
            ts = t.get("timestamp", "")

            trade_record = {
                "timestamp": ts,
                "time_str": time.strftime("%H:%M:%S"),
                "outcome": outcome,
                "side": side,
                "size": size,
                "price": price,
                "notional": notional,
                "wallet": wallet,
                "tx_hash": tx_hash,
            }
            self.all_trades.append(trade_record)
            self.total_trade_count += 1
            self.total_volume += notional
            new_trades.append(trade_record)

            # Update wallet stats
            if wallet:
                if wallet not in self.wallets:
                    self.wallets[wallet] = WalletStats(wallet)
                ws = self.wallets[wallet]
                ws.trade_count += 1
                ws.trades.append(trade_record)

                if side == "BUY":
                    ws.total_bought += notional
                    ws.buy_count += 1
                    ws.net_pos[outcome] += size
                    ws.running_deployed += notional
                else:
                    ws.total_sold += notional
                    ws.sell_count += 1
                    ws.net_pos[outcome] -= size
                    ws.running_deployed -= notional
                    ws.running_deployed = max(ws.running_deployed, 0)

                ws.max_deployed = max(ws.max_deployed, ws.running_deployed)
                if ws.first_seen is None:
                    ws.first_seen = ts or time.strftime("%H:%M:%S")
                ws.last_seen = ts or time.strftime("%H:%M:%S")

        if new_trades:
            for cb in self._on_new_trades:
                cb(new_trades)

    # ── Queries ────────────────────────────────────────────────────

    def get_top_wallets(self, limit: int = 30, sort_by: str = "trade_count") -> list[dict]:
        """Get top wallets sorted by specified metric."""
        wallets = list(self.wallets.values())
        if sort_by == "volume":
            wallets.sort(key=lambda w: w.total_bought + w.total_sold, reverse=True)
        elif sort_by == "pnl":
            wallets.sort(key=lambda w: w.net_pnl, reverse=True)
        else:
            wallets.sort(key=lambda w: w.trade_count, reverse=True)
        return [w.to_dict() for w in wallets[:limit]]

    def get_watched_wallets(self) -> list[dict]:
        """Get stats for watched wallets only."""
        results = []
        for addr in self.watched_addresses:
            ws = self.wallets.get(addr)
            if ws:
                d = ws.to_dict()
                d["watched"] = True
                results.append(d)
            else:
                results.append({
                    "address": addr,
                    "watched": True,
                    "trade_count": 0,
                    "total_bought": 0,
                    "total_sold": 0,
                })
        return results

    def get_recent_trades(self, limit: int = 50) -> list[dict]:
        return list(self.all_trades)[-limit:]

    def get_wallet_trades(self, address: str, limit: int = 50) -> list[dict]:
        addr = address.lower()
        ws = self.wallets.get(addr)
        if not ws:
            return []
        return list(ws.trades)[-limit:]

    def get_summary(self) -> dict:
        return {
            "total_trades": self.total_trade_count,
            "total_volume": round(self.total_volume, 2),
            "unique_wallets": len(self.wallets),
            "watched_count": len(self.watched_addresses),
        }
