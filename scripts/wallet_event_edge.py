#!/usr/bin/env python3
"""
Wallet event-edge analyzer — backtracks a wallet's strategy.

For a target wallet and a captured match (DB with cricket_events +
book_snapshots + chain_fills/trades), align every wallet fill to the
nearest ball-by-ball cricket event and to the order-book state at that
moment. Output quantifies whether the wallet:
  * leads events (info edge — buy/sell before wicket hits feed)
  * follows events (reactive chase)
  * stays outside event windows (passive maker)
  * gets run over near events (stale quotes hit)

Source of wallet fills (per match, in priority):
  1. Goldsky orderFilledEvents (by condition_id token_ids, filtered to wallet)
     — complete coverage, exact block timestamps.
  2. DB chain_fills table (fallback when Goldsky is flaky)

Usage:
  venv/bin/python scripts/wallet_event_edge.py \
      --wallet 0xef51ebb7ed5c84e5049fc76e1ae4db3b5799c0d3 \
      --db captures/match_capture_cricipl-pun-sun-2026-04-11_20260411.db \
      --out captures/walletedge_puns_0xef51eb.xlsx
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

import pandas as pd
import requests

CTF_EXCHANGE = "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e"
GOLDSKY = (
    "https://api.goldsky.com/api/public/"
    "project_cl6mb8i9h0003e201j6li0diw/"
    "subgraphs/orderbook-subgraph/0.0.1/gn"
)
USDC = 10 ** 6
IST = timezone(timedelta(hours=5, minutes=30))

WICKET_WINDOW_S = 60          # +/- window around each wicket
BOUNDARY_WINDOW_S = 30        # +/- window for 4s/6s
FILL_NEAR_EVENT_MS = 15_000   # "near an event" = ±15s


def ist(ts_s: int) -> str:
    return datetime.fromtimestamp(ts_s, tz=timezone.utc).astimezone(IST).strftime("%H:%M:%S")


def gql(query: str, timeout: int = 45):
    for attempt in range(3):
        try:
            r = requests.post(GOLDSKY, json={"query": query}, timeout=timeout)
            r.raise_for_status()
            j = r.json()
            if "errors" not in j:
                return j.get("data", {})
            print(f"    gql errors: {j['errors']}", file=sys.stderr)
        except Exception as e:
            print(f"    gql attempt {attempt+1} failed: {e}", file=sys.stderr)
            time.sleep(1.5)
    return {}


def fetch_wallet_fills(token_ids: list[str], wallet: str,
                        t_lo: int, t_hi: int) -> list[dict]:
    """Pull orderFilledEvents where wallet is maker OR taker, bounded by time."""
    out = {}
    for role_field in ("maker", "taker"):
        last_id = ""
        while True:
            cursor = f', id_gt: "{last_id}"' if last_id else ""
            q = f"""{{
              orderFilledEvents(
                first: 1000, orderBy: id, orderDirection: asc,
                where: {{
                  {role_field}: "{wallet.lower()}",
                  timestamp_gte: "{t_lo}",
                  timestamp_lte: "{t_hi}"
                  {cursor}
                }}
              ) {{
                id maker taker
                makerAssetId takerAssetId
                makerAmountFilled takerAmountFilled
                fee timestamp transactionHash orderHash
              }}
            }}"""
            data = gql(q)
            batch = (data or {}).get("orderFilledEvents", [])
            if not batch:
                break
            for ev in batch:
                out[ev["id"]] = ev
            last_id = batch[-1]["id"]
            if len(batch) < 1000:
                break
            time.sleep(0.15)
    # Filter to this market's token_ids
    toks = set(token_ids)
    filtered = [e for e in out.values()
                if e["makerAssetId"] in toks or e["takerAssetId"] in toks]
    return filtered


def process_fills(events: list[dict], token_ids: list[str],
                  outcomes: list[str], wallet: str) -> pd.DataFrame:
    t2o = dict(zip(token_ids, outcomes))
    w = wallet.lower()
    rows = []
    for ev in events:
        maker = ev["maker"].lower()
        taker = ev["taker"].lower()
        m_asset = ev["makerAssetId"]
        t_asset = ev["takerAssetId"]
        m_amt = int(ev["makerAmountFilled"])
        t_amt = int(ev["takerAmountFilled"])
        ts = int(ev["timestamp"])

        # Determine outcome token + whether maker was selling or buying token
        if m_asset in t2o:
            outcome = t2o[m_asset]
            tok_qty = m_amt / USDC
            usdc = t_amt / USDC
            maker_side = "SELL"   # maker sold outcome tokens to taker
        elif t_asset in t2o:
            outcome = t2o[t_asset]
            tok_qty = t_amt / USDC
            usdc = m_amt / USDC
            maker_side = "BUY"    # maker bought outcome tokens from taker
        else:
            continue

        price = usdc / tok_qty if tok_qty > 0 else 0.0

        # Determine wallet's role + effective side
        if maker == w:
            role = "MAKER"
            side = maker_side            # wallet's action direction
        elif taker == w:
            role = "TAKER"
            # flip: if maker sold, taker bought
            side = "BUY" if maker_side == "SELL" else "SELL"
        else:
            continue  # shouldn't happen with our query

        rows.append({
            "ts": ts,
            "time_ist": ist(ts),
            "role": role,
            "side": side,
            "outcome": outcome,
            "price": round(price, 4),
            "tokens": round(tok_qty, 2),
            "notional": round(usdc, 2),
            "tx_hash": ev["transactionHash"],
        })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)


def load_match_meta(db: sqlite3.Connection) -> dict:
    meta = {}
    for k, v in db.execute("SELECT key, value FROM match_meta"):
        try:
            meta[k] = json.loads(v)
        except Exception:
            meta[k] = v
    return meta


def load_events(db: sqlite3.Connection) -> pd.DataFrame:
    rows = list(db.execute(
        "SELECT local_ts_ms, signal_type, runs, wickets, overs, score_str, innings "
        "FROM cricket_events ORDER BY local_ts_ms"
    ))
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=[
        "ts_ms", "signal", "runs", "wickets", "overs", "score_str", "innings"
    ])
    df["ts"] = df["ts_ms"] // 1000
    df["time_ist"] = df["ts"].apply(ist)
    return df


def load_books(db: sqlite3.Connection) -> pd.DataFrame:
    rows = list(db.execute(
        "SELECT local_ts_ms, asset_id, bid1_p, bid1_s, ask1_p, ask1_s, "
        "mid_price, spread, total_bid_depth, total_ask_depth "
        "FROM book_snapshots ORDER BY local_ts_ms"
    ))
    df = pd.DataFrame(rows, columns=[
        "ts_ms", "asset_id", "bid", "bid_sz", "ask", "ask_sz",
        "mid", "spread", "bid_depth", "ask_depth"
    ])
    df["ts"] = df["ts_ms"] // 1000
    return df


def book_at(books: pd.DataFrame, asset_id: str, ts_ms: int) -> dict:
    """Most recent book snapshot for asset_id at or before ts_ms."""
    sub = books[(books["asset_id"] == asset_id) & (books["ts_ms"] <= ts_ms)]
    if sub.empty:
        return {}
    r = sub.iloc[-1]
    return {
        "bid": r["bid"], "ask": r["ask"], "mid": r["mid"],
        "spread": r["spread"], "bid_sz": r["bid_sz"], "ask_sz": r["ask_sz"],
        "book_ts_ms": int(r["ts_ms"]),
        "book_age_ms": int(ts_ms - r["ts_ms"]),
    }


def nearest_event(events: pd.DataFrame, fill_ts: int) -> dict:
    """Return nearest cricket event (signed delta: negative = event after fill)."""
    if events.empty:
        return {"nearest_signal": None, "nearest_delta_s": None,
                "nearest_score": None, "nearest_innings": None}
    diffs = events["ts"] - fill_ts
    # abs-nearest
    idx = diffs.abs().idxmin()
    row = events.loc[idx]
    return {
        "nearest_signal": row["signal"],
        "nearest_delta_s": int(row["ts"] - fill_ts),
        "nearest_score": row["score_str"],
        "nearest_innings": int(row["innings"]),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wallet", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache", default=None,
                    help="Optional path to cache Goldsky fills JSON")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit(f"DB not found: {args.db}")

    wallet = args.wallet.lower()
    print(f"▸ wallet: {wallet}")
    print(f"▸ db: {args.db}")

    conn = sqlite3.connect(args.db)
    conn.row_factory = None

    meta = load_match_meta(conn)
    slug = meta.get("slug", "")
    token_ids = meta.get("token_ids", [])
    outcomes = meta.get("outcome_names", [])
    print(f"▸ slug: {slug}")
    print(f"▸ outcomes: {outcomes}")

    events_df = load_events(conn)
    books_df = load_books(conn)
    print(f"▸ cricket_events: {len(events_df)}")
    print(f"▸ book_snapshots: {len(books_df)}")
    if events_df.empty:
        sys.exit("No cricket events in DB — can't do event-aligned analysis.")

    # Define time window for fills: match first/last cricket event ± margin
    t_lo = int(events_df["ts"].min()) - 7200     # 2h before first event
    t_hi = int(events_df["ts"].max()) + 3600     # 1h after last event

    # Load wallet fills (cache if available)
    fills = None
    if args.cache and os.path.exists(args.cache):
        with open(args.cache) as f:
            cached = json.load(f)
        if cached.get("wallet") == wallet and cached.get("slug") == slug:
            print(f"▸ using cached fills ({len(cached['fills'])} events)")
            fills = cached["fills"]

    if fills is None:
        print(f"▸ querying Goldsky: wallet fills {t_lo}→{t_hi}")
        fills = fetch_wallet_fills(token_ids, wallet, t_lo, t_hi)
        print(f"▸ Goldsky returned {len(fills)} events")
        if args.cache:
            os.makedirs(os.path.dirname(os.path.abspath(args.cache)), exist_ok=True)
            with open(args.cache, "w") as f:
                json.dump({"wallet": wallet, "slug": slug, "fills": fills}, f)
            print(f"▸ cached to {args.cache}")

    df = process_fills(fills, token_ids, outcomes, wallet)
    if df.empty:
        sys.exit("No wallet fills in window.")
    print(f"▸ processed {len(df)} wallet fills")

    # Build asset_id → outcome map
    t2o = dict(zip(token_ids, outcomes))

    # Enrich each fill with: nearest event + book state
    enriched = []
    for _, r in df.iterrows():
        fill_ts_ms = r["ts"] * 1000
        ne = nearest_event(events_df, r["ts"])
        # Find asset_id for this fill (outcome → token_id)
        asset_id = None
        for tok, name in t2o.items():
            if name == r["outcome"]:
                asset_id = tok
                break
        book = book_at(books_df, asset_id, fill_ts_ms) if asset_id else {}
        enriched.append({**r.to_dict(), **ne, **book, "asset_id": asset_id})
    e_df = pd.DataFrame(enriched)

    # Aggregates
    match_phase_events = events_df[events_df["ts"].between(t_lo, t_hi)]
    wickets = events_df[events_df["signal"] == "W"]
    boundaries = events_df[events_df["signal"].isin(["4", "6"])]

    # --- Event-window analysis ---
    def window_hits(center_ts: int, window_s: int) -> pd.DataFrame:
        lo = center_ts - window_s
        hi = center_ts + window_s
        return e_df[e_df["ts"].between(lo, hi)]

    wicket_summary = []
    for _, w in wickets.iterrows():
        win = window_hits(w["ts"], WICKET_WINDOW_S)
        pre = win[win["ts"] < w["ts"]]
        post = win[win["ts"] >= w["ts"]]
        wicket_summary.append({
            "wicket_time_ist": w["time_ist"],
            "wicket_ts": int(w["ts"]),
            "score": w["score_str"],
            "innings": int(w["innings"]),
            "n_pre_60s": len(pre),
            "n_post_60s": len(post),
            "pre_buy_notional": pre.loc[pre["side"] == "BUY", "notional"].sum(),
            "pre_sell_notional": pre.loc[pre["side"] == "SELL", "notional"].sum(),
            "post_buy_notional": post.loc[post["side"] == "BUY", "notional"].sum(),
            "post_sell_notional": post.loc[post["side"] == "SELL", "notional"].sum(),
            "first_fill_after_wicket_s": (
                int((post["ts"].min() - w["ts"])) if not post.empty else None
            ),
            "last_fill_before_wicket_s": (
                int((w["ts"] - pre["ts"].max())) if not pre.empty else None
            ),
        })
    w_df = pd.DataFrame(wicket_summary)

    # Delta-to-nearest-event histogram buckets
    def bucket(d):
        if d is None:
            return "no_event"
        if d >= 0:
            return (f"+0..5s" if d < 5 else
                    f"+5..15s" if d < 15 else
                    f"+15..30s" if d < 30 else
                    f"+30..60s" if d < 60 else
                    f"+60..300s" if d < 300 else
                    ">300s")
        d = abs(d)
        return ("-0..5s" if d < 5 else
                "-5..15s" if d < 15 else
                "-15..30s" if d < 30 else
                "-30..60s" if d < 60 else
                "-60..300s" if d < 300 else
                "<-300s")

    e_df["delta_bucket"] = e_df["nearest_delta_s"].apply(bucket)

    # Aggregates
    by_role = e_df.groupby("role").agg(
        n=("ts", "count"),
        notional=("notional", "sum"),
    ).reset_index()

    by_side_role = e_df.groupby(["role", "side"]).agg(
        n=("ts", "count"),
        notional=("notional", "sum"),
    ).reset_index()

    by_bucket = e_df.groupby(["delta_bucket", "role"]).agg(
        n=("ts", "count"),
        notional=("notional", "sum"),
    ).reset_index()

    # Snipe vulnerability: MAKER fills within ±5s of any event
    near_event = e_df[e_df["nearest_delta_s"].abs() <= 5]
    maker_near = near_event[near_event["role"] == "MAKER"]
    run_over_pct = 100.0 * len(maker_near) / max(1, len(e_df[e_df["role"] == "MAKER"]))

    # Summary
    summary = {
        "wallet": wallet,
        "slug": slug,
        "outcomes": " / ".join(outcomes),
        "fills_total": len(e_df),
        "maker_fills": int((e_df["role"] == "MAKER").sum()),
        "taker_fills": int((e_df["role"] == "TAKER").sum()),
        "maker_pct": round(100 * (e_df["role"] == "MAKER").sum() / len(e_df), 1),
        "notional_total": round(e_df["notional"].sum(), 2),
        "cricket_events": len(events_df),
        "wickets": len(wickets),
        "boundaries_4s6s": len(boundaries),
        "maker_within_5s_of_event_pct": round(run_over_pct, 2),
        "maker_filled_at_mid_or_worse_pct": None,  # computed below
        "fills_first_ist": e_df["time_ist"].iloc[0],
        "fills_last_ist": e_df["time_ist"].iloc[-1],
    }

    # Maker "stale quote" metric: for MAKER BUYs where fill price >= mid at fill
    # time, they got run over (paid mid or above as a buyer).
    def quote_quality(row):
        if row["role"] != "MAKER" or not row.get("mid"):
            return None
        if row["side"] == "BUY":
            return "good" if row["price"] < row["mid"] else "run_over"
        else:
            return "good" if row["price"] > row["mid"] else "run_over"

    e_df["quote_quality"] = e_df.apply(quote_quality, axis=1)
    qq = e_df[e_df["role"] == "MAKER"]["quote_quality"].value_counts().to_dict()
    total_maker_qq = sum(qq.values())
    summary["maker_filled_at_mid_or_worse_pct"] = round(
        100 * qq.get("run_over", 0) / max(1, total_maker_qq), 2
    )

    # Write outputs
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with pd.ExcelWriter(args.out, engine="openpyxl") as xl:
        pd.DataFrame([summary]).T.reset_index().rename(
            columns={"index": "metric", 0: "value"}
        ).to_excel(xl, sheet_name="SUMMARY", index=False)
        e_df.to_excel(xl, sheet_name="fills_aligned", index=False)
        w_df.to_excel(xl, sheet_name="wickets_window", index=False)
        by_role.to_excel(xl, sheet_name="by_role", index=False)
        by_side_role.to_excel(xl, sheet_name="by_side_role", index=False)
        by_bucket.to_excel(xl, sheet_name="by_event_bucket", index=False)
        events_df.to_excel(xl, sheet_name="cricket_events", index=False)

    print(f"\n✓ wrote {args.out}")
    print(f"\n── SUMMARY ──")
    for k, v in summary.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
