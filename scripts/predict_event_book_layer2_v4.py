"""Layer 2 v4 — push to 0% test over-prediction. Mid-range only.

Approaches:
  H) Strict envelope (per-bucket min) + global safety margin sweep
  I) Per-bucket min + per-bucket k·std safety
  J) Multi-fold CV: take worst residual across folds (more conservative)
  K) GBM quantile regressor (tight τ)
  L) Stacked: max-conservative of {A q=0.001, F envelope, J cv-min}
"""
from __future__ import annotations

from pathlib import Path
import numpy as np
import pandas as pd

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
                worst_overshoot=abs_err[err < 0].max() if (err < 0).any() else 0.0)


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


# Strategy H: strict envelope per (event, phase, innings) + global margin
def strat_h(train, test, margin_c):
    by3 = {}
    for k, g in train.groupby(["event", "phase", "innings"]):
        if len(g) >= 5:
            by3[k] = g["resid_c"].min()
    glob = train["resid_c"].min()

    def pred(df):
        def lookup(r):
            return by3.get((r["event"], r["phase"], int(r["innings"])), glob)
        off = df.apply(lookup, axis=1)
        return df["pred_delta_c"] + off - margin_c
    return pred(train), pred(test)


# Strategy I: per-bucket min - k * std
def strat_i(train, test, k):
    by3_min = {}; by3_std = {}
    for key, g in train.groupby(["event", "phase", "innings"]):
        if len(g) >= 5:
            by3_min[key] = g["resid_c"].min()
            by3_std[key] = g["resid_c"].std() if len(g) > 1 else 0.0
    glob_min = train["resid_c"].min()
    glob_std = train["resid_c"].std()

    def pred(df):
        def lookup(r):
            key = (r["event"], r["phase"], int(r["innings"]))
            mn = by3_min.get(key, glob_min)
            sd = by3_std.get(key, glob_std)
            return mn - k * sd
        off = df.apply(lookup, axis=1)
        return df["pred_delta_c"] + off
    return pred(train), pred(test)


# Strategy J: leave-one-match-out worst residual, then min across folds
def strat_j(train, test, margin_c):
    """For each (event, phase, innings) bucket, compute the min residual across
    LOMO held-out folds (i.e., the worst residual we'd see if we'd predicted
    this match using the others). Take the min across all folds. This is more
    pessimistic than in-sample min and approximates OOS behavior.
    """
    matches = sorted(train["slug"].unique())
    fold_mins = {}
    for held_out in matches:
        sub = train[train["slug"] != held_out]
        for key, g in sub.groupby(["event", "phase", "innings"]):
            if len(g) >= 5:
                v = g["resid_c"].min()
                fold_mins.setdefault(key, []).append(v)
    by3_pessimistic = {k: min(v) for k, v in fold_mins.items()}
    glob = train["resid_c"].min()

    def pred(df):
        def lookup(r):
            return by3_pessimistic.get(
                (r["event"], r["phase"], int(r["innings"])), glob)
        off = df.apply(lookup, axis=1)
        return df["pred_delta_c"] + off - margin_c
    return pred(train), pred(test)


# Strategy K: GBM quantile regressor (if available)
def strat_k(train, test, q):
    try:
        import lightgbm as lgb
    except Exception:
        return None, None, "lightgbm not available"

    events = sorted(train["event"].unique())
    phases = sorted(train["phase"].unique())

    def featurize(df):
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

    X_train = featurize(train); y_train = train["actual_delta_c"].values
    X_test  = featurize(test)
    model = lgb.LGBMRegressor(objective="quantile", alpha=q,
                               n_estimators=500, learning_rate=0.05,
                               num_leaves=31, min_data_in_leaf=20, verbosity=-1)
    model.fit(X_train, y_train)
    return model.predict(X_train), model.predict(X_test), None


# Strategy L: stacked min of (A q=0.001, F envelope, J)
def strat_l(train, test, margin_c):
    # A q=0.001
    by3_a = {}
    for k, g in train.groupby(["event", "phase", "innings"]):
        if len(g) >= 12: by3_a[k] = g["resid_c"].quantile(0.001)
    glob_a = train["resid_c"].quantile(0.001)
    # F min
    by3_f = {}
    for k, g in train.groupby(["event", "phase", "innings"]):
        if len(g) >= 5: by3_f[k] = g["resid_c"].min()
    glob_f = train["resid_c"].min()
    # J LOMO min
    matches = sorted(train["slug"].unique())
    fold_mins = {}
    for held in matches:
        sub = train[train["slug"] != held]
        for key, g in sub.groupby(["event", "phase", "innings"]):
            if len(g) >= 5:
                fold_mins.setdefault(key, []).append(g["resid_c"].min())
    by3_j = {k: min(v) for k, v in fold_mins.items()}

    def pred(df):
        def lookup(r):
            k3 = (r["event"], r["phase"], int(r["innings"]))
            a = by3_a.get(k3, glob_a)
            f = by3_f.get(k3, glob_f)
            j = by3_j.get(k3, glob_f)
            return min(a, f, j)
        off = df.apply(lookup, axis=1)
        return df["pred_delta_c"] + off - margin_c
    return pred(train), pred(test)


