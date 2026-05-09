#!/usr/bin/env python3
"""
Open-ended wallet market profile.

For a target wallet and a match DB, derive the wallet's strategy fingerprint
entirely from market data:
  • Fills from Goldsky orderFilledEvents (filtered to wallet + token_ids)
  • 5-level order book snapshots (every book tick)
  • Cricket events (used as-is from DB for context annotation only)

Outputs:
  • Order size distribution
  • Price level preferences
  • Inter-fill time distribution (quote cadence proxy)
  • Inventory path (net position per outcome)
  • Mark-to-market cumulative PnL path (mid-based)
  • Maker/taker split across match phases
  • Effective spread captured (per maker fill, fill price vs concurrent mid)
  • Market events detected from book (sustained mid jumps)
  • Wallet activity inside vs outside market-event windows
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import pandas as pd
import requests

CTF = "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e"
GOLDSKY = ("https://api.goldsky.com/api/public/"
           "project_cl6mb8i9h0003e201j6li0diw/"
           "subgraphs/orderbook-subgraph/0.0.1/gn")
USDC = 10 ** 6
IST = timezone(timedelta(hours=5, minutes=30))


def ist(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(IST).strftime("%H:%M:%S")


def gql(q, timeout=45):
    for i in range(3):
        try:
            r = requests.post(GOLDSKY, json={"query": q}, timeout=timeout)
            r.raise_for_status()
            j = r.json()
            if "errors" not in j:
                return j.get("data", {})
        except Exception as e:
            print(f"    gql retry: {e}", file=sys.stderr)
            time.sleep(1)
    return {}


def fetch_wallet_fills(token_ids, wallet, t_lo, t_hi):
    out = {}
    for role in ("maker", "taker"):
        last = ""
        while True:
            cur = f', id_gt: "{last}"' if last else ""
            q = f"""{{
              orderFilledEvents(
                first: 1000, orderBy: id, orderDirection: asc,
                where: {{
                  {role}: "{wallet.lower()}",
                  timestamp_gte: "{t_lo}", timestamp_lte: "{t_hi}"
                  {cur}
                }}
              ) {{
                id maker taker makerAssetId takerAssetId
                makerAmountFilled takerAmountFilled fee timestamp
                transactionHash orderHash
              }}
            }}"""
            d = gql(q)
            b = (d or {}).get("orderFilledEvents", [])
            if not b:
                break
            for ev in b:
                out[ev["id"]] = ev
            last = b[-1]["id"]
            if len(b) < 1000:
                break
            time.sleep(0.1)
    toks = set(token_ids)
    return [e for e in out.values()
            if e["makerAssetId"] in toks or e["takerAssetId"] in toks]


def process_fills(events, token_ids, outcomes, wallet):
    t2o = dict(zip(token_ids, outcomes))
    o2t = {v: k for k, v in t2o.items()}
    w = wallet.lower()
    rows = []
    for ev in events:
        m = ev["maker"].lower()
        t = ev["taker"].lower()
        ma = ev["makerAssetId"]
        ta = ev["takerAssetId"]
        m_amt = int(ev["makerAmountFilled"])
        t_amt = int(ev["takerAmountFilled"])
        ts = int(ev["timestamp"])

        if ma in t2o:
            outcome = t2o[ma]
            tok_q = m_amt / USDC
            usdc = t_amt / USDC
            maker_side = "SELL"
        elif ta in t2o:
            outcome = t2o[ta]
            tok_q = t_amt / USDC
            usdc = m_amt / USDC
            maker_side = "BUY"
        else:
            continue
        price = usdc / tok_q if tok_q > 0 else 0

        if m == w:
            role = "MAKER"
            side = maker_side
            cp = t
        elif t == w:
            role = "TAKER"
            side = "BUY" if maker_side == "SELL" else "SELL"
            cp = m
        else:
            continue

        rows.append({
            "ts": ts,
            "time_ist": ist(ts),
            "role": role,
            "side": side,
            "outcome": outcome,
            "asset_id": o2t[outcome],
            "price": round(price, 5),
            "tokens": round(tok_q, 3),
            "notional": round(usdc, 3),
            "counterparty": cp,
            "tx_hash": ev["transactionHash"],
        })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)


def load_meta(db):
    meta = {}
    for k, v in db.execute("SELECT key, value FROM match_meta"):
        try:
            meta[k] = json.loads(v)
        except Exception:
            meta[k] = v
    return meta


def mid_lookup_factory(db, token_ids):
    """Return fast mid/bid/ask lookup function keyed by (asset_id, ts_s)."""
    cache = {}
    for tok in token_ids:
        rows = list(db.execute(
            "SELECT local_ts_ms/1000, mid_price, bid1_p, ask1_p, spread, "
            "total_bid_depth, total_ask_depth FROM book_snapshots "
            "WHERE asset_id=? ORDER BY local_ts_ms", (tok,)
        ))
        cache[tok] = {
            "ts": [r[0] for r in rows],
            "mid": [r[1] for r in rows],
            "bid": [r[2] for r in rows],
            "ask": [r[3] for r in rows],
            "spread": [r[4] for r in rows],
            "bid_d": [r[5] for r in rows],
            "ask_d": [r[6] for r in rows],
        }

    def lookup(asset_id, ts_s):
        c = cache.get(asset_id)
        if not c or not c["ts"]:
            return None
        idx = bisect_left(c["ts"], ts_s) - 1
        if idx < 0:
            return None
        return {
            "mid": c["mid"][idx], "bid": c["bid"][idx], "ask": c["ask"][idx],
            "spread": c["spread"][idx], "bid_depth": c["bid_d"][idx],
            "ask_depth": c["ask_d"][idx], "snap_ts": c["ts"][idx],
        }
    return lookup, cache


def detect_market_events(cache, min_move_bp=150, hold_window_s=30):
    """Detect sustained mid moves: mid(t+hold) - mid(t) >= min_move_bp.
    Returns list of events (asset_id, ts, mid_before, mid_after, magnitude).
    """
    events = []
    for asset_id, c in cache.items():
        ts_arr = c["ts"]
        mid_arr = c["mid"]
        if len(ts_arr) < 100:
            continue
        # Sample every ~5 seconds to speed up
        step = max(1, int(len(ts_arr) / max(1, (ts_arr[-1] - ts_arr[0]) / 5)))
        i = 0
        while i < len(ts_arr) - 10:
            base_mid = mid_arr[i]
            if base_mid is None or base_mid < 0.01 or base_mid > 0.99:
                i += step
                continue
            target_ts = ts_arr[i] + hold_window_s
            j = bisect_left(ts_arr, target_ts)
            if j >= len(ts_arr):
                break
            post_mid = mid_arr[j]
            if post_mid is None:
                i += step
                continue
            # basis points move vs base
            diff = post_mid - base_mid
            # threshold scales with mid (absolute move of min_move_bp/10000 or 0.005)
            thresh = max(0.005, base_mid * min_move_bp / 10000)
            if abs(diff) >= thresh:
                events.append({
                    "asset_id": asset_id,
                    "ts": ts_arr[i],
                    "ts_post": ts_arr[j],
                    "mid_before": round(base_mid, 4),
                    "mid_after": round(post_mid, 4),
                    "magnitude": round(diff, 4),
                    "direction": "UP" if diff > 0 else "DOWN",
                })
                i = j  # skip forward past this event
            else:
                i += step
    # Dedupe: an event on asset A (UP) usually has a mirror event on asset B (DOWN)
    # — keep both, but sort
    events.sort(key=lambda e: e["ts"])
    return events


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wallet", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache", default=None)
    ap.add_argument("--min-move-bp", type=int, default=150,
                    help="Min mid move in bp to flag as market event (default 150 = 1.5%)")
    args = ap.parse_args()

    wallet = args.wallet.lower()
    conn = sqlite3.connect(args.db)
    meta = load_meta(conn)
    slug = meta["slug"]
    token_ids = meta["token_ids"]
    outcomes = meta["outcome_names"]
    t2o = dict(zip(token_ids, outcomes))
    o2t = {v: k for k, v in t2o.items()}
    print(f"▸ {slug} — {outcomes}")

    # Cricket events (used as-is, for context annotation only)
    cric = pd.read_sql_query(
        "SELECT local_ts_ms/1000 AS ts, signal_type, runs, wickets, overs, "
        "score_str, innings FROM cricket_events ORDER BY ts", conn,
    )
    print(f"▸ cricket_events: {len(cric)}")

    # Book snapshots → fast lookup + full cache
    mid_at, book_cache = mid_lookup_factory(conn, token_ids)
    n_books = sum(len(c["ts"]) for c in book_cache.values())
    print(f"▸ book snapshots (both assets): {n_books}")

    # Time window for fills
    if not cric.empty:
        t_lo, t_hi = int(cric["ts"].min()) - 7200, int(cric["ts"].max()) + 3600
    else:
        # Use book timestamps
        all_ts = [ts for c in book_cache.values() for ts in c["ts"]]
        t_lo, t_hi = min(all_ts) - 3600, max(all_ts) + 3600

    # Fills
    fills = None
    if args.cache and os.path.exists(args.cache):
        cached = json.load(open(args.cache))
        if cached.get("wallet") == wallet and cached.get("slug") == slug:
            fills = cached["fills"]
            print(f"▸ using cached Goldsky fills: {len(fills)}")
    if fills is None:
        print(f"▸ querying Goldsky for wallet fills {ist(t_lo)} → {ist(t_hi)}")
        fills = fetch_wallet_fills(token_ids, wallet, t_lo, t_hi)
        print(f"▸ Goldsky returned {len(fills)} events")
        if args.cache:
            os.makedirs(os.path.dirname(os.path.abspath(args.cache)), exist_ok=True)
            json.dump({"wallet": wallet, "slug": slug, "fills": fills}, open(args.cache, "w"))

    df = process_fills(fills, token_ids, outcomes, wallet)
    if df.empty:
        sys.exit("No wallet fills.")
    print(f"▸ {len(df)} wallet fills")

    # Enrich with concurrent book state
    enrich = []
    for _, r in df.iterrows():
        b = mid_at(r["asset_id"], r["ts"])
        if b is None or b["mid"] is None:
            enrich.append({"mid": None, "bid": None, "ask": None, "spread": None,
                           "bid_depth": None, "ask_depth": None, "book_age_s": None,
                           "edge_to_mid_c": None})
            continue
        mid = b["mid"]
        if r["side"] == "SELL":
            edge = (r["price"] - mid) * 100
        else:
            edge = (mid - r["price"]) * 100
        enrich.append({"mid": mid, "bid": b["bid"], "ask": b["ask"],
                       "spread": b["spread"], "bid_depth": b["bid_depth"],
                       "ask_depth": b["ask_depth"],
                       "book_age_s": int(r["ts"] - b["snap_ts"]),
                       "edge_to_mid_c": round(edge, 3)})
    df = pd.concat([df, pd.DataFrame(enrich)], axis=1)

    # Directional signed tokens (positive = buy, negative = sell) per outcome
    df["signed_tokens"] = df.apply(
        lambda r: r["tokens"] if r["side"] == "BUY" else -r["tokens"], axis=1
    )
    df["signed_notional"] = df.apply(
        lambda r: r["notional"] if r["side"] == "BUY" else -r["notional"], axis=1
    )

    # Inventory path per outcome (cumulative)
    for outcome in outcomes:
        mask = df["outcome"] == outcome
        df.loc[mask, f"inv_{outcome[:4]}"] = df.loc[mask, "signed_tokens"].cumsum()

    # Mark-to-market PnL (over time): cost basis + (current inv × current mid)
    # Simple approach: cumulative realized cash flow + current inv × last mid
    df["cash_flow"] = -df["signed_notional"]  # buy spends, sell earns
    df["cum_cash"] = df["cash_flow"].cumsum()

    # Inter-fill time gap
    df["gap_s"] = df["ts"].diff().fillna(0).astype(int)

    # Match phases from cricket_events (use as-is)
    phases = {"pre": (0, 10**12), "inn1": None, "break": None, "inn2": None, "post": None}
    if not cric.empty:
        # naive inference: innings 1 is the stretch before a big gap > 600s in events
        cric_ts = cric["ts"].tolist()
        gaps = [(cric_ts[i+1] - cric_ts[i], i) for i in range(len(cric_ts) - 1)]
        gaps.sort(reverse=True)
        # Largest gap is likely the break between innings
        if gaps and gaps[0][0] > 600:
            break_idx = gaps[0][1]
            inn1_end = cric_ts[break_idx]
            inn2_start = cric_ts[break_idx + 1]
            inn1_start = cric_ts[0]
            match_end = cric_ts[-1]
            phases = {
                "pre": (0, inn1_start - 1),
                "inn1": (inn1_start, inn1_end),
                "break": (inn1_end + 1, inn2_start - 1),
                "inn2": (inn2_start, match_end),
                "post": (match_end + 1, 10**12),
            }

    def phase_of(ts):
        for p, rng in phases.items():
            if rng and rng[0] <= ts <= rng[1]:
                return p
        return "pre"

    df["phase"] = df["ts"].apply(phase_of)

    # === ANALYSES ===

    # 1. Order size distribution
    size_dist = df.groupby(pd.cut(
        df["notional"],
        [0, 1, 10, 100, 1000, 10000, 1e9],
        labels=["<$1", "$1-$10", "$10-$100", "$100-$1k", "$1k-$10k", ">$10k"]
    ), observed=True).agg(
        n=("notional", "count"),
        total_notional=("notional", "sum"),
        median_tokens=("tokens", "median")
    ).reset_index()

    # 2. Price level preferences — tick-round histogram
    df["price_tick"] = (df["price"] * 1000).round() / 1000  # nearest 0.1c
    df["price_round"] = (df["price"] * 100).round() / 100   # nearest 1c
    # Is price on a 1-cent round tick?
    df["is_round_1c"] = (df["price"] * 100 - (df["price"] * 100).round()).abs() < 0.001

    # 3. Inter-fill cadence
    gap_stats = df["gap_s"].describe(percentiles=[.1, .25, .5, .75, .9, .99]).to_dict()

    # 4. Phase breakdown
    by_phase = df.groupby(["phase", "role"], observed=True).agg(
        n=("ts", "count"),
        notional=("notional", "sum"),
        avg_size=("notional", "mean"),
        median_edge_c=("edge_to_mid_c", "median"),
    ).reset_index()

    # 5. Effective spread (maker) — edge_to_mid_c stats
    maker_edge = df[df["role"] == "MAKER"]["edge_to_mid_c"].dropna()
    taker_edge = df[df["role"] == "TAKER"]["edge_to_mid_c"].dropna()

    # 6. Side dominance per outcome
    by_outcome_side = df.groupby(["outcome", "role", "side"], observed=True).agg(
        n=("ts", "count"),
        notional=("notional", "sum"),
    ).reset_index()

    # 7. Counterparty concentration
    counterparty_dist = df[df["counterparty"] != CTF].groupby("counterparty").agg(
        n=("ts", "count"),
        notional=("notional", "sum"),
    ).reset_index().sort_values("notional", ascending=False).head(30)
    # Also show: what fraction of fills had CTF as counterparty (aggregated matching)
    ctf_frac = (df["counterparty"] == CTF).mean() * 100

    # 8. Market events detected from book
    print(f"▸ detecting market events (min move {args.min_move_bp}bp)…")
    events = detect_market_events(book_cache, min_move_bp=args.min_move_bp,
                                   hold_window_s=30)
    print(f"▸ found {len(events)} market events")
    events_df = pd.DataFrame(events)
    if not events_df.empty:
        events_df["time_ist"] = events_df["ts"].apply(ist)
        events_df["outcome"] = events_df["asset_id"].apply(lambda a: t2o.get(a, a[:10]))
        # Collapse mirror events on the two tokens: keep only the one where mid went UP
        # (less than 0.5 → UP means outcome became more likely)
        events_df = events_df[events_df["direction"] == "UP"].reset_index(drop=True)
        # Annotate with nearest cricket event
        if not cric.empty:
            def nearest_cric(ts):
                idx = (cric["ts"] - ts).abs().idxmin()
                r = cric.loc[idx]
                return f'{r["signal_type"]} {r["score_str"]} Δ={int(r["ts"]-ts)}s'
            events_df["nearest_cricket"] = events_df["ts"].apply(nearest_cric)
    print(f"▸ market events (UP direction, dedup): {len(events_df)}")

    # 9. Wallet activity in market-event windows (±30s)
    mkt_event_window = []
    if not events_df.empty:
        for _, ev in events_df.iterrows():
            win = df[(df["ts"] >= ev["ts"] - 30) & (df["ts"] <= ev["ts_post"] + 30)]
            pre = win[win["ts"] < ev["ts"]]
            at = win[(win["ts"] >= ev["ts"]) & (win["ts"] <= ev["ts_post"])]
            post = win[win["ts"] > ev["ts_post"]]
            # Wallet's signed notional on the UP-direction outcome
            up_out = ev["outcome"]
            def signed_on(df_sub, oc):
                s = df_sub[df_sub["outcome"] == oc]
                return s.loc[s["side"]=="BUY","notional"].sum() - s.loc[s["side"]=="SELL","notional"].sum()
            mkt_event_window.append({
                "event_ist": ev["time_ist"],
                "event_ts": int(ev["ts"]),
                "outcome_up": up_out,
                "magnitude": ev["magnitude"],
                "mid_before": ev["mid_before"],
                "mid_after": ev["mid_after"],
                "nearest_cricket": ev.get("nearest_cricket", ""),
                "pre_fills": len(pre),
                "at_fills": len(at),
                "post_fills": len(post),
                "pre_on_up_signed_notional": round(signed_on(pre, up_out), 2),
                "at_on_up_signed_notional": round(signed_on(at, up_out), 2),
                "post_on_up_signed_notional": round(signed_on(post, up_out), 2),
                "pre_maker_pct": round(100 * (pre["role"]=="MAKER").mean(), 1) if len(pre) else None,
                "at_maker_pct": round(100 * (at["role"]=="MAKER").mean(), 1) if len(at) else None,
                "post_maker_pct": round(100 * (post["role"]=="MAKER").mean(), 1) if len(post) else None,
            })
    mew_df = pd.DataFrame(mkt_event_window)

    # 10. Top aggressive taker counterparties (who sends them the flow when they maker-sell?)
    cp_when_maker = df[(df["role"] == "MAKER") & (df["counterparty"] != CTF)]
    top_cp_maker = cp_when_maker.groupby("counterparty").agg(
        n=("ts", "count"),
        notional=("notional", "sum"),
        median_mid_edge_c=("edge_to_mid_c", "median"),
    ).reset_index().sort_values("notional", ascending=False).head(20)

    # === SUMMARY ===
    summary = {
        "wallet": wallet,
        "slug": slug,
        "outcomes": " / ".join(outcomes),
        "fills_total": len(df),
        "maker_fills": int((df["role"]=="MAKER").sum()),
        "taker_fills": int((df["role"]=="TAKER").sum()),
        "maker_pct": round(100 * (df["role"]=="MAKER").mean(), 2),
        "total_notional": round(df["notional"].sum(), 2),
        "maker_notional": round(df.loc[df["role"]=="MAKER","notional"].sum(), 2),
        "taker_notional": round(df.loc[df["role"]=="TAKER","notional"].sum(), 2),
        "median_fill_size_$": round(df["notional"].median(), 2),
        "p90_fill_size_$": round(df["notional"].quantile(0.9), 2),
        "p99_fill_size_$": round(df["notional"].quantile(0.99), 2),
        "max_fill_size_$": round(df["notional"].max(), 2),
        "round_1c_price_pct": round(100 * df["is_round_1c"].mean(), 2),
        "ctf_counterparty_pct": round(ctf_frac, 2),
        "maker_edge_median_c": round(maker_edge.median(), 3) if not maker_edge.empty else None,
        "maker_edge_mean_c": round(maker_edge.mean(), 3) if not maker_edge.empty else None,
        "maker_edge_q10_c": round(maker_edge.quantile(0.1), 3) if not maker_edge.empty else None,
        "maker_edge_q90_c": round(maker_edge.quantile(0.9), 3) if not maker_edge.empty else None,
        "taker_edge_median_c": round(taker_edge.median(), 3) if not taker_edge.empty else None,
        "fill_gap_median_s": int(gap_stats.get("50%", 0)),
        "fill_gap_p90_s": int(gap_stats.get("90%", 0)),
        "market_events_detected": len(events_df),
        "market_events_wallet_had_pre_fills": int((mew_df["pre_fills"] > 0).sum()) if not mew_df.empty else 0,
        "market_events_wallet_had_at_fills": int((mew_df["at_fills"] > 0).sum()) if not mew_df.empty else 0,
        "first_fill_ist": df.iloc[0]["time_ist"],
        "last_fill_ist": df.iloc[-1]["time_ist"],
        "net_token_A": round(df.loc[df["outcome"]==outcomes[0], "signed_tokens"].sum(), 2),
        "net_token_B": round(df.loc[df["outcome"]==outcomes[1], "signed_tokens"].sum(), 2),
        "cum_cash_flow_$": round(df["cash_flow"].sum(), 2),
    }

    # Write output
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with pd.ExcelWriter(args.out, engine="openpyxl") as xl:
        pd.DataFrame([summary]).T.reset_index().rename(
            columns={"index": "metric", 0: "value"}
        ).to_excel(xl, sheet_name="profile", index=False)
        df.to_excel(xl, sheet_name="fills", index=False)
        size_dist.to_excel(xl, sheet_name="size_dist", index=False)
        by_phase.to_excel(xl, sheet_name="by_phase", index=False)
        by_outcome_side.to_excel(xl, sheet_name="by_outcome_side", index=False)
        counterparty_dist.to_excel(xl, sheet_name="all_counterparties", index=False)
        top_cp_maker.to_excel(xl, sheet_name="top_cp_when_maker", index=False)
        events_df.to_excel(xl, sheet_name="market_events", index=False)
        mew_df.to_excel(xl, sheet_name="wallet_around_market_events", index=False)

    print("\n══════ PROFILE SUMMARY ══════")
    for k, v in summary.items():
        print(f"  {k:40} = {v}")
    print(f"\n✓ wrote {args.out}")


if __name__ == "__main__":
    main()
