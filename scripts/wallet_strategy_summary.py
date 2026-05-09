#!/usr/bin/env python3
"""
Aggregate wallet strategy summary across all analyzed matches.
Consolidates per-match outputs from wallet_event_edge.py and wallet_lead_lag.py
into a single Excel with quantitative classification evidence.
"""
import os
import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta

import pandas as pd

IST = timezone(timedelta(hours=5, minutes=30))


def ist(t):
    return datetime.fromtimestamp(t, tz=timezone.utc).astimezone(IST).strftime("%H:%M:%S")


MATCHES = [
    ("puns", "cricipl-pun-sun-2026-04-11", "captures/match_capture_cricipl-pun-sun-2026-04-11_20260411.db"),
    ("kol",  "cricipl-kol-luc-2026-04-09", "captures/match_capture_cricipl-kol-luc-2026-04-09_20260409.db"),
    ("raj",  "cricipl-raj-roy-2026-04-10", "captures/match_capture_cricipl-raj-roy-2026-04-10_20260410.db"),
    ("che",  "cricipl-che-kol-2026-04-14", "captures/match_capture_cricipl-che-kol-2026-04-14_20260414.db"),
]

WALLET = "0xef51ebb7ed5c84e5049fc76e1ae4db3b5799c0d3"
OUT = "captures/wss_0xef51eb_aggregate.xlsx"


