#!/usr/bin/env python3
"""Polymarket Live Market Monitor.

Streams orderbook updates + trades via WebSocket, detects cancelled orders by
diffing book snapshots, and tracks per-wallet capital rotation in real time.

Usage:
    python live_monitor.py <slug>
    python live_monitor.py --slug <slug> --address 0x... --poll-interval 5
"""

import argparse
import asyncio
import csv
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import websockets
from tabulate import tabulate

# ── Constants ────────────────────────────────────────────────────────────────

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

SETTINGS_PATH = Path(__file__).parent / "polymarket-taker" / "settings.json"

CLEAR_SCREEN = "\033[2J\033[H"


# ── Settings ─────────────────────────────────────────────────────────────────

def load_settings():
    if SETTINGS_PATH.exists():
        with open(SETTINGS_PATH) as f:
            return json.load(f)
    return {}


# ── Gamma API ────────────────────────────────────────────────────────────────

def fetch_market(slug: str) -> dict:
    resp = requests.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=15)
    resp.raise_for_status()
    markets = resp.json()
    if not markets:
        print(f"Error: No market found for slug '{slug}'")
        sys.exit(1)
    return markets[0]


def parse_market_tokens(market: dict) -> tuple[list[str], list[str]]:
    """Return (token_ids, outcome_names)."""
    tokens = market.get("clobTokenIds", "")
    outcomes = market.get("outcomes", "[]")
    if isinstance(tokens, str):
        try:
            tokens = json.loads(tokens)
        except json.JSONDecodeError:
            tokens = tokens.split(",") if tokens else []
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except json.JSONDecodeError:
            outcomes = []
    return tokens, outcomes


# ── Shared State ─────────────────────────────────────────────────────────────

