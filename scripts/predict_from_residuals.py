"""Apply rising-team Layer 2 predictor to a match using the pre-computed
workbook residuals CSV. Same time basis as training (ESPN-aligned), so the
held-out evaluation reproduces match-by-match.

Usage: predict_from_residuals.py <slug>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import lightgbm as lgb

THIS = Path(__file__).resolve()
sys.path.insert(0, str(THIS.parent))
from predict_rising_team import (  # type: ignore
    RELIABLE_EVENTS, build_training_data, featurize, RESID_CSV,
)

CAPTURES = Path("/Users/sobhagyaxd/DeepWork/totem-mm/captures")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("slug")
    ap.add_argument("--safety-extra-c", type=float, default=2.0)
    args = ap.parse_args()

    df_full = build_training_data()
    target_match = df_full[df_full["slug"] == args.slug].copy()
    if target_match.empty:
        print(f"Slug {args.slug} not in residuals CSV. Available:")
        for s in sorted(df_full["slug"].unique()):
            print(f"  {s}")
        return 1

    train = df_full[df_full["slug"] != args.slug].copy()
    train_mid = train[(train["t40_rising"] >= 0.10) &
                      (train["t40_rising"] <= 0.90) &
                      (train["event"].isin(RELIABLE_EVENTS))].copy()
    print(f"Training rows (mid-range, reliable events, leave-{args.slug}-out): "
          f"{len(train_mid)}")

    events = sorted(train_mid["event"].unique())
    phases = sorted(train_mid["phase"].unique())

    # Inner CV for conformal margin (LOMO over the training set)
    cv_overs = []
    for held in sorted(train_mid["slug"].unique()):
        sub_tr = train_mid[train_mid["slug"] != held]
        sub_va = train_mid[train_mid["slug"] == held]
        if sub_va.empty: continue
        Xt = featurize(sub_tr, events, phases)
        yt = sub_tr["actual_rise_c"].values
        m = lgb.LGBMRegressor(objective="quantile", alpha=0.005,
                              n_estimators=600, learning_rate=0.04,
                              num_leaves=31, min_data_in_leaf=15, verbosity=-1)
        m.fit(Xt, yt)
        Xv = featurize(sub_va, events, phases)
        cv_overs.append((m.predict(Xv) - sub_va["actual_rise_c"].values).max())
    conformal_c = max(cv_overs)
    margin_c = conformal_c + args.safety_extra_c
    print(f"  LOMO conformal margin: {conformal_c:.2f}c   "
          f"+ safety {args.safety_extra_c}c   = total {margin_c:.2f}c")

    # Train final on all-but-target
    Xf = featurize(train_mid, events, phases)
    yf = train_mid["actual_rise_c"].values
    final = lgb.LGBMRegressor(objective="quantile", alpha=0.005,
                              n_estimators=600, learning_rate=0.04,
                              num_leaves=31, min_data_in_leaf=15, verbosity=-1)
    final.fit(Xf, yf)

    # Predict on target match — keep ALL events for output, predict only on
    # mid-range reliable ones.
    target_match["in_range"] = ((target_match["t40_rising"] >= 0.10) &
                                 (target_match["t40_rising"] <= 0.90))
    target_match["reliable"] = target_match["event"].isin(RELIABLE_EVENTS)
    pred_mask = target_match["in_range"] & target_match["reliable"]
    pred_df = target_match[pred_mask].copy()
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
        print(f"\n=== {args.slug} match summary ===")
        print(f"  Predictable events (mid-range, reliable): {n}")
        print(f"  Overshoots: {n_over}/{n} ({n_over/n*100:.2f}%)")
        print(f"  Mean abs error: {pred_df['err_c'].abs().mean():.2f}c")
        print(f"  Median abs error: {pred_df['err_c'].abs().median():.2f}c")
        print(f"  Max abs error: {pred_df['err_c'].abs().max():.2f}c")
        if n_over:
            print(f"  Worst overshoot: {(-pred_df['err_c'][pred_df['err_c']<0]).max():.2f}c")
    else:
        print("No predictable events in target match.")
        return 0

    # Merge back & write markdown
    out = target_match.merge(pred_df[["pred_rise_c", "pred_rising_t", "t_rising",
                                       "err_c", "status"]],
                              left_index=True, right_index=True, how="left")
    fallback_status = np.where(~target_match["in_range"], "NO_TRADE_extreme_price",
                                "NO_TRADE_unreliable_event")
    out["status"] = out["status"].where(out["status"].notna(),
                                          pd.Series(fallback_status, index=out.index))
    out["t_rising"] = out["t_rising"].fillna(out["t40_rising"] + out["actual_rise_c"]/100)

    md_path = CAPTURES / f"predictions_{args.slug}.md"
    with open(md_path, "w") as f:
        f.write(f"# {args.slug} — rising-team predictions (zero-overshoot model)\n\n")
        f.write(f"Model: GBM quantile τ=0.005 + LOMO conformal + safety = "
                f"**{margin_c:.2f}c** total margin.\n\n")
        f.write(f"Match held out from training. Result: **{n_over}/{n} overshoots "
                f"({n_over/n*100:.2f}%)**, mean abs error "
                f"**{pred_df['err_c'].abs().mean():.2f}c**.\n\n")
        f.write("Filter: only event ∈ {4, 6, NB} (>99% direction-reliable in training) "
                "AND 0.10 ≤ rising-team t-40 ≤ 0.90.\n\n")
        f.write("| # | event | rising | t-40 (rising) | predicted t (rising) | "
                "actual t (rising) | pred rise (c) | actual rise (c) | err (c) | status |\n")
        f.write("|---:|:---:|:---:|---:|---:|---:|---:|---:|---:|---|\n")
        for i, r in out.reset_index(drop=True).iterrows():
            rt = "A" if r["dp_rises_A"] else "B"
            pt = f"{r['pred_rising_t']:.3f}" if pd.notna(r.get("pred_rising_t")) else "—"
            tt = f"{r['t_rising']:.3f}"
            pr = f"{r['pred_rise_c']:.2f}" if pd.notna(r.get("pred_rise_c")) else "—"
            er = f"{r['err_c']:+.2f}" if pd.notna(r.get("err_c")) else "—"
            f.write(f"| {i+1} | {r['event']} | {rt} | {r['t40_rising']:.3f} | "
                    f"{pt} | {tt} | {pr} | {r['actual_rise_c']:+.2f} | {er} | "
                    f"{r['status']} |\n")
    print(f"\nMarkdown: {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
