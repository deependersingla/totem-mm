#!/usr/bin/env python3
"""
Unified match analysis: ESPN scores + Polymarket trades + market reaction
detection + taker backtest + maker backtest.

Usage:
    python analyze_match.py --slug crint-nzl-zaf-2026-03-25 --espn-id 1491739 \
        [--max-trade-usdc 500] [--safe-min 0.02] [--safe-max 0.98] \
        [--oracle-lead 5] [--boundary-edge 1] [--wicket-edge 2] \
        [--fill-probability 0.10] [--split-usdc 5000]
"""

import argparse
import json
import os
import random
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import requests

# ── Constants ────────────────────────────────────────────

IST = timezone(timedelta(hours=5, minutes=30))

GAMMA_API = "https://gamma-api.polymarket.com"
GOLDSKY_ENDPOINT = (
    "https://api.goldsky.com/api/public/"
    "project_cl6mb8i9h0003e201j6li0diw/"
    "subgraphs/orderbook-subgraph/0.0.1/gn"
)

ESPN_SCOREBOARD_URL = "https://site.web.api.espn.com/apis/site/v2/sports/cricket/8/scoreboard/{espn_id}"
ESPN_PLAYBYPLAY_URL = "https://site.web.api.espn.com/apis/site/v2/sports/cricket/8676/playbyplay"

# Market reaction detection
LOOKBACK_SEC = 120
LOOKBACK_EXTENDED = 480
MIN_LATENCY_SEC = 5
REACTION_WINDOW = 3

# Maker backtest
MAKER_SAFE_MIN = 0.03
MAKER_SAFE_MAX = 0.97
MAX_TRADE_PER_FILL = 500.0
SETTLE_DELAY_S = 3

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
    "ned": ["netherlands"],
    "sco": ["scotland"],
}


def ts_to_ist_str(ts_unix):
    """Unix timestamp to IST time string."""
    return datetime.fromtimestamp(ts_unix, tz=timezone.utc).astimezone(IST).strftime("%H:%M:%S")


def ts_to_ist_full(ts_unix):
    """Unix timestamp to full IST datetime string."""
    return datetime.fromtimestamp(ts_unix, tz=timezone.utc).astimezone(IST).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def ts_to_ist_dt(ts_unix):
    """Unix timestamp to IST datetime object."""
    return datetime.fromtimestamp(ts_unix, tz=timezone.utc).astimezone(IST)


def round_to_tick(val, tick):
    return round(round(val / tick) * tick, 6) if tick > 0 else val


def _team_matches(abbrev, full_name):
    abbr = abbrev.lower()
    name = full_name.lower()
    if abbr in name:
        return True
    for keyword in TEAM_ABBREV_MAP.get(abbr, []):
        if keyword in name:
            return True
    return False


# ═══════════════════════════════════════════════════════════
# STEP 1: Fetch ESPN Data
# ═══════════════════════════════════════════════════════════

def fetch_espn_scoreboard(espn_id):
    """Fetch match scoreboard from ESPN API."""
    url = ESPN_SCOREBOARD_URL.format(espn_id=espn_id)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


def parse_scoreboard(data):
    """Extract team names, winner, match date, description from scoreboard."""
    event = data
    # Might be wrapped in a list or have events key
    if isinstance(data, list):
        event = data[0]
    elif "events" in data:
        event = data["events"][0] if data["events"] else data

    competitions = event.get("competitions", [{}])
    comp = competitions[0] if competitions else {}

    competitors = comp.get("competitors", [])
    team_a_full = competitors[0].get("team", {}).get("displayName", "") if len(competitors) > 0 else ""
    team_b_full = competitors[1].get("team", {}).get("displayName", "") if len(competitors) > 1 else ""
    team_a_abbr = competitors[0].get("team", {}).get("abbreviation", "") if len(competitors) > 0 else ""
    team_b_abbr = competitors[1].get("team", {}).get("abbreviation", "") if len(competitors) > 1 else ""

    winner_abbr = ""
    for c in competitors:
        w = c.get("winner", False)
        if w is True or (isinstance(w, str) and w.lower() == "true"):
            winner_abbr = c.get("team", {}).get("abbreviation", "")
            break

    description = event.get("name", "") or event.get("shortName", "")
    match_date = event.get("date", "")[:10]  # YYYY-MM-DD

    return {
        "team_a_full": team_a_full,
        "team_b_full": team_b_full,
        "team_a_abbr": team_a_abbr,
        "team_b_abbr": team_b_abbr,
        "winner_abbr": winner_abbr,
        "description": description,
        "match_date": match_date,
    }


def fetch_espn_playbyplay(espn_id):
    """Fetch all pages of play-by-play commentary from ESPN."""
    all_items = []
    page = 1
    page_count = None

    while True:
        resp = requests.get(ESPN_PLAYBYPLAY_URL,
                            params={"event": espn_id, "page": page},
                            timeout=30)
        resp.raise_for_status()
        data = resp.json()

        commentary = data.get("commentary", {})
        if page_count is None:
            page_count = commentary.get("pageCount", 1)

        items = commentary.get("items", [])
        all_items.extend(items)
        print(f"  ESPN playbyplay page {page}/{page_count}: {len(items)} items")

        if page >= page_count:
            break
        page += 1
        time.sleep(0.3)

    # Items are in REVERSE order (latest first). Reverse them.
    all_items.reverse()
    return all_items


def parse_playbyplay(items):
    """Parse ESPN playbyplay items into ball records (same logic as definitive_ball_by_ball.py)."""
    balls = []
    for it in items:
        inn = it.get("period", 1)
        over_obj = it.get("over", {})
        over_1idx = over_obj.get("number", 1)
        ball_in_over = over_obj.get("ball", 1)
        ov_str = f"{over_1idx - 1}.{ball_in_over}"

        pt = it.get("playType", {})
        pt_id = int(pt.get("id", 0))
        pt_desc = pt.get("description", "")

        bbb_ts = it.get("bbbTimestamp", 0) / 1000.0

        if inn == 1:
            score_str = it.get("homeScore", "")
        else:
            score_str = it.get("awayScore", "")
        runs = it.get("scoreValue", 0)

        batsman = it.get("batsman", {}).get("athlete", {}).get("shortName", "")
        bowler = it.get("bowler", {}).get("athlete", {}).get("shortName", "")
        short_text = it.get("shortText", "")
        batting_team = it.get("team", {}).get("abbreviation", "")

        dismissal = it.get("dismissal", {})
        dis_type = dismissal.get("type", "") if dismissal.get("dismissal") else ""
        is_runout = (pt_id != 9 and "OUT" in short_text and "out" not in pt_desc)

        event = ""
        summary = ""
        is_special = False

        if pt_id == 9 or is_runout:
            event = "W"
            if is_runout:
                summary = f"RUN OUT (+{runs} run{'s' if runs != 1 else ''})"
                dis_type = "run out"
            else:
                summary = f"WICKET ({dis_type})" if dis_type else "WICKET"
            is_special = True
        elif pt_id == 3:
            event = "4"
            summary = "FOUR"
            is_special = True
        elif pt_id == 4:
            event = "6"
            summary = "SIX"
            is_special = True
        elif pt_id == 6:
            event = "wd"
            summary = f"WIDE (+{runs})"
        elif pt_id == 5:
            event = "nb"
            summary = f"NO BALL (+{runs})"
        elif pt_id == 7:
            event = "bye"
            summary = f"BYE (+{runs})"
        elif pt_id == 8:
            event = "lb"
            summary = f"LEG BYE (+{runs})"
        elif pt_id == 1:
            event = str(runs)
            summary = f'{runs} run{"s" if runs != 1 else ""}'
        elif pt_id == 2:
            event = "0"
            summary = "dot ball"
        else:
            event = str(runs)
            summary = short_text

        balls.append({
            "inn": inn,
            "over": ov_str,
            "batting_team": batting_team,
            "score": score_str,
            "runs": runs,
            "event": event,
            "summary": summary,
            "is_special": is_special,
            "batsman": batsman,
            "bowler": bowler,
            "bbbTimestamp": bbb_ts,
            "bbbTimestamp_ist": datetime.fromtimestamp(bbb_ts, IST).strftime(
                "%Y-%m-%d %H:%M:%S.%f")[:-3] if bbb_ts > 0 else "",
        })

    return balls


