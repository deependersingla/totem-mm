#!/usr/bin/env python3
# Run:  venv/bin/python wallet_match_report.py

# ══════════════════════════════════════════════════════════
# CONFIGURATION — Change these for each wallet/match
# ══════════════════════════════════════════════════════════

WALLET = "0x4a3d9401153b513cb6b7391ba74ef5f288704841"
SLUG = "crint-ind-nzl-2026-03-08"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", SLUG)
os.makedirs(DATA_DIR, exist_ok=True)
OUTPUT = os.path.join(DATA_DIR, "report_0xef51eb_ind_nzl.xlsx")

TEAM_A = "India"          # maps to first outcome
TEAM_B = "New Zealand"    # maps to second outcome
WINNER = "India"          # for settlement ($1 for winner, $0 for loser)

# Match timings in IST (24h "HH:MM").
# These define phases: pre → inn1 → break → inn2 → post
MATCH_DATE = "2026-03-08"               # YYYY-MM-DD
INN1_START = "19:00"   # 1st innings starts (e.g. toss at 18:30, play at 19:00)
INN1_END   = "20:43"   # 1st innings ends
INN2_START = "20:57"   # 2nd innings starts
MATCH_END  = "22:48"   # Match over

REDEMPTION_FEE = 0.0   # 0.0 for cricket (no fee). set 0.01 for 1% if needed

SNIPE_WINDOW = 15         # seconds to look ahead for price move
SNIPE_THRESHOLD = 0.002   # 0.2 cents — minimum adverse move to count as snipe
MIN_VALID_PRICE = 0.001   # filter out price=0 garbage from future lookups
SAME_DIR_WINDOW = 5       # ±seconds for same-direction detection
SAME_DIR_MIN_OVERLAPS = 3 # minimum same-dir overlaps to include a wallet

# ══════════════════════════════════════════════════════════
# END OF CONFIGURATION
# ══════════════════════════════════════════════════════════

import json
import os
import sys
import time
from bisect import bisect_left, bisect_right
from collections import deque, Counter
from datetime import datetime, timezone, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import pandas as pd
import requests

# ── Constants ────────────────────────────────────────────────────────────────

GAMMA_API = "https://gamma-api.polymarket.com"
GOLDSKY_ENDPOINT = (
    "https://api.goldsky.com/api/public/"
    "project_cl6mb8i9h0003e201j6li0diw/"
    "subgraphs/orderbook-subgraph/0.0.1/gn"
)
ACTIVITY_SUBGRAPH = (
    "https://api.goldsky.com/api/public/"
    "project_cl6mb8i9h0003e201j6li0diw/"
    "subgraphs/activity-subgraph/0.0.3/gn"
)
DATA_API = "https://data-api.polymarket.com"
CTF_EXCHANGE = "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e"
USDC_DECIMALS = 6
IST = timezone(timedelta(hours=5, minutes=30))


# ── Time Helpers ─────────────────────────────────────────────────────────────

def ist_to_unix(date_str: str, time_str: str) -> int:
    """Convert IST date+time ("2026-03-08", "19:00") to UTC unix timestamp."""
    dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    dt_ist = dt.replace(tzinfo=IST)
    return int(dt_ist.timestamp())


def ts_to_phase(ts: int) -> str:
    """Map unix timestamp to match phase."""
    if ts < _INN1_START_UNIX:
        return "pre"
    elif ts <= _INN1_END_UNIX:
        return "inn1"
    elif ts < _INN2_START_UNIX:
        return "break"
    elif ts <= _MATCH_END_UNIX:
        return "inn2"
    return "post"


def ts_to_ist_str(ts: int) -> str:
    """Unix timestamp → "HH:MM:SS" IST string."""
    return (datetime.fromtimestamp(ts, tz=timezone.utc)
            .astimezone(IST).strftime("%H:%M:%S"))


def _short_addr(addr: str) -> str:
    if addr == CTF_EXCHANGE:
        return "CTF_EXCHANGE"
    if len(addr) > 12:
        return addr[:6] + ".." + addr[-4:]
    return addr


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


# ── Goldsky Subgraph ─────────────────────────────────────────────────────────

