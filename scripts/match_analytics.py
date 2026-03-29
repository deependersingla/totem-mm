#!/usr/bin/env python3
"""
Match-Level Analytics for Polymarket Prediction Markets.

Fetches ALL trades for a market from Goldsky subgraph, computes per-wallet
FIFO PnL, maker/taker attribution, overlap detection, sniper identification,
and exports a multi-sheet Excel workbook.

Usage:  python match_analytics.py
"""

import os

# ══════════════════════════════════════════════════════════
# CONFIGURATION — Change these for each match
# ══════════════════════════════════════════════════════════

SLUG = "crint-nzl-zaf-2026-03-25"
DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", SLUG)
os.makedirs(DATA_DIR, exist_ok=True)
OUTPUT = os.path.join(DATA_DIR, f"match_analytics_{SLUG}.xlsx")

TEAM_A = "New Zealand"
TEAM_B = "South Africa"
WINNER = "South Africa"

MATCH_DATE = "2026-03-25"
INN1_START = "11:45"
INN1_END   = "13:22"
INN2_START = "13:32"
MATCH_END  = "15:04"

REDEMPTION_FEE = 0.0

SNIPE_WINDOW = 15
SNIPE_THRESHOLD = 0.002
MIN_VALID_PRICE = 0.001
SAME_DIR_WINDOW = 5
SAME_DIR_MIN_OVERLAPS = 3

# ══════════════════════════════════════════════════════════
# END OF CONFIGURATION
# ══════════════════════════════════════════════════════════

import json
import os
import sys
import time
from bisect import bisect_left, bisect_right
from collections import deque, defaultdict, Counter
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import requests

# ── Constants ────────────────────────────────────────────

GAMMA_API = "https://gamma-api.polymarket.com"
GOLDSKY_ENDPOINT = (
    "https://api.goldsky.com/api/public/"
    "project_cl6mb8i9h0003e201j6li0diw/"
    "subgraphs/orderbook-subgraph/0.0.1/gn"
)
DATA_API = "https://data-api.polymarket.com"
CTF_EXCHANGE = "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e"
USDC_DECIMALS = 6
IST = timezone(timedelta(hours=5, minutes=30))

_INN1_START_UNIX = 0
_INN1_END_UNIX = 0
_INN2_START_UNIX = 0
_MATCH_END_UNIX = 0


# ── Time Helpers ─────────────────────────────────────────

def ist_to_unix(date_str: str, time_str: str) -> int:
    dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    return int(dt.replace(tzinfo=IST).timestamp())


def ts_to_phase(ts: int) -> str:
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
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(IST).strftime("%H:%M:%S")