def main():
    rows_match = []
    rows_wicket = []
    rows_first_mover = []
    rows_direction = []
    all_takers = []

    for short, slug, db_path in MATCHES:
        edge = f"captures/wef_{short}_0xef51eb.xlsx"
        lag = f"captures/wll_{short}_0xef51eb.xlsx"
        if not (os.path.exists(edge) and os.path.exists(lag)):
            print(f"skip {slug}: missing output files")
            continue

        fills = pd.read_excel(edge, "fills_aligned")
        lag_per = pd.read_excel(lag, "per_wicket")
        lag_sum = pd.read_excel(lag, "SUMMARY").set_index("metric")["value"]

        conn = sqlite3.connect(db_path)
        meta = dict(conn.execute("SELECT key,value FROM match_meta").fetchall())
        tids = json.loads(meta["token_ids"])
        names = json.loads(meta["outcome_names"])
        o2t = dict(zip(names, tids))

        wickets = pd.read_sql_query(
            "SELECT local_ts_ms/1000 AS ts, score_str FROM cricket_events "
            "WHERE signal_type='W' ORDER BY ts", conn
        )

        # Per-match metrics
        takers = fills[fills["role"] == "TAKER"]
        makers = fills[fills["role"] == "MAKER"]

        row = {
            "match": slug,
            "fills_total": len(fills),
            "maker_fills": len(makers),
            "taker_fills": len(takers),
            "maker_pct": round(100 * len(makers) / max(1, len(fills)), 1),
            "total_notional": round(fills["notional"].sum(), 2),
            "maker_notional": round(makers["notional"].sum(), 2),
            "taker_notional": round(takers["notional"].sum(), 2),
            "n_wickets": len(wickets),
            "wickets_w_pretrades": int(lag_sum.get("wickets_w_wallet_pretrades", 0)),
            "wickets_consistent_bet": int(lag_sum.get("wickets_w_consistent_bet", 0)),
            "wickets_contrary_bet": int(lag_sum.get("wickets_w_contrary_bet", 0)),
            "median_lead_vs_market_s": lag_sum.get("median_lead_vs_market_s"),
            "median_lead_vs_capture_s": lag_sum.get("median_lead_vs_capture_s"),
            "n_wallet_ahead_market": int(lag_sum.get("n_wallet_ahead_of_market", 0)),
            "n_wallet_after_market": int(lag_sum.get("n_wallet_after_market", 0)),
        }
        rows_match.append(row)

        # Per-wicket first-mover + direction
        def mid_at(asset, t):
            r = conn.execute(
                "SELECT mid_price FROM book_snapshots WHERE asset_id=? AND local_ts_ms<=? "
                "ORDER BY local_ts_ms DESC LIMIT 1", (asset, t * 1000)
            ).fetchone()
            return r[0] if r else None

        for _, w in wickets.iterrows():
            wt = int(w["ts"])
            pre = takers[(takers["ts"] >= wt - 60) & (takers["ts"] < wt)]
            # first-mover
            pre_all_trades = conn.execute(
                "SELECT clob_ts_ms/1000, taker_wallet, notional_usdc FROM trades "
                "WHERE clob_ts_ms >= ? AND clob_ts_ms < ? AND notional_usdc >= 50 "
                "ORDER BY clob_ts_ms",
                ((wt - 120) * 1000, wt * 1000)
            ).fetchall()
            first_mover = (pre_all_trades[0][1] or "")[:10] if pre_all_trades else None
            target_is_first = (pre_all_trades[0][1] or "").lower() == WALLET if pre_all_trades else False
            total_pre_vol = sum(r[2] for r in pre_all_trades) if pre_all_trades else 0
            target_vol = sum(r[2] for r in pre_all_trades
                             if (r[1] or "").lower() == WALLET) if pre_all_trades else 0
            target_share = (target_vol / total_pre_vol) if total_pre_vol else 0

            # direction correctness
            mid_delta = {}
            for n, tok in o2t.items():
                base = mid_at(tok, wt - 30)
                post = mid_at(tok, wt + 30)
                mid_delta[n] = (post - base) if (base and post) else 0
            benefit = max(mid_delta, key=lambda k: mid_delta[k])
            loser = min(mid_delta, key=lambda k: mid_delta[k])
            ben_net = (pre.loc[(pre["outcome"] == benefit) & (pre["side"] == "BUY"), "notional"].sum()
                       - pre.loc[(pre["outcome"] == benefit) & (pre["side"] == "SELL"), "notional"].sum())
            los_net = (pre.loc[(pre["outcome"] == loser) & (pre["side"] == "BUY"), "notional"].sum()
                       - pre.loc[(pre["outcome"] == loser) & (pre["side"] == "SELL"), "notional"].sum())
            dir_score = ben_net - los_net

            rows_first_mover.append({
                "match": short,
                "wicket_ist": ist(wt),
                "score": w["score_str"],
                "mid_move_mag": round(mid_delta[benefit] - mid_delta[loser], 4),
                "first_mover": first_mover,
                "target_is_first": target_is_first,
                "target_share_of_pre_vol": round(target_share, 3),
                "n_target_takers_pre": len(pre),
                "target_taker_notional": round(pre["notional"].sum(), 2),
                "target_dir_correct": dir_score > 0,
                "target_dir_score": round(dir_score, 2),
            })

        # Collect all takers (for bucket dist)
        t_ann = takers.copy()
        if len(wickets) > 0:
            wt_list = wickets["ts"].tolist()
            t_ann["delta_to_nearest_wicket_s"] = t_ann["ts"].apply(
                lambda ts: min((ts - wt for wt in wt_list), key=abs)
            )
        t_ann["match"] = short
        all_takers.append(t_ann[["match", "ts", "time_ist", "side", "outcome",
                                  "price", "tokens", "notional",
                                  "delta_to_nearest_wicket_s"]])

    match_df = pd.DataFrame(rows_match)
    first_df = pd.DataFrame(rows_first_mover)
    takers_df = pd.concat(all_takers) if all_takers else pd.DataFrame()

    # Aggregate summary
    total_fills = match_df["fills_total"].sum()
    total_maker = match_df["maker_fills"].sum()
    total_taker = match_df["taker_fills"].sum()

    total_taker_not = match_df["taker_notional"].sum()
    pre_taker_not = takers_df.loc[
        takers_df["delta_to_nearest_wicket_s"].between(-60, -1), "notional"
    ].sum() if not takers_df.empty else 0
    post_taker_not = takers_df.loc[
        takers_df["delta_to_nearest_wicket_s"].between(0, 60), "notional"
    ].sum() if not takers_df.empty else 0

    agg = {
        "wallet": WALLET,
        "matches_analyzed": len(match_df),
        "fills_total": int(total_fills),
        "maker_fills": int(total_maker),
        "taker_fills": int(total_taker),
        "maker_pct_by_count": round(100 * total_maker / max(1, total_fills), 2),
        "total_notional": round(match_df["total_notional"].sum(), 0),
        "taker_notional": round(total_taker_not, 0),
        "taker_notional_pre_wicket_60s": round(pre_taker_not, 0),
        "taker_notional_post_wicket_60s": round(post_taker_not, 0),
        "pre_wicket_concentration_vs_baseline": (
            f"{round(100 * pre_taker_not / max(1, total_taker_not) / 9.4, 2)}x"
            if total_taker_not > 0 else "n/a"
        ),
        "wickets_total": int(match_df["n_wickets"].sum()),
        "wickets_w_pretrades": int(match_df["wickets_w_pretrades"].sum()),
        "pct_wickets_target_was_first_mover": round(
            100 * first_df["target_is_first"].sum() / max(1, len(first_df)), 2
        ),
        "mean_target_share_of_pre_wicket_vol": round(
            100 * first_df["target_share_of_pre_vol"].mean(), 2
        ),
        "pre_wicket_taker_direction_correct_pct": round(
            100 * first_df.loc[first_df["n_target_takers_pre"] > 0, "target_dir_correct"].mean(), 2
        ),
        "median_lead_vs_market_s": float(match_df["median_lead_vs_market_s"].dropna().median()),
        "median_lead_vs_capture_s": float(match_df["median_lead_vs_capture_s"].dropna().median()),
    }

    print("\n══════════════════ AGGREGATE CLASSIFICATION ══════════════════")
    for k, v in agg.items():
        print(f"  {k:48} = {v}")

    # Write output
    with pd.ExcelWriter(OUT, engine="openpyxl") as xl:
        pd.DataFrame([agg]).T.reset_index().rename(
            columns={"index": "metric", 0: "value"}
        ).to_excel(xl, sheet_name="classification", index=False)
        match_df.to_excel(xl, sheet_name="per_match", index=False)
        first_df.to_excel(xl, sheet_name="per_wicket", index=False)
        takers_df.to_excel(xl, sheet_name="all_takers", index=False)

    print(f"\n✓ wrote {OUT}")


if __name__ == "__main__":
    main()