def _query_events_cursor(field_name: str, token_ids: list[str],
                         page_size: int = 1000) -> list[dict]:
    """Cursor-based pagination using id_gt — no skip limit."""
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
          }}
        }}"""

        data = None
        actual_size = page_size
        retry_sizes = [500, 200]
        for attempt in range(3):
            actual_size = page_size if attempt == 0 else retry_sizes[min(attempt - 1, len(retry_sizes) - 1)]
            cur_timeout = 30 if attempt == 0 else 60
            if attempt > 0:
                cur_query = query.replace(f"first: {page_size}", f"first: {actual_size}")
                print(f"  Retry {attempt}/2 with page_size={actual_size}, timeout={cur_timeout}s")
            else:
                cur_query = query
            try:
                resp = requests.post(
                    GOLDSKY_ENDPOINT, json={"query": cur_query}, timeout=cur_timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                if "errors" not in data:
                    break
                print(f"  GraphQL error (attempt {attempt+1}): {data['errors']}")
                data = None
            except Exception as e:
                print(f"  Error on page {page} (attempt {attempt+1}): {e}")
                data = None

        if data is None or "errors" in (data or {}):
            print(f"  Giving up on page {page} after retries")
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


def fetch_all_order_filled_events(token_ids: list[str]) -> list[dict]:
    """Fetch every OrderFilled event, deduped by event id."""
    all_events = {}
    for field_name in ["makerAssetId_in", "takerAssetId_in"]:
        batch = _query_events_cursor(field_name, token_ids)
        for ev in batch:
            all_events[ev["id"]] = ev
    print(f"  Deduplicated: {len(all_events)} unique events")
    return list(all_events.values())


# ── Process Events ───────────────────────────────────────────────────────────

def process_events(events: list[dict], token_ids: list[str],
                   outcome_names: list[str]) -> pd.DataFrame:
    """Convert raw subgraph events into a clean trades DataFrame."""
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
        tx_hash = ev["transactionHash"]
        order_hash = ev.get("orderHash", "")

        if maker_asset in token_ids:
            outcome = token_to_outcome.get(maker_asset, maker_asset[:16] + "...")
            token_amount_raw = maker_amount
            usdc_amount_raw = taker_amount
            seller = maker
            buyer = taker
            maker_side = "SELL"
        elif taker_asset in token_ids:
            outcome = token_to_outcome.get(taker_asset, taker_asset[:16] + "...")
            token_amount_raw = taker_amount
            usdc_amount_raw = maker_amount
            seller = taker
            buyer = maker
            maker_side = "BUY"
        else:
            continue

        usdc_value = usdc_amount_raw / (10 ** USDC_DECIMALS)
        token_qty = token_amount_raw / (10 ** USDC_DECIMALS)
        fee_usdc = fee / (10 ** USDC_DECIMALS)
        price = usdc_value / token_qty if token_qty > 0 else 0

        rows.append({
            "timestamp": datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "timestamp_unix": ts,
            "outcome": outcome,
            "maker_side": maker_side,
            "price": round(price, 6),
            "token_amount": round(token_qty, 4),
            "usdc_amount": round(usdc_value, 4),
            "fee_usdc": round(fee_usdc, 4),
            "maker": maker,
            "taker": taker,
            "buyer": buyer,
            "seller": seller,
            "is_exchange_taker": taker == CTF_EXCHANGE,
            "is_exchange_maker": maker == CTF_EXCHANGE,
            "tx_hash": tx_hash,
            "order_hash": order_hash,
            "event_id": ev["id"],
            "maker_asset_id": maker_asset,
            "taker_asset_id": taker_asset,
            "maker_amount_raw": maker_amount,
            "taker_amount_raw": taker_amount,
            "fee_raw": fee,
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("timestamp_unix").reset_index(drop=True)
    return df


# ── Financial Role Classification ────────────────────────────────────────────

def classify_financial_role(df: pd.DataFrame, wallet: str) -> dict[str, str]:
    """Classify each event's financial maker/taker role for a wallet.

    MAKER = resting limit order (added liquidity)
    TAKER = crossing/market order (removed liquidity)
    """
    w_all = df[(df["maker"] == wallet) | (df["taker"] == wallet)]
    tx_has_wallet_as_taker = set(
        w_all[w_all["taker"] == wallet]["tx_hash"].unique()
    )
    w_maker = df[df["maker"] == wallet]
    roles = {}
    for _, row in w_maker.iterrows():
        eid = row["event_id"]
        if row["taker"] != CTF_EXCHANGE:
            roles[eid] = "MAKER"
        elif row["tx_hash"] in tx_has_wallet_as_taker:
            roles[eid] = "TAKER"
        else:
            roles[eid] = "MAKER"
    return roles


# ── FIFO Engine ──────────────────────────────────────────────────────────────

class FIFOQueue:
    """FIFO lot queue for a single outcome token."""

    def __init__(self, outcome: str):
        self.outcome = outcome
        self.lots: deque[dict] = deque()
        self.total_qty = 0.0

    def buy(self, qty: float, price: float):
        self.lots.append({"qty": qty, "cost_per_token": price})
        self.total_qty += qty

    def sell(self, qty: float, sell_price: float) -> tuple[float, float, list[dict]]:
        """Match against oldest lots. Returns (realized_pnl, avg_cost_basis, matched_lots)."""
        remaining = qty
        realized_pnl = 0.0
        total_cost = 0.0
        total_matched = 0.0
        matched = []

        while remaining > 1e-8 and self.lots:
            lot = self.lots[0]
            take = min(remaining, lot["qty"])
            pnl = (sell_price - lot["cost_per_token"]) * take
            realized_pnl += pnl
            total_cost += lot["cost_per_token"] * take
            total_matched += take
            matched.append({
                "lot_cost": round(lot["cost_per_token"], 6),
                "lot_qty_used": round(take, 4),
                "lot_pnl": round(pnl, 4),
            })
            lot["qty"] -= take
            remaining -= take
            self.total_qty -= take
            if lot["qty"] < 1e-8:
                self.lots.popleft()

        avg_cost = total_cost / total_matched if total_matched > 0 else 0.0

        if remaining > 1e-4:
            realized_pnl += sell_price * remaining
            matched.append({
                "lot_cost": 0.0,
                "lot_qty_used": round(remaining, 4),
                "lot_pnl": round(sell_price * remaining, 4),
                "WARNING": f"FIFO_UNDERFLOW: {remaining:.4f} tokens sold without inventory",
            })

        return realized_pnl, avg_cost, matched

    def settle(self, settlement_price: float, fee_rate: float = 0.0) -> list[dict]:
        """Settle remaining inventory."""
        entries = []
        net_price = settlement_price * (1 - fee_rate) if settlement_price > 0 else 0.0
        while self.lots:
            lot = self.lots.popleft()
            qty = lot["qty"]
            if qty < 1e-8:
                continue
            pnl = (net_price - lot["cost_per_token"]) * qty
            entries.append({
                "qty": qty,
                "cost_basis": lot["cost_per_token"],
                "settle_price": net_price,
                "pnl": pnl,
            })
            self.total_qty -= qty
        return entries

    @property
    def remaining_qty(self) -> float:
        return self.total_qty

    @property
    def avg_cost(self) -> float:
        if not self.lots or self.total_qty < 1e-8:
            return 0.0
        total = sum(l["qty"] * l["cost_per_token"] for l in self.lots)
        return total / self.total_qty


# ── Data API (Cross-Check) ───────────────────────────────────────────────────

def fetch_data_api(wallet: str, condition_id: str) -> dict:
    """Fetch activity from Polymarket Data API.

    The API returns at most 1000 recent activities with no real pagination.
    If the target market's activity is buried under newer markets, the
    cross-check will show 0 matches — this is flagged in the Sources sheet.

    Returns dict with counts, profile info, and net shares per outcome.
    """
    all_activities = []

    try:
        resp = requests.get(
            f"{DATA_API}/activity",
            params={"user": wallet, "limit": 1000},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  Data API error: {e}")
        data = []

    all_activities = data if isinstance(data, list) else data.get("data", data.get("history", []))
    if not isinstance(all_activities, list):
        all_activities = []

    print(f"  [data-api] fetched {len(all_activities)} activities")

    # Filter to our condition
    matched = [a for a in all_activities
               if a.get("conditionId") == condition_id
               or a.get("condition_id") == condition_id]

    if not matched and all_activities:
        # Show what conditions we did get, to help debug
        cond_counts = Counter(a.get("conditionId", "?")[:16] for a in all_activities)
        top_cond = cond_counts.most_common(3)
        print(f"  WARNING: 0/{len(all_activities)} activities match target condition")
        print(f"  Top conditions found: {', '.join(f'{c}..({n})' for c,n in top_cond)}")
        print(f"  (Data API only returns ~1000 most recent; older markets may be unreachable)")

    # Parse profile info
    profile_name = ""
    for src in [all_activities, matched]:
        if src:
            first = src[0]
            profile_name = (first.get("name") or first.get("username")
                           or first.get("pseudonym") or "")
            if profile_name:
                break

    # Count by type
    buy_count = sum(1 for a in matched if a.get("type", "").upper() == "BUY"
                    or a.get("side", "").upper() == "BUY")
    sell_count = sum(1 for a in matched if a.get("type", "").upper() == "SELL"
                     or a.get("side", "").upper() == "SELL")
    redeem_count = sum(1 for a in matched if "REDEEM" in a.get("type", "").upper())

    # Net shares per outcome
    net_shares = {}
    for a in matched:
        outcome = a.get("outcome", a.get("title", ""))
        tokens = float(a.get("size", a.get("amount", a.get("shares", 0))))
        action = a.get("type", a.get("side", "")).upper()
        if outcome not in net_shares:
            net_shares[outcome] = 0.0
        if "BUY" in action:
            net_shares[outcome] += tokens
        elif "SELL" in action:
            net_shares[outcome] -= tokens

    return {
        "total": len(matched),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "redeem_count": redeem_count,
        "net_shares": net_shares,
        "profile_name": profile_name,
        "raw_total": len(all_activities),
    }


# ── Activity Subgraph (Starting Position) ────────────────────────────────────

def check_starting_position(wallet: str, condition_id: str) -> dict:
    """Check splits/merges/redemptions via activity subgraph."""
    result = {"splits": 0, "merges": 0, "split_amount": 0.0, "merge_amount": 0.0}

    for entity in ["splits", "merges"]:
        query = f"""{{
          {entity}(
            first: 100,
            where: {{ stakeholder: "{wallet.lower()}" }}
          ) {{
            id
            stakeholder
            amount
            conditionId
            timestamp
          }}
        }}"""

        try:
            resp = requests.post(ACTIVITY_SUBGRAPH, json={"query": query}, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  Activity subgraph error ({entity}): {e}")
            continue

        items = data.get("data", {}).get(entity, [])
        # Filter to our condition
        matched = [i for i in items if i.get("conditionId", "") == condition_id]
        count = len(matched)
        total_amount = sum(int(i.get("amount", 0)) for i in matched) / (10 ** USDC_DECIMALS)

        result[entity] = count
        result[f"{entity[:-1]}_amount"] = total_amount

    return result


# ── Sheet 1: Transactions ────────────────────────────────────────────────────

def build_transactions(w_df: pd.DataFrame, roles: dict[str, str],
                       outcome_map: dict[str, str]) -> pd.DataFrame:
    """Build the Transactions sheet from wallet's maker-only events."""
    rows = []
    for i, (_, r) in enumerate(w_df.iterrows(), 1):
        ts = int(r["timestamp_unix"])
        is_buyer = r["buyer"] == WALLET
        side = "BUY" if is_buyer else "SELL"
        outcome_raw = r["outcome"]
        outcome_label = outcome_map.get(outcome_raw, outcome_raw)

        rows.append({
            "#": i,
            "time_ist": ts_to_ist_str(ts),
            "phase": ts_to_phase(ts),
            "outcome": outcome_label,
            "side": side,
            "order_type": roles.get(r["event_id"], "UNKNOWN"),
            "tokens": round(r["token_amount"], 4),
            "price": round(r["price"], 6),
            "notional": round(r["usdc_amount"], 2),
            "counterparty": r["taker"],
            "tx_hash": r["tx_hash"],
        })

    df = pd.DataFrame(rows)

    # Add summary rows at the bottom
    if not df.empty:
        summary_rows = []
        for team_label in [TEAM_A, TEAM_B]:
            team_df = df[df["outcome"] == team_label]
            bought = team_df[team_df["side"] == "BUY"]
            sold = team_df[team_df["side"] == "SELL"]
            summary_rows.append({
                "#": "", "time_ist": "", "phase": "",
                "outcome": f"bought {team_label}",
                "side": f"{len(bought)} trades",
                "order_type": "",
                "tokens": round(bought["tokens"].sum(), 2),
                "price": "",
                "notional": round(bought["notional"].sum(), 2),
                "counterparty": "", "tx_hash": "",
            })
            summary_rows.append({
                "#": "", "time_ist": "", "phase": "",
                "outcome": f"sold {team_label}",
                "side": f"{len(sold)} trades",
                "order_type": "",
                "tokens": round(sold["tokens"].sum(), 2),
                "price": "",
                "notional": round(sold["notional"].sum(), 2),
                "counterparty": "", "tx_hash": "",
            })

        total_trades = len(df)
        total_vol = df["notional"].sum()
        summary_rows.append({
            "#": "", "time_ist": "", "phase": "",
            "outcome": "TOTAL",
            "side": f"{total_trades} trades",
            "order_type": "",
            "tokens": "",
            "price": "",
            "notional": round(total_vol, 2),
            "counterparty": "", "tx_hash": "",
        })
        summary_df = pd.DataFrame(summary_rows)

        # Blank separator row
        blank = pd.DataFrame([{c: "" for c in df.columns}])
        df = pd.concat([df, blank, summary_df], ignore_index=True)

    return df