def extract_innings_timings(balls):
    """Extract innings start/end timestamps from ball records."""
    inn1_balls = [b for b in balls if b["inn"] == 1 and b["bbbTimestamp"] > 0]
    inn2_balls = [b for b in balls if b["inn"] == 2 and b["bbbTimestamp"] > 0]

    timings = {}
    if inn1_balls:
        ts_list = [b["bbbTimestamp"] for b in inn1_balls]
        timings["inn1_start"] = min(ts_list)
        timings["inn1_end"] = max(ts_list)
    if inn2_balls:
        ts_list = [b["bbbTimestamp"] for b in inn2_balls]
        timings["inn2_start"] = min(ts_list)
        timings["match_end"] = max(ts_list)

    return timings


# ═══════════════════════════════════════════════════════════
# STEP 2: Fetch Polymarket Data
# ═══════════════════════════════════════════════════════════

def fetch_gamma_market(slug):
    """Fetch market info from Polymarket Gamma API."""
    resp = requests.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=15)
    resp.raise_for_status()
    markets = resp.json()
    if not markets:
        print(f"ERROR: No market found for slug '{slug}'")
        sys.exit(1)
    market = markets[0]

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

    tick_size = float(market.get("orderPriceMinTickSize", 0.01))
    question = market.get("question", "")
    condition_id = market.get("conditionId", "")

    return {
        "question": question,
        "condition_id": condition_id,
        "token_ids": tokens,
        "outcomes": outcomes,
        "tick_size": tick_size,
        "raw": market,
    }


def _query_events_cursor(field_name, token_ids, page_size=1000):
    """Fetch OrderFilled events from Goldsky with cursor pagination.
    Copied from match_analytics.py."""
    events = []
    last_id = ""
    side_label = "maker-side" if "maker" in field_name else "taker-side"
    page = 0

    while True:
        id_filter = f', id_gt: "{last_id}"' if last_id else ""
        query = f"""{{
          orderFilledEvents(
            first: {page_size},
            orderBy: id, orderDirection: asc,
            where: {{ {field_name}: {json.dumps(token_ids)}{id_filter} }}
          ) {{
            id maker taker makerAssetId takerAssetId
            makerAmountFilled takerAmountFilled fee
            timestamp transactionHash orderHash
          }}
        }}"""

        data = None
        actual_size = page_size
        retry_sizes = [500, 200]
        for attempt in range(3):
            actual_size = page_size if attempt == 0 else retry_sizes[min(attempt - 1, len(retry_sizes) - 1)]
            cur_timeout = 30 if attempt == 0 else 60
            cur_query = query if attempt == 0 else query.replace(
                f"first: {page_size}", f"first: {actual_size}"
            )
            if attempt > 0:
                print(f"  Retry {attempt}/2 with page_size={actual_size}")
            try:
                resp = requests.post(
                    GOLDSKY_ENDPOINT, json={"query": cur_query}, timeout=cur_timeout
                )
                resp.raise_for_status()
                data = resp.json()
                if "errors" not in data:
                    break
                print(f"  GraphQL error (attempt {attempt + 1}): {data['errors']}")
                data = None
            except Exception as e:
                print(f"  Error page {page} attempt {attempt + 1}: {e}")
                data = None

        if data is None or "errors" in (data or {}):
            print(f"  Giving up on page {page}")
            break

        batch = data.get("data", {}).get("orderFilledEvents", [])
        if not batch:
            break

        events.extend(batch)
        last_id = batch[-1]["id"]
        page += 1
        print(f"  [{side_label}] page {page}: +{len(batch)}, total={len(events)}")

        if len(batch) < actual_size:
            break
        time.sleep(0.15)

    return events


def fetch_all_events(token_ids):
    """Fetch all OrderFilled events for both maker and taker sides."""
    all_events = {}
    for field in ["makerAssetId_in", "takerAssetId_in"]:
        for ev in _query_events_cursor(field, token_ids):
            all_events[ev["id"]] = ev
    print(f"  Deduplicated: {len(all_events)} unique events")
    return list(all_events.values())


