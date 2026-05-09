"""Layer 2 v2 — multiple calibration strategies, mid-range only (0.10 ≤ t-40 ≤ 0.90).

Each strategy fits on first 13 matches by date, evaluated on last 4. Goal:
0% predicted > actual on test set with lowest mean abs error.

Strategies:
  A) Bucket (event, phase, innings)              [baseline from prior run]
  B) Bucket (event, phase, innings, price_bin)   [adds price-level granularity]
  C) Logit-space DP delta + per-bucket offset    [boundary-aware]
  D) Asymmetric shrinkage by event direction     [+ deltas shrink, − amplify]
  E) Quantile linear regression on residuals     [features: DP_delta, price]
  F) Strict envelope: per-bucket min(residual)   [fit to worst-case]

For each strategy we sweep the safety margin and pick the smallest margin
that yields 0% over-prediction on training, then report test metrics.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

RESID_CSV = Path("/Users/sobhagyaxd/DeepWork/totem-mm/captures/predict_dp_residuals.csv")


def logit(p, eps=1e-6):
    p = np.clip(p, eps, 1 - eps)
    return np.log(p / (1 - p))


def sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def load_data():
    df = pd.read_csv(RESID_CSV)
    df["date"] = df["slug"].str.extract(r"(\d{4}-\d{2}-\d{2})$")[0]
    df["resid_c"] = df["actual_delta_c"] - df["pred_delta_c"]
    # Mid-range filter on t-40
    df = df[(df["t-40"] >= 0.10) & (df["t-40"] <= 0.90)].copy()
    df = df.sort_values(["date", "slug"]).reset_index(drop=True)
    return df


def split(df):
    sorted_dates = sorted(df["date"].unique())
    train_dates = set(sorted_dates[:-4])
    test_dates  = set(sorted_dates[-4:])
    return df[df["date"].isin(train_dates)].copy(), df[df["date"].isin(test_dates)].copy()


def metrics(df, pred_col):
    err = df["actual_delta_c"] - df[pred_col]
    abs_err = err.abs()
    over = (err < 0).mean() * 100
    overshoot = abs_err[err < 0].mean() if (err < 0).any() else 0.0
    return {
        "n": len(df),
        "mean_abs": abs_err.mean(),
        "median_abs": abs_err.median(),
        "p90_abs": abs_err.quantile(0.9),
        "p99_abs": abs_err.quantile(0.99),
        "max_abs": abs_err.max(),
        "pct_over": over,
        "overshoot": overshoot,
    }


def find_zero_over_margin(train_df, base_pred_col):
    """Return the safety margin (cents) such that 0% of train predictions
    over-predict.  i.e., margin = max(pred - actual) for all training rows."""
    over = train_df[base_pred_col] - train_df["actual_delta_c"]
    return max(0.0, float(over.max()))


# ==== Strategy A: bucket (event, phase, innings) ====
def strategy_a(train, test, q):
    by_evpi = {}
    for k, g in train.groupby(["event", "phase", "innings"]):
        if len(g) >= 12:
            by_evpi[k] = g["resid_c"].quantile(q)
    glob = train["resid_c"].quantile(q)
    def pred(df):
        out = df.copy()
        out["off"] = out.apply(lambda r: by_evpi.get(
            (r["event"], r["phase"], int(r["innings"])), glob), axis=1)
        out["pred_l2"] = out["pred_delta_c"] + out["off"]
        return out
    train_p = pred(train); test_p = pred(test)
    margin = find_zero_over_margin(train_p, "pred_l2")
    train_p["pred_l2"] -= margin
    test_p["pred_l2"]  -= margin
    return train_p, test_p, margin


# ==== Strategy B: bucket (event, phase, innings, price_bin) ====
def make_price_bin(p):
    if p < 0.20: return "0.10-0.20"
    if p < 0.35: return "0.20-0.35"
    if p < 0.50: return "0.35-0.50"
    if p < 0.65: return "0.50-0.65"
    if p < 0.80: return "0.65-0.80"
    return "0.80-0.90"


def strategy_b(train, test, q):
    train = train.copy(); test = test.copy()
    train["pbin"] = train["t-40"].apply(make_price_bin)
    test["pbin"]  = test["t-40"].apply(make_price_bin)
    by4 = {}; by3 = {}; by2 = {}; by1 = {}
    for k, g in train.groupby(["event", "phase", "innings", "pbin"]):
        if len(g) >= 8: by4[k] = g["resid_c"].quantile(q)
    for k, g in train.groupby(["event", "phase", "innings"]):
        if len(g) >= 12: by3[k] = g["resid_c"].quantile(q)
    for k, g in train.groupby(["event", "innings"]):
        if len(g) >= 12: by2[k] = g["resid_c"].quantile(q)
    for k, g in train.groupby(["event"]):
        if len(g) >= 12: by1[(k,)] = g["resid_c"].quantile(q)
    glob = train["resid_c"].quantile(q)

    def lookup(r):
        key = (r["event"], r["phase"], int(r["innings"]), r["pbin"])
        if key in by4: return by4[key]
        key3 = (r["event"], r["phase"], int(r["innings"]))
        if key3 in by3: return by3[key3]
        key2 = (r["event"], int(r["innings"]))
        if key2 in by2: return by2[key2]
        if (r["event"],) in by1: return by1[(r["event"],)]
        return glob

    def pred(df):
        out = df.copy()
        out["off"] = out.apply(lookup, axis=1)
        out["pred_l2"] = out["pred_delta_c"] + out["off"]
        return out
    train_p = pred(train); test_p = pred(test)
    margin = find_zero_over_margin(train_p, "pred_l2")
    train_p["pred_l2"] -= margin
    test_p["pred_l2"]  -= margin
    return train_p, test_p, margin


# ==== Strategy C: logit-space DP delta + per-bucket offset ====
def strategy_c(train, test, q):
    train = train.copy(); test = test.copy()
    # Reconstruct original DP-fair WP at t-40 implied by pred_delta_c.
    # But we only have DP delta in cents. Use the DP delta as a logit-space
    # delta scale: convert pred_delta_c to logit-space delta given current
    # t-40 price.
    for d in (train, test):
        d["t40_logit"] = logit(d["t-40"].values)
        d["t_logit"]   = logit(d["t"].values)
        # DP-implied logit prediction: anchor at t-40, add DP delta in
        # PROBABILITY terms then re-logit.
        d["pred_p"] = np.clip(d["t-40"] + d["pred_delta_c"]/100, 0.001, 0.999)
        d["pred_logit"] = logit(d["pred_p"].values)
        # Logit-space residual and actual logit delta
        d["actual_logit_delta"] = d["t_logit"] - d["t40_logit"]
        d["pred_logit_delta"]   = d["pred_logit"] - d["t40_logit"]
        d["resid_logit"] = d["actual_logit_delta"] - d["pred_logit_delta"]

    by_evpi = {}
    for k, g in train.groupby(["event", "phase", "innings"]):
        if len(g) >= 12:
            by_evpi[k] = g["resid_logit"].quantile(q)
    glob = train["resid_logit"].quantile(q)

    def pred(df):
        out = df.copy()
        out["off_logit"] = out.apply(lambda r: by_evpi.get(
            (r["event"], r["phase"], int(r["innings"])), glob), axis=1)
        out["pred_logit_l2"] = out["pred_logit"] + out["off_logit"]
        out["pred_p_l2"] = sigmoid(out["pred_logit_l2"].values)
        out["pred_l2"] = (out["pred_p_l2"] - out["t-40"]) * 100
        return out
    train_p = pred(train); test_p = pred(test)
    margin = find_zero_over_margin(train_p, "pred_l2")
    train_p["pred_l2"] -= margin
    test_p["pred_l2"]  -= margin
    return train_p, test_p, margin


# ==== Strategy D: asymmetric shrinkage by event direction ====
def strategy_d(train, test, q):
    """Per-event multiplier α plus offset β fit to lower-quantile residuals.
    actual_delta ≈ α*DP_delta + β   at the τ-th quantile."""
    train = train.copy(); test = test.copy()
    params = {}
    for ev, g in train.groupby("event"):
        if len(g) < 12:
            continue
        # Solve α, β by quantile regression at q via brute search
        x = g["pred_delta_c"].values
        y = g["actual_delta_c"].values
        best = (None, None, np.inf)
        # Simple grid search; α ∈ [0, 1.5], β ∈ [-15, 5]
        for alpha in np.arange(0.0, 1.51, 0.05):
            for beta in np.arange(-15, 5.01, 0.5):
                resid = y - alpha * x - beta
                # quantile loss
                loss = np.where(resid >= 0, q * resid, (q - 1) * resid).sum()
                if loss < best[2]:
                    best = (alpha, beta, loss)
        params[ev] = best[:2]

    glob_alpha = 1.0
    glob_beta  = train["resid_c"].quantile(q)

    def pred(df):
        out = df.copy()
        def apply(r):
            a, b = params.get(r["event"], (glob_alpha, glob_beta))
            return a * r["pred_delta_c"] + b
        out["pred_l2"] = out.apply(apply, axis=1)
        return out
    train_p = pred(train); test_p = pred(test)
    margin = find_zero_over_margin(train_p, "pred_l2")
    train_p["pred_l2"] -= margin
    test_p["pred_l2"]  -= margin
    return train_p, test_p, margin


# ==== Strategy E: linear quantile regression with multiple features ====
def strategy_e(train, test, q):
    """Fit a single linear model: actual_delta = w·features + b at τ=q.
    Features: DP_delta, t-40, t-40², event one-hot, phase one-hot, innings."""
    train = train.copy(); test = test.copy()
    events = sorted(train["event"].unique())
    phases = sorted(train["phase"].unique())

    def featurize(df):
        feats = [df["pred_delta_c"].values,
                 df["t-40"].values,
                 (df["t-40"].values ** 2),
                 df["innings"].values.astype(float)]
        for e in events:
            feats.append((df["event"] == e).astype(float).values)
        for p in phases:
            feats.append((df["phase"] == p).astype(float).values)
        return np.column_stack(feats + [np.ones(len(df))])

    X_train = featurize(train); y_train = train["actual_delta_c"].values
    X_test = featurize(test)

    # Quantile regression via scipy linprog. Without scipy, use closed-form
    # gradient descent on quantile loss.
    n, d = X_train.shape
    w = np.zeros(d)
    lr = 1e-4
    for it in range(3000):
        pred = X_train @ w
        resid = y_train - pred
        # subgradient of quantile loss
        grad = -X_train.T @ np.where(resid >= 0, q, q - 1) / n
        w -= lr * grad

    train["pred_l2"] = X_train @ w
    test["pred_l2"]  = X_test  @ w
    margin = find_zero_over_margin(train, "pred_l2")
    train["pred_l2"] -= margin
    test["pred_l2"]  -= margin
    return train, test, margin


# ==== Strategy F: strict per-bucket envelope (worst case) ====
def strategy_f(train, test):
    by_evpi = {}
    for k, g in train.groupby(["event", "phase", "innings"]):
        if len(g) >= 5:
            by_evpi[k] = g["resid_c"].min()
    glob = train["resid_c"].min()
    def pred(df):
        out = df.copy()
        out["off"] = out.apply(lambda r: by_evpi.get(
            (r["event"], r["phase"], int(r["innings"])), glob), axis=1)
        out["pred_l2"] = out["pred_delta_c"] + out["off"]
        return out
    return pred(train), pred(test), 0.0


def main() -> int:
    print("Loading and filtering to mid-range (0.10 ≤ t-40 ≤ 0.90)…")
    df = load_data()
    print(f"  Mid-range events: {len(df)} (from {df['slug'].nunique()} matches)")
    train, test = split(df)
    print(f"  Train: {len(train)} events, {train['slug'].nunique()} matches")
    print(f"  Test : {len(test)} events, {test['slug'].nunique()} matches")

    strategies = [
        ("A: (event,phase,inn) bucket  q=0.005", lambda: strategy_a(train, test, 0.005)),
        ("A: (event,phase,inn) bucket  q=0.01 ", lambda: strategy_a(train, test, 0.010)),
        ("B: + price_bin              q=0.005", lambda: strategy_b(train, test, 0.005)),
        ("B: + price_bin              q=0.01 ", lambda: strategy_b(train, test, 0.010)),
        ("B: + price_bin              q=0.025", lambda: strategy_b(train, test, 0.025)),
        ("C: logit-space bucket       q=0.005", lambda: strategy_c(train, test, 0.005)),
        ("C: logit-space bucket       q=0.01 ", lambda: strategy_c(train, test, 0.010)),
        ("D: per-event α,β            q=0.005", lambda: strategy_d(train, test, 0.005)),
        ("D: per-event α,β            q=0.01 ", lambda: strategy_d(train, test, 0.010)),
        ("E: quantile lin-regress     q=0.005", lambda: strategy_e(train, test, 0.005)),
        ("E: quantile lin-regress     q=0.01 ", lambda: strategy_e(train, test, 0.010)),
        ("F: strict envelope (min)            ", lambda: strategy_f(train, test)),
    ]

    print("\n" + "=" * 130)
    print(f"{'strategy':<38}  {'margin':>7}  "
          f"{'TR mean':>7}  {'TR over%':>9}  "
          f"{'TE mean':>7}  {'TE med':>7}  {'TE p90':>7}  {'TE max':>7}  {'TE over%':>9}  {'overshoot':>9}")
    print("-" * 130)
    results = []
    for name, fn in strategies:
        try:
            tr_p, te_p, margin = fn()
        except Exception as exc:
            print(f"{name}  -- FAILED: {exc}")
            continue
        m_tr = metrics(tr_p, "pred_l2")
        m_te = metrics(te_p, "pred_l2")
        results.append((name, margin, m_tr, m_te))
        print(f"{name:<38}  {margin:>7.2f}  "
              f"{m_tr['mean_abs']:>7.2f}  {m_tr['pct_over']:>8.2f}%  "
              f"{m_te['mean_abs']:>7.2f}  {m_te['median_abs']:>7.2f}  "
              f"{m_te['p90_abs']:>7.2f}  {m_te['max_abs']:>7.2f}  "
              f"{m_te['pct_over']:>8.2f}%  {m_te['overshoot']:>9.2f}")

    print("\n=== BEST (lowest test mean_abs with test pct_over ≤ 0.5%) ===")
    eligible = [r for r in results if r[3]["pct_over"] <= 0.5]
    if eligible:
        best = min(eligible, key=lambda r: r[3]["mean_abs"])
        print(f"  {best[0]}  margin={best[1]:.2f}c")
        print(f"    train: mean_abs={best[2]['mean_abs']:.2f}c  pct_over={best[2]['pct_over']:.2f}%")
        print(f"    test : mean_abs={best[3]['mean_abs']:.2f}c  median={best[3]['median_abs']:.2f}c  "
              f"p90={best[3]['p90_abs']:.2f}c  max={best[3]['max_abs']:.2f}c  "
              f"pct_over={best[3]['pct_over']:.2f}%")
    else:
        print("  (no strategy hit ≤ 0.5% test over-prediction)")

    print("\n=== BEST (zero test over-prediction) ===")
    zero = [r for r in results if r[3]["pct_over"] == 0.0]
    if zero:
        best = min(zero, key=lambda r: r[3]["mean_abs"])
        print(f"  {best[0]}  margin={best[1]:.2f}c")
        print(f"    train: mean_abs={best[2]['mean_abs']:.2f}c  pct_over={best[2]['pct_over']:.2f}%")
        print(f"    test : mean_abs={best[3]['mean_abs']:.2f}c  median={best[3]['median_abs']:.2f}c  "
              f"p90={best[3]['p90_abs']:.2f}c  max={best[3]['max_abs']:.2f}c")
    else:
        print("  (none — see closest)")
        closest = sorted(results, key=lambda r: r[3]["pct_over"])[:3]
        for r in closest:
            print(f"    {r[0]}: test pct_over={r[3]['pct_over']:.2f}%  "
                  f"mean_abs={r[3]['mean_abs']:.2f}c")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