# ── Sheet 2: Summary ─────────────────────────────────────────────────────────

def build_summary(w_df: pd.DataFrame, roles: dict[str, str],
                  outcome_map: dict[str, str], settlement: dict[str, float],
                  fifo_df: pd.DataFrame, market: dict,
                  start_pos: dict, profile_name: str) -> pd.DataFrame:
    """Build the Summary sheet with key metrics."""
    wallet = WALLET.lower()
    rows = []

    def add(metric, value):
        rows.append({"metric": metric, "value": value})

    # ── Market info
    add("market", market.get("question", ""))
    add("wallet", WALLET)
    add("profile", profile_name)

    # ── Trade counts
    total_trades = len(w_df)
    maker_count = sum(1 for v in roles.values() if v == "MAKER")
    taker_count = sum(1 for v in roles.values() if v == "TAKER")
    add("total_trades", total_trades)
    add("maker_trades", f"{maker_count} ({maker_count/total_trades*100:.1f}%)")
    add("taker_trades", f"{taker_count} ({taker_count/total_trades*100:.1f}%)")

    # ── Volume & Turnover
    total_bought_usdc = w_df[w_df["buyer"] == wallet]["usdc_amount"].sum()
    total_sold_usdc = w_df[w_df["seller"] == wallet]["usdc_amount"].sum()
    turnover = total_bought_usdc + total_sold_usdc
    add("total_bought_$", round(total_bought_usdc, 2))
    add("total_sold_$", round(total_sold_usdc, 2))
    add("turnover_$", round(turnover, 2))

    # ── Positions
    pos = {}
    for team in [TEAM_A, TEAM_B]:
        team_df = w_df[w_df["outcome"] == team]
        bought = team_df[team_df["buyer"] == wallet]["token_amount"].sum()
        sold = team_df[team_df["seller"] == wallet]["token_amount"].sum()
        pos[team] = bought - sold

    pre_trades = len(w_df[w_df["timestamp_unix"] < _INN1_START_UNIX])
    started_zero = pre_trades == 0 and start_pos.get("splits", 0) == 0
    add("started_at_zero", "YES" if started_zero else f"NO ({pre_trades} pre-match)")
    add(f"end_pos_{TEAM_A}", round(pos[TEAM_A], 2))
    add(f"end_pos_{TEAM_B}", round(pos[TEAM_B], 2))

    # ── Max Exposure (peak net capital deployed)
    cum_bought = 0.0
    cum_sold = 0.0
    max_exposure = 0.0
    for _, r in w_df.iterrows():
        if r["buyer"] == wallet:
            cum_bought += r["usdc_amount"]
        else:
            cum_sold += r["usdc_amount"]
        exposure = cum_bought - cum_sold
        if exposure > max_exposure:
            max_exposure = exposure
    add("max_exposure_$", round(max_exposure, 2))

    # ── Max Risk (peak token inventory — max of either outcome at any point)
    running_pos = {TEAM_A: 0.0, TEAM_B: 0.0}
    max_risk_tokens = 0.0
    max_risk_label = ""
    for _, r in w_df.iterrows():
        outcome = r["outcome"]
        if outcome not in running_pos:
            continue
        if r["buyer"] == wallet:
            running_pos[outcome] += r["token_amount"]
        else:
            running_pos[outcome] -= r["token_amount"]
        for team, qty in running_pos.items():
            if qty > max_risk_tokens:
                max_risk_tokens = qty
                max_risk_label = team
    add("max_risk_tokens", f"{round(max_risk_tokens, 2)} ({max_risk_label})")
    add("max_risk_$_at_par", round(max_risk_tokens, 2))  # at $1 settlement

    # ── Capital Rotation (turnover / max_exposure — how many times capital recycled)
    capital_rotation = turnover / max_exposure if max_exposure > 0 else 0
    add("capital_rotation_x", round(capital_rotation, 2))

    # ── PnL
    fifo_data = fifo_df[fifo_df["side"].isin(["BUY", "SELL", "SETTLEMENT"])]
    sell_pnl = fifo_data[fifo_data["side"] == "SELL"]["realized_pnl"].sum()
    settle_pnl = fifo_data[fifo_data["side"] == "SETTLEMENT"]["realized_pnl"].sum()
    total_pnl = sell_pnl + settle_pnl
    maker_pnl = fifo_data[fifo_data["order_type"] == "MAKER"]["realized_pnl"].sum()
    taker_pnl = fifo_data[fifo_data["order_type"] == "TAKER"]["realized_pnl"].sum()

    add("pnl_from_sells_$", round(sell_pnl, 2))
    add("pnl_from_settlement_$", round(settle_pnl, 2))
    add("total_pnl_$", round(total_pnl, 2))
    add("maker_pnl_$", round(maker_pnl, 2))
    add("taker_pnl_$", round(taker_pnl, 2))

    # ── Efficiency metrics
    pnl_bps = (total_pnl / turnover * 10000) if turnover > 0 else 0
    add("pnl_bps (vs turnover)", round(pnl_bps, 1))

    roi_pct = (total_pnl / max_exposure * 100) if max_exposure > 0 else 0
    add("roi_%  (vs max_exposure)", round(roi_pct, 2))

    avg_pnl_per_trade = total_pnl / total_trades if total_trades > 0 else 0
    avg_notional = turnover / total_trades if total_trades > 0 else 0
    avg_bps_per_trade = (avg_pnl_per_trade / avg_notional * 10000) if avg_notional > 0 else 0
    add("avg_pnl_per_trade_$", round(avg_pnl_per_trade, 4))
    add("avg_profit_bps_per_trade", round(avg_bps_per_trade, 1))

    # ── Win rate (trades where realized PnL > 0)
    sell_trades = fifo_data[fifo_data["side"] == "SELL"]
    winning_sells = len(sell_trades[sell_trades["realized_pnl"] > 0])
    total_sells = len(sell_trades)
    win_rate = (winning_sells / total_sells * 100) if total_sells > 0 else 0
    add("win_rate_%", f"{round(win_rate, 1)}% ({winning_sells}/{total_sells} sells)")

    # ── Profit factor (gross wins / gross losses)
    gross_wins = sell_trades[sell_trades["realized_pnl"] > 0]["realized_pnl"].sum()
    gross_losses = abs(sell_trades[sell_trades["realized_pnl"] < 0]["realized_pnl"].sum())
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else float('inf')
    add("profit_factor", round(profit_factor, 2) if profit_factor != float('inf') else "∞ (no losses)")

    # ── Avg win / avg loss
    avg_win = gross_wins / winning_sells if winning_sells > 0 else 0
    losing_sells = total_sells - winning_sells
    avg_loss = gross_losses / losing_sells if losing_sells > 0 else 0
    add("avg_win_$", round(avg_win, 2))
    add("avg_loss_$", round(avg_loss, 2))
    edge_ratio = avg_win / avg_loss if avg_loss > 0 else float('inf')
    add("edge_ratio (avg_win/avg_loss)", round(edge_ratio, 2) if edge_ratio != float('inf') else "∞")

    # ── PnL per phase
    for phase in ["pre", "inn1", "break", "inn2", "post"]:
        phase_rows = fifo_data[fifo_data["phase"] == phase]
        phase_pnl = phase_rows["realized_pnl"].sum()
        phase_count = len(phase_rows)
        if phase_count > 0:
            add(f"pnl_{phase}", f"${round(phase_pnl, 2)} ({phase_count} trades)")

    # ── Settlement
    redeem_val = (max(pos[TEAM_A], 0) * settlement.get(TEAM_A, 0)
                  + max(pos[TEAM_B], 0) * settlement.get(TEAM_B, 0))
    redeem_val *= (1 - REDEMPTION_FEE)
    add("settlement_redeemed_$", round(redeem_val, 2))

    return pd.DataFrame(rows)


# ── Sheet 3: FIFO PnL ───────────────────────────────────────────────────────