def process_events(events, token_ids, outcome_names):
    """Process raw Goldsky events into a DataFrame. Same logic as match_analytics.py."""
    token_to_outcome = dict(zip(token_ids, outcome_names))
    rows = []
    for ev in events:
        maker = ev["maker"].lower()
        taker = ev["taker"].lower()
        maker_asset = ev["makerAssetId"]
        taker_asset = ev["takerAssetId"]
        maker_amount = int(ev["makerAmountFilled"])
        taker_amount = int(ev["takerAmountFilled"])
        fee = int(ev["fee"])
        ts = int(ev["timestamp"])

        if maker_asset in token_ids:
            outcome = token_to_outcome.get(maker_asset, "?")
            token_raw = maker_amount
            usdc_raw = taker_amount
            seller, buyer = maker, taker
            maker_side = "SELL"
        elif taker_asset in token_ids:
            outcome = token_to_outcome.get(taker_asset, "?")
            token_raw = taker_amount
            usdc_raw = maker_amount
            seller, buyer = taker, maker
            maker_side = "BUY"
        else:
            continue

        usdc = usdc_raw / 1e6
        tokens = token_raw / 1e6
        fee_usdc = fee / 1e6
        price = usdc / tokens if tokens > 0 else 0

        rows.append({
            "event_id": ev["id"],
            "timestamp_unix": ts,
            "outcome": outcome,
            "maker_side": maker_side,
            "price": round(price, 6),
            "token_amount": round(tokens, 6),
            "usdc_amount": round(usdc, 6),
            "fee_usdc": round(fee_usdc, 6),
            "maker": maker,
            "taker": taker,
            "buyer": buyer,
            "seller": seller,
            "tx_hash": ev["transactionHash"],
            "order_hash": ev.get("orderHash", ""),
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["timestamp_unix", "event_id"]).reset_index(drop=True)
    return df


# ═══════════════════════════════════════════════════════════
# STEP 3: Detect Market Reactions
# ═══════════════════════════════════════════════════════════

def build_price_timeline_from_goldsky(trades_df, team_a_outcome):
    """Build per-second median price timeline for team_a from Goldsky trades.
    For team_b trades, convert: team_a_price = 1 - team_b_price."""
    if trades_df.empty:
        return []

    price_by_sec = defaultdict(list)
    for _, row in trades_df.iterrows():
        ts = int(row["timestamp_unix"])
        outcome = row["outcome"]
        price = float(row["price"])

        if outcome == team_a_outcome:
            a_price = price
        else:
            a_price = 1.0 - price

        sec_key = ts  # already integer seconds
        price_by_sec[sec_key].append(a_price)

    def median(vals):
        s = sorted(vals)
        return s[len(s) // 2]

    timeline = sorted(
        [(sec, median(prices)) for sec, prices in price_by_sec.items()],
        key=lambda x: x[0]
    )
    print(f"  Built price timeline: {len(timeline)} seconds with trades")
    return timeline


def find_event_reaction(timeline, event_ts_unix, expected_dir, is_wicket,
                        prev_price=None, boundary_jump=1, wicket_jump=2):
    """Find the exact instant the market reacted to an event.
    Same logic as event_market_lead.py:find_event_reaction() but using unix timestamps."""
    if prev_price is not None and (prev_price < 0.15 or prev_price > 0.85):
        jump_threshold = 1
    else:
        jump_threshold = wicket_jump if is_wicket else boundary_jump

    for lookback in (LOOKBACK_SEC, LOOKBACK_EXTENDED):
        t_start = event_ts_unix - lookback
        t_end = event_ts_unix

        pts = [(sec, p) for sec, p in timeline if t_start <= sec <= t_end]
        if len(pts) < 2:
            continue

        for i in range(len(pts) - 1):
            sec_i, p_i = pts[i]

            for j in range(i + 1, len(pts)):
                sec_j, p_j = pts[j]
                gap = sec_j - sec_i
                if gap > REACTION_WINDOW:
                    break

                diff_c = round((p_j - p_i) * 100)
                hit = False
                if expected_dir == "down" and diff_c <= -jump_threshold:
                    hit = True
                    ticks = abs(diff_c)
                elif expected_dir == "up" and diff_c >= jump_threshold:
                    hit = True
                    ticks = diff_c

                if hit:
                    latency = event_ts_unix - sec_i
                    if latency < MIN_LATENCY_SEC:
                        continue
                    return {
                        "reaction_time_unix": sec_i,
                        "reaction_time_ist": ts_to_ist_str(sec_i),
                        "price_before": p_i,
                        "price_after": p_j,
                        "ticks": ticks,
                        "latency": latency,
                        "reaction_secs": round(gap, 1),
                    }

    return None


def detect_all_reactions(special_events, timeline, team_a_abbr, team_b_abbr,
                         team_a_outcome, boundary_jump=1, wicket_jump=2):
    """For each special event, detect market reaction."""
    last_known_price = None
    results = []

    for idx, evt in enumerate(special_events):
        batting_team = evt["batting_team"]
        event_type = evt["event"]
        event_ts = evt["bbbTimestamp"]
        summary = evt["summary"]

        if event_ts <= 0:
            results.append({**evt, "reaction": None})
            continue

        is_wicket = event_type == "W"
        is_boundary = event_type in ("4", "6")

        # Map batting team
        bt = batting_team
        if not _team_matches(bt, team_a_abbr) and not _team_matches(bt, team_b_abbr):
            # Try direct match
            if team_a_abbr.lower() in bt.lower():
                bt = team_a_abbr
            elif team_b_abbr.lower() in bt.lower():
                bt = team_b_abbr

        batting_is_a = _team_matches(bt, team_a_abbr) or team_a_abbr.lower() in bt.lower()

        if is_wicket or "RUN OUT" in summary.upper():
            beneficiary_is_a = not batting_is_a
        elif is_boundary:
            beneficiary_is_a = batting_is_a
        else:
            beneficiary_is_a = batting_is_a

        expected_dir = "up" if beneficiary_is_a else "down"

        reaction = find_event_reaction(
            timeline, int(event_ts), expected_dir, is_wicket,
            prev_price=last_known_price,
            boundary_jump=boundary_jump, wicket_jump=wicket_jump,
        )

        if reaction:
            last_known_price = reaction["price_after"]

        results.append({
            **evt,
            "expected_dir": f"{team_a_abbr}_UP" if expected_dir == "up" else f"{team_a_abbr}_DOWN",
            "reaction": reaction,
        })

    return results


# ═══════════════════════════════════════════════════════════
# STEP 4: Taker Backtest
# ═══════════════════════════════════════════════════════════

def run_taker_backtest(events_with_reactions, trades_df, team_a_abbr, team_b_abbr,
                       team_a_outcome, team_b_outcome, tick_size,
                       max_trade_usdc=500, safe_min=0.02, safe_max=0.98,
                       oracle_lead=5, boundary_edge=1, wicket_edge=2):
    """Run taker backtest using Goldsky trade data for volume-aware revert fills."""
    taker_trades = []
    total_pnl = 0.0

    for evt in events_with_reactions:
        reaction = evt.get("reaction")
        if reaction is None:
            continue

        event_type = evt["event"]
        reaction_time = reaction["reaction_time_unix"]
        price_before = reaction["price_before"]
        oracle_time = reaction_time - oracle_lead

        # Safe price check
        team_b_price = 1.0 - price_before
        if price_before < safe_min or price_before > safe_max or \
           team_b_price < safe_min or team_b_price > safe_max:
            taker_trades.append({
                "inn": evt["inn"],
                "over": evt["over"],
                "event": event_type,
                "summary": evt["summary"],
                "batting_team": evt["batting_team"],
                "event_time_ist": evt.get("bbbTimestamp_ist", ""),
                "reaction_time_ist": reaction["reaction_time_ist"],
                "price_before": round(price_before, 4),
                "expected_dir": evt.get("expected_dir", ""),
                "entry_price": None,
                "edge_ticks": None,
                "revert_price": None,
                "tokens": None,
                "volume_at_revert": None,
                "fill_status": "SKIPPED_SAFE_RANGE",
                "pnl": 0.0,
            })
            continue

        # Determine direction
        expected_dir = evt.get("expected_dir", "")
        is_wicket = event_type == "W"

        # Edge in ticks
        edge = wicket_edge if is_wicket else boundary_edge

        # Entry: we know the pre-reaction price
        entry_price = price_before
        tokens = max_trade_usdc / entry_price if entry_price > 0 else 0

        # Compute revert price
        if "UP" in expected_dir:
            # We BUY team_a at entry, SELL at entry + edge * tick
            revert_price = round_to_tick(entry_price + edge * tick_size, tick_size)
        else:
            # We SELL team_a at entry, BUY at entry - edge * tick
            # Actually for taker: we buy the other side. Let's think about this.
            # If team_a goes DOWN, we sell team_a tokens and buy team_b tokens.
            # Revert: buy back team_a at lower price (entry - edge * tick)
            revert_price = round_to_tick(entry_price - edge * tick_size, tick_size)

        if revert_price <= 0 or revert_price >= 1:
            taker_trades.append({
                "inn": evt["inn"],
                "over": evt["over"],
                "event": event_type,
                "summary": evt["summary"],
                "batting_team": evt["batting_team"],
                "event_time_ist": evt.get("bbbTimestamp_ist", ""),
                "reaction_time_ist": reaction["reaction_time_ist"],
                "price_before": round(price_before, 4),
                "expected_dir": expected_dir,
                "entry_price": round(entry_price, 4),
                "edge_ticks": edge,
                "revert_price": None,
                "tokens": round(tokens, 2),
                "volume_at_revert": 0,
                "fill_status": "INVALID_REVERT",
                "pnl": 0.0,
            })
            continue

        # Volume-aware revert fill check
        # Look at Goldsky trades in [reaction_time + 3s, reaction_time + 120s]
        revert_start = reaction_time + 3
        revert_end = reaction_time + 120

        if not trades_df.empty:
            window = trades_df[
                (trades_df["timestamp_unix"] >= revert_start) &
                (trades_df["timestamp_unix"] <= revert_end)
            ]
        else:
            window = pd.DataFrame()

        volume_at_revert = 0.0
        fill_status = "NOT_FILLED"

        if not window.empty:
            if "UP" in expected_dir:
                # We want to SELL team_a at revert_price. Check for BUY trades at >= revert_price.
                # team_a trades where someone buys at >= our sell price
                relevant_a = window[
                    (window["outcome"] == team_a_outcome) &
                    (window["price"] >= revert_price)
                ]
                # Also team_b sells (which means team_a buys from their perspective)
                # team_b trades at <= (1 - revert_price) also count
                relevant_b = window[
                    (window["outcome"] == team_b_outcome) &
                    (window["price"] <= round(1.0 - revert_price, 6))
                ]
                vol_a = relevant_a["token_amount"].sum() if not relevant_a.empty else 0
                vol_b = relevant_b["token_amount"].sum() if not relevant_b.empty else 0
                volume_at_revert = vol_a + vol_b
            else:
                # We want to BUY team_a at revert_price. Check for SELL trades at <= revert_price.
                relevant_a = window[
                    (window["outcome"] == team_a_outcome) &
                    (window["price"] <= revert_price)
                ]
                relevant_b = window[
                    (window["outcome"] == team_b_outcome) &
                    (window["price"] >= round(1.0 - revert_price, 6))
                ]
                vol_a = relevant_a["token_amount"].sum() if not relevant_a.empty else 0
                vol_b = relevant_b["token_amount"].sum() if not relevant_b.empty else 0
                volume_at_revert = vol_a + vol_b

            if volume_at_revert >= tokens:
                fill_status = "FILLED"
            elif volume_at_revert > 0:
                fill_status = "PARTIAL"
            else:
                fill_status = "NOT_FILLED"

        # PnL
        if fill_status == "FILLED":
            pnl = edge * tick_size * tokens
        elif fill_status == "PARTIAL":
            # Partial fill: proportional PnL on filled portion
            filled_tokens = min(volume_at_revert, tokens)
            pnl = edge * tick_size * filled_tokens
        else:
            pnl = 0.0  # break-even exit

        total_pnl += pnl

        taker_trades.append({
            "inn": evt["inn"],
            "over": evt["over"],
            "event": event_type,
            "summary": evt["summary"],
            "batting_team": evt["batting_team"],
            "event_time_ist": evt.get("bbbTimestamp_ist", ""),
            "reaction_time_ist": reaction["reaction_time_ist"],
            "price_before": round(price_before, 4),
            "expected_dir": expected_dir,
            "entry_price": round(entry_price, 4),
            "edge_ticks": edge,
            "revert_price": round(revert_price, 4),
            "tokens": round(tokens, 2),
            "volume_at_revert": round(volume_at_revert, 2),
            "fill_status": fill_status,
            "pnl": round(pnl, 2),
        })

    return taker_trades, round(total_pnl, 2)


# ═══════════════════════════════════════════════════════════
# STEP 5: Maker Backtest
# ═══════════════════════════════════════════════════════════

def run_maker_backtest(events_with_reactions, trades_df, team_a_outcome, team_b_outcome,
                       team_a_abbr, team_b_abbr, tick_size, timings, winner_abbr,
                       oracle_lead=5, fill_probability=0.10, split_usdc=5000):
    """Run maker backtest using Goldsky trade data."""
    random.seed(42)  # reproducible

    t1_stl = 1.0 if _team_matches(winner_abbr, team_a_abbr) else 0.0
    t2_stl = 1.0 - t1_stl

    # Build event schedule from reactions
    schedule = []
    for evt in events_with_reactions:
        reaction = evt.get("reaction")
        if reaction is None:
            continue
        reaction_unix = reaction["reaction_time_unix"]

        schedule.append({
            "cancel_at": reaction_unix - oracle_lead,
            "reaction_at": reaction_unix,
            "repost_at": reaction_unix + SETTLE_DELAY_S,
            "event": evt,
            "fair_before": reaction["price_before"],
            "fair_after": reaction["price_after"],
        })
    schedule.sort(key=lambda s: s["cancel_at"])

    # Match time windows
    inn1_start_unix = int(timings.get("inn1_start", 0))
    inn1_end_unix = int(timings.get("inn1_end", 0))
    inn2_start_unix = int(timings.get("inn2_start", 0))
    match_end_unix = int(timings.get("match_end", 0))

    def in_match(ts):
        return (inn1_start_unix <= ts <= inn1_end_unix) or \
               (inn2_start_unix <= ts <= match_end_unix)

    # State
    tokens_a = float(split_usdc)
    tokens_b = float(split_usdc)
    usdc = 0.0
    fair_a = 0.50
    tick = tick_size
    quotes = {}
    QUOTING, DARK = "QUOTING", "DARK"
    state = DARK
    sched_idx = 0
    match_started = False

    open_buys = []
    fills_log = []
    event_log = []
    spread_captured = 0.0
    round_trips = 0
    rebalances = 0
    quoting_secs = 0.0
    dark_secs = 0.0
    prev_ts = None

    def post_quotes():
        nonlocal quotes
        fair_b = 1.0 - fair_a
        quotes = {}
        bid_a = round_to_tick(fair_a - tick, tick)
        ask_a = round_to_tick(fair_a + tick, tick)
        bid_b = round_to_tick(fair_b - tick, tick)
        ask_b = round_to_tick(fair_b + tick, tick)

        buy_a_sz = usdc / bid_a if bid_a > 0 and usdc > 0 else 0
        buy_b_sz = usdc / bid_b if bid_b > 0 and usdc > 0 else 0

        if MAKER_SAFE_MIN <= bid_a <= MAKER_SAFE_MAX and buy_a_sz > 0:
            quotes["A-BID"] = (bid_a, buy_a_sz)
        if MAKER_SAFE_MIN <= ask_a <= MAKER_SAFE_MAX and tokens_a > 0:
            quotes["A-ASK"] = (ask_a, tokens_a)
        if MAKER_SAFE_MIN <= bid_b <= MAKER_SAFE_MAX and buy_b_sz > 0:
            quotes["B-BID"] = (bid_b, buy_b_sz)
        if MAKER_SAFE_MIN <= ask_b <= MAKER_SAFE_MAX and tokens_b > 0:
            quotes["B-ASK"] = (ask_b, tokens_b)

    def rebalance_all():
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

    # Filter trades to match time only
    if trades_df.empty:
        match_trades = pd.DataFrame()
    else:
        match_trades = trades_df[trades_df["timestamp_unix"].apply(in_match)].copy()
        match_trades = match_trades.sort_values("timestamp_unix").reset_index(drop=True)

    for _, row in match_trades.iterrows():
        ts = int(row["timestamp_unix"])
        outcome = row["outcome"]
        price = float(row["price"])
        trade_size = float(row["token_amount"])

        if not match_started:
            match_started = True
            state = QUOTING

        if prev_ts and in_match(prev_ts):
            dt_s = ts - prev_ts
            if 0 < dt_s < 60:
                if state == QUOTING:
                    quoting_secs += dt_s
                else:
                    dark_secs += dt_s
        prev_ts = ts

        # Oracle fires: go DARK
        if state == QUOTING and sched_idx < len(schedule):
            sc = schedule[sched_idx]
            if ts >= sc["cancel_at"]:
                cancelled = dict(quotes)
                quotes = {}
                rebalanced = rebalance_all()
                open_buys.clear()

                event_log.append({
                    "oracle_fires_ist": ts_to_ist_str(sc["cancel_at"]),
                    "market_moves_ist": ts_to_ist_str(sc["reaction_at"]),
                    "repost_at_ist": ts_to_ist_str(sc["repost_at"]),
                    "event": sc["event"]["event"],
                    "over": sc["event"].get("over", ""),
                    "fair_before": round(sc["fair_before"], 4),
                    "fair_after": round(sc["fair_after"], 4),
                    "cancelled_quotes": " | ".join(
                        f"{l}@{v[0]:.3f}" for l, v in cancelled.items()) or "none",
                    "rebalanced": " | ".join(
                        f"{t} {s:.0f}@{p:.3f}(BE)" for t, s, p in rebalanced) or "none",
                    "reposted_quotes": "",
                    "inventory_at_cancel": f"A={tokens_a:.0f} B={tokens_b:.0f} ${usdc:.0f}",
                })
                state = DARK

        # Settle: repost
        if state == DARK and sched_idx < len(schedule):
            sc = schedule[sched_idx]
            if ts >= sc["repost_at"]:
                fair_a = sc["fair_after"]
                merge = max(0, min(tokens_a, tokens_b) - MAX_TRADE_PER_FILL * 2)
                if merge > 0:
                    tokens_a -= merge
                    tokens_b -= merge
                    usdc += merge

                if MAKER_SAFE_MIN <= fair_a <= MAKER_SAFE_MAX:
                    post_quotes()
                    rq = " | ".join(f"{l}@{v[0]:.3f}" for l, v in sorted(quotes.items()))
                else:
                    rq = "outside safe range"

                if event_log:
                    event_log[-1]["reposted_quotes"] = rq
                    event_log[-1]["inventory_at_repost"] = (
                        f"A={tokens_a:.0f} B={tokens_b:.0f} ${usdc:.0f}"
                    )
                state = QUOTING
                sched_idx += 1

        # Update fair + check fills
        if outcome == team_a_outcome:
            fair_a = price
            tp = "A"
        elif outcome == team_b_outcome:
            fair_a = 1.0 - price
            tp = "B"
        else:
            continue

        if state != QUOTING:
            continue

        bid_leg = f"{tp}-BID"
        ask_leg = f"{tp}-ASK"

        # BID HIT
        if bid_leg in quotes and price <= quotes[bid_leg][0]:
            if random.random() < fill_probability:
                bid_px = quotes[bid_leg][0]
                max_affordable = usdc / bid_px if bid_px > 0 else 0
                sz = min(trade_size, quotes[bid_leg][1], max_affordable, MAX_TRADE_PER_FILL)
                if sz > 0:
                    if tp == "A":
                        tokens_a += sz
                    else:
                        tokens_b += sz
                    usdc -= sz * bid_px
                    open_buys.append((tp, sz, bid_px))
                    old_sz = quotes[bid_leg][1]
                    remaining_sz = old_sz - sz
                    if remaining_sz > 0.01:
                        quotes[bid_leg] = (bid_px, remaining_sz)
                    else:
                        quotes.pop(bid_leg, None)
                    fills_log.append({
                        "time_ist": ts_to_ist_str(ts),
                        "action": f"BUY {tp} (bid hit)",
                        "price": bid_px,
                        "size": round(sz, 2),
                        "usdc": round(sz * bid_px, 2),
                        "spread": "",
                        "inventory": f"A={tokens_a:.0f} B={tokens_b:.0f} ${usdc:.0f}",
                    })

        # ASK LIFTED
        if ask_leg in quotes and price >= quotes[ask_leg][0]:
            if random.random() < fill_probability:
                ask_px = quotes[ask_leg][0]
                avail = tokens_a if tp == "A" else tokens_b
                sz = min(trade_size, quotes[ask_leg][1], avail, MAX_TRADE_PER_FILL)
                if sz > 0:
                    if tp == "A":
                        tokens_a -= sz
                    else:
                        tokens_b -= sz
                    usdc += sz * ask_px

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

                    old_sz = quotes[ask_leg][1]
                    remaining_sz = old_sz - sz
                    if remaining_sz > 0.01:
                        quotes[ask_leg] = (ask_px, remaining_sz)
                    else:
                        quotes.pop(ask_leg, None)
                    fills_log.append({
                        "time_ist": ts_to_ist_str(ts),
                        "action": f"SELL {tp} (ask lifted)",
                        "price": ask_px,
                        "size": round(sz, 2),
                        "usdc": round(sz * ask_px, 2),
                        "spread": round(this_spread, 2),
                        "inventory": f"A={tokens_a:.0f} B={tokens_b:.0f} ${usdc:.0f}",
                    })

    # End of match
    rebalance_all()
    open_buys.clear()
    merge = min(tokens_a, tokens_b)
    if merge > 0:
        tokens_a -= merge
        tokens_b -= merge
        usdc += merge

    stl_a = tokens_a * t1_stl
    stl_b = tokens_b * t2_stl
    total = usdc + stl_a + stl_b
    initial = float(split_usdc)
    pnl = total - initial

    total_secs = quoting_secs + dark_secs
    uptime = quoting_secs / total_secs * 100 if total_secs > 0 else 0
    fills_df = pd.DataFrame(fills_log) if fills_log else pd.DataFrame()
    buys_count = len([f for f in fills_log if f["action"].startswith("BUY")])
    sells_count = len([f for f in fills_log if f["action"].startswith("SELL")])

    maker_summary = {
        "split_usdc": split_usdc,
        "fill_probability": fill_probability,
        "oracle_lead": oracle_lead,
        "tick_size": tick_size,
        "total_fills": len(fills_log),
        "buys": buys_count,
        "sells": sells_count,
        "round_trips": round_trips,
        "rebalances": rebalances,
        "spread_captured": round(spread_captured, 2),
        "quoting_uptime_pct": round(uptime, 1),
        "final_tokens_a": round(tokens_a, 2),
        "final_tokens_b": round(tokens_b, 2),
        "final_usdc": round(usdc, 2),
        "settlement_value": round(total, 2),
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl / initial * 100, 2) if initial > 0 else 0,
    }

    return maker_summary, fills_log, event_log


# ═══════════════════════════════════════════════════════════
# STEP 6: Output
# ═══════════════════════════════════════════════════════════

def auto_width(writer, sheet_name, max_width=50):
    """Auto-size columns in an Excel sheet."""
    ws = writer.sheets[sheet_name]
    for col in ws.columns:
        mx = max(len(str(c.value or "")) for c in col)
        header_len = len(str(col[0].value or ""))
        ws.column_dimensions[col[0].column_letter].width = min(max(mx, header_len) + 2, max_width)


def write_scores_xlsx(output_path, balls, special_events, match_info, timings, slug, espn_id):
    """Write scores xlsx with 3 sheets."""
    # Ball by Ball
    bbb_df = pd.DataFrame([{
        "Inn": b["inn"],
        "Over": b["over"],
        "Team": b["batting_team"],
        "Score": b["score"],
        "Runs": b["runs"],
        "Event": b["event"],
        "Summary": b["summary"],
        "Batsman": b["batsman"],
        "Bowler": b["bowler"],
        "bbbTimestamp_IST": b["bbbTimestamp_ist"],
    } for b in balls])

    # Special Events
    se_df = pd.DataFrame([{
        "Inn": b["inn"],
        "Over": b["over"],
        "Team": b["batting_team"],
        "Score": b["score"],
        "Runs": b["runs"],
        "Event": b["event"],
        "Summary": b["summary"],
        "bbbTimestamp_IST": b["bbbTimestamp_ist"],
    } for b in special_events])

    # Match Summary
    timing_strs = {}
    for key, val in timings.items():
        if val > 0:
            timing_strs[key] = ts_to_ist_full(int(val))

    summary_rows = [
        {"Field": "Team A", "Value": match_info["team_a_full"]},
        {"Field": "Team A Abbr", "Value": match_info["team_a_abbr"]},
        {"Field": "Team B", "Value": match_info["team_b_full"]},
        {"Field": "Team B Abbr", "Value": match_info["team_b_abbr"]},
        {"Field": "Winner", "Value": match_info["winner_abbr"]},
        {"Field": "Description", "Value": match_info["description"]},
        {"Field": "Match Date", "Value": match_info["match_date"]},
        {"Field": "Inn 1 Start (IST)", "Value": timing_strs.get("inn1_start", "")},
        {"Field": "Inn 1 End (IST)", "Value": timing_strs.get("inn1_end", "")},
        {"Field": "Inn 2 Start (IST)", "Value": timing_strs.get("inn2_start", "")},
        {"Field": "Match End (IST)", "Value": timing_strs.get("match_end", "")},
        {"Field": "Slug", "Value": slug},
        {"Field": "ESPN ID", "Value": espn_id},
        {"Field": "Total Deliveries", "Value": len(balls)},
        {"Field": "Special Events (W/4/6)", "Value": len(special_events)},
    ]
    ms_df = pd.DataFrame(summary_rows)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        bbb_df.to_excel(writer, sheet_name="Ball by Ball", index=False)
        se_df.to_excel(writer, sheet_name="Special Events", index=False)
        ms_df.to_excel(writer, sheet_name="Match Summary", index=False)
        for sn in ["Ball by Ball", "Special Events", "Match Summary"]:
            auto_width(writer, sn)

    print(f"  Saved: {output_path}")


def write_match_analytics_xlsx(output_path, trades_df):
    """Write match analytics xlsx (Event Log, Wallet Summary)."""
    if trades_df.empty:
        print(f"  Skipping match analytics (no trades)")
        return

    # Add IST time column
    df = trades_df.copy()
    df["time_ist"] = df["timestamp_unix"].apply(ts_to_ist_str)

    # Event Log sheet
    event_log = df[[
        "event_id", "timestamp_unix", "time_ist", "outcome", "maker_side",
        "price", "token_amount", "usdc_amount", "fee_usdc",
        "maker", "taker", "buyer", "seller", "tx_hash",
    ]]

    # Wallet Summary
    wallets = set(df["maker"].tolist() + df["taker"].tolist())
    wallet_rows = []
    for w in wallets:
        as_maker = df[df["maker"] == w]
        as_taker = df[df["taker"] == w]
        wallet_rows.append({
            "wallet": w,
            "as_maker_count": len(as_maker),
            "as_taker_count": len(as_taker),
            "total_trades": len(as_maker) + len(as_taker),
            "total_usdc_volume": round(
                as_maker["usdc_amount"].sum() + as_taker["usdc_amount"].sum(), 2
            ),
        })
    wallet_df = pd.DataFrame(wallet_rows).sort_values("total_usdc_volume", ascending=False)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        event_log.to_excel(writer, sheet_name="Event Log", index=False)
        wallet_df.to_excel(writer, sheet_name="Wallet Summary", index=False)
        for sn in ["Event Log", "Wallet Summary"]:
            auto_width(writer, sn)

    print(f"  Saved: {output_path}")


def write_analysis_xlsx(output_path, match_info, timings, slug, espn_id, tick_size,
                        events_with_reactions, team_a_abbr, team_b_abbr,
                        taker_trades, taker_total_pnl, taker_config,
                        maker_summary):
    """Write the main analysis xlsx with 6 sheets."""

    # 1. Match Summary
    timing_strs = {}
    for key, val in timings.items():
        if val > 0:
            timing_strs[key] = ts_to_ist_full(int(val))

    summary_rows = [
        {"Field": "Team A", "Value": f"{match_info['team_a_full']} ({match_info['team_a_abbr']})"},
        {"Field": "Team B", "Value": f"{match_info['team_b_full']} ({match_info['team_b_abbr']})"},
        {"Field": "Winner", "Value": match_info["winner_abbr"]},
        {"Field": "Match Date", "Value": match_info["match_date"]},
        {"Field": "Inn 1 Start (IST)", "Value": timing_strs.get("inn1_start", "")},
        {"Field": "Inn 1 End (IST)", "Value": timing_strs.get("inn1_end", "")},
        {"Field": "Inn 2 Start (IST)", "Value": timing_strs.get("inn2_start", "")},
        {"Field": "Match End (IST)", "Value": timing_strs.get("match_end", "")},
        {"Field": "Slug", "Value": slug},
        {"Field": "ESPN ID", "Value": espn_id},
        {"Field": "Tick Size", "Value": tick_size},
    ]
    summary_df = pd.DataFrame(summary_rows)

    # 2. Special Events + Market
    se_market_rows = []
    for evt in events_with_reactions:
        reaction = evt.get("reaction")
        se_market_rows.append({
            "Inn": evt["inn"],
            "Over": evt["over"],
            "Team": evt["batting_team"],
            "Score": evt["score"],
            "Event": evt["event"],
            "Summary": evt["summary"],
            "event_time_ist": evt.get("bbbTimestamp_ist", ""),
            "expected_dir": evt.get("expected_dir", ""),
            "market_reaction_ist": reaction["reaction_time_ist"] if reaction else "",
            "market_lead_sec": reaction["latency"] if reaction else None,
            "reaction_ticks": reaction["ticks"] if reaction else None,
            "reaction_secs": reaction["reaction_secs"] if reaction else None,
        })
    se_market_df = pd.DataFrame(se_market_rows)

    # 3. Market Movements
    movement_rows = []
    for evt in events_with_reactions:
        reaction = evt.get("reaction")
        if reaction:
            b_before = round(1.0 - reaction["price_before"], 4)
            b_after = round(1.0 - reaction["price_after"], 4)
            movement_rows.append({
                "Inn": evt["inn"],
                "Over": evt["over"],
                "Event": evt["event"],
                "Summary": evt["summary"],
                "Score": evt["score"],
                "event_time_ist": evt.get("bbbTimestamp_ist", ""),
                "expected_dir": evt.get("expected_dir", ""),
                "market_reaction_ist": reaction["reaction_time_ist"],
                f"{team_a_abbr}_before": round(reaction["price_before"], 4),
                f"{team_a_abbr}_after": round(reaction["price_after"], 4),
                f"{team_b_abbr}_before": b_before,
                f"{team_b_abbr}_after": b_after,
                "reaction_ticks": reaction["ticks"],
                "reaction_secs": reaction["reaction_secs"],
                "market_lead_sec": reaction["latency"],
                "status": "matched",
            })
        else:
            movement_rows.append({
                "Inn": evt["inn"],
                "Over": evt["over"],
                "Event": evt["event"],
                "Summary": evt["summary"],
                "Score": evt["score"],
                "event_time_ist": evt.get("bbbTimestamp_ist", ""),
                "expected_dir": evt.get("expected_dir", ""),
                "market_reaction_ist": "",
                f"{team_a_abbr}_before": None,
                f"{team_a_abbr}_after": None,
                f"{team_b_abbr}_before": None,
                f"{team_b_abbr}_after": None,
                "reaction_ticks": None,
                "reaction_secs": None,
                "market_lead_sec": None,
                "status": "no_reaction",
            })
    movements_df = pd.DataFrame(movement_rows)

    # 4. Taker Trades
    taker_df = pd.DataFrame(taker_trades)

    # 5. Taker Summary
    taker_summary_rows = [
        {"Field": "Config", "Value": ""},
        {"Field": "max_trade_usdc", "Value": taker_config["max_trade_usdc"]},
        {"Field": "safe_min", "Value": taker_config["safe_min"]},
        {"Field": "safe_max", "Value": taker_config["safe_max"]},
        {"Field": "oracle_lead", "Value": taker_config["oracle_lead"]},
        {"Field": "boundary_edge", "Value": taker_config["boundary_edge"]},
        {"Field": "wicket_edge", "Value": taker_config["wicket_edge"]},
        {"Field": "tick_size", "Value": tick_size},
        {"Field": "", "Value": ""},
        {"Field": "Results", "Value": ""},
        {"Field": "Total Events", "Value": len(events_with_reactions)},
        {"Field": "Events with Reaction", "Value": sum(1 for e in events_with_reactions if e.get("reaction"))},
        {"Field": "Trades Attempted", "Value": sum(1 for t in taker_trades if t["fill_status"] not in ("SKIPPED_SAFE_RANGE", "INVALID_REVERT"))},
        {"Field": "Filled", "Value": sum(1 for t in taker_trades if t["fill_status"] == "FILLED")},
        {"Field": "Partial", "Value": sum(1 for t in taker_trades if t["fill_status"] == "PARTIAL")},
        {"Field": "Not Filled", "Value": sum(1 for t in taker_trades if t["fill_status"] == "NOT_FILLED")},
        {"Field": "Total PnL", "Value": f"${taker_total_pnl:.2f}"},
    ]
    taker_summary_df = pd.DataFrame(taker_summary_rows)

    # 6. Maker Summary
    maker_rows = [
        {"Field": "Config", "Value": ""},
        {"Field": "split_usdc", "Value": maker_summary["split_usdc"]},
        {"Field": "fill_probability", "Value": maker_summary["fill_probability"]},
        {"Field": "oracle_lead", "Value": maker_summary["oracle_lead"]},
        {"Field": "tick_size", "Value": maker_summary["tick_size"]},
        {"Field": "", "Value": ""},
        {"Field": "Results", "Value": ""},
        {"Field": "Total Fills", "Value": maker_summary["total_fills"]},
        {"Field": "Buys", "Value": maker_summary["buys"]},
        {"Field": "Sells", "Value": maker_summary["sells"]},
        {"Field": "Round-trips", "Value": maker_summary["round_trips"]},
        {"Field": "Rebalances", "Value": maker_summary["rebalances"]},
        {"Field": "Spread Captured", "Value": f"${maker_summary['spread_captured']:.2f}"},
        {"Field": "Quoting Uptime", "Value": f"{maker_summary['quoting_uptime_pct']:.1f}%"},
        {"Field": "", "Value": ""},
        {"Field": "Final Position", "Value": ""},
        {"Field": f"Tokens A ({team_a_abbr})", "Value": maker_summary["final_tokens_a"]},
        {"Field": f"Tokens B ({team_b_abbr})", "Value": maker_summary["final_tokens_b"]},
        {"Field": "USDC", "Value": f"${maker_summary['final_usdc']:.2f}"},
        {"Field": "Settlement Value", "Value": f"${maker_summary['settlement_value']:.2f}"},
        {"Field": "PnL", "Value": f"${maker_summary['pnl']:.2f} ({maker_summary['pnl_pct']:.2f}%)"},
    ]
    maker_df = pd.DataFrame(maker_rows)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="Match Summary", index=False)
        se_market_df.to_excel(writer, sheet_name="Special Events + Market", index=False)
        movements_df.to_excel(writer, sheet_name="Market Movements", index=False)
        taker_df.to_excel(writer, sheet_name="Taker Trades", index=False)
        taker_summary_df.to_excel(writer, sheet_name="Taker Summary", index=False)
        maker_df.to_excel(writer, sheet_name="Maker Summary", index=False)
        for sn in writer.sheets:
            auto_width(writer, sn)

    print(f"  Saved: {output_path}")


