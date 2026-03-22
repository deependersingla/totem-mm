#!/usr/bin/env python3
"""
Backtest the cricket event-driven taker strategy against captured order book data.

Strategy:
  - On event favoring team1 (side=team1): BUY team1 tokens (FAK at ask), SELL team2 tokens (FAK at bid)
  - On event favoring team2 (side=team2): BUY team2 tokens (FAK at ask), SELL team1 tokens (FAK at bid)
  - After revert_delay: place GTC revert orders with tiered edge

Timing: We trade at market_move_start (before market moves), simulating that
we had the score signal at the real event time.

Fill model: Reconstruct actual order book from JSONL at event time, sweep real liquidity.

Usage:
    python backtest_taker.py <events.json> <capture.jsonl> <market_slug> <nz_team> <sa_team> [--settlement nz|sa]
    python backtest_taker.py events.json capture.jsonl crint-nzl-zaf-2026-03-22 NZ SA --settlement sa
"""

import argparse
import json
try:
    import httpx
except ImportError:
    import urllib.request, urllib.parse
    httpx = None
from datetime import datetime, timedelta
from collections import defaultdict
from copy import deepcopy
from pathlib import Path

# === CONFIG ===
INITIAL_CAPITAL = 5000.0
TOKEN_CONVERSION = 2500.0   # $2500 -> 2500 Yes + 2500 No
TRADING_CASH = 2500.0
MAX_TRADE_USDC = 500.0      # per leg

REVERT_DELAY_S = 11          # seconds before placing revert
REVERT_WINDOW_S = 120        # how long revert GTC stays active (check for fills)

GAMMA_API = "https://gamma-api.polymarket.com"


def fetch_tick_size(slug):
    """Fetch tick size from Polymarket Gamma API for a given market slug."""
    try:
        url = f"{GAMMA_API}/markets?slug={slug}"
        if httpx:
            resp = httpx.get(url, timeout=15)
            resp.raise_for_status()
            markets = resp.json()
        else:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=15) as resp:
                markets = json.loads(resp.read())
        if markets:
            tick = markets[0].get("orderPriceMinTickSize")
            if tick is not None:
                return float(tick)
    except Exception as e:
        print(f"  [WARN] Could not fetch tick size from API: {e}")
    print("  [FALLBACK] Using tick size from JSONL tick_size_change events")
    return None


def get_tick_sizes_from_jsonl(jsonl_path):
    """Extract tick size changes from JSONL capture data."""
    changes = []
    initial_tick = 0.01

    with open(jsonl_path) as f:
        for line in f:
            d = json.loads(line)
            if d.get("type") == "tick_size_change":
                ist = d.get("ist", "")
                new_tick = float(d["new_tick_size"])
                dt = parse_ist(ist)
                if dt and (not changes or changes[-1][1] != new_tick):
                    changes.append((dt, new_tick))
    return initial_tick, changes


def get_tick_at_time(initial_tick, tick_changes, event_time):
    """Get the active tick size at a given event time."""
    tick = initial_tick
    for change_time, new_tick in tick_changes:
        if event_time >= change_time:
            tick = new_tick
        else:
            break
    return tick


def round_to_tick(price, tick):
    return round(round(price / tick) * tick, 6)


def compute_revert_price(entry_price, direction, tick):
    """
    Compute revert price with tiered edge based on price level.

    Tiered edge (in ticks):
      - price > 0.15:  2 ticks edge
      - 0.05 <= price <= 0.15:  1 tick edge
      - price < 0.05:  already at spread, hold position (no revert)
    """
    if entry_price < 0.05:
        return None

    if entry_price > 0.15:
        edge_ticks = 2
    else:
        edge_ticks = 1

    if direction == "BUY":
        revert = round_to_tick(entry_price + edge_ticks * tick, tick)
    else:
        revert = round_to_tick(entry_price - edge_ticks * tick, tick)
        if revert <= 0:
            return None

    return revert