def build_fifo(w_df: pd.DataFrame, roles: dict[str, str],
               outcomes: list[str], settlement: dict[str, float],
               outcome_map: dict[str, str]) -> pd.DataFrame:
    """Build FIFO PnL sheet with per-trade realized PnL."""
    queues = {o: FIFOQueue(o) for o in outcomes}
    rows = []
    cumulative_pnl = 0.0
    warnings = []

    for _, r in w_df.iterrows():
        ts = int(r["timestamp_unix"])
        outcome = r["outcome"]
        queue = queues.get(outcome)
        if queue is None:
            continue

        is_buyer = r["buyer"] == WALLET
        order_type = roles.get(r.get("event_id", ""), "UNKNOWN")
        outcome_label = outcome_map.get(outcome, outcome)

        if is_buyer:
            queue.buy(r["token_amount"], r["price"])
            row = {
                "#": len(rows) + 1,
                "time_ist": ts_to_ist_str(ts),
                "phase": ts_to_phase(ts),
                "outcome": outcome_label,
                "side": "BUY",
                "order_type": order_type,
                "tokens": round(r["token_amount"], 4),
                "price": round(r["price"], 6),
                "notional": round(r["usdc_amount"], 2),
                "cost_basis": round(r["price"], 6),
                "realized_pnl": 0.0,
                "cumulative_pnl": round(cumulative_pnl, 2),
                f"{TEAM_A}_pos": round(queues[TEAM_A].remaining_qty, 2) if TEAM_A in queues else 0,
                f"{TEAM_B}_pos": round(queues[TEAM_B].remaining_qty, 2) if TEAM_B in queues else 0,
            }
            rows.append(row)
        else:
            pnl, avg_cost, matched = queue.sell(r["token_amount"], r["price"])
            cumulative_pnl += pnl
            for m in matched:
                if "WARNING" in m:
                    warnings.append(f"Row {len(rows)+1}: {m['WARNING']}")

            row = {
                "#": len(rows) + 1,
                "time_ist": ts_to_ist_str(ts),
                "phase": ts_to_phase(ts),
                "outcome": outcome_label,
                "side": "SELL",
                "order_type": order_type,
                "tokens": round(r["token_amount"], 4),
                "price": round(r["price"], 6),
                "notional": round(r["usdc_amount"], 2),
                "cost_basis": round(avg_cost, 6),
                "realized_pnl": round(pnl, 2),
                "cumulative_pnl": round(cumulative_pnl, 2),
                f"{TEAM_A}_pos": round(queues[TEAM_A].remaining_qty, 2) if TEAM_A in queues else 0,
                f"{TEAM_B}_pos": round(queues[TEAM_B].remaining_qty, 2) if TEAM_B in queues else 0,
            }
            rows.append(row)

    # Settlement rows
    for outcome in outcomes:
        queue = queues[outcome]
        if queue.remaining_qty < 1e-8:
            continue
        settle_price = settlement.get(outcome, 0.0)
        entries = queue.settle(settle_price, REDEMPTION_FEE)
        outcome_label = outcome_map.get(outcome, outcome)

        for entry in entries:
            cumulative_pnl += entry["pnl"]
            net_settle = entry["settle_price"]
            row = {
                "#": len(rows) + 1,
                "time_ist": "SETTLE",
                "phase": "post",
                "outcome": outcome_label,
                "side": "SETTLEMENT",
                "order_type": "",
                "tokens": round(entry["qty"], 4),
                "price": round(net_settle, 6),
                "notional": round(entry["qty"] * net_settle, 2),
                "cost_basis": round(entry["cost_basis"], 6),
                "realized_pnl": round(entry["pnl"], 2),
                "cumulative_pnl": round(cumulative_pnl, 2),
                f"{TEAM_A}_pos": 0.0,
                f"{TEAM_B}_pos": 0.0,
            }
            rows.append(row)

    ledger = pd.DataFrame(rows)

    # Summary rows at bottom
    if not ledger.empty:
        sell_pnl = ledger[ledger["side"] == "SELL"]["realized_pnl"].sum()
        settle_pnl = ledger[ledger["side"] == "SETTLEMENT"]["realized_pnl"].sum()
        fifo_total = cumulative_pnl

        # Cash method cross-check
        total_bought = w_df[w_df["buyer"] == WALLET]["usdc_amount"].sum()
        total_sold = w_df[w_df["seller"] == WALLET]["usdc_amount"].sum()
        cash_settle = 0.0
        for outcome in outcomes:
            team_df = w_df[w_df["outcome"] == outcome]
            net_tokens = (team_df[team_df["buyer"] == WALLET]["token_amount"].sum()
                         - team_df[team_df["seller"] == WALLET]["token_amount"].sum())
            if net_tokens > 0:
                sp = settlement.get(outcome, 0.0)
                cash_settle += net_tokens * sp * (1 - REDEMPTION_FEE)
        cash_total = total_sold - total_bought + cash_settle

        # Maker/Taker PnL
        maker_pnl = ledger[ledger["order_type"] == "MAKER"]["realized_pnl"].sum()
        taker_pnl = ledger[ledger["order_type"] == "TAKER"]["realized_pnl"].sum()

        blank = {c: "" for c in ledger.columns}
        summary = [
            {**blank, "outcome": "Realized from sells", "realized_pnl": round(sell_pnl, 2)},
            {**blank, "outcome": "Settlement PnL", "realized_pnl": round(settle_pnl, 2)},
            {**blank, "outcome": "Total PnL (FIFO)", "realized_pnl": round(fifo_total, 2)},
            {**blank, "outcome": f"Cross-check (cash method)",
             "realized_pnl": round(cash_total, 2)},
            {**blank, "outcome": "Maker PnL", "realized_pnl": round(maker_pnl, 2)},
            {**blank, "outcome": "Taker PnL", "realized_pnl": round(taker_pnl, 2)},
        ]
        if warnings:
            for w in warnings:
                summary.append({**blank, "outcome": f"WARNING: {w}"})

        blank_row = pd.DataFrame([blank])
        summary_df = pd.DataFrame(summary)
        ledger = pd.concat([ledger, blank_row, summary_df], ignore_index=True)

    return ledger


# ── Sheet 4: Snipers ─────────────────────────────────────────────────────────

def build_snipers(df: pd.DataFrame, w_df: pd.DataFrame,
                  outcome_map: dict[str, str]) -> tuple[pd.DataFrame, set]:
    """Identify wallets that adversely selected this wallet's resting orders.

    Returns (DataFrame for Excel sheet, set of full sniper addresses).
    """
    # Pre-filter valid prices
    valid_df = df[df["price"] >= MIN_VALID_PRICE].copy()

    # Pre-group valid-price events by outcome, sorted by timestamp for bisect
    outcome_groups = {}
    for outcome, grp in valid_df.groupby("outcome"):
        sorted_grp = grp.sort_values("timestamp_unix").reset_index(drop=True)
        outcome_groups[outcome] = sorted_grp

    # Our events where a real counterparty filled our resting order
    our_filled = w_df[w_df["taker"] != CTF_EXCHANGE].copy()

    rows = []
    sniper_counter = Counter()
    sniper_tokens = Counter()
    sniper_adverse = Counter()
    sniper_worst = {}

    for _, r in our_filled.iterrows():
        ts = int(r["timestamp_unix"])
        outcome = r["outcome"]
        fill_price = r["price"]
        is_buyer = r["buyer"] == WALLET
        our_side = "BUY" if is_buyer else "SELL"
        taker_addr = r["taker"]

        # Look ahead in same-outcome valid-price events within SNIPE_WINDOW
        grp = outcome_groups.get(outcome)
        if grp is None or grp.empty:
            continue

        ts_col = grp["timestamp_unix"].values
        # Find events in (ts, ts + SNIPE_WINDOW]
        left = bisect_right(ts_col, ts)
        right = bisect_right(ts_col, ts + SNIPE_WINDOW)

        if left >= right:
            continue  # no future events in window

        # Take the last price in the window
        price_later = float(grp.iloc[right - 1]["price"])
        if price_later < MIN_VALID_PRICE:
            continue

        # Compute adverse move
        if our_side == "BUY":
            adverse = fill_price - price_later  # price dropped, we overpaid
        else:
            adverse = price_later - fill_price  # price rose, we sold too cheap

        if adverse <= SNIPE_THRESHOLD:
            continue

        outcome_label = outcome_map.get(outcome, outcome)
        tokens = r["token_amount"]
        notional = r["usdc_amount"]

        rows.append({
            "#": len(rows) + 1,
            "time_ist": ts_to_ist_str(ts),
            "phase": ts_to_phase(ts),
            "outcome": outcome_label,
            "our_side": our_side,
            "fill_price": round(fill_price, 6),
            "price_15s_later": round(price_later, 6),
            "adverse_move_cents": round(adverse, 4),
            "tokens": round(tokens, 4),
            "notional": round(notional, 2),
            "sniper": taker_addr,
        })

        sniper_counter[taker_addr] += 1
        sniper_tokens[taker_addr] += tokens
        sniper_adverse[taker_addr] += adverse * tokens
        if taker_addr not in sniper_worst or adverse > sniper_worst[taker_addr]:
            sniper_worst[taker_addr] = adverse

    result_df = pd.DataFrame(rows)
    sniper_addrs = set(sniper_counter.keys())

    # Add summary rows at bottom
    if not result_df.empty and sniper_counter:
        blank = {c: "" for c in result_df.columns}
        summary_rows = [blank.copy()]
        summary_rows[0]["#"] = "--- SNIPER SUMMARY ---"

        for addr, count in sniper_counter.most_common():
            summary_rows.append({
                **blank,
                "#": "",
                "sniper": addr,
                "tokens": round(sniper_tokens[addr], 2),
                "adverse_move_cents": round(sniper_worst[addr], 4),
                "notional": round(sniper_adverse[addr], 4),
                "our_side": f"{count}x",
            })

        summary_df = pd.DataFrame(summary_rows)
        result_df = pd.concat([result_df, summary_df], ignore_index=True)

    return result_df, sniper_addrs


# ── Sheet 5: Same-Direction Makers ───────────────────────────────────────────

