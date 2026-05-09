"""Layer 2 v5 — GBM quantile regressor and conformal envelope.

Approaches added:
  M) LightGBM quantile regressor at very tight τ
  N) Conformal: fit quantile model, then add training-set worst-case residual
  O) GBM quantile + per-bucket conformal margin
  P) Smart: fit GBM quantile, then push down by max(0, sup_train_overpred)
"""
from __future__ import annotations

from pathlib import Path
import numpy as np
import pandas as pd

import lightgbm as lgb

RESID_CSV = Path("/Users/sobhagyaxd/DeepWork/totem-mm/captures/predict_dp_residuals.csv")


def make_pbin(p):
    if p < 0.20: return "0.10-0.20"
    if p < 0.35: return "0.20-0.35"
    if p < 0.50: return "0.35-0.50"
    if p < 0.65: return "0.50-0.65"
    if p < 0.80: return "0.65-0.80"
    return "0.80-0.90"


def metrics(actual, pred):
    err = actual - pred
    abs_err = err.abs()
    over = (err < 0).mean() * 100
    overshoot = abs_err[err < 0].mean() if (err < 0).any() else 0.0
    return dict(n=len(err), mean_abs=abs_err.mean(),
                median_abs=abs_err.median(),
                p90_abs=abs_err.quantile(0.9),
                max_abs=abs_err.max(),
                pct_over=over, overshoot=overshoot,
                worst=abs_err[err < 0].max() if (err < 0).any() else 0.0)


def load_split():
    df = pd.read_csv(RESID_CSV)
    df["date"] = df["slug"].str.extract(r"(\d{4}-\d{2}-\d{2})$")[0]
    df["resid_c"] = df["actual_delta_c"] - df["pred_delta_c"]
    df["pbin"] = df["t-40"].apply(make_pbin)
    df = df[(df["t-40"] >= 0.10) & (df["t-40"] <= 0.90)].copy()
    df = df.sort_values(["date", "slug"]).reset_index(drop=True)
    sorted_dates = sorted(df["date"].unique())
    train_dates = set(sorted_dates[:-4])
    test_dates  = set(sorted_dates[-4:])
    return df[df["date"].isin(train_dates)].copy(), df[df["date"].isin(test_dates)].copy()


def featurize(df, events, phases):
    feats = {
        "pred_delta_c": df["pred_delta_c"].values,
        "t40": df["t-40"].values,
        "innings": df["innings"].values.astype(float),
    }
    for e in events:
        feats[f"ev_{e}"] = (df["event"] == e).astype(float).values
    for p in phases:
        feats[f"ph_{p}"] = (df["phase"] == p).astype(float).values
    return pd.DataFrame(feats)


def gbm_quantile(train, test, q):
    events = sorted(train["event"].unique())
    phases = sorted(train["phase"].unique())
    X_tr = featurize(train, events, phases)
    X_te = featurize(test, events, phases)
    y_tr = train["actual_delta_c"].values
    model = lgb.LGBMRegressor(objective="quantile", alpha=q,
                               n_estimators=600, learning_rate=0.05,
                               num_leaves=31, min_data_in_leaf=20, verbosity=-1)
    model.fit(X_tr, y_tr)
    return model, model.predict(X_tr), model.predict(X_te)