class MonitorState:
    def __init__(self, token_ids: list[str], outcome_names: list[str],
                 our_address: str, min_size: float):
        self.token_ids = token_ids
        self.outcome_names = outcome_names
        self.token_to_outcome = dict(zip(token_ids, outcome_names))
        self.our_address = our_address.lower() if our_address else ""
        self.min_size = min_size
        self.start_time = time.time()

        # L2 orderbook per token: {token_id: {"bids": {price: size}, "asks": {price: size}}}
        self.books: dict[str, dict[str, dict[float, float]]] = {
            tid: {"bids": {}, "asks": {}} for tid in token_ids
        }

        # Cancel detection
        self.cancel_log: list[dict] = []  # [{time, token, outcome, side, price, size, notional}]
        self.total_cancel_count = 0
        self.total_cancel_notional = 0.0

        # Recent fills from WS (for cancel vs fill disambiguation)
        # List of {time, price, side, asset_id, size}
        self.recent_ws_fills: list[dict] = []
        self.ws_fill_window = 2.0  # seconds

        # Trade log from REST poller (with wallet info)
        self.trades: list[dict] = []
        self.seen_tx_hashes: set[str] = set()
        self.total_trade_count = 0

        # Per-wallet stats: {address: {total_bought, total_sold, net_pos_by_outcome, trade_count, first_seen, last_seen, buys_count, sells_count}}
        self.wallet_stats: dict[str, dict] = defaultdict(lambda: {
            "total_bought": 0.0,
            "total_sold": 0.0,
            "net_pos": defaultdict(float),  # outcome -> tokens
            "trade_count": 0,
            "buy_count": 0,
            "sell_count": 0,
            "first_seen": None,
            "last_seen": None,
            "max_deployed": 0.0,
            "running_deployed": 0.0,
        })

        # CSV writer
        self._csv_file = None
        self._csv_writer = None

    def init_csv(self, path: str):
        self._csv_file = open(path, "a", newline="")
        self._csv_writer = csv.writer(self._csv_file)
        if os.path.getsize(path) == 0:
            self._csv_writer.writerow([
                "timestamp", "outcome", "side", "size", "price",
                "notional", "proxyWallet", "transactionHash",
            ])

    def close_csv(self):
        if self._csv_file:
            self._csv_file.close()

    # ── Book updates ─────────────────────────────────────────────────────

    def apply_book_snapshot(self, asset_id: str, bids: list, asks: list):
        """Replace entire book for an asset."""
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
        """Incremental book update with cancel detection."""
        if asset_id not in self.books:
            return
        book = self.books[asset_id]
        now = time.time()

        for change in changes:
            price = float(change.get("price", 0))
            new_size = float(change.get("size", 0))
            side_str = change.get("side", "")

            side_key = "bids" if side_str == "BUY" else "asks"
            old_size = book[side_key].get(price, 0.0)

            # Cancel detection: size decreased without a matching fill
            if new_size < old_size:
                removed = old_size - new_size
                if not self._recent_fill_at_price(asset_id, price, side_str, now):
                    notional = removed * price
                    outcome = self.token_to_outcome.get(asset_id, "?")
                    self.cancel_log.append({
                        "time": now,
                        "time_str": datetime.now().strftime("%H:%M:%S"),
                        "outcome": outcome,
                        "side": side_str,
                        "price": price,
                        "size": removed,
                        "notional": notional,
                    })
                    self.total_cancel_count += 1
                    self.total_cancel_notional += notional
                    # Keep only last 200 cancel events
                    if len(self.cancel_log) > 200:
                        self.cancel_log = self.cancel_log[-200:]

            # Update book
            if new_size <= 0:
                book[side_key].pop(price, None)
            else:
                book[side_key][price] = new_size

    def _recent_fill_at_price(self, asset_id: str, price: float, side: str, now: float) -> bool:
        """Check if there was a WS fill event at this price within the window."""
        cutoff = now - self.ws_fill_window
        for fill in reversed(self.recent_ws_fills):
            if fill["time"] < cutoff:
                break
            if (fill["asset_id"] == asset_id
                    and abs(fill["price"] - price) < 0.0001
                    and fill["side"] == side):
                return True
        return False

    def record_ws_fill(self, asset_id: str, price: float, size: float, side: str):
        """Record a last_trade_price event for cancel disambiguation."""
        now = time.time()
        self.recent_ws_fills.append({
            "time": now, "asset_id": asset_id,
            "price": price, "size": size, "side": side,
        })
        # Prune old fills
        cutoff = now - self.ws_fill_window * 2
        self.recent_ws_fills = [f for f in self.recent_ws_fills if f["time"] > cutoff]

    # ── Trade processing (from REST) ─────────────────────────────────────

    def process_new_trades(self, raw_trades: list[dict]):
        """Process trades from REST API, deduplicate, update wallet stats."""
        new_count = 0
        for t in raw_trades:
            tx_hash = t.get("transactionHash", "")
            if not tx_hash or tx_hash in self.seen_tx_hashes:
                continue
            self.seen_tx_hashes.add(tx_hash)
            new_count += 1

            size = float(t.get("size", 0))
            price = float(t.get("price", 0))
            notional = size * price
            side = t.get("side", "BUY")
            wallet = t.get("proxyWallet", "").lower()
            outcome = t.get("outcome", t.get("asset", "?"))
            ts = t.get("timestamp", "")

            trade_record = {
                "time_str": datetime.now().strftime("%H:%M:%S"),
                "timestamp": ts,
                "outcome": outcome,
                "side": side,
                "size": size,
                "price": price,
                "notional": notional,
                "wallet": wallet,
                "tx_hash": tx_hash,
            }
            self.trades.append(trade_record)
            self.total_trade_count += 1

            # CSV append
            if self._csv_writer:
                self._csv_writer.writerow([
                    ts, outcome, side, size, price, notional, wallet, tx_hash,
                ])
                self._csv_file.flush()

            # Wallet stats
            if wallet:
                ws = self.wallet_stats[wallet]
                ws["trade_count"] += 1
                if side == "BUY":
                    ws["total_bought"] += notional
                    ws["buy_count"] += 1
                    ws["net_pos"][outcome] += size
                    ws["running_deployed"] += notional
                else:
                    ws["total_sold"] += notional
                    ws["sell_count"] += 1
                    ws["net_pos"][outcome] -= size
                    ws["running_deployed"] -= notional
                    ws["running_deployed"] = max(ws["running_deployed"], 0)
                ws["max_deployed"] = max(ws["max_deployed"], ws["running_deployed"])

                if ws["first_seen"] is None:
                    ws["first_seen"] = ts or datetime.now().strftime("%H:%M:%S")
                ws["last_seen"] = ts or datetime.now().strftime("%H:%M:%S")

        # Keep only last 500 trades in memory
        if len(self.trades) > 500:
            self.trades = self.trades[-500:]

        return new_count


# ── WebSocket Listener ───────────────────────────────────────────────────────