def ts_to_ist_full(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(IST).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def short_addr(addr: str) -> str:
    if addr == CTF_EXCHANGE:
        return "CTF_EXCHANGE"
    return addr[:6] + ".." + addr[-4:] if len(addr) > 12 else addr


# ── Gamma API ────────────────────────────────────────────

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


# ── Goldsky Subgraph ─────────────────────────────────────

def _query_events_cursor(field_name: str, token_ids: list[str],
                         page_size: int = 1000) -> list[dict]:
    events: list[dict] = []
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


def fetch_all_events(token_ids: list[str]) -> list[dict]:
    all_events: dict[str, dict] = {}
    for field in ["makerAssetId_in", "takerAssetId_in"]:
        for ev in _query_events_cursor(field, token_ids):
            all_events[ev["id"]] = ev
    print(f"  Deduplicated: {len(all_events)} unique events")
    return list(all_events.values())


# ── Event Processing ─────────────────────────────────────

def process_events(events: list[dict], token_ids: list[str],
                   outcome_names: list[str]) -> pd.DataFrame:
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
            "phase": ts_to_phase(ts),
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


# ── Financial Role Classification (batch) ────────────────

def classify_all_roles(df: pd.DataFrame) -> dict[str, str]:
    """Classify MAKER/TAKER for every event's maker column wallet.

    MAKER = resting limit order (added liquidity)
    TAKER = crossing order via matchOrders (removed liquidity)

    Logic per event where maker != CTF_EXCHANGE:
      - taker != CTF_EXCHANGE → direct fill → MAKER
      - taker == CTF_EXCHANGE AND maker appears as taker in same tx → TAKER
      - taker == CTF_EXCHANGE AND maker NOT in taker column of same tx → MAKER
    """
    wallet_taker_txs: dict[str, set[str]] = defaultdict(set)
    for _, row in df.iterrows():
        t = row["taker"]
        if t != CTF_EXCHANGE:
            wallet_taker_txs[t].add(row["tx_hash"])

    roles: dict[str, str] = {}
    for _, row in df.iterrows():
        m = row["maker"]
        if m == CTF_EXCHANGE:
            continue
        eid = row["event_id"]
        if row["taker"] != CTF_EXCHANGE:
            roles[eid] = "MAKER"
        elif row["tx_hash"] in wallet_taker_txs.get(m, set()):
            roles[eid] = "TAKER"
        else:
            roles[eid] = "MAKER"
    return roles


# ── FIFO Engine ──────────────────────────────────────────

class FIFOQueue:
    __slots__ = ("lots", "total_qty")

    def __init__(self):
        self.lots: deque[list] = deque()  # [qty, price]
        self.total_qty = 0.0

    def buy(self, qty: float, price: float):
        self.lots.append([qty, price])
        self.total_qty += qty

    def sell(self, qty: float, sell_price: float) -> float:
        remaining = qty
        realized = 0.0
        while remaining > 1e-8 and self.lots:
            lot = self.lots[0]
            take = min(remaining, lot[0])
            realized += (sell_price - lot[1]) * take
            lot[0] -= take
            remaining -= take
            self.total_qty -= take
            if lot[0] < 1e-8:
                self.lots.popleft()
        if remaining > 1e-4:
            realized += sell_price * remaining
        return realized

    def settle(self, settle_price: float, fee_rate: float = 0.0) -> float:
        net_price = settle_price * (1 - fee_rate) if settle_price > 0 else 0.0
        pnl = 0.0
        while self.lots:
            lot = self.lots.popleft()
            if lot[0] < 1e-8:
                continue
            pnl += (net_price - lot[1]) * lot[0]
            self.total_qty -= lot[0]
        return pnl

    @property
    def remaining(self) -> float:
        return self.total_qty

    @property
    def avg_cost(self) -> float:
        if self.total_qty < 1e-8:
            return 0.0
        return sum(l[0] * l[1] for l in self.lots) / self.total_qty


# ── Direction Helper ─────────────────────────────────────

def get_direction(is_buyer: bool, outcome: str) -> str:
    """Map buyer/seller + outcome to a directional label."""
    if is_buyer:
        return f"LONG_{TEAM_A}" if outcome == TEAM_A else f"LONG_{TEAM_B}"
    else:
        return f"LONG_{TEAM_B}" if outcome == TEAM_A else f"LONG_{TEAM_A}"


# ── Wallet Summary Stats (clean ~15 cols) ────────────────

def compute_wallet_summary(wallet: str, w_df: pd.DataFrame,
                           roles: dict[str, str], outcomes: list[str],
                           settlement: dict[str, float]) -> dict | None:
    """Clean wallet summary — only the headline numbers."""
    total = len(w_df)
    if total == 0:
        return None

    wl = wallet.lower()
    buys_mask = w_df["buyer"] == wl
    sells_mask = w_df["seller"] == wl

    # Financial role
    fin_maker = sum(1 for eid in w_df["event_id"] if roles.get(eid) == "MAKER")
    fin_taker = total - fin_maker
    maker_pct = round(fin_maker / total * 100, 1)

    # Per-outcome FIFO PnL
    fifo = {o: FIFOQueue() for o in outcomes}
    per_outcome_pnl = {o: 0.0 for o in outcomes}
    mk_sell_pnl = 0.0
    tk_sell_pnl = 0.0

    for _, r in w_df.iterrows():
        o = r["outcome"]
        if o not in fifo:
            continue
        if r["buyer"] == wl:
            fifo[o].buy(r["token_amount"], r["price"])
        else:
            pnl = fifo[o].sell(r["token_amount"], r["price"])
            per_outcome_pnl[o] += pnl
            role = roles.get(r["event_id"], "")
            if role == "MAKER":
                mk_sell_pnl += pnl
            else:
                tk_sell_pnl += pnl

    # Settlement
    settlement_usdc = 0.0
    for o in outcomes:
        sp = settlement.get(o, 0.0)
        s_pnl = fifo[o].settle(sp, REDEMPTION_FEE)
        per_outcome_pnl[o] += s_pnl
        net_tok = w_df.loc[(w_df["outcome"] == o) & buys_mask, "token_amount"].sum() \
                - w_df.loc[(w_df["outcome"] == o) & sells_mask, "token_amount"].sum()
        if net_tok > 0:
            settlement_usdc += net_tok * sp * (1 - REDEMPTION_FEE)

    total_pnl = sum(per_outcome_pnl.values())

    # Volume
    total_bought = w_df.loc[buys_mask, "usdc_amount"].sum()
    total_sold = w_df.loc[sells_mask, "usdc_amount"].sum()
    turnover = total_bought + total_sold

    # Max exposure
    cum = 0.0
    max_exposure = 0.0
    for _, r in w_df.iterrows():
        cum += r["usdc_amount"] if r["buyer"] == wl else -r["usdc_amount"]
        if cum > max_exposure:
            max_exposure = cum

    # Direction
    per_outcome_trades = {o: int((w_df["outcome"] == o).sum()) for o in outcomes}
    a_net_tok = w_df.loc[(w_df["outcome"] == TEAM_A) & buys_mask, "token_amount"].sum() \
              - w_df.loc[(w_df["outcome"] == TEAM_A) & sells_mask, "token_amount"].sum()
    b_net_tok = w_df.loc[(w_df["outcome"] == TEAM_B) & buys_mask, "token_amount"].sum() \
              - w_df.loc[(w_df["outcome"] == TEAM_B) & sells_mask, "token_amount"].sum()
    delta = a_net_tok - b_net_tok
    if delta > 0.1:
        direction_bias = f"LONG_{TEAM_A}"
    elif delta < -0.1:
        direction_bias = f"LONG_{TEAM_B}"
    else:
        direction_bias = "NEUTRAL"

    cap_rot = round(turnover / max_exposure, 2) if max_exposure > 0 else 0
    pnl_bps = round(total_pnl / turnover * 10000, 1) if turnover > 0 else 0
    roi_pct = round(total_pnl / max_exposure * 100, 2) if max_exposure > 0 else 0

    first_ts = int(w_df["timestamp_unix"].iloc[0])
    last_ts = int(w_df["timestamp_unix"].iloc[-1])

    return {
        "address": wallet,
        "type": "",  # filled in later after maker/taker classification
        "total_trades": total,
        "total_pnl": round(total_pnl, 2),
        "settlement_usdc": round(settlement_usdc, 2),
        f"{TEAM_A}_pnl": round(per_outcome_pnl.get(TEAM_A, 0), 2),
        f"{TEAM_B}_pnl": round(per_outcome_pnl.get(TEAM_B, 0), 2),
        "turnover": round(turnover, 2),
        "max_exposure": round(max_exposure, 2),
        "capital_rotation": cap_rot,
        "pnl_bps": pnl_bps,
        "roi_pct": roi_pct,
        "maker_pct": maker_pct,
        "direction_bias": direction_bias,
        "first_trade_ist": ts_to_ist_str(first_ts),
        "last_trade_ist": ts_to_ist_str(last_ts),
        # internal fields for classification (not in final sheet)
        "_fin_maker": fin_maker,
        "_fin_taker": fin_taker,
        "_trades_a": per_outcome_trades.get(TEAM_A, 0),
        "_trades_b": per_outcome_trades.get(TEAM_B, 0),
        "_mk_sell_pnl": mk_sell_pnl,
        "_tk_sell_pnl": tk_sell_pnl,
    }


# ── Maker Metrics ────────────────────────────────────────

def compute_maker_metrics(wallet: str, w_df: pd.DataFrame,
                          roles: dict[str, str], outcomes: list[str],
                          price_lookup: dict,
                          settlement: dict[str, float] | None = None) -> dict:
    """Maker-specific metrics: spread capture, flow toxicity, phase stats."""
    wl = wallet.lower()
    total = len(w_df)
    buys_mask = w_df["buyer"] == wl
    sells_mask = w_df["seller"] == wl

    # Per-outcome trade counts + spread capture
    result: dict = {"address": wallet, "total_trades": total}

    for o in outcomes:
        om = w_df["outcome"] == o
        ob = w_df.loc[om & buys_mask]
        os_ = w_df.loc[om & sells_mask]
        result[f"trades_{o}"] = int(om.sum())
        avg_buy = ob["price"].mean() if len(ob) else 0
        avg_sell = os_["price"].mean() if len(os_) else 0
        result[f"spread_capture_{o}"] = round(avg_sell - avg_buy, 6) if avg_buy > 0 and avg_sell > 0 else 0

    # Maker PnL (single FIFO, sell-side attribution)
    fifo = {o: FIFOQueue() for o in outcomes}
    maker_pnl = 0.0
    for _, r in w_df.iterrows():
        o = r["outcome"]
        if o not in fifo:
            continue
        if r["buyer"] == wl:
            fifo[o].buy(r["token_amount"], r["price"])
        else:
            pnl = fifo[o].sell(r["token_amount"], r["price"])
            if roles.get(r["event_id"]) == "MAKER":
                maker_pnl += pnl
    result["maker_pnl"] = round(maker_pnl, 2)

    # Total PnL (FIFO including settlement)
    fifo2 = {o: FIFOQueue() for o in outcomes}
    total_sell_pnl = 0.0
    for _, r in w_df.iterrows():
        o = r["outcome"]
        if o not in fifo2:
            continue
        if r["buyer"] == wl:
            fifo2[o].buy(r["token_amount"], r["price"])
        else:
            total_sell_pnl += fifo2[o].sell(r["token_amount"], r["price"])
    _stl = settlement or {}
    settle_pnl = sum(fifo2[o].settle(_stl.get(o, 0.0), REDEMPTION_FEE) for o in outcomes)
    result["total_pnl"] = round(total_sell_pnl + settle_pnl, 2)

    # Flow toxicity — adverse fills on MAKER events
    maker_events = w_df[w_df["event_id"].map(lambda eid: roles.get(eid) == "MAKER")]
    adverse_count = 0
    adverse_cents_list: list[float] = []

    for _, r in maker_events.iterrows():
        ts = int(r["timestamp_unix"])
        outcome = r["outcome"]
        fill_price = r["price"]
        maker_is_buyer = r["buyer"] == wl

        lookup = price_lookup.get(outcome)
        if lookup is None:
            continue
        ts_arr, px_arr = lookup
        left = bisect_right(ts_arr, ts)
        right = bisect_right(ts_arr, ts + SNIPE_WINDOW)
        if left >= right:
            continue

        price_later = float(px_arr[right - 1])
        if price_later < MIN_VALID_PRICE:
            continue

        if maker_is_buyer:
            adverse = fill_price - price_later
        else:
            adverse = price_later - fill_price

        if adverse > SNIPE_THRESHOLD:
            adverse_count += 1
            adverse_cents_list.append(adverse * 100)

    total_maker_fills = len(maker_events)
    result["adverse_fills"] = adverse_count
    result["flow_toxicity_pct"] = round(adverse_count / total_maker_fills * 100, 1) if total_maker_fills > 0 else 0
    result["avg_adverse_cents"] = round(np.mean(adverse_cents_list), 2) if adverse_cents_list else 0

    # Ending position + hedge
    for o in outcomes:
        net = w_df.loc[(w_df["outcome"] == o) & buys_mask, "token_amount"].sum() \
            - w_df.loc[(w_df["outcome"] == o) & sells_mask, "token_amount"].sum()
        result[f"net_pos_{o}"] = round(net, 2)

    a_net = result.get(f"net_pos_{TEAM_A}", 0)
    b_net = result.get(f"net_pos_{TEAM_B}", 0)
    if a_net > 0 and b_net > 0:
        result["hedge_ratio_pct"] = round(min(a_net, b_net) / (a_net + b_net) * 100, 1)
    else:
        result["hedge_ratio_pct"] = 0.0

    # Volume & efficiency
    turnover = w_df.loc[buys_mask, "usdc_amount"].sum() + w_df.loc[sells_mask, "usdc_amount"].sum()
    cum = 0.0
    max_exp = 0.0
    for _, r in w_df.iterrows():
        cum += r["usdc_amount"] if r["buyer"] == wl else -r["usdc_amount"]
        if cum > max_exp:
            max_exp = cum

    result["turnover"] = round(turnover, 2)
    result["pnl_bps"] = round(maker_pnl / turnover * 10000, 1) if turnover > 0 else 0
    result["capital_rotation"] = round(turnover / max_exp, 2) if max_exp > 0 else 0

    # Phase activity
    phases = w_df["phase"].value_counts().to_dict()
    result["trades_inn1"] = phases.get("inn1", 0)
    result["trades_inn2"] = phases.get("inn2", 0)
    result["trades_pre"] = phases.get("pre", 0)

    return result


# ── Taker Metrics ────────────────────────────────────────

REVERSAL_WINDOW = 20  # seconds

def detect_reversals(w_df: pd.DataFrame, wallet: str,
                     roles: dict[str, str]) -> dict:
    """For a wallet's taker trades, find same-outcome opposite-action reversals."""
    wl = wallet.lower()

    # Get taker trades only
    taker_mask = w_df["event_id"].map(lambda eid: roles.get(eid) == "TAKER")
    taker_df = w_df[taker_mask].copy()

    if taker_df.empty:
        return {"total_taker_trades": 0, "reversed_count": 0,
                "reversal_pct": 0, "avg_reversal_time_s": 0}

    total_taker = len(taker_df)

    # For each outcome, separate buys and sells
    reversed_set: set[int] = set()  # indices already matched
    reversal_times: list[float] = []

    for outcome in taker_df["outcome"].unique():
        o_df = taker_df[taker_df["outcome"] == outcome].reset_index()
        buys = o_df[o_df["buyer"] == wl]
        sells = o_df[o_df["seller"] == wl]

        if buys.empty or sells.empty:
            continue

        sell_ts = sells["timestamp_unix"].values.astype(np.int64)

        for _, buy_row in buys.iterrows():
            if buy_row["index"] in reversed_set:
                continue
            buy_ts = int(buy_row["timestamp_unix"])

            # Find first unmatched sell within window
            left = bisect_left(sell_ts, buy_ts)
            right = bisect_right(sell_ts, buy_ts + REVERSAL_WINDOW)

            for j in range(left, right):
                sell_idx = sells.iloc[j]["index"]
                if sell_idx in reversed_set:
                    continue
                sell_t = int(sell_ts[j])
                if sell_t < buy_ts:
                    continue
                # Match found
                reversed_set.add(buy_row["index"])
                reversed_set.add(sell_idx)
                reversal_times.append(sell_t - buy_ts)
                break

    reversed_count = len(reversed_set) // 2  # pairs
    return {
        "total_taker_trades": total_taker,
        "reversed_count": reversed_count,
        "reversal_pct": round(reversed_count * 2 / total_taker * 100, 1) if total_taker > 0 else 0,
        "avg_reversal_time_s": round(np.mean(reversal_times), 1) if reversal_times else 0,
    }


def compute_snipe_score(w_df: pd.DataFrame, wallet: str,
                        roles: dict[str, str],
                        price_lookup: dict) -> float:
    """Average post-trade price move in wallet's favor for taker trades (cents)."""
    wl = wallet.lower()
    taker_df = w_df[w_df["event_id"].map(lambda eid: roles.get(eid) == "TAKER")]
    if taker_df.empty:
        return 0.0

    edges: list[float] = []
    for _, r in taker_df.iterrows():
        ts = int(r["timestamp_unix"])
        outcome = r["outcome"]
        fill_price = r["price"]
        is_buyer = r["buyer"] == wl

        lookup = price_lookup.get(outcome)
        if lookup is None:
            continue
        ts_arr, px_arr = lookup
        left = bisect_right(ts_arr, ts)
        right = bisect_right(ts_arr, ts + SNIPE_WINDOW)
        if left >= right:
            continue

        price_later = float(px_arr[right - 1])
        if price_later < MIN_VALID_PRICE:
            continue

        if is_buyer:
            edge = price_later - fill_price  # price went up → good for buyer
        else:
            edge = fill_price - price_later  # price went down → good for seller
        edges.append(edge * 100)  # in cents

    return round(np.mean(edges), 2) if edges else 0.0


def compute_taker_metrics(wallet: str, w_df: pd.DataFrame,
                          roles: dict[str, str], outcomes: list[str],
                          price_lookup: dict,
                          is_also_maker: bool,
                          settlement: dict[str, float] | None = None) -> dict:
    """Taker-specific metrics: reversal, snipe score, classification."""
    wl = wallet.lower()
    buys_mask = w_df["buyer"] == wl
    sells_mask = w_df["seller"] == wl

    # Taker PnL (single FIFO, sell-side attribution)
    fifo = {o: FIFOQueue() for o in outcomes}
    taker_pnl = 0.0
    for _, r in w_df.iterrows():
        o = r["outcome"]
        if o not in fifo:
            continue
        if r["buyer"] == wl:
            fifo[o].buy(r["token_amount"], r["price"])
        else:
            pnl = fifo[o].sell(r["token_amount"], r["price"])
            if roles.get(r["event_id"]) == "TAKER":
                taker_pnl += pnl

    # Total PnL (full FIFO including settlement)
    _stl = settlement or {}
    fifo2 = {o: FIFOQueue() for o in outcomes}
    _total_sell = 0.0
    for _, r in w_df.iterrows():
        o = r["outcome"]
        if o not in fifo2:
            continue
        if r["buyer"] == wl:
            fifo2[o].buy(r["token_amount"], r["price"])
        else:
            _total_sell += fifo2[o].sell(r["token_amount"], r["price"])
    _settle = sum(fifo2[o].settle(_stl.get(o, 0.0), REDEMPTION_FEE) for o in outcomes)
    total_pnl = _total_sell + _settle

    taker_count = sum(1 for eid in w_df["event_id"] if roles.get(eid) == "TAKER")

    rev = detect_reversals(w_df, wallet, roles)
    snipe_score = compute_snipe_score(w_df, wallet, roles, price_lookup)

    # Net position
    net_pos = {}
    for o in outcomes:
        net = w_df.loc[(w_df["outcome"] == o) & buys_mask, "token_amount"].sum() \
            - w_df.loc[(w_df["outcome"] == o) & sells_mask, "token_amount"].sum()
        net_pos[o] = round(net, 2)

    # Taker turnover
    taker_df = w_df[w_df["event_id"].map(lambda eid: roles.get(eid) == "TAKER")]
    turnover_taker = taker_df["usdc_amount"].sum()

    # Classification
    reversal_pct = rev["reversal_pct"]
    if reversal_pct >= 50:
        taker_type = "ARB"
    elif is_also_maker and taker_pnl < 0:
        taker_type = "RECOVERY"
    elif snipe_score > 0.2:
        taker_type = "SNIPER"
    else:
        taker_type = "DIRECTIONAL"

    result = {
        "address": wallet,
        "taker_type": taker_type,
        "total_pnl": round(total_pnl, 2),
        "taker_trades": taker_count,
        "taker_pnl": round(taker_pnl, 2),
        "reversal_pct_20s": reversal_pct,
        "avg_reversal_time_s": rev["avg_reversal_time_s"],
        "snipe_score_15s": snipe_score,
    }
    for o in outcomes:
        result[f"net_pos_{o}"] = net_pos.get(o, 0)
    result["also_maker"] = "YES" if is_also_maker else "NO"
    result["turnover_as_taker"] = round(turnover_taker, 2)
    result["pnl_bps"] = round(taker_pnl / turnover_taker * 10000, 1) if turnover_taker > 0 else 0

    return result


# ── Overlap Detection ────────────────────────────────────

def detect_overlaps(df: pd.DataFrame, roles: dict[str, str],
                    min_events: int = 5) -> pd.DataFrame:
    """Find wallet pairs trading same/opposite direction within ±SAME_DIR_WINDOW.

    Uses sweep-line with bisect for efficiency.
    Only considers wallets with >= min_events maker-column events.
    """
    records = []
    for _, r in df.iterrows():
        m = r["maker"]
        if m == CTF_EXCHANGE:
            continue
        is_buyer = r["buyer"] == m
        direction = get_direction(is_buyer, r["outcome"])
        records.append((int(r["timestamp_unix"]), m, direction))

    if not records:
        return pd.DataFrame()

    wallet_counts = Counter(rec[1] for rec in records)
    active_wallets = {w for w, c in wallet_counts.items() if c >= min_events}
    records = [rec for rec in records if rec[1] in active_wallets]
    records.sort(key=lambda x: x[0])

    ts_arr = np.array([r[0] for r in records])

    pair_same: Counter = Counter()
    pair_opp: Counter = Counter()

    for i, (ts, wallet, direction) in enumerate(records):
        right = bisect_right(ts_arr, ts + SAME_DIR_WINDOW)
        for j in range(i + 1, right):
            ts2, wallet2, dir2 = records[j]
            if wallet2 == wallet:
                continue
            if ts2 - ts > SAME_DIR_WINDOW:
                break
            pair = (min(wallet, wallet2), max(wallet, wallet2))
            if direction == dir2:
                pair_same[pair] += 1
            else:
                pair_opp[pair] += 1

    rows = []
    for w1, w2 in set(pair_same.keys()) | set(pair_opp.keys()):
        same = pair_same.get((w1, w2), 0)
        opp = pair_opp.get((w1, w2), 0)
        total = same + opp
        if total < SAME_DIR_MIN_OVERLAPS:
            continue
        rows.append({
            "wallet_1": w1, "wallet_2": w2,
            "w1_short": short_addr(w1), "w2_short": short_addr(w2),
            "same_dir_overlaps": same, "opp_dir_overlaps": opp,
            "total_overlaps": total,
            "coordination_pct": round(same / total * 100, 1) if total else 0,
        })

    rdf = pd.DataFrame(rows)
    if not rdf.empty:
        rdf = rdf.sort_values("same_dir_overlaps", ascending=False).reset_index(drop=True)
    return rdf


# ── Sniper Detection (enhanced) ──────────────────────────

def detect_snipers(df: pd.DataFrame,
                   taker_types: dict[str, str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Detect adverse selection events. Enhanced with taker_type cross-reference."""
    price_lookup: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    valid = df[df["price"] >= MIN_VALID_PRICE]
    for outcome, grp in valid.groupby("outcome"):
        sg = grp.sort_values("timestamp_unix")
        price_lookup[outcome] = (
            sg["timestamp_unix"].values.astype(np.int64),
            sg["price"].values.astype(np.float64),
        )

    direct = df[df["taker"] != CTF_EXCHANGE].copy()
    snipe_rows = []

    for _, r in direct.iterrows():
        ts = int(r["timestamp_unix"])
        outcome = r["outcome"]
        fill_price = r["price"]
        maker_is_buyer = r["buyer"] == r["maker"]

        lookup = price_lookup.get(outcome)
        if lookup is None:
            continue
        ts_arr, px_arr = lookup

        left = bisect_right(ts_arr, ts)
        right = bisect_right(ts_arr, ts + SNIPE_WINDOW)
        if left >= right:
            continue

        price_later = float(px_arr[right - 1])
        if price_later < MIN_VALID_PRICE:
            continue

        if maker_is_buyer:
            adverse = fill_price - price_later
        else:
            adverse = price_later - fill_price

        if adverse <= SNIPE_THRESHOLD:
            continue

        snipe_rows.append({
            "time_ist": ts_to_ist_str(ts),
            "phase": ts_to_phase(ts),
            "outcome": outcome,
            "maker": r["maker"],
            "sniper": r["taker"],
            "sniper_type": taker_types.get(r["taker"], ""),
            "maker_side": "BUY" if maker_is_buyer else "SELL",
            "fill_price": round(fill_price, 6),
            "price_after": round(price_later, 6),
            "adverse_cents": round(adverse * 100, 2),
            "tokens": round(r["token_amount"], 2),
            "usdc": round(r["usdc_amount"], 2),
        })

    events_df = pd.DataFrame(snipe_rows)

    summary_rows = []
    if not events_df.empty:
        for sniper, grp in events_df.groupby("sniper"):
            summary_rows.append({
                "sniper": sniper,
                "sniper_short": short_addr(sniper),
                "sniper_type": taker_types.get(sniper, ""),
                "snipe_count": len(grp),
                "unique_victims": grp["maker"].nunique(),
                "total_usdc_sniped": round(grp["usdc"].sum(), 2),
                "avg_adverse_cents": round(grp["adverse_cents"].mean(), 2),
                "max_adverse_cents": round(grp["adverse_cents"].max(), 2),
                "top_victim": short_addr(grp["maker"].value_counts().index[0]),
            })
        summary_df = pd.DataFrame(summary_rows).sort_values(
            "snipe_count", ascending=False
        ).reset_index(drop=True)
    else:
        summary_df = pd.DataFrame()

    return events_df, summary_df


# ── Event Log Builder ────────────────────────────────────

def build_event_log(df: pd.DataFrame, roles: dict[str, str]) -> pd.DataFrame:
    """Build the full event log with IST timestamps and sub-second ordering."""
    rows = []
    prev_ts = -1
    seq = 0
    for i, (_, r) in enumerate(df.iterrows()):
        ts = int(r["timestamp_unix"])
        if ts == prev_ts:
            seq += 1
        else:
            seq = 1
            prev_ts = ts

        maker = r["maker"]
        is_buyer = r["buyer"] == maker
        direction = get_direction(is_buyer, r["outcome"]) if maker != CTF_EXCHANGE else ""

        rows.append({
            "seq": i + 1,
            "timestamp_ist": ts_to_ist_full(ts),
            "time_ist": ts_to_ist_str(ts),
            "intra_sec_seq": seq,
            "timestamp_unix": ts,
            "phase": ts_to_phase(ts),
            "outcome": r["outcome"],
            "maker_side": r["maker_side"],
            "financial_role": roles.get(r["event_id"], ""),
            "direction": direction,
            "price": r["price"],
            "tokens": round(r["token_amount"], 4),
            "usdc": round(r["usdc_amount"], 4),
            "fee_usdc": round(r["fee_usdc"], 4),
            "maker": r["maker"],
            "maker_short": short_addr(r["maker"]),
            "taker": r["taker"],
            "taker_short": short_addr(r["taker"]),
            "buyer": r["buyer"],
            "seller": r["seller"],
            "tx_hash": r["tx_hash"],
            "event_id": r["event_id"],
        })

    return pd.DataFrame(rows)


# ── Guide Sheet ──────────────────────────────────────────

def build_guide(outcomes: list[str], settlement: dict) -> pd.DataFrame:
    rows = [
        ("MATCH INFO", ""),
        ("Teams", f"{TEAM_A} vs {TEAM_B}"),
        ("Date / Winner", f"{MATCH_DATE} / {WINNER}"),
        ("Settlement", ", ".join(f"{o}=${settlement[o]:.2f}" for o in outcomes)),
        ("Innings 1 / 2", f"{INN1_START}-{INN1_END} / {INN2_START}-{MATCH_END} IST"),
        ("", ""),
        ("METHODOLOGY", ""),
        ("Data Source", "Goldsky orderbook-subgraph (on-chain OrderFilled events)"),
        ("Wallet Filter", "maker-column only (avoids matchOrders double-counting)"),
        ("PnL", "FIFO per outcome token, single queue. Cash cross-check for validation."),
        ("", ""),
        ("SHEET: WALLET SUMMARY", ""),
        ("type", "MAKER / ARB / SNIPER / RECOVERY / DIRECTIONAL / MIXED"),
        ("total_pnl", "FIFO realized PnL from sells + settlement"),
        ("settlement_usdc", "USDC received from redeeming winning tokens"),
        (f"{TEAM_A}_pnl / {TEAM_B}_pnl", "PnL split by outcome (trading + settlement)"),
        ("pnl_bps", "total_pnl / turnover * 10000"),
        ("roi_pct", "total_pnl / max_exposure * 100"),
        ("", ""),
        ("SHEET: MAKERS", f"Qualification: >=10 trades on EACH outcome ({TEAM_A} AND {TEAM_B})"),
        ("maker_pnl", "PnL from sells where wallet was financial MAKER (single FIFO, sell-side attribution)"),
        ("spread_capture", "avg_sell_price - avg_buy_price per outcome. Positive = earning spread"),
        ("flow_toxicity_pct", "% of maker fills adversely selected within {SNIPE_WINDOW}s. Lower = better flow"),
        ("adverse_fills", f"Count of fills with >{SNIPE_THRESHOLD*100:.1f}c adverse move in {SNIPE_WINDOW}s"),
        ("hedge_ratio_pct", "min(pos_A, pos_B) / (pos_A + pos_B) * 100 when both >0"),
        ("", ""),
        ("SHEET: TAKERS", "Qualification: >=5 financial-TAKER trades"),
        ("taker_type", "ARB (reversal>=50%) / SNIPER (score>0.2c) / RECOVERY (also maker, neg PnL) / DIRECTIONAL"),
        ("reversal_pct_20s", "% of taker trades reversed (same outcome, opposite action) within 20s"),
        ("snipe_score_15s", "Avg post-trade price move in their favor (cents). Positive = informed"),
        ("also_maker", "YES if wallet also qualifies as maker → RECOVERY when taker_pnl < 0"),
        ("", ""),
        ("OVERLAPS", f"Wallet pairs trading within ±{SAME_DIR_WINDOW}s"),
        ("SNIPERS", f"Direct fills with >{SNIPE_THRESHOLD*100:.1f}c adverse move in {SNIPE_WINDOW}s"),
    ]
    return pd.DataFrame(rows, columns=["field", "description"])


# ── Main ─────────────────────────────────────────────────

def main():
    global _INN1_START_UNIX, _INN1_END_UNIX, _INN2_START_UNIX, _MATCH_END_UNIX

    _INN1_START_UNIX = ist_to_unix(MATCH_DATE, INN1_START)
    _INN1_END_UNIX = ist_to_unix(MATCH_DATE, INN1_END)
    _INN2_START_UNIX = ist_to_unix(MATCH_DATE, INN2_START)
    _MATCH_END_UNIX = ist_to_unix(MATCH_DATE, MATCH_END)

    print("Match Analytics")
    print("=" * 60)

    # 1. Market metadata
    print(f"\n1. Fetching market for '{SLUG}'...")
    market = fetch_market(SLUG)
    question = market.get("question", SLUG)
    token_ids, outcome_names = parse_market_tokens(market)
    print(f"   {question}")
    print(f"   Outcomes: {', '.join(outcome_names)}")

    if not token_ids:
        print("Error: No token IDs")
        sys.exit(1)

    settlement = {n: (1.0 if n == WINNER else 0.0) for n in outcome_names}

    # 2. Fetch events
    print(f"\n2. Fetching ALL OrderFilled events...")
    events = fetch_all_events(token_ids)
    if not events:
        print("No events."); sys.exit(0)

    # 3. Process
    print("\n3. Processing events...")
    df = process_events(events, token_ids, outcome_names)
    print(f"   {len(df)} trades | "
          f"{ts_to_ist_full(int(df['timestamp_unix'].iloc[0]))} → "
          f"{ts_to_ist_full(int(df['timestamp_unix'].iloc[-1]))} IST")

    # 4. Classify roles
    print("\n4. Classifying financial roles...")
    roles = classify_all_roles(df)
    mk_n = sum(1 for v in roles.values() if v == "MAKER")
    print(f"   MAKER: {mk_n} | TAKER: {len(roles) - mk_n}")

    # 5. Pre-build price lookup for maker/taker metrics
    price_lookup: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    valid = df[df["price"] >= MIN_VALID_PRICE]
    for outcome, grp in valid.groupby("outcome"):
        sg = grp.sort_values("timestamp_unix")
        price_lookup[outcome] = (
            sg["timestamp_unix"].values.astype(np.int64),
            sg["price"].values.astype(np.float64),
        )

    # 6. Wallet summary (all wallets)
    all_wallets = df[df["maker"] != CTF_EXCHANGE]["maker"].unique()
    print(f"\n5. Computing wallet summaries ({len(all_wallets)} wallets)...")
    summaries = []
    for i, w in enumerate(all_wallets):
        if (i + 1) % 200 == 0:
            print(f"   {i+1}/{len(all_wallets)}...")
        w_df = df[df["maker"] == w].sort_values(["timestamp_unix", "event_id"]).reset_index(drop=True)
        s = compute_wallet_summary(w, w_df, roles, outcome_names, settlement)
        if s:
            summaries.append(s)
    wallet_df = pd.DataFrame(summaries)
    if not wallet_df.empty:
        wallet_df = wallet_df.sort_values("turnover", ascending=False).reset_index(drop=True)

    # 7. Maker sheet (>=10 trades each outcome)
    print("\n6. Building Maker sheet...")
    maker_addrs = set()
    if not wallet_df.empty:
        maker_qual = wallet_df[
            (wallet_df["_trades_a"] >= 10) & (wallet_df["_trades_b"] >= 10)
        ]
        maker_addrs = set(maker_qual["address"])

    maker_rows = []
    for w in maker_addrs:
        w_df = df[df["maker"] == w].sort_values(["timestamp_unix", "event_id"]).reset_index(drop=True)
        m = compute_maker_metrics(w, w_df, roles, outcome_names, price_lookup, settlement)
        maker_rows.append(m)
    makers_df = pd.DataFrame(maker_rows)
    if not makers_df.empty:
        makers_df = makers_df.sort_values("turnover", ascending=False).reset_index(drop=True)
    print(f"   Makers (>=10 each outcome): {len(makers_df)}")

    # 8. Taker sheet (>=5 taker trades)
    print("\n7. Building Taker sheet...")
    taker_rows = []
    if not wallet_df.empty:
        taker_qual = wallet_df[wallet_df["_fin_taker"] >= 5]
        for _, row in taker_qual.iterrows():
            w = row["address"]
            w_df = df[df["maker"] == w].sort_values(["timestamp_unix", "event_id"]).reset_index(drop=True)
            is_also_maker = w in maker_addrs
            t = compute_taker_metrics(w, w_df, roles, outcome_names, price_lookup, is_also_maker, settlement)
            taker_rows.append(t)
    takers_df = pd.DataFrame(taker_rows)
    if not takers_df.empty:
        takers_df = takers_df.sort_values("taker_trades", ascending=False).reset_index(drop=True)
    print(f"   Takers (>=5 taker trades): {len(takers_df)}")

    # 9. Classify wallet types using maker/taker results
    taker_types: dict[str, str] = {}
    if not takers_df.empty:
        for _, t in takers_df.iterrows():
            taker_types[t["address"]] = t["taker_type"]

    if not wallet_df.empty:
        def classify(row):
            addr = row["address"]
            if addr in maker_addrs:
                return "MAKER"
            if addr in taker_types:
                return taker_types[addr]
            return "MIXED"
        wallet_df["type"] = wallet_df.apply(classify, axis=1)

    # 10. Drop internal columns from wallet summary
    internal_cols = [c for c in wallet_df.columns if c.startswith("_")]
    wallet_summary_df = wallet_df.drop(columns=internal_cols) if not wallet_df.empty else pd.DataFrame()

    # 11. Overlaps
    print(f"\n8. Detecting overlaps (±{SAME_DIR_WINDOW}s)...")
    overlaps_df = detect_overlaps(df, roles)
    print(f"   Pairs: {len(overlaps_df)}")

    # 12. Snipers (enhanced)
    print(f"\n9. Detecting snipers...")
    snipe_events_df, sniper_summary_df = detect_snipers(df, taker_types)
    print(f"   Events: {len(snipe_events_df)} | Snipers: {len(sniper_summary_df)}")

    # 13. Event log
    print("\n10. Building Event Log...")
    event_log_df = build_event_log(df, roles)

    # 14. Guide
    guide_df = build_guide(outcome_names, settlement)

    # 15. Write Excel
    print(f"\n11. Writing → {OUTPUT}")
    with pd.ExcelWriter(OUTPUT, engine="openpyxl") as writer:
        event_log_df.to_excel(writer, sheet_name="Event Log", index=False)
        wallet_summary_df.to_excel(writer, sheet_name="Wallet Summary", index=False)

        if not makers_df.empty:
            makers_df.to_excel(writer, sheet_name="Makers", index=False)
        if not takers_df.empty:
            takers_df.to_excel(writer, sheet_name="Takers", index=False)
        if not overlaps_df.empty:
            overlaps_df.to_excel(writer, sheet_name="Overlaps", index=False)
        if not sniper_summary_df.empty:
            sniper_summary_df.to_excel(writer, sheet_name="Sniper Summary", index=False)
        if not snipe_events_df.empty:
            snipe_events_df.to_excel(writer, sheet_name="Snipe Events", index=False)

        guide_df.to_excel(writer, sheet_name="Guide", index=False)
        ws = writer.sheets["Guide"]
        ws.column_dimensions["A"].width = 35
        ws.column_dimensions["B"].width = 100

    # ── Console summary ──
    print(f"\nDone! → {OUTPUT}")
    n_makers = len(makers_df)
    n_takers = len(takers_df)
    arb_n = len(takers_df[takers_df["taker_type"] == "ARB"]) if not takers_df.empty else 0
    sniper_n = len(takers_df[takers_df["taker_type"] == "SNIPER"]) if not takers_df.empty else 0
    recovery_n = len(takers_df[takers_df["taker_type"] == "RECOVERY"]) if not takers_df.empty else 0

    print(f"\n{'='*60}")
    print(f"  {question}")
    print(f"{'='*60}")
    print(f"  Events: {len(df)} | Wallets: {len(wallet_summary_df)}")
    print(f"  Makers: {n_makers} | Takers: {n_takers} (ARB:{arb_n} SNIPER:{sniper_n} RECOVERY:{recovery_n})")

    if not makers_df.empty:
        print(f"\n  TOP MAKERS:")
        for _, m in makers_df.head(10).iterrows():
            print(f"    {short_addr(m['address'])} | "
                  f"{m['total_trades']:>4} trades | "
                  f"PnL ${m['maker_pnl']:>8,.0f} | "
                  f"toxicity {m['flow_toxicity_pct']:>4.0f}% | "
                  f"spread {m.get(f'spread_capture_{TEAM_A}', 0):>+.4f}")

    if not takers_df.empty:
        print(f"\n  TOP TAKERS:")
        for _, t in takers_df.head(10).iterrows():
            print(f"    {short_addr(t['address'])} | "
                  f"{t['taker_type']:>11} | "
                  f"{t['taker_trades']:>3} trades | "
                  f"PnL ${t['taker_pnl']:>8,.0f} | "
                  f"rev {t['reversal_pct_20s']:>4.0f}% | "
                  f"snipe {t['snipe_score_15s']:>+5.1f}c")

    if not overlaps_df.empty:
        print(f"\n  TOP OVERLAPS:")
        for _, o in overlaps_df.head(5).iterrows():
            print(f"    {o['w1_short']} ↔ {o['w2_short']} | "
                  f"same:{o['same_dir_overlaps']} opp:{o['opp_dir_overlaps']} "
                  f"coord:{o['coordination_pct']}%")


if __name__ == "__main__":
    main()