class OrderBook:
    """Maintains bid/ask levels for one outcome."""

    def __init__(self):
        self.bids = {}
        self.asks = {}

    def update(self, side, price, new_size):
        book = self.bids if side == "BUY" else self.asks
        price = round(price, 4)
        if new_size <= 0:
            book.pop(price, None)
        else:
            book[price] = new_size

    def snapshot(self):
        return OrderBook._from_raw(deepcopy(self.bids), deepcopy(self.asks))

    @staticmethod
    def _from_raw(bids, asks):
        ob = OrderBook()
        ob.bids = bids
        ob.asks = asks
        return ob

    def best_bid(self):
        return max(self.bids.keys()) if self.bids else None

    def best_ask(self):
        return min(self.asks.keys()) if self.asks else None

    def sweep_asks(self, max_usdc, max_tokens=float('inf'), ref_price=None):
        """Simulate FAK BUY at best ask level (single-level fill)."""
        tokens = 0.0
        spent = 0.0
        fills = []
        min_valid = (ref_price - 0.05) if ref_price else 0
        for price in sorted(self.asks.keys()):
            if price < min_valid:
                continue
            available = self.asks[price]
            affordable = max_usdc / price if price > 0 else 0
            fillable = min(available, affordable, max_tokens)
            if fillable <= 0:
                break
            cost = fillable * price
            tokens += fillable
            spent += cost
            fills.append({"price": price, "tokens": fillable, "usdc": cost})
            break  # FAK: single level fill
        avg_price = spent / tokens if tokens > 0 else 0
        return tokens, spent, avg_price, fills

    def sweep_bids(self, max_usdc, max_tokens=float('inf'), ref_price=None):
        """Simulate FAK SELL at best bid level (single-level fill)."""
        tokens = 0.0
        received = 0.0
        fills = []
        max_valid = (ref_price + 0.05) if ref_price else float('inf')
        for price in sorted(self.bids.keys(), reverse=True):
            if price > max_valid:
                continue
            available = self.bids[price]
            affordable = max_usdc / price if price > 0 else 0
            fillable = min(available, affordable, max_tokens)
            if fillable <= 0:
                break
            proceeds = fillable * price
            tokens += fillable
            received += proceeds
            fills.append({"price": price, "tokens": fillable, "usdc": proceeds})
            break  # FAK: single level fill
        avg_price = received / tokens if tokens > 0 else 0
        return tokens, received, avg_price, fills

    def depth_summary(self, levels=5):
        top_bids = sorted(self.bids.items(), reverse=True)[:levels]
        top_asks = sorted(self.asks.items())[:levels]
        return {"bids": top_bids, "asks": top_asks}


class Position:
    """Track token holdings and cash."""

    def __init__(self, t1_tokens, t2_tokens, cash, t1_name, t2_name,
                 t1_settlement, t2_settlement):
        self.t1 = t1_tokens
        self.t2 = t2_tokens
        self.cash = cash
        self.t1_name = t1_name
        self.t2_name = t2_name
        self.t1_settlement = t1_settlement
        self.t2_settlement = t2_settlement
        self.trades = []

    def buy_t1(self, tokens, cost):
        self.t1 += tokens
        self.cash -= cost

    def sell_t1(self, tokens, proceeds):
        self.t1 -= tokens
        self.cash += proceeds

    def buy_t2(self, tokens, cost):
        self.t2 += tokens
        self.cash -= cost

    def sell_t2(self, tokens, proceeds):
        self.t2 -= tokens
        self.cash += proceeds

    def net_value(self):
        return self.cash + self.t1 * self.t1_settlement + self.t2 * self.t2_settlement

    def __repr__(self):
        return (f"{self.t1_name}={self.t1:.1f} {self.t2_name}={self.t2:.1f} "
                f"Cash=${self.cash:.2f} NetVal=${self.net_value():.2f}")