def build_same_direction_makers(df: pd.DataFrame, w_df: pd.DataFrame,
                                outcome_map: dict[str, str]) -> tuple[pd.DataFrame, set]:
    """Find other makers trading same direction within ±SAME_DIR_WINDOW seconds.

    Direction mapping:
      BUY TEAM_A  = LONG_TEAM_A,   SELL TEAM_A = LONG_TEAM_B
      BUY TEAM_B  = LONG_TEAM_B,   SELL TEAM_B = LONG_TEAM_A

    Returns (DataFrame for Excel sheet, set of full wallet addresses).
    """
    wallet = WALLET.lower()

    def get_direction(side: str, outcome: str) -> str:
        """Map side+outcome to a directional label."""
        if side == "BUY":
            return f"LONG_{outcome_map.get(outcome, outcome)}"
        else:
            # Selling outcome X = going long the other side
            other = TEAM_B if outcome_map.get(outcome, outcome) == TEAM_A else TEAM_A
            return f"LONG_{other}"

    # All other-wallet maker events (exclude our wallet and CTF_EXCHANGE on both sides)
    other_makers = df[
        (df["maker"] != wallet) &
        (df["maker"] != CTF_EXCHANGE) &
        (df["taker"] != CTF_EXCHANGE)
    ].copy()

    if other_makers.empty:
        return pd.DataFrame(), set()

    other_makers = other_makers.sort_values("timestamp_unix").reset_index(drop=True)
    other_ts = other_makers["timestamp_unix"].values

    from bisect import bisect_left, bisect_right

    # Per-wallet tallies
    wallet_same = Counter()
    wallet_opp = Counter()
    wallet_volume = Counter()
    wallet_directions = {}  # addr -> Counter of directions
    wallet_sample_times = {}  # addr -> list of sample IST times

    for _, r in w_df.iterrows():
        ts = int(r["timestamp_unix"])
        is_buyer = r["buyer"] == wallet
        our_side = "BUY" if is_buyer else "SELL"
        our_dir = get_direction(our_side, r["outcome"])

        # Find other makers within ±SAME_DIR_WINDOW
        left = bisect_left(other_ts, ts - SAME_DIR_WINDOW)
        right = bisect_right(other_ts, ts + SAME_DIR_WINDOW)

        for idx in range(left, right):
            om = other_makers.iloc[idx]
            om_addr = om["maker"]
            om_is_buyer = om["buyer"] == om_addr
            om_side = "BUY" if om_is_buyer else "SELL"
            om_dir = get_direction(om_side, om["outcome"])

            if om_dir == our_dir:
                wallet_same[om_addr] += 1
                if om_addr not in wallet_sample_times:
                    wallet_sample_times[om_addr] = []
                if len(wallet_sample_times[om_addr]) < 3:
                    wallet_sample_times[om_addr].append(ts_to_ist_str(ts))
            else:
                wallet_opp[om_addr] += 1

            wallet_volume[om_addr] += om["usdc_amount"]

            if om_addr not in wallet_directions:
                wallet_directions[om_addr] = Counter()
            wallet_directions[om_addr][om_dir] += 1

    # Filter: >= SAME_DIR_MIN_OVERLAPS same-direction overlaps
    qualifying = {addr for addr, cnt in wallet_same.items()
                  if cnt >= SAME_DIR_MIN_OVERLAPS}

    if not qualifying:
        return pd.DataFrame(), set()

    rows = []
    sorted_wallets = sorted(qualifying, key=lambda a: wallet_same[a], reverse=True)

    for i, addr in enumerate(sorted_wallets, 1):
        same = wallet_same[addr]
        opp = wallet_opp.get(addr, 0)
        total = same + opp
        coord_score = (same / total * 100) if total > 0 else 0
        vol = wallet_volume.get(addr, 0)
        dominant = wallet_directions[addr].most_common(1)[0][0] if wallet_directions.get(addr) else ""
        samples = ", ".join(wallet_sample_times.get(addr, []))

        rows.append({
            "#": i,
            "wallet": addr,
            "same_dir_count": same,
            "opp_dir_count": opp,
            "total_overlaps": total,
            "coordination_score_%": round(coord_score, 1),
            "total_volume": round(vol, 2),
            "dominant_direction": dominant,
            "sample_times": samples,
        })

    result_df = pd.DataFrame(rows)
    return result_df, qualifying


# ── Chart 1: Inventory Timeline ──────────────────────────────────────────────

def build_inventory_chart(w_df: pd.DataFrame, roles: dict[str, str],
                          outcomes: list[str], settlement: dict[str, float],
                          outcome_map: dict[str, str]) -> str:
    """Build inventory + realized PnL timeline chart. Returns PNG path."""
    wallet = WALLET.lower()
    fifo = {o: FIFOQueue(o) for o in outcomes}

    times = []
    pos_a_vals = []
    pos_b_vals = []
    pnl_vals = []

    running_pos = {outcomes[0]: 0.0, outcomes[1]: 0.0}
    cumulative_pnl = 0.0

    for _, r in w_df.iterrows():
        ts = int(r["timestamp_unix"])
        outcome = r["outcome"]
        is_buyer = r["buyer"] == wallet
        qty = r["token_amount"]
        price = r["price"]

        if is_buyer:
            running_pos[outcome] += qty
            fifo[outcome].buy(qty, price)
        else:
            running_pos[outcome] -= qty
            rp, _, _ = fifo[outcome].sell(qty, price)
            cumulative_pnl += rp

        dt_ist = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(IST)
        times.append(dt_ist)
        pos_a_vals.append(running_pos[outcomes[0]])
        pos_b_vals.append(running_pos[outcomes[1]])
        pnl_vals.append(cumulative_pnl)

    fig, ax1 = plt.subplots(figsize=(16, 7))

    label_a = outcome_map.get(outcomes[0], outcomes[0])
    label_b = outcome_map.get(outcomes[1], outcomes[1])

    ax1.step(times, pos_a_vals, where="post", color="#2196F3", linewidth=1.2,
             label=f"{label_a} tokens", alpha=0.9)
    ax1.step(times, pos_b_vals, where="post", color="#FF9800", linewidth=1.2,
             label=f"{label_b} tokens", alpha=0.9)
    ax1.set_xlabel("Time (IST)")
    ax1.set_ylabel("Token Holdings")
    ax1.tick_params(axis="y")

    ax2 = ax1.twinx()
    ax2.step(times, pnl_vals, where="post", color="#4CAF50", linewidth=1.5,
             label="Realized PnL ($)", alpha=0.8, linestyle="--")
    ax2.set_ylabel("Realized PnL ($)")
    ax2.tick_params(axis="y")

    # Phase boundaries
    for ts_unix, lbl in [
        (_INN1_START_UNIX, "Inn1 Start"), (_INN1_END_UNIX, "Inn1 End"),
        (_INN2_START_UNIX, "Inn2 Start"), (_MATCH_END_UNIX, "Match End"),
    ]:
        dt = datetime.fromtimestamp(ts_unix, tz=timezone.utc).astimezone(IST)
        ax1.axvline(dt, color="gray", linestyle=":", alpha=0.6)
        ax1.text(dt, ax1.get_ylim()[1] * 0.95, lbl, fontsize=7,
                 rotation=90, va="top", ha="right", color="gray")

    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=IST))
    ax1.tick_params(axis="x", rotation=45)

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left", fontsize=8)

    ax1.set_title(f"Inventory & Realized PnL — {WALLET[:10]}... on {SLUG}")
    ax1.grid(True, alpha=0.3)
    fig.tight_layout()

    png_path = OUTPUT.replace(".xlsx", "_inventory.png")
    fig.savefig(png_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"    Saved: {png_path}")
    return png_path


# ── Chart 2: Price Action + Wallet Trades ─────────────────────────────────────