def main() -> int:
    train, test = load_split()
    print(f"Mid-range only.  Train: {len(train)} events, Test: {len(test)} events.\n")

    print(f"{'strategy':<48}  {'TR mean':>7}  {'TR over%':>9}  "
          f"{'TE mean':>7}  {'TE med':>7}  {'TE p90':>7}  {'TE max':>7}  "
          f"{'TE over%':>9}  {'overshoot':>9}  {'worst':>7}")
    print("-" * 140)

    runs = []
    # H: strict envelope + global margin sweep
    for m in [0, 1, 2, 3, 5, 7, 10]:
        tr, te = strat_h(train, test, m)
        m_tr = metrics(train["actual_delta_c"], tr)
        m_te = metrics(test["actual_delta_c"], te)
        name = f"H: strict 3-key + {m}c margin"
        runs.append((name, m_tr, m_te))
        print(f"{name:<48}  {m_tr['mean_abs']:>7.2f}  {m_tr['pct_over']:>8.2f}%  "
              f"{m_te['mean_abs']:>7.2f}  {m_te['median_abs']:>7.2f}  "
              f"{m_te['p90_abs']:>7.2f}  {m_te['max_abs']:>7.2f}  "
              f"{m_te['pct_over']:>8.2f}%  {m_te['overshoot']:>9.2f}  {m_te['worst_overshoot']:>7.2f}")
    print()

    # I: min - k*std
    for k in [0, 1, 2, 3, 5]:
        tr, te = strat_i(train, test, k)
        m_tr = metrics(train["actual_delta_c"], tr)
        m_te = metrics(test["actual_delta_c"], te)
        name = f"I: min - {k}*std"
        runs.append((name, m_tr, m_te))
        print(f"{name:<48}  {m_tr['mean_abs']:>7.2f}  {m_tr['pct_over']:>8.2f}%  "
              f"{m_te['mean_abs']:>7.2f}  {m_te['median_abs']:>7.2f}  "
              f"{m_te['p90_abs']:>7.2f}  {m_te['max_abs']:>7.2f}  "
              f"{m_te['pct_over']:>8.2f}%  {m_te['overshoot']:>9.2f}  {m_te['worst_overshoot']:>7.2f}")
    print()

    # J: LOMO min + margin sweep
    for m in [0, 1, 2, 3, 5]:
        tr, te = strat_j(train, test, m)
        m_tr = metrics(train["actual_delta_c"], tr)
        m_te = metrics(test["actual_delta_c"], te)
        name = f"J: LOMO-min + {m}c margin"
        runs.append((name, m_tr, m_te))
        print(f"{name:<48}  {m_tr['mean_abs']:>7.2f}  {m_tr['pct_over']:>8.2f}%  "
              f"{m_te['mean_abs']:>7.2f}  {m_te['median_abs']:>7.2f}  "
              f"{m_te['p90_abs']:>7.2f}  {m_te['max_abs']:>7.2f}  "
              f"{m_te['pct_over']:>8.2f}%  {m_te['overshoot']:>9.2f}  {m_te['worst_overshoot']:>7.2f}")
    print()

    # K: GBM quantile regressor
    for q in [0.001, 0.005, 0.01]:
        tr, te, err = strat_k(train, test, q)
        if err:
            print(f"K: GBM q={q}  -- {err}")
            continue
        m_tr = metrics(train["actual_delta_c"], tr)
        m_te = metrics(test["actual_delta_c"], te)
        name = f"K: GBM quantile q={q}"
        runs.append((name, m_tr, m_te))
        print(f"{name:<48}  {m_tr['mean_abs']:>7.2f}  {m_tr['pct_over']:>8.2f}%  "
              f"{m_te['mean_abs']:>7.2f}  {m_te['median_abs']:>7.2f}  "
              f"{m_te['p90_abs']:>7.2f}  {m_te['max_abs']:>7.2f}  "
              f"{m_te['pct_over']:>8.2f}%  {m_te['overshoot']:>9.2f}  {m_te['worst_overshoot']:>7.2f}")
    print()

    # L: stacked
    for m in [0, 1, 2, 3, 5]:
        tr, te = strat_l(train, test, m)
        m_tr = metrics(train["actual_delta_c"], tr)
        m_te = metrics(test["actual_delta_c"], te)
        name = f"L: stacked-min + {m}c margin"
        runs.append((name, m_tr, m_te))
        print(f"{name:<48}  {m_tr['mean_abs']:>7.2f}  {m_tr['pct_over']:>8.2f}%  "
              f"{m_te['mean_abs']:>7.2f}  {m_te['median_abs']:>7.2f}  "
              f"{m_te['p90_abs']:>7.2f}  {m_te['max_abs']:>7.2f}  "
              f"{m_te['pct_over']:>8.2f}%  {m_te['overshoot']:>9.2f}  {m_te['worst_overshoot']:>7.2f}")

    print("\n=== BEST AT EACH OVER%-TIER (test set) ===")
    for tier_label, tier_max in [
        ("== 0.0% over",   0.0),
        ("≤ 0.25% over",   0.25),
        ("≤ 0.5% over",    0.5),
        ("≤ 1.0% over",    1.0),
    ]:
        eligible = [r for r in runs if r[2]["pct_over"] <= tier_max]
        if eligible:
            best = min(eligible, key=lambda r: r[2]["mean_abs"])
            print(f"  {tier_label}: {best[0]:<40}  "
                  f"mean_abs={best[2]['mean_abs']:.2f}c  "
                  f"median={best[2]['median_abs']:.2f}c  "
                  f"max={best[2]['max_abs']:.2f}c  "
                  f"actual over%={best[2]['pct_over']:.2f}%  "
                  f"worst overshoot={best[2]['worst_overshoot']:.2f}c")
        else:
            print(f"  {tier_label}: no strategy qualifies")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
