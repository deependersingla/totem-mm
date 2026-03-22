"""Polls the Goldsky subgraph for definitive fill events with maker/taker attribution.

This is the ONLY source of individual order fills with wallet addresses for both sides.
The REST data-api/trades endpoint gives taker trades, but the subgraph gives maker+taker
for every fill on-chain.

Records:
- maker address (resting order owner)
- taker address (aggressive order owner)
- makerAmountFilled / takerAmountFilled (exact fill amounts in base units)
- fee, timestamp, transactionHash, orderHash
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Optional

import httpx

log = logging.getLogger(__name__)

GOLDSKY_ENDPOINT = (
    "https://api.goldsky.com/api/public/"
    "project_cl6mb8i9h0003e201j6li0diw/subgraphs/"
    "orderbook-subgraph/0.0.1/gn"
)

USDC_DECIMALS = 6
CTF_DECIMALS = 6

# Query fills by token IDs (makerAssetId or takerAssetId matches our tokens)
# The subgraph doesn't have a "condition" field — we filter by asset IDs.
FILL_QUERY_BY_MAKER_ASSET = """
query($tokenId: String!, $cursor: String!) {
  orderFilledEvents(
    where: { makerAssetId: $tokenId, id_gt: $cursor }
    orderBy: id
    orderDirection: asc
    first: 500
  ) {
    id
    maker
    taker
    makerAssetId
    takerAssetId
    makerAmountFilled
    takerAmountFilled
    fee
    timestamp
    transactionHash
    orderHash
  }
}
"""

FILL_QUERY_BY_TAKER_ASSET = """
query($tokenId: String!, $cursor: String!) {
  orderFilledEvents(
    where: { takerAssetId: $tokenId, id_gt: $cursor }
    orderBy: id
    orderDirection: asc
    first: 500
  ) {
    id
    maker
    taker
    makerAssetId
    takerAssetId
    makerAmountFilled
    takerAmountFilled
    fee
    timestamp
    transactionHash
    orderHash
  }
}
"""


def _parse_amount(raw: str) -> float:
    """Convert base-unit string to human-readable float."""
    try:
        return int(raw) / (10 ** USDC_DECIMALS)
    except (ValueError, TypeError):
        return 0.0


def _classify_fill(fill: dict, token_ids: list[str]) -> dict:
    """Classify a subgraph fill into a readable trade record.

    On Polymarket CTF Exchange:
    - BUY side: maker gives USDC (makerAssetId = 0 or USDC), gets tokens
    - SELL side: maker gives tokens (makerAssetId = token_id), gets USDC
    """
    maker = fill.get("maker", "").lower()
    taker = fill.get("taker", "").lower()
    maker_asset = fill.get("makerAssetId", "")
    taker_asset = fill.get("takerAssetId", "")
    maker_amount = _parse_amount(fill.get("makerAmountFilled", "0"))
    taker_amount = _parse_amount(fill.get("takerAmountFilled", "0"))
    fee = _parse_amount(fill.get("fee", "0"))
    ts = int(fill.get("timestamp", 0))

    # Determine which token was traded and the direction
    token_id = ""
    if maker_asset in token_ids:
        token_id = maker_asset
        # Maker gave tokens → maker is SELLING
        maker_side = "SELL"
        taker_side = "BUY"
        size = maker_amount  # tokens
        price = taker_amount / maker_amount if maker_amount > 0 else 0
    elif taker_asset in token_ids:
        token_id = taker_asset
        # Taker gave tokens → taker is SELLING
        maker_side = "BUY"
        taker_side = "SELL"
        size = taker_amount  # tokens
        price = maker_amount / taker_amount if taker_amount > 0 else 0
    else:
        # Can't determine — use amounts heuristically
        token_id = maker_asset or taker_asset
        maker_side = "UNKNOWN"
        taker_side = "UNKNOWN"
        size = max(maker_amount, taker_amount)
        price = min(maker_amount, taker_amount) / size if size > 0 else 0

    return {
        "id": fill.get("id", ""),
        "maker": maker,
        "taker": taker,
        "maker_side": maker_side,
        "taker_side": taker_side,
        "token_id": token_id,
        "size": round(size, 6),
        "price": round(price, 6),
        "notional": round(size * price, 6),
        "fee": round(fee, 6),
        "timestamp": ts,
        "tx_hash": fill.get("transactionHash", ""),
        "order_hash": fill.get("orderHash", ""),
    }


class FillPoller:
    """Polls Goldsky subgraph for all fill events in a market."""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._condition_id: Optional[str] = None
        self._token_ids: list[str] = []
        self._cursor = ""
        self._seen_ids: set[str] = set()
        self.fills: deque[dict] = deque(maxlen=5000)
        self.total_count = 0
        self._on_new_fills: list = []

    def on_new_fills(self, cb):
        self._on_new_fills.append(cb)

    def start(self, condition_id: str, token_ids: list[str], poll_interval: float = 10.0):
        self.stop()
        self._condition_id = condition_id
        self._token_ids = token_ids
        # Per-token cursors for paginating both maker and taker queries
        self._cursors: dict[str, str] = {}
        for tid in token_ids:
            self._cursors[f"maker_{tid}"] = ""
            self._cursors[f"taker_{tid}"] = ""
        self._seen_ids.clear()
        self.fills.clear()
        self.total_count = 0
        self._task = asyncio.create_task(self._poll_loop(poll_interval))

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
            self._task = None

    async def _poll_loop(self, interval: float):
        while True:
            try:
                await self._fetch_fills()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("Goldsky poll error: %s", e)
            await asyncio.sleep(interval)

    async def _fetch_fills(self):
        if not self._token_ids:
            return

        new_fills = []
        async with httpx.AsyncClient(timeout=30) as client:
            # Query fills where our tokens appear as makerAssetId OR takerAssetId
            for tid in self._token_ids:
                for side, query in [
                    ("maker", FILL_QUERY_BY_MAKER_ASSET),
                    ("taker", FILL_QUERY_BY_TAKER_ASSET),
                ]:
                    cursor_key = f"{side}_{tid}"
                    cursor = self._cursors.get(cursor_key, "")

                    try:
                        resp = await client.post(GOLDSKY_ENDPOINT, json={
                            "query": query,
                            "variables": {"tokenId": tid, "cursor": cursor},
                        })
                        if resp.status_code != 200:
                            continue

                        data = resp.json()
                        events = data.get("data", {}).get("orderFilledEvents", [])
                        for raw_fill in events:
                            fid = raw_fill.get("id", "")
                            if fid in self._seen_ids:
                                # Still advance cursor
                                self._cursors[cursor_key] = fid
                                continue
                            self._seen_ids.add(fid)
                            self._cursors[cursor_key] = fid

                            classified = _classify_fill(raw_fill, self._token_ids)
                            self.fills.append(classified)
                            new_fills.append(classified)
                            self.total_count += 1
                    except Exception as e:
                        log.debug("Goldsky query error for %s/%s: %s", side, tid[:8], e)

        if new_fills:
            log.info("Goldsky: %d new fills (total %d)", len(new_fills), self.total_count)
            for cb in self._on_new_fills:
                cb(new_fills)

    def get_recent_fills(self, limit: int = 50) -> list[dict]:
        return list(self.fills)[-limit:]

    def get_fills_by_wallet(self, address: str, limit: int = 50) -> list[dict]:
        addr = address.lower()
        matches = [f for f in self.fills if f["maker"] == addr or f["taker"] == addr]
        return matches[-limit:]

    def get_summary(self) -> dict:
        makers = set()
        takers = set()
        for f in self.fills:
            if f["maker"]:
                makers.add(f["maker"])
            if f["taker"]:
                takers.add(f["taker"])
        return {
            "total_fills": self.total_count,
            "unique_makers": len(makers),
            "unique_takers": len(takers),
        }
