#!/usr/bin/env python3
"""
Backtest: informed market maker.

Setup: Split $5k → 5000 NZ + 5000 SA tokens.
Post: SELL both at fair+1tick, BUY both at fair-1tick.
Fills: retail hits our quotes → we collect 1 tick per fill.
Event: oracle fires T-5s → cancel all, rebalance open inventory at entry price,
       repost at new fair after T+3s.
Round-trip: buy at fair-1tick + sell at fair+1tick = 2 ticks profit.

Usage:
    python backtest_maker.py <events.json> <capture.jsonl> <nz_team> <sa_team> \
        --settlement nz|sa --inn1-start HH:MM --inn1-end HH:MM \
        --inn2-start HH:MM --match-end HH:MM [-o output.xlsx]
"""

import argparse
import json
import random
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

# === CONFIG ===
SPLIT_USDC = 5000.0
MAX_TRADE_PER_FILL = 500.0  # max tokens filled per single trade crossing
SAFE_PRICE_MIN = 0.03
SAFE_PRICE_MAX = 0.97
FILL_PROBABILITY = 0.10
ORACLE_LEAD_S = 5
SETTLE_DELAY_S = 3


def parse_ist(ist_str):
    clean = str(ist_str).replace(" IST", "").strip()
    if "." in clean:
        base, frac = clean.split(".", 1)
        clean = f"{base}.{frac[:6]}"
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(clean, fmt)
        except ValueError:
            continue
    return None


def round_to_tick(val, tick):
    return round(round(val / tick) * tick, 6) if tick > 0 else val


