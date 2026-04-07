#!/usr/bin/env python3
"""Live Match Capture — Order book, trades, on-chain fills, cricket events.

Streams ALL data into a SQLite database for post-match analysis.

Usage:
    python live_capture.py --slug cricipl-raj-mum-2026-04-07 --match a-rz--cricket--Og123456
    python live_capture.py --slug cricipl-raj-mum-2026-04-07   # without cricket

Requires: pip install websockets httpx aiosqlite python-dotenv
Optional: CRICKET_API_KEY in .env for cricket events
"""

import argparse
import asyncio
import json
import os
import sys
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path

import aiosqlite
import httpx
import websockets
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# ── Constants ──────────────────────────────────────────────────────────────

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
GOLDSKY_URL = (
    "https://api.goldsky.com/api/public/"
    "project_cl6mb8i9h0003e201j6li0diw/"
    "subgraphs/orderbook-subgraph/0.0.1/gn"
)

IST = timezone(timedelta(hours=5, minutes=30))
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

# Intervals
TRADE_POLL_S = 2.5
GOLDSKY_POLL_S = 10
DB_FLUSH_S = 1.0
DB_FLUSH_BATCH = 100
HEARTBEAT_S = 60

# Terminal colors
C = {
    "R": "\033[0m", "G": "\033[92m", "RD": "\033[91m", "Y": "\033[93m",
    "C": "\033[96m", "B": "\033[1m", "D": "\033[90m", "M": "\033[95m",
}


def ist_now():
    return datetime.now(IST).strftime("%H:%M:%S.%f")[:-3]


def ts_ms():
    return time.time_ns() // 1_000_000


def log(tag, msg, color="D"):
    print(f"{C['D']}{ist_now()}{C['R']} {C[color]}{tag}{C['R']}  {msg}")


# ── Market Resolution ──────────────────────────────────────────────────────

def fetch_market(slug: str) -> dict:
    import requests
    resp = requests.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=15)
    resp.raise_for_status()
    markets = resp.json()
    if not markets:
        print(f"No market found for slug '{slug}'")
        sys.exit(1)
    return markets[0]


def parse_tokens(market: dict) -> tuple[list[str], list[str]]:
    tokens = market.get("clobTokenIds", "")
    outcomes = market.get("outcomes", "[]")
    if isinstance(tokens, str):
        tokens = json.loads(tokens) if tokens else []
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes) if outcomes else []
    return tokens, outcomes


# ── SQLite ─────────────────────────────────────────────────────────────────

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS book_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    local_ts_ms INTEGER NOT NULL,
    asset_id TEXT NOT NULL,
    msg_type TEXT NOT NULL,
    bid1_p REAL, bid1_s REAL,
    bid2_p REAL, bid2_s REAL,
    bid3_p REAL, bid3_s REAL,
    bid4_p REAL, bid4_s REAL,
    bid5_p REAL, bid5_s REAL,
    ask1_p REAL, ask1_s REAL,
    ask2_p REAL, ask2_s REAL,
    ask3_p REAL, ask3_s REAL,
    ask4_p REAL, ask4_s REAL,
    ask5_p REAL, ask5_s REAL,
    mid_price REAL,
    spread REAL,
    total_bid_depth REAL,
    total_ask_depth REAL
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clob_ts_ms INTEGER NOT NULL,
    block_timestamp INTEGER,
    local_ts_ms INTEGER NOT NULL,
    local_poll_ts_ms INTEGER,
    transaction_hash TEXT UNIQUE NOT NULL,
    asset_id TEXT NOT NULL,
    outcome TEXT,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    size REAL NOT NULL,
    notional_usdc REAL,
    fee_rate_bps TEXT,
    taker_wallet TEXT
);

CREATE TABLE IF NOT EXISTS chain_fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_timestamp INTEGER NOT NULL,
    local_poll_ts_ms INTEGER NOT NULL,
    transaction_hash TEXT NOT NULL,
    order_hash TEXT NOT NULL,
    maker TEXT,
    taker TEXT,
    maker_asset_id TEXT,
    taker_asset_id TEXT,
    maker_amount REAL,
    taker_amount REAL,
    fee REAL,
    subgraph_id TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS cricket_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    local_ts_ms INTEGER NOT NULL,
    signal_type TEXT NOT NULL,
    runs INTEGER,
    wickets INTEGER,
    overs TEXT,
    score_str TEXT,
    innings INTEGER
);

