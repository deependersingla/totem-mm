#!/usr/bin/env python3
"""
Per-wallet IPL season report across all matches on Polymarket.

Usage:
    venv/bin/python scripts/wallet_ipl_report.py 0x1234...abcd

Outputs: captures/wallet_ipl_{wallet[:8]}.xlsx
"""

import json
import os
import sys
import time
from collections import deque
from datetime import datetime, timezone, timedelta

import pandas as pd
import requests

# ── Constants ────────────────────────────────────────────────────────────────

GAMMA_API = "https://gamma-api.polymarket.com"
GOLDSKY_ENDPOINT = (
    "https://api.goldsky.com/api/public/"
    "project_cl6mb8i9h0003e201j6li0diw/"
    "subgraphs/orderbook-subgraph/0.0.1/gn"
)
CTF_EXCHANGE = "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e"
USDC_DECIMALS = 6
IST = timezone(timedelta(hours=5, minutes=30))

REDEMPTION_FEE = 0.0  # 0.0 for cricket (no fee)

IPL_SLUGS = [
    "cricipl-del-mum-2026-04-04",
    "cricipl-guj-raj-2026-04-04",
    "cricipl-sun-luc-2026-04-05",
    "cricipl-roy-che-2026-04-05",
    "cricipl-kol-pun-2026-04-06",
    "cricipl-raj-mum-2026-04-07",
    "cricipl-del-guj-2026-04-08",
    "cricipl-kol-luc-2026-04-09",
    "cricipl-raj-roy-2026-04-10",
    "cricipl-pun-sun-2026-04-11",
    "cricipl-luc-guj-2026-04-12",
    "cricipl-mum-roy-2026-04-12",
    "cricipl-sun-raj-2026-04-13",
    "cricipl-che-kol-2026-04-14",
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def ts_to_ist_str(ts: int) -> str:
    """Unix timestamp -> 'HH:MM:SS' IST string."""
    return (datetime.fromtimestamp(ts, tz=timezone.utc)
            .astimezone(IST).strftime("%H:%M:%S"))


def _short_addr(addr: str) -> str:
    if addr == CTF_EXCHANGE:
        return "CTF_EXCHANGE"
    if len(addr) > 12:
        return addr[:6] + ".." + addr[-4:]
    return addr


# ── Gamma API ────────────────────────────────────────────────────────────────

def fetch_market(slug):
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(f"{GAMMA_API}/markets", params={"slug": slug}, headers=headers, timeout=15)
    resp.raise_for_status()
    markets = resp.json()
    if markets:
        return markets[0]
    resp = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, headers=headers, timeout=15)
    resp.raise_for_status()
    events = resp.json()
    if events:
        for m in events[0].get("markets", []):
            if m.get("slug") == slug:
                return m
    return None


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
# Copied exactly from wallet_match_report.py (lines 138-221)

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
# Copied exactly from wallet_match_report.py (lines 226-295)

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
# Copied exactly from wallet_match_report.py (lines 300-320)

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
# Copied exactly from wallet_match_report.py (lines 325-404)

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


# ── Per-match PnL computation ────────────────────────────────────────────────
# FIFO logic mirrors build_fifo from wallet_match_report.py (lines 763-900)