async def ws_listener(state: MonitorState, render_event: asyncio.Event):
    """Connect to Polymarket market WS and process events."""
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=None) as ws:
                # Subscribe
                sub_msg = json.dumps({
                    "assets_ids": state.token_ids,
                    "type": "market",
                })
                await ws.send(sub_msg)

                # PING keepalive task
                async def keepalive():
                    while True:
                        await asyncio.sleep(10)
                        try:
                            await ws.send("PING")
                        except Exception:
                            return

                ping_task = asyncio.create_task(keepalive())

                try:
                    async for raw_msg in ws:
                        if raw_msg == "PONG":
                            continue

                        try:
                            _process_ws_message(state, raw_msg)
                            render_event.set()
                        except Exception as e:
                            pass  # skip unparseable messages
                finally:
                    ping_task.cancel()

        except (websockets.ConnectionClosed, ConnectionError, OSError):
            pass
        except Exception:
            pass

        await asyncio.sleep(2)


def _process_ws_message(state: MonitorState, text: str):
    """Parse and apply a single WS message."""
    # Try as array first, then single object
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return

    events = data if isinstance(data, list) else [data]

    for event in events:
        if not isinstance(event, dict):
            continue

        asset_id = event.get("asset_id")
        event_type = event.get("event_type") or event.get("type")

        # Market-level price_changes (no event_type, has price_changes array)
        if "price_changes" in event:
            for change in event["price_changes"]:
                aid = change.get("asset_id", "")
                if aid in state.books:
                    state.apply_price_change(aid, [change])
            continue

        if not asset_id or asset_id not in state.books:
            continue

        if event_type == "book":
            bids = event.get("bids", [])
            asks = event.get("asks", [])
            state.apply_book_snapshot(asset_id, bids, asks)

        elif event_type == "price_change":
            changes = event.get("changes", [])
            if changes:
                state.apply_price_change(asset_id, changes)
            else:
                # Fallback: bids/asks arrays in event
                bid_changes = [{"price": b.get("price", b[0] if isinstance(b, list) else 0),
                                "size": b.get("size", b[1] if isinstance(b, list) else 0),
                                "side": "BUY"}
                               for b in event.get("bids", [])]
                ask_changes = [{"price": a.get("price", a[0] if isinstance(a, list) else 0),
                                "size": a.get("size", a[1] if isinstance(a, list) else 0),
                                "side": "SELL"}
                               for a in event.get("asks", [])]
                state.apply_price_change(asset_id, bid_changes + ask_changes)

        elif event_type == "last_trade_price":
            price = float(event.get("price", 0))
            size = float(event.get("size", 0))
            side = event.get("side", "")
            state.record_ws_fill(asset_id, price, size, side)


# ── REST Trade Poller ────────────────────────────────────────────────────────

async def trade_poller(state: MonitorState, condition_id: str,
                       poll_interval: float, render_event: asyncio.Event):
    """Poll REST API for new trades with wallet attribution."""
    while True:
        try:
            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: requests.get(
                    f"{DATA_API}/trades",
                    params={"market": condition_id, "limit": 100},
                    timeout=15,
                ),
            )
            if resp.ok:
                trades = resp.json()
                new = state.process_new_trades(trades)
                if new > 0:
                    render_event.set()
        except Exception:
            pass

        await asyncio.sleep(poll_interval)


# ── TUI Renderer ─────────────────────────────────────────────────────────────