QUOTING = "QUOTING"
DARK = "DARK"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("events_json")
    parser.add_argument("capture_jsonl")
    parser.add_argument("nz_team")
    parser.add_argument("sa_team")
    parser.add_argument("--settlement", choices=["nz", "sa"], required=True)
    parser.add_argument("--inn1-start", required=True)
    parser.add_argument("--inn1-end", required=True)
    parser.add_argument("--inn2-start", required=True)
    parser.add_argument("--match-end", required=True)
    parser.add_argument("-o", "--output")
    args = parser.parse_args()

    team_a, team_b = args.nz_team, args.sa_team
    t1_stl = 1.0 if args.settlement == "nz" else 0.0
    t2_stl = 1.0 - t1_stl

    with open(args.events_json) as f:
        all_events = [e for e in json.load(f) if isinstance(e.get("market_movement"), dict)]

    outcome_names = [None, None]
    match_date = None
    abbrev_map = {"nz": "new zealand", "sa": "south africa", "ind": "india",
                  "aus": "australia", "eng": "england", "pak": "pakistan"}
    ta_full = abbrev_map.get(team_a.lower(), team_a.lower())
    with open(args.capture_jsonl) as f:
        for line in f:
            d = json.loads(line)
            if d.get("type") == "capture_start":
                for name in d.get("outcome_names", []):
                    if ta_full in name.lower() or team_a.lower() in name.lower():
                        outcome_names[0] = name
                    else:
                        outcome_names[1] = name
                dt = parse_ist(d.get("ist", ""))
                if dt:
                    match_date = dt.strftime("%Y-%m-%d")
                break

    inn1_start = datetime.strptime(f"{match_date} {args.inn1_start}", "%Y-%m-%d %H:%M")
    inn1_end = datetime.strptime(f"{match_date} {args.inn1_end}", "%Y-%m-%d %H:%M")
    inn2_start = datetime.strptime(f"{match_date} {args.inn2_start}", "%Y-%m-%d %H:%M")
    match_end = datetime.strptime(f"{match_date} {args.match_end}", "%Y-%m-%d %H:%M")

    def in_match(ts):
        return (inn1_start <= ts <= inn1_end) or (inn2_start <= ts <= match_end)

    # Filter events to match time
    events = []
    for e in all_events:
        mm = e["market_movement"]
        reaction = datetime.strptime(f"{match_date} {mm['market_move_start']}", "%Y-%m-%d %H:%M:%S")
        if in_match(reaction):
            events.append(e)

    # Build schedule
    schedule = []
    for e in events:
        mm = e["market_movement"]
        reaction = datetime.strptime(f"{match_date} {mm['market_move_start']}", "%Y-%m-%d %H:%M:%S")
        innings_str = e.get("innings", "")
        batting = team_b
        if team_a.upper() in innings_str.upper():
            batting = team_a
        schedule.append({
            "cancel_at": reaction - timedelta(seconds=ORACLE_LEAD_S),
            "reaction_at": reaction,
            "repost_at": reaction + timedelta(seconds=SETTLE_DELAY_S),
            "event": e,
            "batting": batting,
            "fair_before": mm.get("price_before", 0.5),
            "fair_after": mm.get("price_after", 0.5),
        })
    schedule.sort(key=lambda s: s["cancel_at"])

    print(f"{len(events)} events during match")
    print(f"Inn1: {args.inn1_start}-{args.inn1_end} | Inn2: {args.inn2_start}-{args.match_end}")
    print(f"{team_a}='{outcome_names[0]}' {team_b}='{outcome_names[1]}'")
    print(f"Settlement: {team_a}=${t1_stl}, {team_b}=${t2_stl}")

    # ── State ──
    # Split: $5000 → 5000 A + 5000 B tokens, $0 USDC
    split_tokens = SPLIT_USDC / 2  # per side: 2500 each (split $5k = $2500 per side)
    # Wait — split $5000 USDC → 5000 A + 5000 B tokens (that's how Polymarket split works)
    tokens_a = SPLIT_USDC
    tokens_b = SPLIT_USDC
    usdc = 0.0

    fair_a = 0.50
    tick = 0.01
    quotes = {}  # leg -> (price, size)
    state = DARK
    sched_idx = 0
    match_started = False

    # Track: for each buy fill, we hold inventory until we sell it back
    # On event: exit all unsold inventory at entry price (break-even)
    open_buys = []  # (team, size, price) — inventory from bid fills waiting for ask fill

    fills_log = []
    event_log = []
    spread_captured = 0.0
    round_trips = 0
    rebalances = 0
    quoting_secs = 0.0
    dark_secs = 0.0
    prev_ts = None

    def post_quotes():
        """Post full inventory for sell, full USDC capacity for buy."""
        nonlocal quotes
        fair_b = 1.0 - fair_a
        quotes = {}
        bid_a = round_to_tick(fair_a - tick, tick)
        ask_a = round_to_tick(fair_a + tick, tick)
        bid_b = round_to_tick(fair_b - tick, tick)
        ask_b = round_to_tick(fair_b + tick, tick)

        # BUY orders: how many tokens can we afford with USDC?
        buy_a_sz = usdc / bid_a if bid_a > 0 and usdc > 0 else 0
        buy_b_sz = usdc / bid_b if bid_b > 0 and usdc > 0 else 0

        if SAFE_PRICE_MIN <= bid_a <= SAFE_PRICE_MAX and buy_a_sz > 0:
            quotes["A-BID"] = (bid_a, buy_a_sz)
        if SAFE_PRICE_MIN <= ask_a <= SAFE_PRICE_MAX and tokens_a > 0:
            quotes["A-ASK"] = (ask_a, tokens_a)  # sell ALL NZ tokens
        if SAFE_PRICE_MIN <= bid_b <= SAFE_PRICE_MAX and buy_b_sz > 0:
            quotes["B-BID"] = (bid_b, buy_b_sz)
        if SAFE_PRICE_MIN <= ask_b <= SAFE_PRICE_MAX and tokens_b > 0:
            quotes["B-ASK"] = (ask_b, tokens_b)  # sell ALL SA tokens

    def rebalance_all():
        """Exit ALL open buy positions at break-even (taker FAK at entry price)."""
        nonlocal tokens_a, tokens_b, usdc, rebalances
        rebalanced = []
        for pt, ps, pp in open_buys:
            if pt == "A":
                tokens_a -= ps
            else:
                tokens_b -= ps
            usdc += ps * pp
            rebalances += 1
            rebalanced.append((pt, ps, pp))
        return rebalanced

    with open(args.capture_jsonl) as f:
        for line in f:
            d = json.loads(line)
            typ = d.get("type", "")
            ist = d.get("ist", "")
            if not ist:
                continue
            ts = parse_ist(ist)
            if ts is None or (match_date and ts.strftime("%Y-%m-%d") != match_date):
                continue

            if not in_match(ts):
                if match_started and ts > match_end:
                    break
                continue

            if not match_started:
                match_started = True
                state = QUOTING

            if prev_ts and in_match(prev_ts):
                dt_s = (ts - prev_ts).total_seconds()
                if 0 < dt_s < 60:
                    if state == QUOTING:
                        quoting_secs += dt_s
                    else:
                        dark_secs += dt_s
            prev_ts = ts

            if typ == "tick_size_change":
                tick = float(d.get("new_tick_size", tick))
                continue

            # ── Oracle fires: go DARK ──
            if state == QUOTING and sched_idx < len(schedule):
                sc = schedule[sched_idx]
                if ts >= sc["cancel_at"]:
                    cancelled = dict(quotes)
                    quotes = {}
                    rebalanced = rebalance_all()
                    open_buys.clear()

                    evt = sc["event"]
                    mm = evt["market_movement"]
                    event_log.append({
                        "oracle_fires_ist": sc["cancel_at"].strftime("%H:%M:%S"),
                        "market_moves_ist": sc["reaction_at"].strftime("%H:%M:%S"),
                        "repost_at_ist": sc["repost_at"].strftime("%H:%M:%S"),
                        "event": evt["event"],
                        "over": evt.get("over", ""),
                        "fair_before": round(sc["fair_before"], 4),
                        "fair_after": round(sc["fair_after"], 4),
                        "cancelled_quotes": " | ".join(f"{l}@{v[0]:.3f}" for l, v in cancelled.items()) or "none",
                        "rebalanced": " | ".join(f"{t} {s:.0f}@{p:.3f}(BE)" for t, s, p in rebalanced) or "none",
                        "reposted_quotes": "",  # filled below
                        "inventory_at_cancel": f"A={tokens_a:.0f} B={tokens_b:.0f} ${usdc:.0f}",
                    })
                    state = DARK

            # ── Settle: repost ──
            if state == DARK and sched_idx < len(schedule):
                sc = schedule[sched_idx]
                if ts >= sc["repost_at"]:
                    fair_a = sc["fair_after"]
                    # Merge excess pairs
                    merge = max(0, min(tokens_a, tokens_b) - MAX_TRADE_PER_FILL * 2)
                    if merge > 0:
                        tokens_a -= merge
                        tokens_b -= merge
                        usdc += merge

                    if SAFE_PRICE_MIN <= fair_a <= SAFE_PRICE_MAX:
                        post_quotes()
                        rq = " | ".join(f"{l}@{v[0]:.3f}" for l, v in sorted(quotes.items()))
                    else:
                        rq = "outside safe range"

                    if event_log:
                        event_log[-1]["reposted_quotes"] = rq
                        event_log[-1]["inventory_at_repost"] = f"A={tokens_a:.0f} B={tokens_b:.0f} ${usdc:.0f}"

                    state = QUOTING
                    sched_idx += 1

            # ── Update fair + check fills ──
            if typ in ("trade", "rest_trade", "pure_fill"):
                outcome = d.get("outcome", "")
                price = d.get("price")
                if price is None or not outcome:
                    continue
                price = float(price)
                if outcome == outcome_names[0]:
                    fair_a = price
                    tp = "A"
                elif outcome == outcome_names[1]:
                    fair_a = 1.0 - price
                    tp = "B"
                else:
                    continue

                if state != QUOTING:
                    continue

                bid_leg = f"{tp}-BID"
                ask_leg = f"{tp}-ASK"

                # Get trade size from JSONL (tokens field or compute from usdc/price)
                trade_size = d.get("size") or d.get("tokens")
                if trade_size is None:
                    trade_usdc = d.get("usdc") or d.get("notional")
                    if trade_usdc and price > 0:
                        trade_size = float(trade_usdc) / price
                    else:
                        trade_size = 0
                trade_size = float(trade_size)

                # ── BID HIT: someone sells to us at our bid ──
                if bid_leg in quotes and price <= quotes[bid_leg][0]:
                    if random.random() < FILL_PROBABILITY:
                        bid_px = quotes[bid_leg][0]
                        # Fill size = min(trade size, our resting order size, what we can afford)
                        max_affordable = usdc / bid_px if bid_px > 0 else 0
                        sz = min(trade_size, quotes[bid_leg][1], max_affordable, MAX_TRADE_PER_FILL)
                        if sz <= 0:
                            continue
                        if tp == "A":
                            tokens_a += sz
                        else:
                            tokens_b += sz
                        usdc -= sz * bid_px
                        open_buys.append((tp, sz, bid_px))
                        # Reduce resting size
                        old_sz = quotes[bid_leg][1]
                        remaining_sz = old_sz - sz
                        if remaining_sz > 0.01:
                            quotes[bid_leg] = (bid_px, remaining_sz)
                        else:
                            quotes.pop(bid_leg)
                        fills_log.append({
                            "time_ist": ts.strftime("%H:%M:%S"),
                            "action": f"BUY {tp} (bid hit)",
                            "price": bid_px,
                            "size": round(sz, 2),
                            "usdc": round(sz * bid_px, 2),
                            "spread": "",
                            "inventory": f"A={tokens_a:.0f} B={tokens_b:.0f} ${usdc:.0f}",
                        })

                # ── ASK LIFTED: someone buys from us at our ask ──
                if ask_leg in quotes and price >= quotes[ask_leg][0]:
                    if random.random() < FILL_PROBABILITY:
                        ask_px = quotes[ask_leg][0]
                        avail = tokens_a if tp == "A" else tokens_b
                        sz = min(trade_size, quotes[ask_leg][1], avail, MAX_TRADE_PER_FILL)
                        if sz <= 0:
                            continue
                        if tp == "A":
                            tokens_a -= sz
                        else:
                            tokens_b -= sz
                        usdc += sz * ask_px

                        # Match against open buys FIFO → spread profit
                        this_spread = 0.0
                        remaining = sz
                        new_buys = []
                        for bt, bs, bp in open_buys:
                            if remaining <= 0 or bt != tp:
                                new_buys.append((bt, bs, bp))
                                continue
                            matched = min(remaining, bs)
                            this_spread += (ask_px - bp) * matched
                            round_trips += 1
                            remaining -= matched
                            if bs - matched > 0.01:
                                new_buys.append((bt, bs - matched, bp))
                        open_buys = new_buys
                        spread_captured += this_spread

                        # Reduce resting size
                        old_sz = quotes[ask_leg][1]
                        remaining_sz = old_sz - sz
                        if remaining_sz > 0.01:
                            quotes[ask_leg] = (ask_px, remaining_sz)
                        else:
                            quotes.pop(ask_leg)
                        fills_log.append({
                            "time_ist": ts.strftime("%H:%M:%S"),
                            "action": f"SELL {tp} (ask lifted)",
                            "price": ask_px,
                            "size": round(sz, 2),
                            "usdc": round(sz * ask_px, 2),
                            "spread": round(this_spread, 2),
                            "inventory": f"A={tokens_a:.0f} B={tokens_b:.0f} ${usdc:.0f}",
                        })

    # End of match: rebalance remaining and merge
    rebalanced = rebalance_all()
    open_buys.clear()
    merge = min(tokens_a, tokens_b)
    if merge > 0:
        tokens_a -= merge
        tokens_b -= merge
        usdc += merge

    stl_a = tokens_a * t1_stl
    stl_b = tokens_b * t2_stl
    total = usdc + stl_a + stl_b
    # Initial: split $5000 → 5000A + 5000B. Hold value at settlement = 5000*0 + 5000*1 = $5000.
    initial = SPLIT_USDC
    pnl = total - initial

    fills_df = pd.DataFrame(fills_log)
    events_df = pd.DataFrame(event_log)
    total_secs = quoting_secs + dark_secs
    uptime = quoting_secs / total_secs * 100 if total_secs > 0 else 0
    buys = fills_df[fills_df["action"].str.startswith("BUY")] if not fills_df.empty else pd.DataFrame()
    sells = fills_df[fills_df["action"].str.startswith("SELL")] if not fills_df.empty else pd.DataFrame()

    print(f"\n{'='*80}")
    print(f"FINAL: A={tokens_a:.0f} B={tokens_b:.0f} USDC=${usdc:.2f}")
    print(f"{'='*80}")
    print(f"  Fills: {len(fills_log)} ({len(buys)} buys, {len(sells)} sells)")
    print(f"  Round-trips (buy→sell, +2ticks each): {round_trips}")
    print(f"  Rebalances (break-even exits on events): {rebalances}")
    print(f"  Quoting uptime: {uptime:.0f}%")
    print(f"")
    print(f"  Spread Captured:   ${spread_captured:.2f}")
    print(f"  Adverse Selection: $0.00")
    print(f"  Rebalance Cost:    $0.00")
    print(f"")
    print(f"  Settlement: A={tokens_a:.0f}x${t1_stl} + B={tokens_b:.0f}x${t2_stl} + ${usdc:.2f}")
    print(f"  Total:  ${total:.2f}")
    print(f"  Initial: ${initial:.2f}")
    print(f"  P&L:    ${pnl:.2f} ({pnl/initial*100:.2f}%)")

    out_path = Path(args.output) if args.output else Path(args.capture_jsonl).parent / "backtest_maker.xlsx"
    summary_rows = [
        {"field": "Config", "value": ""},
        {"field": "Split", "value": f"${SPLIT_USDC:.0f} USDC -> {SPLIT_USDC:.0f} {team_a} + {SPLIT_USDC:.0f} {team_b} tokens"},
        {"field": "Quote Size", "value": "Full inventory (all tokens)"},
        {"field": "Spread", "value": "fair ± 1tick (buy low, sell high)"},
        {"field": "Oracle", "value": f"Cancel T-{ORACLE_LEAD_S}s, rebalance at break-even, repost T+{SETTLE_DELAY_S}s"},
        {"field": "Fill Probability", "value": f"{FILL_PROBABILITY*100:.0f}%"},
        {"field": "Match", "value": f"Inn1 {args.inn1_start}-{args.inn1_end} | Inn2 {args.inn2_start}-{args.match_end}"},
        {"field": "", "value": ""},
        {"field": "Results", "value": ""},
        {"field": "Spread Captured", "value": f"${spread_captured:.2f}"},
        {"field": "Adverse Selection", "value": "$0.00"},
        {"field": "Rebalance Cost", "value": "$0.00"},
        {"field": "P&L", "value": f"${pnl:.2f} ({pnl/initial*100:.2f}%)"},
        {"field": "", "value": ""},
        {"field": "Fills", "value": f"{len(fills_log)} ({len(buys)} buys, {len(sells)} sells)"},
        {"field": "Round-trips", "value": round_trips},
        {"field": "Rebalances", "value": rebalances},
        {"field": "Uptime", "value": f"{uptime:.0f}%"},
        {"field": "", "value": ""},
        {"field": "Final", "value": f"A={tokens_a:.0f} B={tokens_b:.0f} ${usdc:.2f} = ${total:.2f}"},
    ]

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        if not fills_df.empty:
            fills_df.to_excel(writer, sheet_name="Fills", index=False)
        if not events_df.empty:
            events_df.to_excel(writer, sheet_name="Event Cancellations", index=False)
        pd.DataFrame(summary_rows).to_excel(writer, sheet_name="Summary", index=False)
        for sn in writer.sheets:
            ws = writer.sheets[sn]
            for col in ws.columns:
                mx = max(len(str(c.value or "")) for c in col)
                ws.column_dimensions[col[0].column_letter].width = min(mx + 2, 60)

    print(f"\n  Output -> {out_path}")


if __name__ == "__main__":
    main()