def update_ledger(data_dir, slug, taker_total_pnl, taker_config, maker_summary):
    """Update data/ledger.md with match results."""
    ledger_path = Path(data_dir).parent / "ledger.md"

    taker_pct = (taker_total_pnl / taker_config["max_trade_usdc"] * 100
                 if taker_config["max_trade_usdc"] > 0 else 0)
    maker_pnl = maker_summary["pnl"]
    maker_pct = maker_summary["pnl_pct"]

    taker_str = f"+${taker_total_pnl:.2f}" if taker_total_pnl >= 0 else f"-${abs(taker_total_pnl):.2f}"
    maker_str = f"+${maker_pnl:.2f}" if maker_pnl >= 0 else f"-${abs(maker_pnl):.2f}"

    new_line = (
        f"| {slug} "
        f"| [analysis]({slug}/analysis_{slug}.xlsx) "
        f"| {taker_str} ({taker_pct:.1f}%) "
        f"| {maker_str} ({maker_pct:.1f}%) |"
    )

    header = (
        "| Slug | Analysis | Taker PnL | Maker PnL |\n"
        "|------|----------|-----------|-----------|"
    )

    if ledger_path.exists():
        content = ledger_path.read_text()
        # Check if slug already in ledger
        if slug in content:
            # Replace existing line
            lines = content.split("\n")
            new_lines = []
            for line in lines:
                if slug in line and line.strip().startswith("|"):
                    new_lines.append(new_line)
                else:
                    new_lines.append(line)
            content = "\n".join(new_lines)
        else:
            # Append
            content = content.rstrip() + "\n" + new_line + "\n"
    else:
        content = header + "\n" + new_line + "\n"

    ledger_path.write_text(content)
    print(f"  Updated: {ledger_path}")