def compute_match_pnl(df: pd.DataFrame, wallet: str,
                      outcomes: list[str], settlement: dict[str, float]
                      ) -> tuple[dict, pd.DataFrame]:
    """
    Compute FIFO PnL for a wallet in a single match.

    Uses maker-only filter (df["maker"] == wallet) to avoid double-counting
    since matchOrders emits 2 events per trade.

    Returns (summary_dict, ledger_df).
    """
    wallet = wallet.lower()
    w_df = df[df["maker"] == wallet]

    if w_df.empty:
        return None, pd.DataFrame()

    roles = classify_financial_role(df, wallet)

    # Build FIFO ledger
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

        is_buyer = r["buyer"] == wallet
        order_type = roles.get(r.get("event_id", ""), "UNKNOWN")

        if is_buyer:
            queue.buy(r["token_amount"], r["price"])
            row = {
                "#": len(rows) + 1,
                "time_ist": ts_to_ist_str(ts),
                "outcome": outcome,
                "side": "BUY",
                "order_type": order_type,
                "tokens": round(r["token_amount"], 4),
                "price": round(r["price"], 6),
                "notional": round(r["usdc_amount"], 2),
                "cost_basis": round(r["price"], 6),
                "realized_pnl": 0.0,
                "cumulative_pnl": round(cumulative_pnl, 2),
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
                "outcome": outcome,
                "side": "SELL",
                "order_type": order_type,
                "tokens": round(r["token_amount"], 4),
                "price": round(r["price"], 6),
                "notional": round(r["usdc_amount"], 2),
                "cost_basis": round(avg_cost, 6),
                "realized_pnl": round(pnl, 2),
                "cumulative_pnl": round(cumulative_pnl, 2),
            }
            rows.append(row)

    # Settlement rows
    for outcome in outcomes:
        queue = queues[outcome]
        if queue.remaining_qty < 1e-8:
            continue
        settle_price = settlement.get(outcome, 0.0)
        entries = queue.settle(settle_price, REDEMPTION_FEE)

        for entry in entries:
            cumulative_pnl += entry["pnl"]
            net_settle = entry["settle_price"]
            row = {
                "#": len(rows) + 1,
                "time_ist": "SETTLE",
                "outcome": outcome,
                "side": "SETTLEMENT",
                "order_type": "",
                "tokens": round(entry["qty"], 4),
                "price": round(net_settle, 6),
                "notional": round(entry["qty"] * net_settle, 2),
                "cost_basis": round(entry["cost_basis"], 6),
                "realized_pnl": round(entry["pnl"], 2),
                "cumulative_pnl": round(cumulative_pnl, 2),
            }
            rows.append(row)

    ledger = pd.DataFrame(rows)

    # ── Compute summary stats ──
    n_trades = len(w_df)
    n_maker = sum(1 for v in roles.values() if v == "MAKER")
    n_taker = sum(1 for v in roles.values() if v == "TAKER")
    maker_pct = (n_maker / n_trades * 100) if n_trades > 0 else 0

    total_bought_usdc = w_df[w_df["buyer"] == wallet]["usdc_amount"].sum()
    total_sold_usdc = w_df[w_df["seller"] == wallet]["usdc_amount"].sum()
    turnover = total_bought_usdc + total_sold_usdc

    # Max exposure
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

    capital_rotation = turnover / max_exposure if max_exposure > 0 else 0

    # PnL from ledger
    if not ledger.empty:
        fifo_data = ledger[ledger["side"].isin(["BUY", "SELL", "SETTLEMENT"])]
        sell_pnl = fifo_data[fifo_data["side"] == "SELL"]["realized_pnl"].sum()
        settle_pnl = fifo_data[fifo_data["side"] == "SETTLEMENT"]["realized_pnl"].sum()
        maker_pnl = fifo_data[fifo_data["order_type"] == "MAKER"]["realized_pnl"].sum()
        taker_pnl = fifo_data[fifo_data["order_type"] == "TAKER"]["realized_pnl"].sum()
    else:
        sell_pnl = settle_pnl = maker_pnl = taker_pnl = 0.0

    total_pnl = sell_pnl + settle_pnl
    pnl_bps = (total_pnl / turnover * 10000) if turnover > 0 else 0
    roi_pct = (total_pnl / max_exposure * 100) if max_exposure > 0 else 0

    # Net position per outcome
    net_pos = {}
    for outcome in outcomes:
        team_df = w_df[w_df["outcome"] == outcome]
        bought = team_df[team_df["buyer"] == wallet]["token_amount"].sum()
        sold = team_df[team_df["seller"] == wallet]["token_amount"].sum()
        net_pos[outcome] = round(bought - sold, 4)

    # Direction bias: which outcome had larger net long
    if net_pos:
        max_label = max(net_pos, key=lambda k: abs(net_pos[k]))
        direction_bias = f"{max_label} {'LONG' if net_pos[max_label] > 0 else 'SHORT'} {abs(net_pos[max_label]):.2f}"
    else:
        direction_bias = "FLAT"

    summary = {
        "n_trades": n_trades,
        "n_maker": n_maker,
        "n_taker": n_taker,
        "maker_pct": round(maker_pct, 1),
        "turnover": round(turnover, 2),
        "max_exposure": round(max_exposure, 2),
        "capital_rotation": round(capital_rotation, 2),
        "maker_pnl": round(maker_pnl, 2),
        "taker_pnl": round(taker_pnl, 2),
        "settle_pnl": round(settle_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "pnl_bps": round(pnl_bps, 1),
        "roi_pct": round(roi_pct, 2),
        "net_position": net_pos,
        "direction_bias": direction_bias,
        "warnings": warnings,
    }

    return summary, ledger


# ── Determine settlement prices ─────────────────────────────────────────────

def get_settlement(market: dict, outcomes: list[str]) -> dict[str, float]:
    """Determine settlement prices from market data.

    Uses outcomePrices for resolved markets (prices pinned at 0/1).
    For unresolved markets, return 0 for all outcomes.
    """
    settlement = {}

    # Check outcomePrices — for resolved markets these are 0/1
    prices = market.get("outcomePrices")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except (json.JSONDecodeError, ValueError):
            prices = None

    closed = market.get("closed", False)
    uma_resolved = market.get("umaResolutionStatus", "") == "resolved"
    resolved = closed or uma_resolved

    if prices and len(prices) >= len(outcomes):
        parsed = [float(p) for p in prices]
        # Resolved markets have prices pinned at 0/1. Use them directly.
        if any(p >= 0.99 or p <= 0.01 for p in parsed):
            for i, outcome in enumerate(outcomes):
                settlement[outcome] = parsed[i]
            return settlement

    if resolved:
        # Fallback: check winner field
        winner = market.get("winner", market.get("resolution", ""))
        for outcome in outcomes:
            settlement[outcome] = 1.0 if outcome == winner else 0.0
    else:
        for outcome in outcomes:
            settlement[outcome] = 0.0

    return settlement


# ── Write Excel workbook ─────────────────────────────────────────────────────

def write_workbook(wallet: str, match_results: list[dict], output_path: str):
    """Write the final xlsx with SUMMARY + per-match sheets."""

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:

        # ── SUMMARY sheet ──
        played = [m for m in match_results if m["summary"] is not None]

        grand_pnl = sum(m["summary"]["total_pnl"] for m in played)
        grand_turnover = sum(m["summary"]["turnover"] for m in played)
        grand_bps = (grand_pnl / grand_turnover * 10000) if grand_turnover > 0 else 0
        grand_trades = sum(m["summary"]["n_trades"] for m in played)
        grand_maker_pnl = sum(m["summary"]["maker_pnl"] for m in played)
        grand_taker_pnl = sum(m["summary"]["taker_pnl"] for m in played)
        grand_settle_pnl = sum(m["summary"]["settle_pnl"] for m in played)

        # Grand totals header
        header_rows = [
            {"label": "Wallet", "value": wallet},
            {"label": "Matches Played", "value": len(played)},
            {"label": "Matches Scanned", "value": len(match_results)},
            {"label": "Grand Total Trades", "value": grand_trades},
            {"label": "Grand Turnover ($)", "value": round(grand_turnover, 2)},
            {"label": "Grand PnL ($)", "value": round(grand_pnl, 2)},
            {"label": "Grand PnL (bps)", "value": round(grand_bps, 1)},
            {"label": "Grand Maker PnL ($)", "value": round(grand_maker_pnl, 2)},
            {"label": "Grand Taker PnL ($)", "value": round(grand_taker_pnl, 2)},
            {"label": "Grand Settle PnL ($)", "value": round(grand_settle_pnl, 2)},
            {"label": "", "value": ""},
        ]
        header_df = pd.DataFrame(header_rows)
        header_df.to_excel(writer, sheet_name="SUMMARY", index=False,
                           startrow=0, header=True)

        # Per-match table
        table_rows = []
        for m in match_results:
            slug = m["slug"]
            s = m["summary"]
            if s is None:
                table_rows.append({
                    "match": slug,
                    "trades": 0,
                    "maker": 0,
                    "taker": 0,
                    "maker%": 0,
                    "turnover": 0,
                    "max_exp": 0,
                    "cap_rot": 0,
                    "maker_pnl": 0,
                    "taker_pnl": 0,
                    "settle_pnl": 0,
                    "total_pnl": 0,
                    "bps": 0,
                    "roi%": 0,
                    "direction": "NO TRADES",
                })
            else:
                table_rows.append({
                    "match": slug,
                    "trades": s["n_trades"],
                    "maker": s["n_maker"],
                    "taker": s["n_taker"],
                    "maker%": s["maker_pct"],
                    "turnover": s["turnover"],
                    "max_exp": s["max_exposure"],
                    "cap_rot": s["capital_rotation"],
                    "maker_pnl": s["maker_pnl"],
                    "taker_pnl": s["taker_pnl"],
                    "settle_pnl": s["settle_pnl"],
                    "total_pnl": s["total_pnl"],
                    "bps": s["pnl_bps"],
                    "roi%": s["roi_pct"],
                    "direction": s["direction_bias"],
                })

        table_df = pd.DataFrame(table_rows)
        start_row = len(header_rows) + 2
        table_df.to_excel(writer, sheet_name="SUMMARY", index=False,
                          startrow=start_row, header=True)

        # ── Per-match sheets ──
        for m in match_results:
            s = m["summary"]
            ledger = m["ledger"]
            slug = m["slug"]

            # Excel sheet name limit is 31 chars
            sheet_name = slug[:31]

            if s is None:
                empty_df = pd.DataFrame([{"info": f"No trades found for {wallet} in {slug}"}])
                empty_df.to_excel(writer, sheet_name=sheet_name, index=False)
                continue

            # Summary stats at top as label/value pairs
            stat_rows = [
                {"label": "Match", "value": slug},
                {"label": "Wallet", "value": wallet},
                {"label": "Trades", "value": s["n_trades"]},
                {"label": "Maker Trades", "value": f"{s['n_maker']} ({s['maker_pct']}%)"},
                {"label": "Taker Trades", "value": s["n_taker"]},
                {"label": "Turnover ($)", "value": s["turnover"]},
                {"label": "Max Exposure ($)", "value": s["max_exposure"]},
                {"label": "Capital Rotation", "value": f"{s['capital_rotation']}x"},
                {"label": "Maker PnL ($)", "value": s["maker_pnl"]},
                {"label": "Taker PnL ($)", "value": s["taker_pnl"]},
                {"label": "Settlement PnL ($)", "value": s["settle_pnl"]},
                {"label": "Total PnL ($)", "value": s["total_pnl"]},
                {"label": "PnL (bps)", "value": s["pnl_bps"]},
                {"label": "ROI (%)", "value": s["roi_pct"]},
                {"label": "Direction Bias", "value": s["direction_bias"]},
            ]
            # Add net positions
            for team, pos in s["net_position"].items():
                stat_rows.append({"label": f"Net Position {team}", "value": pos})
            if s["warnings"]:
                for w in s["warnings"]:
                    stat_rows.append({"label": "WARNING", "value": w})

            stats_df = pd.DataFrame(stat_rows)
            stats_df.to_excel(writer, sheet_name=sheet_name, index=False,
                              startrow=0, header=True)

            # TRADE LEDGER header + data
            ledger_start = len(stat_rows) + 2
            label_df = pd.DataFrame([{"label": "TRADE LEDGER", "value": ""}])
            label_df.to_excel(writer, sheet_name=sheet_name, index=False,
                              startrow=ledger_start, header=False)

            if not ledger.empty:
                ledger.to_excel(writer, sheet_name=sheet_name, index=False,
                                startrow=ledger_start + 1, header=True)

    print(f"\nSaved: {output_path}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: venv/bin/python scripts/wallet_ipl_report.py <wallet_address>")
        sys.exit(1)

    wallet = sys.argv[1].lower().strip()
    print(f"Wallet: {wallet}")
    print(f"Scanning {len(IPL_SLUGS)} IPL matches...\n")

    script_dir = os.path.dirname(os.path.abspath(__file__))
    captures_dir = os.path.join(os.path.dirname(script_dir), "captures")
    os.makedirs(captures_dir, exist_ok=True)
    output_path = os.path.join(captures_dir, f"wallet_ipl_{wallet[:8]}.xlsx")

    match_results = []

    for i, slug in enumerate(IPL_SLUGS, 1):
        print(f"[{i}/{len(IPL_SLUGS)}] {slug}")
        print(f"  Fetching market...")

        market = fetch_market(slug)
        if market is None:
            print(f"  SKIP: no market found for {slug}")
            match_results.append({
                "slug": slug,
                "summary": None,
                "ledger": pd.DataFrame(),
            })
            continue

        token_ids, outcome_names = parse_market_tokens(market)
        if not token_ids or len(token_ids) < 2:
            print(f"  SKIP: insufficient token IDs for {slug}")
            match_results.append({
                "slug": slug,
                "summary": None,
                "ledger": pd.DataFrame(),
            })
            continue

        print(f"  Outcomes: {outcome_names}")
        print(f"  Fetching OrderFilled events...")

        events = fetch_all_order_filled_events(token_ids)
        if not events:
            print(f"  SKIP: no events found")
            match_results.append({
                "slug": slug,
                "summary": None,
                "ledger": pd.DataFrame(),
            })
            continue

        df = process_events(events, token_ids, outcome_names)
        if df.empty:
            print(f"  SKIP: empty DataFrame after processing")
            match_results.append({
                "slug": slug,
                "summary": None,
                "ledger": pd.DataFrame(),
            })
            continue

        # Check if wallet has any trades in this match (maker-only filter)
        w_df = df[df["maker"] == wallet]
        if w_df.empty:
            print(f"  SKIP: wallet not found in match")
            match_results.append({
                "slug": slug,
                "summary": None,
                "ledger": pd.DataFrame(),
            })
            continue

        print(f"  Wallet trades: {len(w_df)}")

        # Determine settlement
        settlement = get_settlement(market, outcome_names)
        print(f"  Settlement: {settlement}")

        # Compute PnL
        summary, ledger = compute_match_pnl(df, wallet, outcome_names, settlement)

        if summary is not None:
            print(f"  PnL: ${summary['total_pnl']:.2f} | "
                  f"Turnover: ${summary['turnover']:.2f} | "
                  f"BPS: {summary['pnl_bps']:.1f} | "
                  f"Trades: {summary['n_trades']}")

        match_results.append({
            "slug": slug,
            "summary": summary,
            "ledger": ledger,
        })

        print()

    # Write workbook
    write_workbook(wallet, match_results, output_path)

    # Final summary to console
    played = [m for m in match_results if m["summary"] is not None]
    if played:
        grand_pnl = sum(m["summary"]["total_pnl"] for m in played)
        grand_turnover = sum(m["summary"]["turnover"] for m in played)
        grand_bps = (grand_pnl / grand_turnover * 10000) if grand_turnover > 0 else 0
        print(f"\n{'='*60}")
        print(f"SEASON SUMMARY — {wallet[:10]}...")
        print(f"  Matches played: {len(played)}/{len(IPL_SLUGS)}")
        print(f"  Grand PnL:      ${grand_pnl:.2f}")
        print(f"  Grand Turnover: ${grand_turnover:.2f}")
        print(f"  Grand BPS:      {grand_bps:.1f}")
        print(f"  Output:         {output_path}")
        print(f"{'='*60}")
    else:
        print(f"\nNo trades found for {wallet} in any IPL match.")


if __name__ == "__main__":
    main()