CREATE TABLE IF NOT EXISTS match_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_txhash ON trades(transaction_hash);
CREATE INDEX IF NOT EXISTS idx_trades_clob_ts ON trades(clob_ts_ms);
CREATE INDEX IF NOT EXISTS idx_trades_block_ts ON trades(block_timestamp);
CREATE INDEX IF NOT EXISTS idx_fills_txhash ON chain_fills(transaction_hash);
CREATE INDEX IF NOT EXISTS idx_fills_ts ON chain_fills(chain_timestamp);
CREATE INDEX IF NOT EXISTS idx_book_ts ON book_snapshots(local_ts_ms);
CREATE INDEX IF NOT EXISTS idx_book_asset_ts ON book_snapshots(asset_id, local_ts_ms);
CREATE INDEX IF NOT EXISTS idx_cricket_ts ON cricket_events(local_ts_ms);
"""


async def init_db(path: str) -> aiosqlite.Connection:
    db = await aiosqlite.connect(path)
    await db.executescript(SCHEMA)
    await db.commit()
    return db


# ── Shared State ───────────────────────────────────────────────────────────

class State:
    def __init__(self, token_ids: list[str], outcome_names: list[str], condition_id: str):
        self.token_ids = token_ids
        self.outcome_names = outcome_names
        self.condition_id = condition_id
        self.token_to_outcome = dict(zip(token_ids, outcome_names))

        # In-memory L2 book: {token_id: {"bids": {price: size}, "asks": {price: size}}}
        self.books: dict[str, dict[str, dict[float, float]]] = {
            tid: {"bids": {}, "asks": {}} for tid in token_ids
        }

        # Insert buffers
        self.book_buf: list[tuple] = []
        self.trade_buf: list[tuple] = []
        self.trade_update_buf: list[tuple] = []  # (block_ts, poll_ts, outcome, wallet, txhash)
        self.fill_buf: list[tuple] = []
        self.cricket_buf: list[tuple] = []

        # Dedup sets
        self.seen_tx_hashes: set[str] = set()
        self.enriched_tx_hashes: set[str] = set()
        self.seen_subgraph_ids: set[str] = set()

        # Goldsky cursors: {f"{field}_{token_id}": last_id}
        self.goldsky_cursors: dict[str, str] = {}

        # Cricket state
        self.last_runs = 0
        self.last_wickets = 0
        self.last_score_str = ""
        self.innings = 1

        # Counters
        self.n_book = 0
        self.n_trades = 0
        self.n_fills = 0
        self.n_cricket = 0
        self.n_enriched = 0
        self.ws_connected = False
        self.cricket_connected = False

    def snapshot_book(self, asset_id: str, msg_type: str):
        """Snapshot top 5 levels and buffer for DB insert."""
        book = self.books.get(asset_id)
        if not book:
            return

        now = ts_ms()
        bids = sorted(book["bids"].items(), key=lambda x: -x[0])[:5]
        asks = sorted(book["asks"].items(), key=lambda x: x[0])[:5]

        row = [now, asset_id, msg_type]
        for i in range(5):
            if i < len(bids):
                row.extend([bids[i][0], bids[i][1]])
            else:
                row.extend([None, None])
        for i in range(5):
            if i < len(asks):
                row.extend([asks[i][0], asks[i][1]])
            else:
                row.extend([None, None])

        best_bid = bids[0][0] if bids else None
        best_ask = asks[0][0] if asks else None
        mid = (best_bid + best_ask) / 2 if best_bid and best_ask else None
        spread = (best_ask - best_bid) if best_bid and best_ask else None
        bid_depth = sum(p * s for p, s in bids)
        ask_depth = sum(p * s for p, s in asks)

        row.extend([mid, spread, bid_depth, ask_depth])
        self.book_buf.append(tuple(row))
        self.n_book += 1

    def apply_book_snapshot(self, asset_id: str, bids: list, asks: list):
        if asset_id not in self.books:
            return
        self.books[asset_id]["bids"] = {}
        self.books[asset_id]["asks"] = {}
        for b in bids:
            p, s = float(b.get("price", 0)), float(b.get("size", 0))
            if s > 0:
                self.books[asset_id]["bids"][p] = s
        for a in asks:
            p, s = float(a.get("price", 0)), float(a.get("size", 0))
            if s > 0:
                self.books[asset_id]["asks"][p] = s

    def apply_price_change(self, asset_id: str, changes: list):
        if asset_id not in self.books:
            return
        book = self.books[asset_id]
        for change in changes:
            price = float(change.get("price", 0))
            new_size = float(change.get("size", 0))
            side_key = "bids" if change.get("side", "") == "BUY" else "asks"
            if new_size <= 0:
                book[side_key].pop(price, None)
            else:
                book[side_key][price] = new_size


# ── Stream 1+2a: Market WebSocket ──────────────────────────────────────────

async def ws_loop(state: State):
    """Connect to market WS. Capture book snapshots + trade events."""
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=None) as ws:
                sub = json.dumps({"assets_ids": state.token_ids, "type": "market"})
                await ws.send(sub)
                state.ws_connected = True
                log("WS", f"connected — {len(state.token_ids)} tokens", "G")

                async def keepalive():
                    while True:
                        await asyncio.sleep(10)
                        try:
                            await ws.send("PING")
                        except Exception:
                            return

                ping_task = asyncio.create_task(keepalive())
                try:
                    async for raw in ws:
                        if raw == "PONG":
                            continue
                        _process_ws(state, raw)
                finally:
                    ping_task.cancel()
                    state.ws_connected = False

        except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
            state.ws_connected = False
            log("WS", f"disconnected: {e}", "RD")
        except Exception as e:
            state.ws_connected = False
            log("WS", f"error: {e}", "RD")

        await asyncio.sleep(2)


def _process_ws(state: State, text: str):
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return

    events = data if isinstance(data, list) else [data]
    for event in events:
        if not isinstance(event, dict):
            continue

        event_type = event.get("event_type") or event.get("type", "")
        asset_id = event.get("asset_id", "")

        # Batch price_changes at top level
        if "price_changes" in event:
            for change in event["price_changes"]:
                aid = change.get("asset_id", "")
                if aid in state.books:
                    state.apply_price_change(aid, [change])
                    state.snapshot_book(aid, "price_change")
            continue

        if event_type == "last_trade_price":
            _handle_trade(state, event)
            continue

        if asset_id not in state.books:
            continue

        if event_type == "book":
            state.apply_book_snapshot(asset_id, event.get("bids", []), event.get("asks", []))
            state.snapshot_book(asset_id, "book")

        elif event_type == "price_change":
            changes = event.get("changes", [])
            if changes:
                state.apply_price_change(asset_id, changes)
            else:
                bid_changes = [{"price": b.get("price", 0), "size": b.get("size", 0), "side": "BUY"}
                               for b in event.get("bids", [])]
                ask_changes = [{"price": a.get("price", 0), "size": a.get("size", 0), "side": "SELL"}
                               for a in event.get("asks", [])]
                state.apply_price_change(asset_id, bid_changes + ask_changes)
            state.snapshot_book(asset_id, "price_change")


def _handle_trade(state: State, event: dict):
    """Process a last_trade_price WS event → buffer for trades table."""
    tx_hash = event.get("transaction_hash", "")
    if not tx_hash or tx_hash in state.seen_tx_hashes:
        return

    state.seen_tx_hashes.add(tx_hash)

    clob_ts = int(event.get("timestamp", 0))
    asset_id = event.get("asset_id", "")
    price = float(event.get("price", 0))
    size = float(event.get("size", 0))
    side = event.get("side", "")
    fee_bps = event.get("fee_rate_bps", "")
    local = ts_ms()

    row = (
        clob_ts,            # clob_ts_ms
        None,               # block_timestamp (enriched later)
        local,              # local_ts_ms
        None,               # local_poll_ts_ms (enriched later)
        tx_hash,            # transaction_hash
        asset_id,           # asset_id
        None,               # outcome (enriched later)
        side,               # side
        price,              # price
        size,               # size
        price * size,       # notional_usdc
        fee_bps,            # fee_rate_bps
        None,               # taker_wallet (enriched later)
    )
    state.trade_buf.append(row)
    state.n_trades += 1


# ── Stream 2b: Data-API Trade Enrichment ───────────────────────────────────

async def trade_enricher(state: State):
    """Poll data-api /trades to enrich WS trades with wallet + block timestamp.

    WS-captured trades have real clob_ts_ms but no wallet/block_timestamp.
    Data-api trades have wallet + block_timestamp but no real clob_ts_ms.
    For trades seen by WS first: UPDATE with wallet + block_timestamp.
    For trades missed by WS (historical or gap): INSERT with block_ts*1000 as fallback clob_ts.
    """
    await asyncio.sleep(5)  # let WS buffer some trades first
    async with httpx.AsyncClient(headers=UA, timeout=15) as client:
        while True:
            try:
                resp = await client.get(
                    f"{DATA_API}/trades",
                    params={"market": state.condition_id, "limit": 1000},
                )
                if resp.status_code == 200:
                    trades = resp.json()
                    now = ts_ms()
                    new = 0
                    enriched = 0
                    for t in trades:
                        tx = t.get("transactionHash", "")
                        if not tx:
                            continue
                        block_ts = int(t.get("timestamp", 0))
                        wallet = t.get("proxyWallet", "")
                        outcome = t.get("outcome", "")

                        if tx not in state.seen_tx_hashes:
                            # Trade we missed via WS — insert with block_ts as fallback
                            state.seen_tx_hashes.add(tx)
                            price = float(t.get("price", 0))
                            size = float(t.get("size", 0))
                            row = (
                                block_ts * 1000,  # fallback: no real clob_ts
                                block_ts,
                                now,
                                now,
                                tx,
                                t.get("asset", ""),
                                outcome,
                                t.get("side", ""),
                                price,
                                size,
                                price * size,
                                t.get("feeRateBps", ""),
                                wallet,
                            )
                            state.trade_buf.append(row)
                            state.n_trades += 1
                            new += 1
                        else:
                            # Enrich existing WS trade with wallet + block_timestamp
                            if tx not in state.enriched_tx_hashes:
                                state.enriched_tx_hashes.add(tx)
                                state.trade_update_buf.append(
                                    (block_ts, now, outcome, wallet, tx)
                                )
                                enriched += 1

                    state.n_enriched += enriched
                    if new > 0:
                        log("ENRICH", f"+{new} trades from data-api (missed by WS)", "Y")
                    if enriched > 0:
                        log("ENRICH", f"enriched {enriched} WS trades with wallets", "D")
            except Exception as e:
                log("ENRICH", f"error: {e}", "RD")

            await asyncio.sleep(TRADE_POLL_S)


# ── Stream 3: Goldsky On-Chain Fills ───────────────────────────────────────

FILL_QUERY = """
query($tokenId: String!, $cursor: String!) {
  orderFilledEvents(
    where: { %s: $tokenId, id_gt: $cursor }
    orderBy: id
    orderDirection: asc
    first: 500
  ) {
    id maker taker makerAssetId takerAssetId
    makerAmountFilled takerAmountFilled fee
    timestamp transactionHash orderHash
  }
}
"""


async def chain_fill_poller(state: State):
    """Poll Goldsky subgraph for on-chain fills."""
    await asyncio.sleep(3)
    async with httpx.AsyncClient(headers={**UA, "Content-Type": "application/json"}, timeout=30) as client:
        while True:
            try:
                for token_id in state.token_ids:
                    for field in ("makerAssetId", "takerAssetId"):
                        cursor_key = f"{field}_{token_id}"
                        cursor = state.goldsky_cursors.get(cursor_key, "")

                        query = FILL_QUERY % field
                        payload = {
                            "query": query,
                            "variables": {"tokenId": token_id, "cursor": cursor},
                        }

                        resp = await client.post(GOLDSKY_URL, json=payload)
                        if resp.status_code != 200:
                            continue

                        fills = resp.json().get("data", {}).get("orderFilledEvents", [])
                        now = ts_ms()

                        for f in fills:
                            sid = f.get("id", "")
                            if not sid or sid in state.seen_subgraph_ids:
                                continue
                            state.seen_subgraph_ids.add(sid)

                            row = (
                                int(f.get("timestamp", 0)),
                                now,
                                f.get("transactionHash", ""),
                                f.get("orderHash", ""),
                                f.get("maker", ""),
                                f.get("taker", ""),
                                f.get("makerAssetId", ""),
                                f.get("takerAssetId", ""),
                                int(f.get("makerAmountFilled", 0)) / 1e6,
                                int(f.get("takerAmountFilled", 0)) / 1e6,
                                int(f.get("fee", 0)) / 1e6,
                                sid,
                            )
                            state.fill_buf.append(row)
                            state.n_fills += 1

                        if fills:
                            state.goldsky_cursors[cursor_key] = fills[-1]["id"]

            except Exception as e:
                log("CHAIN", f"error: {e}", "RD")

            await asyncio.sleep(GOLDSKY_POLL_S)


# ── Stream 4: Cricket SSE ──────────────────────────────────────────────────

async def cricket_sse(state: State, match_key: str | None):
    """Stream live cricket score via Firebase SSE."""
    if not match_key:
        log("CRICKET", "no match key — skipping cricket stream", "Y")
        return

    base = os.getenv("CRICKET_API_KEY", "").rstrip("/")
    if not base:
        log("CRICKET", "CRICKET_API_KEY not set — skipping", "Y")
        return

    score_url = f"{base}/recent-matches/{match_key}/play/live/score.json"
    log("CRICKET", f"connecting to {score_url}", "C")

    while True:
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("GET", score_url,
                                         headers={"Accept": "text/event-stream"}) as resp:
                    resp.raise_for_status()
                    state.cricket_connected = True
                    log("CRICKET", "SSE connected", "G")

                    event_type = None
                    data_buf = []

                    async for line in resp.aiter_lines():
                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                            continue
                        if line.startswith("data:"):
                            data_buf.append(line[5:].strip())
                            continue
                        if line == "" and data_buf:
                            raw = "\n".join(data_buf)
                            data_buf = []
                            if raw == "null" or event_type == "keep-alive":
                                continue
                            try:
                                payload = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            _handle_cricket(state, payload)

        except Exception as e:
            state.cricket_connected = False
            log("CRICKET", f"error: {e}", "RD")

        await asyncio.sleep(2)


def _handle_cricket(state: State, payload):
    if not isinstance(payload, dict):
        return

    data = payload.get("data", payload)
    if isinstance(data, dict) and "path" in data:
        data = data.get("data", data)
    if not isinstance(data, dict):
        return

    runs = data.get("runs", state.last_runs)
    wickets = data.get("wickets", state.last_wickets)
    overs = data.get("overs", "")

    if isinstance(overs, list) and len(overs) == 2:
        overs_str = f"{overs[0]}.{overs[1]}"
    else:
        overs_str = str(overs)

    score_str = f"{runs}/{wickets} ({overs_str})"
    if score_str == state.last_score_str:
        return

    run_diff = runs - state.last_runs
    wicket_diff = wickets - state.last_wickets

    if wicket_diff > 0:
        signal = "W"
    elif run_diff == 6:
        signal = "6"
    elif run_diff == 4:
        signal = "4"
    elif run_diff >= 0:
        signal = str(run_diff)
    else:
        signal = "?"

    state.last_runs = runs
    state.last_wickets = wickets
    state.last_score_str = score_str

    now = ts_ms()
    row = (now, signal, runs, wickets, overs_str, score_str, state.innings)
    state.cricket_buf.append(row)
    state.n_cricket += 1

    if signal in ("4", "6", "W"):
        color = "RD" if signal == "W" else "G"
        log("EVENT", f"{'='*50}", color)
        log("EVENT", f"  {signal}  |  {score_str}", color)
        log("EVENT", f"{'='*50}", color)
    else:
        log("SCORE", f"{score_str}", "C")


# ── DB Flusher ─────────────────────────────────────────────────────────────

async def db_flusher(state: State, db: aiosqlite.Connection):
    """Flush insert buffers to SQLite periodically."""
    while True:
        await asyncio.sleep(DB_FLUSH_S)
        await _flush(state, db)


async def _flush(state: State, db: aiosqlite.Connection):
    """Flush all buffers to DB."""
    flushed = False

    if state.book_buf:
        rows = state.book_buf[:]
        state.book_buf.clear()
        await db.executemany(
            "INSERT INTO book_snapshots VALUES (NULL,"
            + ",".join(["?"] * 27) + ")",
            rows,
        )
        flushed = True

    if state.trade_buf:
        rows = state.trade_buf[:]
        state.trade_buf.clear()
        for row in rows:
            try:
                await db.execute(
                    "INSERT OR IGNORE INTO trades VALUES (NULL,"
                    + ",".join(["?"] * 13) + ")",
                    row,
                )
            except Exception:
                pass  # dedup via UNIQUE constraint
        flushed = True

    if state.trade_update_buf:
        rows = state.trade_update_buf[:]
        state.trade_update_buf.clear()
        await db.executemany(
            "UPDATE trades SET block_timestamp=?, local_poll_ts_ms=?, "
            "outcome=COALESCE(?, outcome), taker_wallet=COALESCE(?, taker_wallet) "
            "WHERE transaction_hash=?",
            rows,
        )
        flushed = True

    if state.fill_buf:
        rows = state.fill_buf[:]
        state.fill_buf.clear()
        for row in rows:
            try:
                await db.execute(
                    "INSERT OR IGNORE INTO chain_fills VALUES (NULL,"
                    + ",".join(["?"] * 12) + ")",
                    row,
                )
            except Exception:
                pass
        flushed = True

    if state.cricket_buf:
        rows = state.cricket_buf[:]
        state.cricket_buf.clear()
        await db.executemany(
            "INSERT INTO cricket_events VALUES (NULL,?,?,?,?,?,?,?)",
            rows,
        )
        flushed = True

    if flushed:
        await db.commit()


# ── Heartbeat ──────────────────────────────────────────────────────────────

async def heartbeat(state: State):
    while True:
        await asyncio.sleep(HEARTBEAT_S)
        ws = f"{C['G']}connected{C['R']}" if state.ws_connected else f"{C['RD']}disconnected{C['R']}"
        cricket = f"{C['G']}connected{C['R']}" if state.cricket_connected else f"{C['D']}off{C['R']}"
        log("STATS",
            f"WS:{ws} Cricket:{cricket} | "
            f"book={state.n_book} trades={state.n_trades} "
            f"enriched={state.n_enriched} fills={state.n_fills} "
            f"cricket={state.n_cricket} | "
            f"dedup: tx={len(state.seen_tx_hashes)} sg={len(state.seen_subgraph_ids)}",
            "M")


# ── Main ───────────────────────────────────────────────────────────────────

async def run(args):
    # Resolve market
    log("INIT", f"resolving market '{args.slug}'...", "B")
    market = fetch_market(args.slug)
    question = market.get("question", args.slug)
    condition_id = market.get("conditionId", "")
    token_ids, outcome_names = parse_tokens(market)

    if not token_ids:
        print("No token IDs found")
        sys.exit(1)

    print(f"  Market:    {question}")
    print(f"  Condition: {condition_id}")
    print(f"  Outcomes:  {', '.join(outcome_names)}")
    print(f"  Tokens:    {len(token_ids)}")
    if args.match:
        print(f"  Match key: {args.match}")
    print()

    state = State(token_ids, outcome_names, condition_id)

    # Init DB
    os.makedirs("captures", exist_ok=True)
    date_str = time.strftime("%Y%m%d")
    db_path = os.path.join("captures", f"match_capture_{args.slug}_{date_str}.db")
    db = await init_db(db_path)

    # Store metadata
    meta = {
        "slug": args.slug,
        "question": question,
        "condition_id": condition_id,
        "token_ids": json.dumps(token_ids),
        "outcome_names": json.dumps(outcome_names),
        "match_key": args.match or "",
        "start_time": datetime.now(IST).isoformat(),
    }
    for k, v in meta.items():
        await db.execute(
            "INSERT OR REPLACE INTO match_meta VALUES (?, ?)", (k, v)
        )
    await db.commit()

    log("INIT", f"DB: {db_path}", "G")
    print(f"  Streams: WS book+trades | Data-API enrichment | Goldsky fills"
          + (" | Cricket SSE" if args.match else ""))
    print(f"  {'='*60}")
    print()

    tasks = [
        ws_loop(state),
        trade_enricher(state),
        chain_fill_poller(state),
        cricket_sse(state, args.match),
        db_flusher(state, db),
        heartbeat(state),
    ]

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        # Final flush
        await _flush(state, db)
        await db.close()

        print(f"\n{'='*60}")
        print(f"  CAPTURE COMPLETE")
        print(f"  Book snapshots: {state.n_book}")
        print(f"  Trades:         {state.n_trades} ({state.n_enriched} enriched)")
        print(f"  Chain fills:    {state.n_fills}")
        print(f"  Cricket events: {state.n_cricket}")
        print(f"  DB: {db_path}")
        print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(description="Live Match Capture")
    parser.add_argument("--slug", required=True, help="Polymarket market slug")
    parser.add_argument("--match", help="Cricket match key (Firebase)")
    args = parser.parse_args()

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print(f"\n{C['B']}Stopped.{C['R']}")


if __name__ == "__main__":
    main()