def build_price_action_chart(df: pd.DataFrame, w_df: pd.DataFrame,
                             outcome_map: dict[str, str]) -> str:
    """Market price over time with wallet trades overlaid. Returns PNG path.

    Zoomed to match window (inn1 start → match end) with 5-minute x-axis ticks.
    """
    wallet = WALLET.lower()
    outcomes = list(outcome_map.keys())

    # Time bounds: from inn1 start to match end (with small padding)
    pad = 300  # 5 min padding on each side
    t_start = _INN1_START_UNIX - pad
    t_end = _MATCH_END_UNIX + pad
    dt_start = datetime.fromtimestamp(t_start, tz=timezone.utc).astimezone(IST)
    dt_end = datetime.fromtimestamp(t_end, tz=timezone.utc).astimezone(IST)

    fig, axes = plt.subplots(2, 1, figsize=(20, 10), sharex=True)

    from matplotlib.lines import Line2D

    for idx, outcome in enumerate(outcomes):
        ax = axes[idx]
        label = outcome_map.get(outcome, outcome)

        # Filter market trades to match window
        market = df[(df["outcome"] == outcome) &
                     (df["timestamp_unix"] >= t_start) &
                     (df["timestamp_unix"] <= t_end)].sort_values("timestamp_unix")
        if market.empty:
            continue

        market_times = [
            datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(IST)
            for ts in market["timestamp_unix"]
        ]
        ax.scatter(market_times, market["price"], s=2, color="#BDBDBD", alpha=0.35,
                   zorder=1)

        # Rolling median price for a cleaner line
        if len(market) > 20:
            rolling_price = market["price"].rolling(20, center=True, min_periods=3).median()
            ax.plot(market_times, rolling_price, color="#424242", linewidth=1.0,
                    alpha=0.8, zorder=2)

        # Wallet trades in match window
        w_outcome = w_df[(w_df["outcome"] == outcome) &
                          (w_df["timestamp_unix"] >= t_start) &
                          (w_df["timestamp_unix"] <= t_end)]
        for _, r in w_outcome.iterrows():
            ts = int(r["timestamp_unix"])
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(IST)
            is_buyer = r["buyer"] == wallet
            color = "#4CAF50" if is_buyer else "#F44336"
            marker = "^" if is_buyer else "v"
            size = max(25, min(r["usdc_amount"] * 0.5, 300))
            ax.scatter(dt, r["price"], s=size, color=color, marker=marker,
                       edgecolors="black", linewidths=0.4, alpha=0.9, zorder=3)

        # Phase boundary vertical lines with labels
        for ts_unix, lbl, clr in [
            (_INN1_START_UNIX, "Inn1 Start", "#1565C0"),
            (_INN1_END_UNIX, "Inn1 End", "#E65100"),
            (_INN2_START_UNIX, "Inn2 Start", "#1565C0"),
            (_MATCH_END_UNIX, "Match End", "#B71C1C"),
        ]:
            dt = datetime.fromtimestamp(ts_unix, tz=timezone.utc).astimezone(IST)
            ax.axvline(dt, color=clr, linestyle="--", linewidth=1.2, alpha=0.7)
            ax.text(dt, ax.get_ylim()[1] if ax.get_ylim()[1] > 0 else 1.0,
                    f" {lbl}", fontsize=7, color=clr, va="top", ha="left",
                    fontweight="bold")

        ax.set_xlim(dt_start, dt_end)
        ax.set_ylabel(f"{label} Price")
        ax.set_ylim(-0.02, 1.02)
        ax.grid(True, alpha=0.3)
        ax.set_title(f"{label} — Market Price & Wallet Trades", fontsize=11)

        legend_elements = [
            Line2D([0], [0], marker="^", color="w", markerfacecolor="#4CAF50",
                   markersize=10, label="Wallet BUY"),
            Line2D([0], [0], marker="v", color="w", markerfacecolor="#F44336",
                   markersize=10, label="Wallet SELL"),
            Line2D([0], [0], marker="o", color="w", markerfacecolor="#BDBDBD",
                   markersize=6, label="Market trades"),
            Line2D([0], [0], color="#424242", linewidth=1, label="Price median"),
        ]
        ax.legend(handles=legend_elements, loc="upper left", fontsize=8)

    # 5-minute tick marks
    axes[-1].xaxis.set_major_locator(mdates.MinuteLocator(interval=5))
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=IST))
    axes[-1].tick_params(axis="x", rotation=45)
    axes[-1].set_xlabel("Time (IST)")

    fig.suptitle(f"Price Action — {SLUG} (Match Window)", fontsize=13, y=1.01)
    fig.tight_layout()

    png_path = OUTPUT.replace(".xlsx", "_price_action.png")
    fig.savefig(png_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"    Saved: {png_path}")
    return png_path


# ── Sheet: Adverse Selection ─────────────────────────────────────────────────

def build_adverse_selection(df: pd.DataFrame, w_df: pd.DataFrame,
                            roles: dict[str, str],
                            outcome_map: dict[str, str]) -> pd.DataFrame:
    """Analyze adverse selection on MAKER fills and spread capture.

    For each resting order that got filled, measure price movement at 5s/15s/30s.
    Adverse = price moved against us. Favorable = price held or moved in our favor.
    """
    wallet = WALLET.lower()

    # Valid price events for price lookups
    valid_df = df[df["price"] >= MIN_VALID_PRICE].copy()
    outcome_groups = {}
    for outcome, grp in valid_df.groupby("outcome"):
        sorted_grp = grp.sort_values("timestamp_unix").reset_index(drop=True)
        outcome_groups[outcome] = sorted_grp

    # Only MAKER fills (resting orders that got hit by a real counterparty)
    maker_fills = w_df[
        (w_df["event_id"].map(roles) == "MAKER") &
        (w_df["taker"] != CTF_EXCHANGE)
    ].copy()

    rows = []
    cp_adverse_total = Counter()  # counterparty -> total adverse cost in USDC
    cp_fill_count = Counter()
    cp_total_notional = Counter()

    for _, r in maker_fills.iterrows():
        ts = int(r["timestamp_unix"])
        outcome = r["outcome"]
        fill_price = r["price"]
        is_buyer = r["buyer"] == wallet
        our_side = "BUY" if is_buyer else "SELL"
        taker_addr = r["taker"]
        tokens = r["token_amount"]
        notional = r["usdc_amount"]
        outcome_label = outcome_map.get(outcome, outcome)

        grp = outcome_groups.get(outcome)
        if grp is None or grp.empty:
            continue
        ts_col = grp["timestamp_unix"].values

        # Get prices at 5s, 15s, 30s after fill
        prices_later = {}
        for window in [5, 15, 30]:
            right_idx = bisect_right(ts_col, ts + window)
            if right_idx > 0:
                prices_later[window] = float(grp.iloc[right_idx - 1]["price"])
            else:
                prices_later[window] = None

        # Adverse move at each window
        moves = {}
        for window, price_later in prices_later.items():
            if price_later is None:
                moves[window] = None
                continue
            if our_side == "BUY":
                moves[window] = fill_price - price_later  # overpaid if positive
            else:
                moves[window] = price_later - fill_price  # undersold if positive

        # Use 15s as primary classification
        move_15 = moves.get(15)
        if move_15 is not None and move_15 > 0:
            classification = "ADVERSE"
            cost_usdc = move_15 * tokens
        elif move_15 is not None:
            classification = "FAVORABLE"
            cost_usdc = move_15 * tokens  # negative = we gained
        else:
            classification = "NO_DATA"
            cost_usdc = 0.0

        rows.append({
            "#": len(rows) + 1,
            "time_ist": ts_to_ist_str(ts),
            "phase": ts_to_phase(ts),
            "outcome": outcome_label,
            "our_side": our_side,
            "fill_price": round(fill_price, 6),
            "price_5s": round(prices_later[5], 6) if prices_later[5] else "",
            "price_15s": round(prices_later[15], 6) if prices_later[15] else "",
            "price_30s": round(prices_later[30], 6) if prices_later[30] else "",
            "move_5s": round(moves[5], 4) if moves[5] is not None else "",
            "move_15s": round(moves[15], 4) if moves[15] is not None else "",
            "move_30s": round(moves[30], 4) if moves[30] is not None else "",
            "classification": classification,
            "cost_usdc": round(cost_usdc, 4),
            "tokens": round(tokens, 4),
            "notional": round(notional, 2),
            "counterparty": taker_addr,
        })

        if move_15 is not None and move_15 > 0:
            cp_adverse_total[taker_addr] += cost_usdc
        cp_fill_count[taker_addr] += 1
        cp_total_notional[taker_addr] += notional

    result_df = pd.DataFrame(rows)

    # Summary
    if not result_df.empty:
        adverse_rows = result_df[result_df["classification"] == "ADVERSE"]
        favorable_rows = result_df[result_df["classification"] == "FAVORABLE"]

        total_adverse_cost = adverse_rows["cost_usdc"].sum() if not adverse_rows.empty else 0
        total_favorable_gain = abs(favorable_rows["cost_usdc"].sum()) if not favorable_rows.empty else 0

        blank = {c: "" for c in result_df.columns}
        summary = [
            blank.copy(),
            {**blank, "#": "--- SUMMARY ---"},
            {**blank, "outcome": "Total MAKER fills", "tokens": len(result_df)},
            {**blank, "outcome": "Adverse fills", "tokens": len(adverse_rows),
             "cost_usdc": round(total_adverse_cost, 2)},
            {**blank, "outcome": "Favorable fills", "tokens": len(favorable_rows),
             "cost_usdc": round(-total_favorable_gain, 2)},
            {**blank, "outcome": "Net adverse selection cost",
             "cost_usdc": round(total_adverse_cost - total_favorable_gain, 2)},
            blank.copy(),
            {**blank, "#": "--- TOP ADVERSE COUNTERPARTIES ---"},
        ]

        for addr, cost in sorted(cp_adverse_total.items(), key=lambda x: -x[1])[:15]:
            summary.append({
                **blank,
                "counterparty": addr,
                "cost_usdc": round(cost, 2),
                "tokens": cp_fill_count[addr],
                "notional": round(cp_total_notional[addr], 2),
            })

        summary_df = pd.DataFrame(summary)
        result_df = pd.concat([result_df, summary_df], ignore_index=True)

    return result_df


