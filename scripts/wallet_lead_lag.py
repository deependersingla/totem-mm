#!/usr/bin/env python3
"""
Wallet lead/lag analyzer: for each wicket, compare wallet's first
directional trade vs the market's first mid-price move.

Output per wicket:
  * detected_wicket_ts        — when our dp_monitor saw the wicket
  * mid_first_move_ts         — first timestamp within [-120s, -1s] where
                                 |mid - mid_at_-120s| >= move_bps (per token)
  * wallet_first_trade_ts     — wallet's first directional trade in window
  * wallet_direction          — net outcome BUY/SELL dir ±notional
  * wallet_leads_market_s     — mid_first_move_ts - wallet_first_trade_ts
                                 >0 means wallet acted BEFORE market moved
  * wallet_leads_capture_s    — detected_wicket_ts - wallet_first_trade_ts
                                 >0 means wallet acted before our detection

Writes a summary + per-wicket Excel.
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

import pandas as pd

IST = timezone(timedelta(hours=5, minutes=30))

WINDOW_S = 120            # look back window before wicket
MOVE_BPS = 50             # 50bp (0.5cent when mid ~0.5) threshold for "mid moved"
WALLET_MIN_NOTIONAL = 5   # ignore dust trades


def ist(ts_s):
    return datetime.fromtimestamp(ts_s, tz=timezone.utc).astimezone(IST).strftime("%H:%M:%S")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--edge-xlsx", required=True,
                    help="Output from wallet_event_edge.py")
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    fills = pd.read_excel(args.edge_xlsx, "fills_aligned")
    conn = sqlite3.connect(args.db)
    meta = dict(conn.execute("SELECT key, value FROM match_meta"))
    slug = meta["slug"]
    token_ids = json.loads(meta["token_ids"])
    outcomes = json.loads(meta["outcome_names"])
    t2o = dict(zip(token_ids, outcomes))
    o2t = {v: k for k, v in t2o.items()}

    wickets = pd.read_sql_query(
        "SELECT local_ts_ms/1000 AS ts, score_str, innings, runs, wickets, overs "
        "FROM cricket_events WHERE signal_type='W' ORDER BY ts", conn,
    )
    print(f"▸ {slug}: {len(wickets)} wickets, {len(fills)} fills")

    # Helper: mid at nearest book snapshot for a given asset at a given sec ts
    def mid_at(asset_id, ts_s):
        row = conn.execute(
            "SELECT mid_price, bid1_p, ask1_p FROM book_snapshots "
            "WHERE asset_id=? AND local_ts_ms<=? "
            "ORDER BY local_ts_ms DESC LIMIT 1",
            (asset_id, ts_s * 1000)
        ).fetchone()
        return row[0] if row else None

    # Helper: find first ts in [lo, hi] where mid moved >= move_bps from baseline
    def mid_first_move(asset_id, lo, hi, move_bps):
        baseline = mid_at(asset_id, lo)
        if baseline is None or baseline < 0.001:
            return None, None
        thresh = max(baseline * move_bps / 10000, 0.0005)
        rows = conn.execute(
            "SELECT local_ts_ms/1000, mid_price FROM book_snapshots "
            "WHERE asset_id=? AND local_ts_ms BETWEEN ? AND ? "
            "ORDER BY local_ts_ms",
            (asset_id, lo * 1000, hi * 1000)
        ).fetchall()
        for ts, mid in rows:
            if mid is None:
                continue
            if abs(mid - baseline) >= thresh:
                return ts, mid
        return None, None

    results = []
    for _, w in wickets.iterrows():
        w_ts = int(w["ts"])
        lo, hi = w_ts - WINDOW_S, w_ts - 1

        # Which team is batting? Infer from innings + wicket count progression.
        # For simplicity, track which outcome's price went DOWN around the wicket.
        move_both = {}
        for name, tok in o2t.items():
            base_mid = mid_at(tok, lo)
            post_mid = mid_at(tok, w_ts + 30)
            move_both[name] = {
                "base": base_mid, "post": post_mid,
                "delta": (post_mid - base_mid) if (base_mid and post_mid) else None,
            }

        # Battling team = team with negative delta (their chances drop on wicket)
        # (but for late-game close situations, this can be small / ambiguous)
        deltas = [(n, v["delta"]) for n, v in move_both.items() if v["delta"] is not None]
        batting_team = None
        if deltas:
            # Team with biggest negative delta is the batting team losing the wicket
            batting_team = min(deltas, key=lambda x: x[1])[0]

        # Wallet activity in window
        win = fills[(fills["ts"] >= lo) & (fills["ts"] <= w_ts + 60)].copy()
        pre = win[win["ts"] <= w_ts].copy()
        pre_sig = pre[pre["notional"] >= WALLET_MIN_NOTIONAL].copy()

        # Net wallet position change in pre-window, per outcome
        def signed_notional(df, outcome):
            sub = df[df["outcome"] == outcome]
            buy = sub.loc[sub["side"] == "BUY", "notional"].sum()
            sell = sub.loc[sub["side"] == "SELL", "notional"].sum()
            return round(buy - sell, 2)

        net_by_out = {o: signed_notional(pre_sig, o) for o in outcomes}

        # Wallet's DIRECTIONAL bet on wicket outcome:
        # if wallet was net BUYING the non-batting team (or net SELLING batting team)
        # that's a bet AGAINST batting team = consistent with wicket impact
        wallet_bet_consistent = None
        if batting_team:
            non_batting = [o for o in outcomes if o != batting_team][0]
            # Sign of non_batting position (positive = net buy non_batting = bet on them)
            bet_on_non_batting = net_by_out[non_batting]
            bet_against_batting = -net_by_out[batting_team]
            wallet_net_dir = bet_on_non_batting + bet_against_batting
            wallet_bet_consistent = wallet_net_dir > 0

        # First wallet trade in pre-window (by any outcome, non-dust)
        first_trade_ts = pre_sig["ts"].min() if not pre_sig.empty else None

        # Market mid first move (for non_batting token — the one rising with wicket)
        mid_first_move_ts = None
        mid_move_mag = None
        if batting_team:
            non_batting = [o for o in outcomes if o != batting_team][0]
            tok = o2t[non_batting]
            mid_first_move_ts, mid_val = mid_first_move(tok, lo, hi, MOVE_BPS)
            if mid_first_move_ts:
                base_mid = mid_at(tok, lo)
                mid_move_mag = round(mid_val - base_mid, 4) if base_mid else None

        # Leads
        leads_market_s = (mid_first_move_ts - first_trade_ts) if (first_trade_ts and mid_first_move_ts) else None
        leads_capture_s = (w_ts - first_trade_ts) if first_trade_ts else None

        results.append({
            "wicket_ts": w_ts,
            "wicket_ist": ist(w_ts),
            "score": w["score_str"],
            "batting_team_inferred": batting_team,
            "base_mid_batting": round(move_both.get(batting_team, {}).get("base", 0) or 0, 4) if batting_team else None,
            "post_mid_batting": round(move_both.get(batting_team, {}).get("post", 0) or 0, 4) if batting_team else None,
            "mid_move_batting": round(move_both.get(batting_team, {}).get("delta", 0) or 0, 4) if batting_team else None,
            "n_wallet_pre_trades": len(pre_sig),
            "net_pos_change_team_A": net_by_out.get(outcomes[0]),
            "net_pos_change_team_B": net_by_out.get(outcomes[1]),
            "wallet_bet_consistent_with_wicket": wallet_bet_consistent,
            "first_wallet_trade_ts": first_trade_ts,
            "first_wallet_trade_ist": ist(int(first_trade_ts)) if first_trade_ts else None,
            "mid_first_move_ts": mid_first_move_ts,
            "mid_first_move_ist": ist(int(mid_first_move_ts)) if mid_first_move_ts else None,
            "mid_move_mag": mid_move_mag,
            "wallet_leads_market_s": int(leads_market_s) if leads_market_s else None,
            "wallet_leads_capture_s": int(leads_capture_s) if leads_capture_s else None,
        })

    r_df = pd.DataFrame(results)

    # Aggregate
    agg = {
        "slug": slug,
        "wickets_total": len(r_df),
        "wickets_w_wallet_pretrades": int((r_df["n_wallet_pre_trades"] > 0).sum()),
        "wickets_w_consistent_bet": int((r_df["wallet_bet_consistent_with_wicket"] == True).sum()),
        "wickets_w_contrary_bet": int((r_df["wallet_bet_consistent_with_wicket"] == False).sum()),
        "median_lead_vs_market_s": float(r_df["wallet_leads_market_s"].median()),
        "median_lead_vs_capture_s": float(r_df["wallet_leads_capture_s"].median()),
        "n_wallet_ahead_of_market": int((r_df["wallet_leads_market_s"] > 0).sum()),
        "n_wallet_after_market": int((r_df["wallet_leads_market_s"] < 0).sum()),
        "n_wallet_ahead_of_capture": int((r_df["wallet_leads_capture_s"] > 0).sum()),
    }

    print("\n── SUMMARY ──")
    for k, v in agg.items():
        print(f"  {k}: {v}")

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with pd.ExcelWriter(args.out, engine="openpyxl") as xl:
        pd.DataFrame([agg]).T.reset_index().rename(
            columns={"index": "metric", 0: "value"}
        ).to_excel(xl, sheet_name="SUMMARY", index=False)
        r_df.to_excel(xl, sheet_name="per_wicket", index=False)

    print(f"\n✓ wrote {args.out}")


if __name__ == "__main__":
    main()
