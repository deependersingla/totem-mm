"""End-to-end predictor for a single match using:
  1. Build workbook entry for that match (ESPN-aligned timestamps)
  2. Compute DP residuals (Layer 1)
  3. Apply Layer 2 zero-overshoot model (trained on the 17-match workbook)
  4. Output markdown table
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import lightgbm as lgb

THIS = Path(__file__).resolve()
sys.path.insert(0, str(THIS.parent))
sys.path.insert(0, str(THIS.parent.parent / "model-simulation" / "src"))

from dp.solver import DPTable, FirstInningsModel  # type: ignore
from dp.states import (  # type: ignore
    OUTCOMES, OUTCOME_CONSUMES_BALL, OUTCOME_IS_WICKET, OUTCOME_RUNS,
    TransitionProbs,
)
from predict_event_book_dp import InningsOneDP  # type: ignore
from predict_rising_team import RELIABLE_EVENTS, build_training_data, featurize  # type: ignore

CAPTURES = Path("/Users/sobhagyaxd/DeepWork/totem-mm/captures")
EVENT_TO_DP = {
    "0": "dot", "1": "single", "2": "double", "3": "triple",
    "4": "four", "5": None, "6": "six", "W": "wicket",
    "WD": "wide", "NB": "noball", "LB": "single", "B": "single",
}


def overs_to_balls(s):
    s = str(s); whole, _, frac = s.partition('.')
    return int(whole)*6 + (int(frac) if frac else 0)


def parse_score(s):
    m = re.match(r"^(\w+)\s+(\d+)/(\d+)\s+\(([\d.]+)\)", str(s).strip())
    if not m: return None
    return m.group(1), int(m.group(2)), int(m.group(3)), overs_to_balls(m.group(4))


def parse_inn1_meta(s):
    m = re.match(r"^(\w+)\s+(\d+)/(\d+)", str(s).strip())
    if not m: return None
    return m.group(1), int(m.group(2)), int(m.group(3))


def compute_residuals_for_match(workbook_path, slug, dp, inn1_dp):
    """Identical logic to predict_event_book_dp.py but for a single workbook."""
    xl = pd.ExcelFile(workbook_path)
    raw = pd.read_excel(xl, sheet_name=slug, header=3, dtype=object)
    meta = pd.read_excel(xl, sheet_name=slug, header=None, nrows=2, dtype=object)
    team_a = str(meta.iloc[0, 1]).split(":")[-1].strip()
    team_b = str(meta.iloc[0, 2]).split(":")[-1].strip()
    inn1_meta = parse_inn1_meta(str(meta.iloc[1, 0]))
    if inn1_meta is None:
        return None
    inn1_team, inn1_total, _ = inn1_meta
    target = inn1_total + 1

    rows = []
    raw = raw.dropna(subset=["time_ist", "event"])
    for _, r in raw.iterrows():
        ev = str(r["event"])
        dp_outcome = EVENT_TO_DP.get(ev)
        if dp_outcome is None: continue
        try:
            innings = int(r["innings"])
        except Exception:
            continue
        parsed = parse_score(r["score"])
        if parsed is None: continue
        bat_team, runs_post, wkts_post, balls_post = parsed
        runs_pre = runs_post - OUTCOME_RUNS[dp_outcome]
        wkts_pre = wkts_post - (1 if OUTCOME_IS_WICKET[dp_outcome] else 0)
        balls_pre = balls_post - (1 if OUTCOME_CONSUMES_BALL[dp_outcome] else 0)
        if runs_pre < 0 or wkts_pre < 0 or balls_pre < 0: continue
        t_minus_40 = r.get("t-40"); t_actual = r.get("t")
        if pd.isna(t_minus_40) or pd.isna(t_actual): continue
        t_minus_40 = float(t_minus_40); t_actual = float(t_actual)
        balls_rem_pre = 120 - balls_pre
        balls_rem_post = 120 - balls_post
        wkts_hand_pre = 10 - wkts_pre
        wkts_hand_post = 10 - wkts_post
        if innings == 2:
            wp_pre = dp.lookup(balls_rem_pre, target - runs_pre, wkts_hand_pre)
            wp_post = dp.lookup(balls_rem_post, target - runs_post, wkts_hand_post)
        else:
            wp_pre = inn1_dp.lookup(balls_rem_pre, runs_pre, wkts_hand_pre)
            wp_post = inn1_dp.lookup(balls_rem_post, runs_post, wkts_hand_post)
        delta_bat = wp_post - wp_pre
        sign = +1 if bat_team == team_a else -1
        delta_team_a = sign * delta_bat
        predicted = max(0.0, min(1.0, t_minus_40 + delta_team_a))
        phase = "PP" if balls_post <= 36 else ("mid" if balls_post <= 90 else "death")
        rows.append({
            "slug": slug,
            "time_ist": str(r["time_ist"]),
            "score": r["score"],
            "innings": innings,
            "event": ev,
            "phase": phase,
            "bat_is_A": (bat_team == team_a),
            "bat_team": bat_team,
            "team_a": team_a,
            "team_b": team_b,
            "t-40": t_minus_40,
            "t": t_actual,
            "pred": predicted,
            "actual_delta_c": (t_actual - t_minus_40) * 100,
            "pred_delta_c": delta_team_a * 100,
        })
    return pd.DataFrame(rows)


def reframe_rising(df):
    df = df.copy()
    df["dp_rises_A"] = (df["pred_delta_c"] > 0)
    df["dp_rise_c"] = df["pred_delta_c"].abs()
    df["t40_rising"] = np.where(df["dp_rises_A"], df["t-40"], 1.0 - df["t-40"])
    df["actual_rise_c"] = np.where(df["dp_rises_A"],
                                    df["actual_delta_c"], -df["actual_delta_c"])
    df["rising_team"] = np.where(df["dp_rises_A"], df["team_a"], df["team_b"])
    return df


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workbook")
    ap.add_argument("slug")
    ap.add_argument("--safety-extra-c", type=float, default=2.0)
    ap.add_argument("--out-md", default=None)
    args = ap.parse_args()

    print("Solving DPs…")
    dp = DPTable(); dp.solve()
    inn1_model = FirstInningsModel(dp)
    inn1_dp = InningsOneDP(inn1_model); inn1_dp.solve()

    print(f"\nComputing DP residuals for {args.slug}…")
    df_match = compute_residuals_for_match(args.workbook, args.slug, dp, inn1_dp)
    if df_match is None or df_match.empty:
        print("No events to predict.")
        return 1
    print(f"  {len(df_match)} events with DP residuals computed")
    df_match = reframe_rising(df_match)

    # Train Layer 2 on the FULL workbook residuals (17 matches)
    print("\nTraining Layer 2 on 17-match workbook (rising-team coords)…")
    train_full = build_training_data()
    train_mid = train_full[(train_full["t40_rising"] >= 0.10) &
                            (train_full["t40_rising"] <= 0.90) &
                            (train_full["event"].isin(RELIABLE_EVENTS))].copy()
    events = sorted(train_mid["event"].unique())
    phases = sorted(train_mid["phase"].unique())
    print(f"  Training rows: {len(train_mid)}")

    cv_overs = []
    for held in sorted(train_mid["slug"].unique()):
        sub_tr = train_mid[train_mid["slug"] != held]
        sub_va = train_mid[train_mid["slug"] == held]
        if sub_va.empty: continue
        Xt = featurize(sub_tr, events, phases); yt = sub_tr["actual_rise_c"].values
        m = lgb.LGBMRegressor(objective="quantile", alpha=0.005,
                              n_estimators=600, learning_rate=0.04,
                              num_leaves=31, min_data_in_leaf=15, verbosity=-1)
        m.fit(Xt, yt)
        Xv = featurize(sub_va, events, phases)
        cv_overs.append((m.predict(Xv) - sub_va["actual_rise_c"].values).max())
    conformal_c = max(cv_overs)
    margin_c = conformal_c + args.safety_extra_c
    print(f"  LOMO conformal margin = {conformal_c:.2f}c  + safety {args.safety_extra_c}c  = total {margin_c:.2f}c")

    Xf = featurize(train_mid, events, phases)
    final = lgb.LGBMRegressor(objective="quantile", alpha=0.005,
                              n_estimators=600, learning_rate=0.04,
                              num_leaves=31, min_data_in_leaf=15, verbosity=-1)
    final.fit(Xf, train_mid["actual_rise_c"].values)

    # Predict on target match
    df_match["in_range"] = ((df_match["t40_rising"] >= 0.10) &
                             (df_match["t40_rising"] <= 0.90))
    df_match["reliable"] = df_match["event"].isin(RELIABLE_EVENTS)
    pred_mask = df_match["in_range"] & df_match["reliable"]
    pred_df = df_match[pred_mask].copy()
    if not pred_df.empty:
        Xp = featurize(pred_df, events, phases)
        pred_df["pred_rise_c"] = np.maximum(0.0, final.predict(Xp) - margin_c)
        pred_df["pred_rising_t"] = (pred_df["t40_rising"] +
                                     pred_df["pred_rise_c"]/100).clip(0.001, 0.999)
        pred_df["t_rising"] = pred_df["t40_rising"] + pred_df["actual_rise_c"]/100
        pred_df["err_c"] = pred_df["actual_rise_c"] - pred_df["pred_rise_c"]
        pred_df["status"] = np.where(pred_df["err_c"] >= 0, "OK", "OVERSHOOT")

        n = len(pred_df)
        n_over = int((pred_df["err_c"] < 0).sum())
        n_break = int((pred_df["actual_rise_c"] == 0).sum())
        ok_profit = pred_df["actual_rise_c"]
        print(f"\n=== {args.slug} match summary ===")
        print(f"  Predictable events (mid-range, reliable): {n}")
        print(f"  Overshoots: {n_over}/{n} ({n_over/n*100:.2f}%)")
        print(f"  Mean profit per trade: {ok_profit.mean():.2f}c")
        print(f"  Median profit:         {ok_profit.median():.2f}c")
        print(f"  Total profit (1 unit per trade): {ok_profit.sum():.2f}c")
        print(f"  Break-even trades (0c): {n_break}")
    else:
        print("No predictable events.")
        return 0

    # Write markdown
    out = df_match.merge(pred_df[["pred_rise_c", "pred_rising_t", "t_rising",
                                    "err_c", "status"]],
                          left_index=True, right_index=True, how="left")
    fb = np.where(~df_match["in_range"], "NO_TRADE_extreme_price",
                   "NO_TRADE_unreliable_event")
    out["status"] = out["status"].where(out["status"].notna(),
                                          pd.Series(fb, index=out.index))

    md = Path(args.out_md) if args.out_md else CAPTURES / f"predictions_full_{args.slug}.md"
    teams = f"{df_match['team_a'].iloc[0]} (A) vs {df_match['team_b'].iloc[0]} (B)"
    with open(md, "w") as f:
        f.write(f"# {args.slug} — full-pipeline rising-team predictions\n\n")
        f.write(f"Teams: {teams}.  Total margin: **{margin_c:.2f}c** (LOMO "
                f"conformal {conformal_c:.2f}c + safety {args.safety_extra_c}c).  \n")
        f.write(f"Result: **{n_over}/{n} overshoots ({n_over/n*100:.2f}%)**, "
                f"mean profit per trade **{ok_profit.mean():.2f}c**, "
                f"total profit **{ok_profit.sum():.2f}c** over {n} trades.\n\n")
        f.write("Filter: event ∈ {4, 6, NB} AND 0.10 ≤ rising-team t-40 ≤ 0.90.\n\n")
        f.write("| # | time | score | event | inn | bat | rising | t-40 (rising) | "
                "predicted t (rising) | actual t (rising) | pred rise | actual rise | err | status |\n")
        f.write("|---:|---|---|:---:|:---:|:---:|:---:|---:|---:|---:|---:|---:|---:|---|\n")
        for i, r in out.reset_index(drop=True).iterrows():
            pt = f"{r['pred_rising_t']:.3f}" if pd.notna(r.get("pred_rising_t")) else "—"
            tt = f"{r['t_rising']:.3f}" if pd.notna(r.get("t_rising")) else f"{r['t40_rising'] + r['actual_rise_c']/100:.3f}"
            pr = f"{r['pred_rise_c']:.2f}c" if pd.notna(r.get("pred_rise_c")) else "—"
            er = f"{r['err_c']:+.2f}c" if pd.notna(r.get("err_c")) else "—"
            f.write(f"| {i+1} | {r['time_ist']} | {r['score']} | {r['event']} | "
                    f"{r['innings']} | {r['bat_team']} | {r['rising_team']} | "
                    f"{r['t40_rising']:.3f} | {pt} | {tt} | "
                    f"{pr} | {r['actual_rise_c']:+.2f}c | {er} | {r['status']} |\n")
    print(f"\nMarkdown: {md}")
    out.to_csv(CAPTURES / f"predictions_full_{args.slug}.csv", index=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