# ── Sheet: Smart Entries (Taker Trades) ──────────────────────────────────────

def build_smart_entries(df: pd.DataFrame, w_df: pd.DataFrame,
                        roles: dict[str, str],
                        outcome_map: dict[str, str]) -> pd.DataFrame:
    """Analyze TAKER trades — did the wallet time its aggressive entries well?

    For each taker fill, measure price movement at 5s, 15s, 30s, 60s.
    "Smart" = price moved in our favor after we crossed the spread.
    """
    wallet = WALLET.lower()

    valid_df = df[df["price"] >= MIN_VALID_PRICE].copy()
    outcome_groups = {}
    for outcome, grp in valid_df.groupby("outcome"):
        sorted_grp = grp.sort_values("timestamp_unix").reset_index(drop=True)
        outcome_groups[outcome] = sorted_grp

    # Only TAKER fills
    taker_fills = w_df[w_df["event_id"].map(roles) == "TAKER"].copy()

    rows = []
    smart_count = 0
    total_edge = 0.0

    for _, r in taker_fills.iterrows():
        ts = int(r["timestamp_unix"])
        outcome = r["outcome"]
        fill_price = r["price"]
        is_buyer = r["buyer"] == wallet
        our_side = "BUY" if is_buyer else "SELL"
        tokens = r["token_amount"]
        notional = r["usdc_amount"]
        outcome_label = outcome_map.get(outcome, outcome)

        grp = outcome_groups.get(outcome)
        if grp is None or grp.empty:
            continue
        ts_col = grp["timestamp_unix"].values

        prices_later = {}
        for window in [5, 15, 30, 60]:
            right_idx = bisect_right(ts_col, ts + window)
            if right_idx > 0:
                prices_later[window] = float(grp.iloc[right_idx - 1]["price"])
            else:
                prices_later[window] = None

        # Edge = favorable price movement
        edges = {}
        for window, price_later in prices_later.items():
            if price_later is None:
                edges[window] = None
                continue
            if our_side == "BUY":
                edges[window] = price_later - fill_price  # price went up = good
            else:
                edges[window] = fill_price - price_later  # price went down = good

        edge_15 = edges.get(15)
        if edge_15 is not None and edge_15 > 0:
            classification = "SMART"
            smart_count += 1
            total_edge += edge_15 * tokens
        elif edge_15 is not None:
            classification = "WRONG"
        else:
            classification = "NO_DATA"

        rows.append({
            "#": len(rows) + 1,
            "time_ist": ts_to_ist_str(ts),
            "phase": ts_to_phase(ts),
            "outcome": outcome_label,
            "our_side": our_side,
            "fill_price": round(fill_price, 6),
            "price_5s": round(prices_later[5], 6) if prices_later.get(5) else "",
            "price_15s": round(prices_later[15], 6) if prices_later.get(15) else "",
            "price_30s": round(prices_later[30], 6) if prices_later.get(30) else "",
            "price_60s": round(prices_later[60], 6) if prices_later.get(60) else "",
            "edge_5s": round(edges[5], 4) if edges.get(5) is not None else "",
            "edge_15s": round(edges[15], 4) if edges.get(15) is not None else "",
            "edge_30s": round(edges[30], 4) if edges.get(30) is not None else "",
            "edge_60s": round(edges[60], 4) if edges.get(60) is not None else "",
            "classification": classification,
            "edge_usdc": round((edge_15 or 0) * tokens, 4),
            "tokens": round(tokens, 4),
            "notional": round(notional, 2),
        })

    result_df = pd.DataFrame(rows)

    if not result_df.empty:
        total_taker = len(result_df)
        hit_rate = smart_count / total_taker * 100 if total_taker > 0 else 0
        avg_edge = total_edge / smart_count if smart_count > 0 else 0

        wrong_rows = result_df[result_df["classification"] == "WRONG"]
        total_wrong_cost = abs(wrong_rows["edge_usdc"].sum()) if not wrong_rows.empty else 0

        blank = {c: "" for c in result_df.columns}
        summary = [
            blank.copy(),
            {**blank, "#": "--- SUMMARY ---"},
            {**blank, "outcome": "Total TAKER trades", "tokens": total_taker},
            {**blank, "outcome": "Smart entries (price moved in favor)",
             "tokens": smart_count, "edge_usdc": round(total_edge, 2)},
            {**blank, "outcome": "Wrong entries (price moved against)",
             "tokens": len(wrong_rows), "edge_usdc": round(-total_wrong_cost, 2)},
            {**blank, "outcome": f"Hit rate", "edge_usdc": f"{hit_rate:.1f}%"},
            {**blank, "outcome": f"Net taker edge",
             "edge_usdc": round(total_edge - total_wrong_cost, 2)},
        ]

        summary_df = pd.DataFrame(summary)
        result_df = pd.concat([result_df, summary_df], ignore_index=True)

    return result_df



# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    global _INN1_START_UNIX, _INN1_END_UNIX, _INN2_START_UNIX, _MATCH_END_UNIX

    # 1. Convert IST config → UTC unix timestamps
    _INN1_START_UNIX = ist_to_unix(MATCH_DATE, INN1_START)
    _INN1_END_UNIX = ist_to_unix(MATCH_DATE, INN1_END)
    _INN2_START_UNIX = ist_to_unix(MATCH_DATE, INN2_START)
    _MATCH_END_UNIX = ist_to_unix(MATCH_DATE, MATCH_END)

    wallet = WALLET.lower()

    print("Wallet Match Report")
    print("=" * 40)

    # 2. Fetch market metadata
    print(f"\n1. Fetching market info for '{SLUG}'...")
    market = fetch_market(SLUG)
    question = market.get("question", SLUG)
    condition_id = market.get("conditionId", "")
    token_ids, outcome_names = parse_market_tokens(market)
    print(f"   Market: {question}")
    print(f"   Condition: {condition_id}")
    print(f"   Outcomes: {', '.join(outcome_names)}")

    if not token_ids:
        print("Error: No token IDs found")
        sys.exit(1)

    # 3. Map outcome names to TEAM_A/TEAM_B
    # outcome_map: raw outcome name → team label
    outcome_map = {}
    for name in outcome_names:
        if name == TEAM_A:
            outcome_map[name] = TEAM_A
        elif name == TEAM_B:
            outcome_map[name] = TEAM_B
        else:
            outcome_map[name] = name

    # Settlement prices
    settlement = {}
    for name in outcome_names:
        settlement[name] = 1.0 if name == WINNER else 0.0
    print(f"   Settlement: {', '.join(f'{n}=${p:.2f}' for n, p in settlement.items())}")

    # 4. Fetch ALL OrderFilled events
    print(f"\n2. Fetching ALL OrderFilled events from Goldsky subgraph...")
    events = fetch_all_order_filled_events(token_ids)
    print(f"   Total unique events: {len(events)}")

    if not events:
        print("No events found.")
        sys.exit(0)

    # 5. Process into trades DataFrame
    print("\n3. Processing events...")
    df = process_events(events, token_ids, outcome_names)
    print(f"   Processed trades: {len(df)}")

    # 6. Filter wallet's maker-only events
    # NOTE: maker-only is CORRECT. In matchOrders, wallet appears as "maker"
    # even when it's the aggressor (taker=CTF_EXCHANGE). The taker-column
    # events are duplicates of the same economic trade. See Polymarket
    # CTF Exchange Trading.sol — matchOrders emits 2 events per trade.
    w_df = df[df["maker"] == wallet].copy()
    w_df = w_df.sort_values("timestamp_unix").reset_index(drop=True)
    print(f"   Wallet maker events: {len(w_df)}")

    if w_df.empty:
        print(f"No maker events found for wallet {wallet}")
        sys.exit(0)

    # 7. Classify MAKER/TAKER per event
    print("\n4. Classifying maker/taker roles...")
    roles = classify_financial_role(df, wallet)
    maker_count = sum(1 for v in roles.values() if v == "MAKER")
    taker_count = sum(1 for v in roles.values() if v == "TAKER")
    print(f"   MAKER: {maker_count} | TAKER: {taker_count}")

    # 8. Fetch profile name from Data API (lightweight — just first page)
    print("\n5. Fetching wallet profile...")
    profile_name = ""
    try:
        resp = requests.get(
            f"{DATA_API}/activity",
            params={"user": wallet, "limit": 1},
            timeout=10,
        )
        resp.raise_for_status()
        pdata = resp.json()
        if isinstance(pdata, list) and pdata:
            profile_name = (pdata[0].get("name") or pdata[0].get("username")
                           or pdata[0].get("pseudonym") or "")
    except Exception:
        pass
    print(f"   Profile: {profile_name or '(unknown)'}")

    # 9. Check starting position
    print("\n6. Checking starting position (splits/merges)...")
    start_pos = check_starting_position(wallet, condition_id)
    print(f"   Splits: {start_pos['splits']}, Merges: {start_pos['merges']}")

    # 10. Build Sheet 1: Transactions
    print("\n7. Building Transactions sheet...")
    tx_df = build_transactions(w_df, roles, outcome_map)

    # 11. Build Sheet 3: FIFO PnL
    print("8. Building FIFO PnL sheet...")
    fifo_df = build_fifo(w_df, roles, outcome_names, settlement, outcome_map)

    # 12. Build Sheet 2: Summary
    print("9. Building Summary sheet...")
    summary_df = build_summary(w_df, roles, outcome_map, settlement,
                               fifo_df, market, start_pos, profile_name)

    # 13. Build Charts
    print("10. Building Inventory Timeline chart...")
    inv_png = build_inventory_chart(w_df, roles, outcome_names, settlement, outcome_map)

    print("11. Building Price Action chart...")
    pa_png = build_price_action_chart(df, w_df, outcome_map)

    # 14. Build Sheet 4: Adverse Selection (replaces Snipers)
    print("12. Building Adverse Selection sheet...")
    adverse_df = build_adverse_selection(df, w_df, roles, outcome_map)
    adv_data = adverse_df[adverse_df["#"].apply(lambda x: isinstance(x, int))] if not adverse_df.empty else adverse_df
    print(f"    Adverse selection fills: {len(adv_data)}")

    # 15. Build Sheet 5: Smart Entries
    print("13. Building Smart Entries sheet...")
    smart_df = build_smart_entries(df, w_df, roles, outcome_map)
    smart_data = smart_df[smart_df["#"].apply(lambda x: isinstance(x, int))] if not smart_df.empty else smart_df
    print(f"    Taker trades analyzed: {len(smart_data)}")

    # 16. Build Sheet 6: Same-Direction Makers
    print("14. Building Same-Direction Makers sheet...")
    samedir_df, samedir_addrs = build_same_direction_makers(df, w_df, outcome_map)
    print(f"    Qualifying wallets: {len(samedir_addrs)}")

    # 17. Write Excel
    print(f"\n15. Writing Excel → {OUTPUT}")
    from openpyxl.drawing.image import Image as XlImage

    with pd.ExcelWriter(OUTPUT, engine="openpyxl") as writer:
        tx_df.to_excel(writer, sheet_name="Transactions", index=False)
        summary_df.to_excel(writer, sheet_name="Summary", index=False)
        fifo_df.to_excel(writer, sheet_name="FIFO PnL", index=False)

        if not adverse_df.empty:
            adverse_df.to_excel(writer, sheet_name="Adverse Selection", index=False)
        if not smart_df.empty:
            smart_df.to_excel(writer, sheet_name="Smart Entries", index=False)
        if not samedir_df.empty:
            samedir_df.to_excel(writer, sheet_name="Same-Dir Makers", index=False)

        # Embed charts
        for png_path, sheet_name in [
            (inv_png, "Inventory Chart"),
            (pa_png, "Price Action Chart"),
        ]:
            if os.path.exists(png_path):
                ws = writer.book.create_sheet(sheet_name)
                img = XlImage(png_path)
                ws.add_image(img, "A1")

        # Auto-size columns
        ws = writer.sheets["Summary"]
        ws.column_dimensions["A"].width = 30
        ws.column_dimensions["B"].width = 50

    # 14. Console summary — pull values from summary_df
    total_trades = len(w_df)
    total_bought_usdc = w_df[w_df["buyer"] == wallet]["usdc_amount"].sum()
    total_sold_usdc = w_df[w_df["seller"] == wallet]["usdc_amount"].sum()
    turnover = total_bought_usdc + total_sold_usdc

    # Positions
    pos_a = (w_df[(w_df["outcome"] == TEAM_A) & (w_df["buyer"] == wallet)]["token_amount"].sum()
             - w_df[(w_df["outcome"] == TEAM_A) & (w_df["seller"] == wallet)]["token_amount"].sum())
    pos_b = (w_df[(w_df["outcome"] == TEAM_B) & (w_df["buyer"] == wallet)]["token_amount"].sum()
             - w_df[(w_df["outcome"] == TEAM_B) & (w_df["seller"] == wallet)]["token_amount"].sum())

    # PnL
    fifo_data = fifo_df[fifo_df["side"].isin(["BUY", "SELL", "SETTLEMENT"])]
    sell_pnl = fifo_data[fifo_data["side"] == "SELL"]["realized_pnl"].sum()
    settle_pnl = fifo_data[fifo_data["side"] == "SETTLEMENT"]["realized_pnl"].sum()
    total_pnl = sell_pnl + settle_pnl
    maker_pnl = fifo_data[fifo_data["order_type"] == "MAKER"]["realized_pnl"].sum()
    taker_pnl = fifo_data[fifo_data["order_type"] == "TAKER"]["realized_pnl"].sum()

    # Read metrics from summary_df for console
    sm = {}
    for _, row in summary_df.iterrows():
        sm[row["metric"]] = row["value"]

    max_exposure = float(str(sm.get("max_exposure_$", 0)))
    pnl_bps = float(str(sm.get("pnl_bps (vs turnover)", 0)))
    roi_pct = float(str(sm.get("roi_%  (vs max_exposure)", 0)))
    avg_bps = float(str(sm.get("avg_profit_bps_per_trade", 0)))
    avg_pnl_per_trade = float(str(sm.get("avg_pnl_per_trade_$", 0)))
    capital_rotation = float(str(sm.get("capital_rotation_x", 0)))

    pre_trades = len(w_df[w_df["timestamp_unix"] < _INN1_START_UNIX])
    started_zero = pre_trades == 0 and start_pos.get("splits", 0) == 0
    redeem_val = (max(pos_a, 0) * settlement.get(TEAM_A, 0)
                  + max(pos_b, 0) * settlement.get(TEAM_B, 0)) * (1 - REDEMPTION_FEE)

    short_wallet = wallet[:8] + "..." + wallet[-4:]

    print(f"\nWallet Match Report")
    print("=" * 40)
    print(f"Market: {question}")
    print(f'Wallet: {short_wallet} ("{profile_name}")')
    print(f"\nTrades: {total_trades} | Turnover: ${turnover:,.0f}")
    print(f"  MAKER: {maker_count} ({maker_count/total_trades*100:.1f}%) | "
          f"TAKER: {taker_count} ({taker_count/total_trades*100:.1f}%)")
    print(f"\nPositions:")
    print(f"  Start: {'0 (verified)' if started_zero else f'{pre_trades} pre-match trades'}")
    print(f"  End:   {pos_a:,.0f} {TEAM_A}, {pos_b:,.0f} {TEAM_B}"
          f" → redeemed ${redeem_val:,.0f}")
    print(f"  Max exposure: ${max_exposure:,.0f}  |  Max risk: {sm.get('max_risk_tokens', '')}")
    print(f"  Capital rotation: {capital_rotation:.2f}x")
    print(f"\nPnL: ${total_pnl:,.2f}")
    print(f"  Sells: ${sell_pnl:,.0f}  |  Settlement: ${settle_pnl:,.0f}")
    print(f"  Maker: ${maker_pnl:,.0f}  |  Taker: ${taker_pnl:,.0f}")
    print(f"  PnL/turnover: {pnl_bps:.1f} bps  |  ROI: {roi_pct:.2f}%")
    print(f"  Avg profit/trade: {avg_bps:.1f} bps (${avg_pnl_per_trade:.4f})")
    print(f"  Win rate: {sm.get('win_rate_%', '')}  |  Profit factor: {sm.get('profit_factor', '')}")

    # Adverse Selection summary
    if not adverse_df.empty and not adv_data.empty:
        adverse_only = adv_data[adv_data.get("classification", "") == "ADVERSE"] if "classification" in adv_data.columns else pd.DataFrame()
        favorable_only = adv_data[adv_data.get("classification", "") == "FAVORABLE"] if "classification" in adv_data.columns else pd.DataFrame()
        adv_cost = adverse_only["cost_usdc"].sum() if not adverse_only.empty else 0
        fav_gain = abs(favorable_only["cost_usdc"].sum()) if not favorable_only.empty else 0
        print(f"\nAdverse Selection (MAKER fills):")
        print(f"  {len(adverse_only)} adverse | {len(favorable_only)} favorable")
        print(f"  Net cost: ${adv_cost - fav_gain:,.2f}")

    # Smart Entries summary
    if not smart_df.empty and not smart_data.empty:
        smart_only = smart_data[smart_data.get("classification", "") == "SMART"] if "classification" in smart_data.columns else pd.DataFrame()
        smart_pct = len(smart_only) / len(smart_data) * 100 if len(smart_data) > 0 else 0
        print(f"\nSmart Entries (TAKER fills):")
        print(f"  {len(smart_only)}/{len(smart_data)} smart ({smart_pct:.0f}% hit rate)")

    # Same-Dir Makers summary
    if samedir_addrs:
        print(f"\nSame-Dir Makers: {len(samedir_addrs)} wallets with ≥{SAME_DIR_MIN_OVERLAPS} overlaps")
    else:
        print(f"\nSame-Dir Makers: 0 wallets with ≥{SAME_DIR_MIN_OVERLAPS} overlaps")

    print(f"\nSaved → {OUTPUT}")


if __name__ == "__main__":
    main()
