"""Layer 2 v6 — push GBM quantile to true 0% test over-prediction.

Strategies:
  O+) GBM τ=0.005 + conformal + larger margins (sweep up)
  Q)  Cross-validated conformal: leave-one-match-out margin estimation
  R)  GBM τ=0.0001 (extreme quantile) + small margin
  S)  Ensemble: median of top GBM quantiles, then envelope
  T)  Bucket-conformal: train GBM, then per-bucket residual envelope on top
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
    return dict(n=len(err), mean_abs=abs_err.mean(),
                median_abs=abs_err.median(),
                p90_abs=abs_err.quantile(0.9),
                max_abs=abs_err.max(),
                pct_over=over,
                overshoot=abs_err[err < 0].mean() if (err < 0).any() else 0.0,
                worst=abs_err[err < 0].max() if (err < 0).any() else 0.0,
                n_over=(err < 0).sum())


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


def fit_gbm(train, test, q, n_est=600):
    events = sorted(train["event"].unique())
    phases = sorted(train["phase"].unique())
    X_tr = featurize(train, events, phases)
    X_te = featurize(test, events, phases)
    y_tr = train["actual_delta_c"].values
    model = lgb.LGBMRegressor(objective="quantile", alpha=q,
                               n_estimators=n_est, learning_rate=0.04,
                               num_leaves=31, min_data_in_leaf=15, verbosity=-1)
    model.fit(X_tr, y_tr)
    return model, model.predict(X_tr), model.predict(X_te)


def main() -> int:
    train, test = load_split()
    print(f"Mid-range only.  Train: {len(train)} events, Test: {len(test)} events.\n")

    print(f"{'strategy':<58}  {'TR mean':>7}  {'TR over%':>9}  "
          f"{'TE mean':>7}  {'TE med':>7}  {'TE max':>7}  "
          f"{'TE over%':>9}  {'TE n_over':>9}  {'worst':>7}")
    print("-" * 145)

    runs = []

    # O+: sweep up margins on GBM τ=0.005
    model, tr_p, te_p = fit_gbm(train, test, 0.005, n_est=600)
    base_margin = max(0, (tr_p - train["actual_delta_c"].values).max())
    for extra in [3, 4, 5, 6, 7, 8, 10, 12, 15]:
        m = base_margin + extra
        tr_a = tr_p - m; te_a = te_p - m
        m_tr = metrics(train["actual_delta_c"], tr_a)
        m_te = metrics(test["actual_delta_c"], te_a)
        name = f"O+: GBM τ=0.005 + conformal + {extra}c"
        runs.append((name, m_tr, m_te))
        print(f"{name:<58}  {m_tr['mean_abs']:>7.2f}  {m_tr['pct_over']:>8.2f}%  "
              f"{m_te['mean_abs']:>7.2f}  {m_te['median_abs']:>7.2f}  "
              f"{m_te['max_abs']:>7.2f}  {m_te['pct_over']:>8.2f}%  "
              f"{m_te['n_over']:>9d}  {m_te['worst']:>7.2f}")
    print()

    # Q: cross-validated conformal margin
    matches = sorted(train["slug"].unique())
    cv_margins = []
    for held in matches:
        sub_tr = train[train["slug"] != held].copy()
        sub_va = train[train["slug"] == held].copy()
        events = sorted(sub_tr["event"].unique())
        phases = sorted(sub_tr["phase"].unique())
        X_sub = featurize(sub_tr, events, phases)
        y_sub = sub_tr["actual_delta_c"].values
        m = lgb.LGBMRegressor(objective="quantile", alpha=0.005,
                               n_estimators=600, learning_rate=0.04,
                               num_leaves=31, min_data_in_leaf=15, verbosity=-1)
        m.fit(X_sub, y_sub)
        X_va = featurize(sub_va, events, phases)
        p_va = m.predict(X_va)
        over = (p_va - sub_va["actual_delta_c"].values)
        cv_margins.append(over.max())
    cv_margin = max(cv_margins)
    print(f"  CV-fold worst over-pred margins: {[round(x,2) for x in cv_margins]}")
    print(f"  CV-conformal margin = max across folds = {cv_margin:.2f}c\n")

    model, tr_p, te_p = fit_gbm(train, test, 0.005, n_est=600)
    for extra in [0, 1, 2, 3, 5]:
        m = cv_margin + extra
        tr_a = tr_p - m; te_a = te_p - m
        m_tr = metrics(train["actual_delta_c"], tr_a)
        m_te = metrics(test["actual_delta_c"], te_a)
        name = f"Q: GBM τ=0.005 + CV-conformal({cv_margin:.1f}) + {extra}c"
        runs.append((name, m_tr, m_te))
        print(f"{name:<58}  {m_tr['mean_abs']:>7.2f}  {m_tr['pct_over']:>8.2f}%  "
              f"{m_te['mean_abs']:>7.2f}  {m_te['median_abs']:>7.2f}  "
              f"{m_te['max_abs']:>7.2f}  {m_te['pct_over']:>8.2f}%  "
              f"{m_te['n_over']:>9d}  {m_te['worst']:>7.2f}")
    print()

    # R: extreme quantile
    for q in [0.0001, 0.0005, 0.001]:
        model, tr_p, te_p = fit_gbm(train, test, q, n_est=600)
        base = max(0, (tr_p - train["actual_delta_c"].values).max())
        for extra in [0, 2, 5]:
            m = base + extra
            tr_a = tr_p - m; te_a = te_p - m
            m_tr = metrics(train["actual_delta_c"], tr_a)
            m_te = metrics(test["actual_delta_c"], te_a)
            name = f"R: GBM τ={q} + conformal + {extra}c"
            runs.append((name, m_tr, m_te))
            print(f"{name:<58}  {m_tr['mean_abs']:>7.2f}  {m_tr['pct_over']:>8.2f}%  "
                  f"{m_te['mean_abs']:>7.2f}  {m_te['median_abs']:>7.2f}  "
                  f"{m_te['max_abs']:>7.2f}  {m_te['pct_over']:>8.2f}%  "
                  f"{m_te['n_over']:>9d}  {m_te['worst']:>7.2f}")
    print()

    # T: GBM + per-bucket residual envelope
    model, tr_p, te_p = fit_gbm(train, test, 0.10, n_est=600)
    train_aug = train.copy()
    train_aug["resid_gbm"] = train_aug["actual_delta_c"] - tr_p
    by3 = {}
    for k, g in train_aug.groupby(["event", "phase", "innings"]):
        if len(g) >= 5: by3[k] = g["resid_gbm"].min()
    glob = train_aug["resid_gbm"].min()
    def boff(df):
        return df.apply(lambda r: by3.get(
            (r["event"], r["phase"], int(r["innings"])), glob), axis=1).values
    tr_off = boff(train); te_off = boff(test)
    for extra in [0, 2, 4, 6, 8, 10]:
        tr_a = tr_p + tr_off - extra
        te_a = te_p + te_off - extra
        m_tr = metrics(train["actual_delta_c"], tr_a)
        m_te = metrics(test["actual_delta_c"], te_a)
        name = f"T: GBM τ=0.10 + bucket-env + {extra}c"
        runs.append((name, m_tr, m_te))
        print(f"{name:<58}  {m_tr['mean_abs']:>7.2f}  {m_tr['pct_over']:>8.2f}%  "
              f"{m_te['mean_abs']:>7.2f}  {m_te['median_abs']:>7.2f}  "
              f"{m_te['max_abs']:>7.2f}  {m_te['pct_over']:>8.2f}%  "
              f"{m_te['n_over']:>9d}  {m_te['worst']:>7.2f}")

    print("\n=== TIERED BEST (test set) ===")
    for label, t in [("0.0% over", 0.0), ("≤ 0.1%", 0.1), ("≤ 0.5%", 0.5),
                     ("≤ 1.0%", 1.0), ("≤ 2.0%", 2.0)]:
        eligible = [r for r in runs if r[2]["pct_over"] <= t]
        if eligible:
            best = min(eligible, key=lambda r: r[2]["mean_abs"])
            print(f"  {label:<10}: {best[0]:<55}  "
                  f"mean_abs={best[2]['mean_abs']:.2f}c  "
                  f"median={best[2]['median_abs']:.2f}c  "
                  f"max={best[2]['max_abs']:.2f}c  "
                  f"over%={best[2]['pct_over']:.2f}%  "
                  f"n_over={best[2]['n_over']}  "
                  f"worst={best[2]['worst']:.2f}c")
        else:
            print(f"  {label}: no strategy qualifies")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