# ═══════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Unified cricket match analysis: ESPN + Polymarket")
    parser.add_argument("--slug", required=True, help="Polymarket market slug")
    parser.add_argument("--espn-id", required=True, help="ESPN match ID")
    parser.add_argument("--max-trade-usdc", type=float, default=500,
                        help="Max USDC per taker trade (default: 500)")
    parser.add_argument("--safe-min", type=float, default=0.02,
                        help="Min safe price for taker (default: 0.02)")
    parser.add_argument("--safe-max", type=float, default=0.98,
                        help="Max safe price for taker (default: 0.98)")
    parser.add_argument("--oracle-lead", type=int, default=5,
                        help="Oracle lead in seconds (default: 5)")
    parser.add_argument("--boundary-edge", type=int, default=1,
                        help="Edge ticks for boundary events (default: 1)")
    parser.add_argument("--wicket-edge", type=int, default=2,
                        help="Edge ticks for wicket events (default: 2)")
    parser.add_argument("--fill-probability", type=float, default=0.10,
                        help="Maker fill probability (default: 0.10)")
    parser.add_argument("--split-usdc", type=float, default=5000,
                        help="Maker split USDC (default: 5000)")
    args = parser.parse_args()

    slug = args.slug
    espn_id = args.espn_id

    # Output directory
    base_dir = Path(__file__).parent.parent / "data" / slug
    base_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 80)
    print(f"  UNIFIED MATCH ANALYSIS: {slug}")
    print("=" * 80)

    # ── Step 1: Fetch ESPN data ──────────────────────────
    print(f"\n[STEP 1] Fetching ESPN data for match {espn_id}...")

    print("  Fetching scoreboard...")
    scoreboard = fetch_espn_scoreboard(espn_id)
    match_info = parse_scoreboard(scoreboard)
    team_a_abbr = match_info["team_a_abbr"]
    team_b_abbr = match_info["team_b_abbr"]
    winner_abbr = match_info["winner_abbr"]
    print(f"  Teams: {match_info['team_a_full']} ({team_a_abbr}) vs "
          f"{match_info['team_b_full']} ({team_b_abbr})")
    print(f"  Winner: {winner_abbr}")
    print(f"  Date: {match_info['match_date']}")

    print("  Fetching play-by-play...")
    pbp_items = fetch_espn_playbyplay(espn_id)
    print(f"  Total items: {len(pbp_items)}")

    balls = parse_playbyplay(pbp_items)
    special_events = [b for b in balls if b["event"] in ("W", "4", "6")]
    print(f"  Parsed: {len(balls)} deliveries, {len(special_events)} special events (W/4/6)")

    timings = extract_innings_timings(balls)
    for key, val in timings.items():
        print(f"  {key}: {ts_to_ist_full(int(val))}")

    print("[STEP 1] Done.")

    # ── Step 2: Fetch Polymarket data ────────────────────
    print(f"\n[STEP 2] Fetching Polymarket data for {slug}...")

    print("  Fetching Gamma market info...")
    market = fetch_gamma_market(slug)
    tick_size = market["tick_size"]
    token_ids = market["token_ids"]
    outcomes = market["outcomes"]
    print(f"  Question: {market['question']}")
    print(f"  Outcomes: {outcomes}")
    print(f"  Tick size: {tick_size}")
    print(f"  Token IDs: {token_ids}")

    # Map team_a to first outcome
    team_a_outcome = outcomes[0]
    team_b_outcome = outcomes[1] if len(outcomes) > 1 else ""
    # Try to match team abbreviations to outcome names
    for i, name in enumerate(outcomes):
        if _team_matches(team_a_abbr, name):
            team_a_outcome = name
            team_b_outcome = outcomes[1 - i] if len(outcomes) > 1 else ""
            break

    print(f"  {team_a_abbr} -> '{team_a_outcome}', {team_b_abbr} -> '{team_b_outcome}'")

    print("  Fetching Goldsky trades...")
    raw_events = fetch_all_events(token_ids)
    print(f"  Raw events: {len(raw_events)}")

    trades_df = process_events(raw_events, token_ids, outcomes)
    print(f"  Processed trades: {len(trades_df)}")

    print("[STEP 2] Done.")

    # ── Step 3: Detect market reactions ──────────────────
    print(f"\n[STEP 3] Detecting market reactions...")

    timeline = build_price_timeline_from_goldsky(trades_df, team_a_outcome)

    events_with_reactions = detect_all_reactions(
        special_events, timeline,
        team_a_abbr, team_b_abbr, team_a_outcome,
        boundary_jump=args.boundary_edge, wicket_jump=args.wicket_edge,
    )

    matched = sum(1 for e in events_with_reactions if e.get("reaction"))
    print(f"  {len(events_with_reactions)} special events | "
          f"{matched} with market reaction | "
          f"{len(events_with_reactions) - matched} no reaction")

    if matched > 0:
        leads = [e["reaction"]["latency"] for e in events_with_reactions if e.get("reaction")]
        print(f"  Avg lead: {sum(leads)/len(leads):.0f}s | "
              f"Median: {sorted(leads)[len(leads)//2]}s | "
              f"Min: {min(leads)}s | Max: {max(leads)}s")

    print("[STEP 3] Done.")

    # ── Step 4: Taker backtest ───────────────────────────
    print(f"\n[STEP 4] Running taker backtest...")

    taker_config = {
        "max_trade_usdc": args.max_trade_usdc,
        "safe_min": args.safe_min,
        "safe_max": args.safe_max,
        "oracle_lead": args.oracle_lead,
        "boundary_edge": args.boundary_edge,
        "wicket_edge": args.wicket_edge,
    }

    taker_trades, taker_total_pnl = run_taker_backtest(
        events_with_reactions, trades_df,
        team_a_abbr, team_b_abbr,
        team_a_outcome, team_b_outcome,
        tick_size,
        max_trade_usdc=args.max_trade_usdc,
        safe_min=args.safe_min,
        safe_max=args.safe_max,
        oracle_lead=args.oracle_lead,
        boundary_edge=args.boundary_edge,
        wicket_edge=args.wicket_edge,
    )

    filled = sum(1 for t in taker_trades if t["fill_status"] == "FILLED")
    partial = sum(1 for t in taker_trades if t["fill_status"] == "PARTIAL")
    not_filled = sum(1 for t in taker_trades if t["fill_status"] == "NOT_FILLED")
    skipped = sum(1 for t in taker_trades if t["fill_status"] in ("SKIPPED_SAFE_RANGE", "INVALID_REVERT"))
    print(f"  Taker trades: {len(taker_trades)} total | "
          f"{filled} filled | {partial} partial | {not_filled} not filled | {skipped} skipped")
    print(f"  Taker PnL: ${taker_total_pnl:.2f}")

    print("[STEP 4] Done.")

    # ── Step 5: Maker backtest ───────────────────────────
    print(f"\n[STEP 5] Running maker backtest...")

    maker_summary, maker_fills, maker_events = run_maker_backtest(
        events_with_reactions, trades_df,
        team_a_outcome, team_b_outcome,
        team_a_abbr, team_b_abbr,
        tick_size, timings, winner_abbr,
        oracle_lead=args.oracle_lead,
        fill_probability=args.fill_probability,
        split_usdc=args.split_usdc,
    )

    print(f"  Maker fills: {maker_summary['total_fills']} "
          f"({maker_summary['buys']} buys, {maker_summary['sells']} sells)")
    print(f"  Round-trips: {maker_summary['round_trips']}")
    print(f"  Spread captured: ${maker_summary['spread_captured']:.2f}")
    print(f"  Quoting uptime: {maker_summary['quoting_uptime_pct']:.1f}%")
    print(f"  Maker PnL: ${maker_summary['pnl']:.2f} ({maker_summary['pnl_pct']:.2f}%)")

    print("[STEP 5] Done.")

    # ── Step 6: Output ───────────────────────────────────
    print(f"\n[STEP 6] Writing output files...")

    # File 1: scores
    scores_path = base_dir / f"scores_{slug}.xlsx"
    write_scores_xlsx(scores_path, balls, special_events, match_info, timings, slug, espn_id)

    # File 2: match_analytics
    analytics_path = base_dir / f"match_analytics_{slug}.xlsx"
    write_match_analytics_xlsx(analytics_path, trades_df)

    # File 3: analysis
    analysis_path = base_dir / f"analysis_{slug}.xlsx"
    write_analysis_xlsx(
        analysis_path, match_info, timings, slug, espn_id, tick_size,
        events_with_reactions, team_a_abbr, team_b_abbr,
        taker_trades, taker_total_pnl, taker_config,
        maker_summary,
    )

    # File 4: ledger
    update_ledger(str(base_dir), slug, taker_total_pnl, taker_config, maker_summary)

    print("[STEP 6] Done.")

    # ── Final Summary ────────────────────────────────────
    print(f"\n{'=' * 80}")
    print(f"  ANALYSIS COMPLETE: {slug}")
    print(f"{'=' * 80}")
    print(f"  Teams: {team_a_abbr} vs {team_b_abbr} | Winner: {winner_abbr}")
    print(f"  Deliveries: {len(balls)} | Special events: {len(special_events)}")
    print(f"  Market reactions detected: {matched}/{len(special_events)}")
    print(f"  Goldsky trades: {len(trades_df)}")
    print(f"  Taker PnL: ${taker_total_pnl:.2f}")
    print(f"  Maker PnL: ${maker_summary['pnl']:.2f} ({maker_summary['pnl_pct']:.2f}%)")
    print(f"")
    print(f"  Output files:")
    print(f"    {scores_path}")
    print(f"    {analytics_path}")
    print(f"    {analysis_path}")
    print(f"    {Path(base_dir).parent / 'ledger.md'}")
    print(f"{'=' * 80}")


if __name__ == "__main__":
    main()