def main() -> int:
    train, test = load_split()
    print(f"Mid-range only.  Train: {len(train)} events, Test: {len(test)} events.\n")

    print(f"{'strategy':<54}  {'TR mean':>7}  {'TR over%':>9}  "
          f"{'TE mean':>7}  {'TE med':>7}  {'TE p90':>7}  {'TE max':>7}  "
          f"{'TE over%':>9}  {'overshoot':>9}  {'worst':>7}")
    print("-" * 145)

    runs = []

    # M: GBM quantile bare
    for q in [0.001, 0.005, 0.01, 0.025, 0.05]:
        model, tr_p, te_p = gbm_quantile(train, test, q)
        m_tr = metrics(train["actual_delta_c"], tr_p)
        m_te = metrics(test["actual_delta_c"], te_p)
        name = f"M: GBM quantile τ={q}"
        runs.append((name, m_tr, m_te, tr_p, te_p))
        print(f"{name:<54}  {m_tr['mean_abs']:>7.2f}  {m_tr['pct_over']:>8.2f}%  "
              f"{m_te['mean_abs']:>7.2f}  {m_te['median_abs']:>7.2f}  "
              f"{m_te['p90_abs']:>7.2f}  {m_te['max_abs']:>7.2f}  "
              f"{m_te['pct_over']:>8.2f}%  {m_te['overshoot']:>9.2f}  {m_te['worst']:>7.2f}")
    print()

    # N: GBM τ=0.05 + conformal margin (max train over-pred shifted away)
    for base_q in [0.05, 0.10, 0.20]:
        model, tr_p, te_p = gbm_quantile(train, test, base_q)
        # conformal: add the training worst over-prediction as margin
        margin = max(0, (tr_p - train["actual_delta_c"].values).max())
        tr_p_adj = tr_p - margin
        te_p_adj = te_p - margin
        m_tr = metrics(train["actual_delta_c"], tr_p_adj)
        m_te = metrics(test["actual_delta_c"], te_p_adj)
        name = f"N: GBM τ={base_q} + conformal margin={margin:.2f}c"
        runs.append((name, m_tr, m_te, tr_p_adj, te_p_adj))
        print(f"{name:<54}  {m_tr['mean_abs']:>7.2f}  {m_tr['pct_over']:>8.2f}%  "
              f"{m_te['mean_abs']:>7.2f}  {m_te['median_abs']:>7.2f}  "
              f"{m_te['p90_abs']:>7.2f}  {m_te['max_abs']:>7.2f}  "
              f"{m_te['pct_over']:>8.2f}%  {m_te['overshoot']:>9.2f}  {m_te['worst']:>7.2f}")
    print()

    # O: GBM τ=0.005 + small additional margin
    model, tr_p, te_p = gbm_quantile(train, test, 0.005)
    for extra in [0, 1, 2, 3, 5]:
        margin = max(0, (tr_p - train["actual_delta_c"].values).max()) + extra
        tr_p_adj = tr_p - margin
        te_p_adj = te_p - margin
        m_tr = metrics(train["actual_delta_c"], tr_p_adj)
        m_te = metrics(test["actual_delta_c"], te_p_adj)
        name = f"O: GBM τ=0.005 + conformal + {extra}c"
        runs.append((name, m_tr, m_te, tr_p_adj, te_p_adj))
        print(f"{name:<54}  {m_tr['mean_abs']:>7.2f}  {m_tr['pct_over']:>8.2f}%  "
              f"{m_te['mean_abs']:>7.2f}  {m_te['median_abs']:>7.2f}  "
              f"{m_te['p90_abs']:>7.2f}  {m_te['max_abs']:>7.2f}  "
              f"{m_te['pct_over']:>8.2f}%  {m_te['overshoot']:>9.2f}  {m_te['worst']:>7.2f}")
    print()

    # P: GBM τ=0.10 (median-ish) + per-bucket residual envelope
    model, tr_p, te_p = gbm_quantile(train, test, 0.10)
    train_aug = train.copy()
    train_aug["resid_gbm"] = train_aug["actual_delta_c"] - tr_p
    by3 = {}
    for k, g in train_aug.groupby(["event", "phase", "innings"]):
        if len(g) >= 5:
            by3[k] = g["resid_gbm"].min()
    glob = train_aug["resid_gbm"].min()
    def bucket_off(df):
        return df.apply(lambda r: by3.get(
            (r["event"], r["phase"], int(r["innings"])), glob), axis=1).values
    tr_off = bucket_off(train); te_off = bucket_off(test)
    for extra in [0, 1, 2, 3]:
        tr_p_adj = tr_p + tr_off - extra
        te_p_adj = te_p + te_off - extra
        m_tr = metrics(train["actual_delta_c"], tr_p_adj)
        m_te = metrics(test["actual_delta_c"], te_p_adj)
        name = f"P: GBM τ=0.10 + bucket-min + {extra}c"
        runs.append((name, m_tr, m_te, tr_p_adj, te_p_adj))
        print(f"{name:<54}  {m_tr['mean_abs']:>7.2f}  {m_tr['pct_over']:>8.2f}%  "
              f"{m_te['mean_abs']:>7.2f}  {m_te['median_abs']:>7.2f}  "
              f"{m_te['p90_abs']:>7.2f}  {m_te['max_abs']:>7.2f}  "
              f"{m_te['pct_over']:>8.2f}%  {m_te['overshoot']:>9.2f}  {m_te['worst']:>7.2f}")

    print("\n=== BEST AT EACH OVER%-TIER (test set) ===")
    for tier_label, tier_max in [
        ("== 0.0% over",   0.0),
        ("≤ 0.1% over",    0.1),
        ("≤ 0.5% over",    0.5),
        ("≤ 1.0% over",    1.0),
        ("≤ 2.0% over",    2.0),
    ]:
        eligible = [r for r in runs if r[2]["pct_over"] <= tier_max]
        if eligible:
            best = min(eligible, key=lambda r: r[2]["mean_abs"])
            print(f"  {tier_label}: {best[0]:<50}  "
                  f"mean_abs={best[2]['mean_abs']:.2f}c  "
                  f"median={best[2]['median_abs']:.2f}c  "
                  f"max={best[2]['max_abs']:.2f}c  "
                  f"over%={best[2]['pct_over']:.2f}%  "
                  f"worst={best[2]['worst']:.2f}c")
        else:
            print(f"  {tier_label}: no strategy qualifies")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