async def renderer(state: MonitorState, render_event: asyncio.Event, market: dict):
    """Refresh console display on events or every 1s."""
    question = market.get("question", "Unknown Market")
    closed = market.get("closed", False)
    resolved = market.get("resolved", False)
    status = "RESOLVED" if resolved else ("CLOSED" if closed else "OPEN")

    while True:
        try:
            await asyncio.wait_for(render_event.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        render_event.clear()

        uptime = time.time() - state.start_time
        mins, secs = divmod(int(uptime), 60)
        hrs, mins = divmod(mins, 60)
        uptime_str = f"{hrs}h {mins}m {secs}s" if hrs else f"{mins}m {secs}s"

        lines = []
        w = 72

        lines.append(CLEAR_SCREEN)
        lines.append("=" * w)
        lines.append(f"  LIVE MONITOR: {question[:60]}")
        lines.append(f"  Status: {status} | Uptime: {uptime_str} | "
                     f"Trades: {state.total_trade_count} | Cancels: {state.total_cancel_count}")
        lines.append("=" * w)

        # ── Orderbook per outcome ────────────────────────────────────────
        for tid, outcome in zip(state.token_ids, state.outcome_names):
            book = state.books.get(tid, {"bids": {}, "asks": {}})
            bids = book["bids"]
            asks = book["asks"]

            lines.append(f"\n--- Orderbook: {outcome} (filtered >=${state.min_size}) ---")

            # Filter and sort
            f_bids = sorted(
                [(p, s) for p, s in bids.items() if s * p >= state.min_size],
                key=lambda x: -x[0],
            )[:10]
            f_asks = sorted(
                [(p, s) for p, s in asks.items() if s * p >= state.min_size],
                key=lambda x: x[0],
            )[:10]

            # Side-by-side display
            lines.append(f"  {'BID':^30s}  {'ASK':^30s}")
            lines.append(f"  {'Price':>8s} {'Size':>8s} {'Notional':>10s}  "
                         f"{'Price':>8s} {'Size':>8s} {'Notional':>10s}")

            max_rows = max(len(f_bids), len(f_asks))
            for i in range(max_rows):
                bid_str = ""
                ask_str = ""
                if i < len(f_bids):
                    p, s = f_bids[i]
                    bid_str = f"  ${p:<7.4f} {s:>8.0f} ${s*p:>9,.2f}"
                else:
                    bid_str = " " * 30

                if i < len(f_asks):
                    p, s = f_asks[i]
                    ask_str = f"  ${p:<7.4f} {s:>8.0f} ${s*p:>9,.2f}"
                else:
                    ask_str = ""

                lines.append(f"{bid_str}  {ask_str}")

            # Spread & imbalance
            if f_bids and f_asks:
                best_bid = f_bids[0][0]
                best_ask = f_asks[0][0]
                spread = best_ask - best_bid
                bid_depth = sum(p * s for p, s in f_bids)
                ask_depth = sum(p * s for p, s in f_asks)
                total_depth = bid_depth + ask_depth
                imbalance = (bid_depth / total_depth * 100) if total_depth > 0 else 50
                lines.append(f"  Spread: ${spread:.4f} | Imbalance: {imbalance:.0f}% bid")

        # ── Recent Trades ────────────────────────────────────────────────
        lines.append(f"\n--- Recent Trades (last 15) ---")
        recent = state.trades[-15:]
        if recent:
            rows = []
            for t in reversed(recent):
                addr = t["wallet"][:8] + "..." if len(t["wallet"]) > 8 else t["wallet"]
                if state.our_address and t["wallet"] == state.our_address:
                    addr = f"[US] {addr}"
                rows.append([
                    t["time_str"],
                    t["side"],
                    t["outcome"][:15],
                    f"{t['size']:.0f}",
                    f"${t['price']:.4f}",
                    f"${t['notional']:.2f}",
                    addr,
                ])
            lines.append(tabulate(rows,
                                  headers=["Time", "Side", "Outcome", "Size", "Price", "Notional", "Wallet"],
                                  tablefmt="simple"))
        else:
            lines.append("  (waiting for trades...)")

        # ── Cancel Detection ─────────────────────────────────────────────
        lines.append(f"\n--- Cancel Detection (last 5 min) ---")
        now = time.time()
        recent_cancels = [c for c in state.cancel_log if now - c["time"] < 300]
        if recent_cancels:
            rows = []
            for c in recent_cancels[-10:]:
                rows.append([
                    c["time_str"],
                    c["side"],
                    c["outcome"][:15],
                    f"${c['price']:.4f}",
                    f"-{c['size']:.0f} tokens",
                    f"${c['notional']:,.2f}",
                    "CANCELLED",
                ])
            lines.append(tabulate(rows,
                                  headers=["Time", "Side", "Outcome", "Price", "Size", "Notional", ""],
                                  tablefmt="simple"))
            cancel_5m_notional = sum(c["notional"] for c in recent_cancels)
            lines.append(f"  5-min total: ${cancel_5m_notional:,.2f} ({len(recent_cancels)} events) | "
                         f"All-time: ${state.total_cancel_notional:,.2f} ({state.total_cancel_count} events)")
        else:
            lines.append("  (no cancels detected yet)")

        # ── Wallet Tracker (sorted by trade count desc) ──────────────────
        lines.append(f"\n--- Wallet Tracker (sorted by trade count) ---")
        if state.wallet_stats:
            wallet_rows = []
            sorted_wallets = sorted(
                state.wallet_stats.items(),
                key=lambda x: x[1]["trade_count"],
                reverse=True,
            )
            for addr, ws in sorted_wallets[:20]:
                display_addr = addr[:8] + "..." + addr[-4:] if len(addr) > 14 else addr
                if state.our_address and addr == state.our_address:
                    display_addr = f"[US] {display_addr}"

                max_dep = ws["max_deployed"]
                rotation = (ws["total_bought"] / max_dep) if max_dep > 0 else 0

                wallet_rows.append([
                    display_addr,
                    ws["trade_count"],
                    ws["buy_count"],
                    ws["sell_count"],
                    f"${ws['total_bought']:,.2f}",
                    f"${ws['total_sold']:,.2f}",
                    f"{rotation:.1f}x",
                    ws["last_seen"] or "",
                ])
            lines.append(tabulate(
                wallet_rows,
                headers=["Address", "Trades", "Buys", "Sells", "Bought", "Sold", "Rotation", "Last Seen"],
                tablefmt="simple",
            ))
        else:
            lines.append("  (waiting for trade data...)")

        lines.append("")
        lines.append(f"  [Ctrl+C to stop] | Poll interval: REST trades every few seconds")

        print("\n".join(lines), flush=True)


# ── Main ─────────────────────────────────────────────────────────────────────

async def run(args):
    settings = load_settings()
    slug = args.slug or args.slug_pos
    if not slug:
        print("Error: Market slug is required")
        sys.exit(1)

    address = args.address or settings.get("polymarket_address", "")

    print(f"Fetching market info for '{slug}'...")
    market = fetch_market(slug)
    condition_id = market.get("conditionId", "")
    question = market.get("question", slug)
    token_ids, outcome_names = parse_market_tokens(market)

    if not token_ids:
        print("Error: No token IDs found for this market")
        sys.exit(1)

    print(f"Market: {question}")
    print(f"Tokens: {len(token_ids)} outcomes: {', '.join(outcome_names)}")
    print(f"Condition: {condition_id}")
    print(f"Tracking address: {address or '(none)'}")
    print("Connecting to WebSocket...")

    state = MonitorState(token_ids, outcome_names, address, args.min_size)

    if args.export_csv:
        data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", slug)
        os.makedirs(data_dir, exist_ok=True)
        csv_path = os.path.join(data_dir, f"live_trades_{slug}.csv")
        state.init_csv(csv_path)
        print(f"Appending trades to {csv_path}")

    render_event = asyncio.Event()

    try:
        await asyncio.gather(
            ws_listener(state, render_event),
            trade_poller(state, condition_id, args.poll_interval, render_event),
            renderer(state, render_event, market),
        )
    except KeyboardInterrupt:
        pass
    finally:
        state.close_csv()

        # Final summary
        print(CLEAR_SCREEN)
        print("=" * 60)
        print("  FINAL SUMMARY")
        print("=" * 60)
        uptime = time.time() - state.start_time
        mins = int(uptime // 60)
        print(f"  Runtime:          {mins}m {int(uptime % 60)}s")
        print(f"  Total trades:     {state.total_trade_count}")
        print(f"  Total cancels:    {state.total_cancel_count}")
        print(f"  Cancel notional:  ${state.total_cancel_notional:,.2f}")
        print(f"  Unique wallets:   {len(state.wallet_stats)}")

        if state.wallet_stats:
            print(f"\n  Top wallets by trade count:")
            sorted_w = sorted(state.wallet_stats.items(),
                              key=lambda x: x[1]["trade_count"], reverse=True)
            for addr, ws in sorted_w[:10]:
                print(f"    {addr[:12]}... | {ws['trade_count']} trades | "
                      f"bought ${ws['total_bought']:,.2f} | sold ${ws['total_sold']:,.2f}")
        print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Polymarket Live Market Monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python live_monitor.py crint-ind-wst-2026-03-01
  python live_monitor.py --slug crint-ind-wst-2026-03-01 --address 0x... --poll-interval 3
  python live_monitor.py crint-ind-wst-2026-03-01 --export-csv --min-size 20
        """,
    )
    parser.add_argument("slug_pos", nargs="?", help="Market slug (positional)")
    parser.add_argument("--slug", help="Market slug")
    parser.add_argument("--address", help="Wallet address to highlight (default from settings.json)")
    parser.add_argument("--poll-interval", type=float, default=5,
                        help="Seconds between REST trade polls (default: 5)")
    parser.add_argument("--export-csv", action="store_true",
                        help="Continuously append trades to CSV")
    parser.add_argument("--min-size", type=float, default=50,
                        help="Min notional ($) for orderbook display (default: 50)")

    args = parser.parse_args()
    if not (args.slug or args.slug_pos):
        parser.error("Market slug is required (positional or --slug)")

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