def parse_ist(ist_str):
    """Parse full IST timestamp."""
    clean = ist_str.replace(" IST", "").strip()
    try:
        return datetime.strptime(clean[:26], "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        try:
            return datetime.strptime(clean[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None


def load_events(events_path):
    """Load events that have market_movement data."""
    with open(events_path) as f:
        all_events = json.load(f)

    tradeable = []
    for e in all_events:
        mm = e.get("market_movement")
        if isinstance(mm, dict) and "market_move_start" in mm:
            tradeable.append(e)
    return tradeable


def build_orderbook_snapshots(events, jsonl_path, match_date, outcome_names):
    """
    Stream through JSONL, maintain order book, snapshot at each event's
    market_move_start time. Also collect post-event trades for revert checking.
    """
    t1_outcome, t2_outcome = outcome_names

    target_times = []
    for e in events:
        mm = e["market_movement"]
        t = datetime.strptime(f"{match_date} {mm['market_move_start']}", "%Y-%m-%d %H:%M:%S")
        target_times.append(t)

    revert_end_times = [t + timedelta(seconds=REVERT_DELAY_S + REVERT_WINDOW_S) for t in target_times]

    targets_sorted = sorted(enumerate(target_times), key=lambda x: x[1])
    target_idx = 0

    t1_book = OrderBook()
    t2_book = OrderBook()

    snapshots = [None] * len(events)
    post_trades = defaultdict(list)
    active_revert_windows = []

    print(f"Streaming JSONL to build {len(events)} order book snapshots...")
    line_count = 0

    with open(jsonl_path) as f:
        for line in f:
            line_count += 1
            d = json.loads(line)
            typ = d.get("type", "")
            ist = d.get("ist", "")

            if not ist:
                continue

            current_time = parse_ist(ist)
            if current_time is None:
                continue

            if typ in ("level_increase", "pure_fill", "pure_cancel", "snipe_mix"):
                outcome = d.get("outcome", "")
                side = d.get("side", "")
                price = d.get("price")
                new_size = d.get("new_size")

                if price is not None and new_size is not None and outcome and side:
                    if outcome == t1_outcome:
                        t1_book.update(side, price, new_size)
                    elif outcome == t2_outcome:
                        t2_book.update(side, price, new_size)

            while target_idx < len(targets_sorted):
                evt_i, evt_time = targets_sorted[target_idx]
                if current_time >= evt_time and snapshots[evt_i] is None:
                    snapshots[evt_i] = (t1_book.snapshot(), t2_book.snapshot())
                    active_revert_windows.append((
                        evt_i,
                        evt_time + timedelta(seconds=REVERT_DELAY_S),
                        evt_time + timedelta(seconds=REVERT_DELAY_S + REVERT_WINDOW_S)
                    ))
                    target_idx += 1
                else:
                    break

            if typ in ("trade", "pure_fill", "snipe_mix") and active_revert_windows:
                outcome = d.get("outcome", "")
                side = d.get("side", "")
                price = d.get("price")
                if price and outcome and side:
                    still_active = []
                    for evt_i, start, end in active_revert_windows:
                        if current_time <= end:
                            if current_time >= start:
                                post_trades[evt_i].append((
                                    current_time, outcome, side, price
                                ))
                            still_active.append((evt_i, start, end))
                    active_revert_windows = still_active

    print(f"Processed {line_count} JSONL lines")
    return snapshots, post_trades


def check_revert_fill(post_trades_list, outcome, revert_side, revert_price):
    """Check if a GTC revert order would have filled."""
    for trade_time, t_outcome, t_side, t_price in post_trades_list:
        if t_outcome != outcome:
            continue

        if revert_side == "SELL":
            if t_side == "BUY" and t_price >= revert_price:
                return True, trade_time
        elif revert_side == "BUY":
            if t_side == "SELL" and t_price <= revert_price:
                return True, trade_time

    return False, None


TEAM_ABBREV_MAP = {
    "nz": ["new zealand", "zealand"],
    "sa": ["south africa", "africa"],
    "ind": ["india"],
    "aus": ["australia"],
    "eng": ["england"],
    "pak": ["pakistan"],
    "sl": ["sri lanka", "lanka"],
    "wi": ["west indies", "indies"],
    "ban": ["bangladesh"],
    "zim": ["zimbabwe"],
    "afg": ["afghanistan"],
    "ire": ["ireland"],
    "rsa": ["south africa", "africa"],
    "gbr": ["england", "great britain"],
}


def _team_matches(abbrev, full_name):
    """Check if a team abbreviation matches a full outcome name."""
    abbr = abbrev.lower()
    name = full_name.lower()
    if abbr in name:
        return True
    for keyword in TEAM_ABBREV_MAP.get(abbr, []):
        if keyword in name:
            return True
    return False


def run_backtest(events_path, jsonl_path, market_slug, t1_name, t2_name,
                 t1_settlement, t2_settlement):
    events = load_events(events_path)
    print(f"Loaded {len(events)} tradeable events\n")

    # Detect outcome names from capture JSONL
    outcome_names = [None, None]
    match_date = None
    with open(jsonl_path) as f:
        for line in f:
            d = json.loads(line)
            if d.get("type") == "capture_start":
                names = d.get("outcome_names", [])
                for name in names:
                    if _team_matches(t1_name, name):
                        outcome_names[0] = name
                    else:
                        outcome_names[1] = name
                ist = d.get("ist", "")
                dt = parse_ist(ist)
                if dt:
                    match_date = dt.strftime("%Y-%m-%d")
                break

    if not outcome_names[0] or not outcome_names[1]:
        print(f"ERROR: Could not detect outcome names for {t1_name}/{t2_name}")
        return

    print(f"Outcomes: {t1_name}='{outcome_names[0]}', {t2_name}='{outcome_names[1]}'")
    print(f"Match date: {match_date}")
    print(f"Settlement: {t1_name}=${t1_settlement}, {t2_name}=${t2_settlement}")

    # Fetch tick size
    api_tick = fetch_tick_size(market_slug)
    initial_tick, tick_changes = get_tick_sizes_from_jsonl(jsonl_path)
    if api_tick:
        initial_tick = api_tick
        print(f"Tick size from API: {api_tick} (using this)")
    if tick_changes:
        for t, ts in tick_changes:
            print(f"Tick size changed to {ts} at {t.strftime('%H:%M:%S')}")
    else:
        print(f"Tick size: {initial_tick}")

    # Build order book snapshots
    snapshots, post_trades = build_orderbook_snapshots(
        events, jsonl_path, match_date, outcome_names
    )

    # Initialize position
    pos = Position(
        t1_tokens=TOKEN_CONVERSION,
        t2_tokens=TOKEN_CONVERSION,
        cash=TRADING_CASH,
        t1_name=t1_name,
        t2_name=t2_name,
        t1_settlement=t1_settlement,
        t2_settlement=t2_settlement,
    )

    print(f"\n{'='*90}")
    print(f"STARTING POSITION: {pos}")
    print(f"{'='*90}\n")

    results = []

    for i, event in enumerate(events):
        mm = event["market_movement"]
        snap = snapshots[i]

        if snap is None:
            print(f"  [SKIP] No order book snapshot for event at {mm['market_move_start']}")
            continue

        t1_ob, t2_ob = snap
        side = event["side"]
        evt_type = event["event"]

        trade_time_str = mm["market_move_start"]
        event_time = datetime.strptime(f"{match_date} {trade_time_str}", "%Y-%m-%d %H:%M:%S")
        tick = get_tick_at_time(initial_tick, tick_changes, event_time)
        t1_ref = mm["price_before"]
        t2_ref = 1.0 - t1_ref

        print(f"\n--- Event #{i+1}: {evt_type} at {trade_time_str} | {event['innings']} "
              f"| over {event['over']} | side={side} | expected {mm['ticks']} ticks ---")
        print(f"  Market ref: {t1_name}={t1_ref:.3f} {t2_name}={t2_ref:.3f} tick={tick}")

        t1_depth = t1_ob.depth_summary(3)
        t2_depth = t2_ob.depth_summary(3)
        print(f"  {t1_name} book: bids={t1_depth['bids'][:3]}  asks={t1_depth['asks'][:3]}")
        print(f"  {t2_name} book: bids={t2_depth['bids'][:3]}  asks={t2_depth['asks'][:3]}")
        print(f"  Position before: {pos}")

        result = {
            "event": evt_type,
            "time": trade_time_str,
            "side": side,
            "innings": event["innings"],
            "over": event["over"],
            "expected_ticks": mm["ticks"],
            "legs": [],
        }

        if side == t1_name:
            # BUY t1 (sweep t1 asks), SELL t2 (sweep t2 bids)
            buy_tokens, buy_cost, buy_avg, buy_fills = t1_ob.sweep_asks(
                MAX_TRADE_USDC, max_tokens=float('inf'), ref_price=t1_ref
            )
            if buy_tokens > 0 and pos.cash >= buy_cost:
                pos.buy_t1(buy_tokens, buy_cost)
                print(f"  BUY {t1_name}: {buy_tokens:.1f} tokens @ avg {buy_avg:.4f} = ${buy_cost:.2f}")
                result["legs"].append({
                    "action": f"BUY_{t1_name}", "tokens": buy_tokens, "avg_price": buy_avg,
                    "usdc": buy_cost, "fills": buy_fills
                })
            elif buy_tokens > 0:
                affordable_cost = pos.cash
                if affordable_cost > 0:
                    ratio = affordable_cost / buy_cost
                    buy_tokens *= ratio
                    buy_cost = affordable_cost
                    buy_avg = buy_cost / buy_tokens if buy_tokens > 0 else 0
                    pos.buy_t1(buy_tokens, buy_cost)
                    print(f"  BUY {t1_name} (cash-limited): {buy_tokens:.1f} tokens @ avg {buy_avg:.4f} = ${buy_cost:.2f}")
                    result["legs"].append({
                        "action": f"BUY_{t1_name}", "tokens": buy_tokens, "avg_price": buy_avg,
                        "usdc": buy_cost, "fills": []
                    })
            else:
                print(f"  BUY {t1_name}: NO LIQUIDITY on ask side")

            available_t2 = pos.t2
            sell_tokens, sell_proceeds, sell_avg, sell_fills = t2_ob.sweep_bids(
                MAX_TRADE_USDC, max_tokens=available_t2, ref_price=t2_ref
            )
            if sell_tokens > 0:
                pos.sell_t2(sell_tokens, sell_proceeds)
                print(f"  SELL {t2_name}: {sell_tokens:.1f} tokens @ avg {sell_avg:.4f} = ${sell_proceeds:.2f}")
                result["legs"].append({
                    "action": f"SELL_{t2_name}", "tokens": sell_tokens, "avg_price": sell_avg,
                    "usdc": sell_proceeds, "fills": sell_fills
                })
            else:
                print(f"  SELL {t2_name}: NO LIQUIDITY on bid side or no tokens")

        else:  # side == t2_name
            # BUY t2 (sweep t2 asks), SELL t1 (sweep t1 bids)
            buy_tokens, buy_cost, buy_avg, buy_fills = t2_ob.sweep_asks(
                MAX_TRADE_USDC, max_tokens=float('inf'), ref_price=t2_ref
            )
            if buy_tokens > 0 and pos.cash >= buy_cost:
                pos.buy_t2(buy_tokens, buy_cost)
                print(f"  BUY {t2_name}: {buy_tokens:.1f} tokens @ avg {buy_avg:.4f} = ${buy_cost:.2f}")
                result["legs"].append({
                    "action": f"BUY_{t2_name}", "tokens": buy_tokens, "avg_price": buy_avg,
                    "usdc": buy_cost, "fills": buy_fills
                })
            elif buy_tokens > 0:
                affordable_cost = pos.cash
                if affordable_cost > 0:
                    ratio = affordable_cost / buy_cost
                    buy_tokens *= ratio
                    buy_cost = affordable_cost
                    buy_avg = buy_cost / buy_tokens if buy_tokens > 0 else 0
                    pos.buy_t2(buy_tokens, buy_cost)
                    print(f"  BUY {t2_name} (cash-limited): {buy_tokens:.1f} tokens @ avg {buy_avg:.4f} = ${buy_cost:.2f}")
                    result["legs"].append({
                        "action": f"BUY_{t2_name}", "tokens": buy_tokens, "avg_price": buy_avg,
                        "usdc": buy_cost, "fills": []
                    })
            else:
                print(f"  BUY {t2_name}: NO LIQUIDITY on ask side")

            available_t1 = pos.t1
            sell_tokens, sell_proceeds, sell_avg, sell_fills = t1_ob.sweep_bids(
                MAX_TRADE_USDC, max_tokens=available_t1, ref_price=t1_ref
            )
            if sell_tokens > 0:
                pos.sell_t1(sell_tokens, sell_proceeds)
                print(f"  SELL {t1_name}: {sell_tokens:.1f} tokens @ avg {sell_avg:.4f} = ${sell_proceeds:.2f}")
                result["legs"].append({
                    "action": f"SELL_{t1_name}", "tokens": sell_tokens, "avg_price": sell_avg,
                    "usdc": sell_proceeds, "fills": sell_fills
                })
            else:
                print(f"  SELL {t1_name}: NO LIQUIDITY on bid side or no tokens")

        # === REVERT SIMULATION ===
        revert_results = []
        event_post_trades = post_trades.get(i, [])

        for leg in result["legs"]:
            action = leg["action"]
            avg_p = leg["avg_price"]
            tokens = leg["tokens"]

            if "BUY" in action:
                direction = "BUY"
                revert_side = "SELL"
            elif "SELL" in action:
                direction = "SELL"
                revert_side = "BUY"
            else:
                continue

            revert_price = compute_revert_price(avg_p, direction, tick)
            # Determine outcome name from action
            team_in_action = action.split("_", 1)[1]  # "NZ" or "SA"
            outcome = outcome_names[0] if team_in_action == t1_name else outcome_names[1]

            if revert_price is None:
                print(f"  REVERT {revert_side} {team_in_action}: SKIP (price {avg_p:.3f} < 0.05, hold to settlement)")
                revert_results.append({
                    "revert_action": f"{revert_side}_{team_in_action}",
                    "revert_price": None,
                    "filled": False,
                    "edge_profit": 0,
                    "reason": "price_too_low",
                })
                continue

            filled, fill_time = check_revert_fill(
                event_post_trades, outcome, revert_side, revert_price
            )
            revert_usdc = tokens * revert_price
            edge_profit = tokens * abs(revert_price - avg_p) if filled else 0

            if filled:
                if revert_side == "SELL":
                    if team_in_action == t1_name:
                        pos.sell_t1(tokens, revert_usdc)
                    else:
                        pos.sell_t2(tokens, revert_usdc)
                else:
                    if team_in_action == t1_name:
                        pos.buy_t1(tokens, revert_usdc)
                    else:
                        pos.buy_t2(tokens, revert_usdc)
                print(f"  REVERT {revert_side} {team_in_action}: {tokens:.1f} @ {revert_price:.3f} "
                      f"= ${revert_usdc:.2f} (edge ${edge_profit:.2f}) FILLED")
            else:
                print(f"  REVERT {revert_side} {team_in_action}: {tokens:.1f} @ {revert_price:.3f} "
                      f"— NOT FILLED (held to settlement)")

            revert_results.append({
                "revert_action": f"{revert_side}_{team_in_action}",
                "revert_price": revert_price,
                "filled": filled,
                "edge_profit": edge_profit,
            })

        result["reverts"] = revert_results
        result["position_after"] = str(pos)
        results.append(result)
        print(f"  Position after: {pos}")

    # === FINAL P&L ===
    print(f"\n{'='*90}")
    print(f"FINAL POSITION: {pos}")
    print(f"{'='*90}")

    settlement_value = pos.t1 * t1_settlement + pos.t2 * t2_settlement
    total_value = pos.cash + settlement_value
    pnl = total_value - INITIAL_CAPITAL

    print(f"\nSettlement ({t1_name} wins):" if t1_settlement > t2_settlement
          else f"\nSettlement ({t2_name} wins):")
    print(f"  {t1_name} tokens: {pos.t1:.1f} x ${t1_settlement} = ${pos.t1 * t1_settlement:.2f}")
    print(f"  {t2_name} tokens: {pos.t2:.1f} x ${t2_settlement} = ${pos.t2 * t2_settlement:.2f}")
    print(f"  Cash: ${pos.cash:.2f}")
    print(f"  Total value: ${total_value:.2f}")
    print(f"  Initial capital: ${INITIAL_CAPITAL:.2f}")
    print(f"  P&L: ${pnl:.2f} ({pnl/INITIAL_CAPITAL*100:.1f}%)")

    total_reverts = sum(len(r["reverts"]) for r in results)
    filled_reverts = sum(1 for r in results for rv in r["reverts"] if rv["filled"])
    total_edge = sum(rv["edge_profit"] for r in results for rv in r["reverts"] if rv["filled"])
    print(f"\n  Reverts: {filled_reverts}/{total_reverts} filled")
    print(f"  Total edge captured from reverts: ${total_edge:.2f}")

    # Save results
    output = {
        "config": {
            "initial_capital": INITIAL_CAPITAL,
            "token_conversion": TOKEN_CONVERSION,
            "trading_cash": TRADING_CASH,
            "max_trade_usdc": MAX_TRADE_USDC,
            "edge_model": "tiered: >0.15=2ticks, 0.05-0.15=1tick, <0.05=hold",
            "settlement": {t1_name: t1_settlement, t2_name: t2_settlement},
        },
        "events_traded": len(results),
        "final_position": {
            f"{t1_name}_tokens": round(pos.t1, 4),
            f"{t2_name}_tokens": round(pos.t2, 4),
            "cash": round(pos.cash, 4),
        },
        "settlement": {
            f"{t1_name}_value": round(pos.t1 * t1_settlement, 2),
            f"{t2_name}_value": round(pos.t2 * t2_settlement, 2),
            "total_value": round(total_value, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / INITIAL_CAPITAL * 100, 2),
        },
        "trades": results,
    }
    data_dir = Path(__file__).parent.parent / "data" / market_slug
    data_dir.mkdir(parents=True, exist_ok=True)
    out_file = data_dir / f"backtest_results_{market_slug}.json"
    with open(out_file, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nDetailed results saved to {out_file}")


def main():
    parser = argparse.ArgumentParser(description="Backtest cricket event-driven taker strategy")
    parser.add_argument("events_json", help="Path to events JSON (with market_movement filled)")
    parser.add_argument("capture_jsonl", help="Path to capture JSONL (orderbook data)")
    parser.add_argument("market_slug", help="Polymarket market slug")
    parser.add_argument("nz_team", help="Team 1 abbreviation (maps to first Polymarket outcome)")
    parser.add_argument("sa_team", help="Team 2 abbreviation (maps to second Polymarket outcome)")
    parser.add_argument("--settlement", choices=["nz", "sa"], required=True,
                        help="Which team won (for settlement calculation)")
    args = parser.parse_args()

    if args.settlement == "nz":
        t1_settlement, t2_settlement = 1.0, 0.0
    else:
        t1_settlement, t2_settlement = 0.0, 1.0

    run_backtest(
        events_path=Path(args.events_json),
        jsonl_path=Path(args.capture_jsonl),
        market_slug=args.market_slug,
        t1_name=args.nz_team,
        t2_name=args.sa_team,
        t1_settlement=t1_settlement,
        t2_settlement=t2_settlement,
    )


if __name__ == "__main__":
    main()
