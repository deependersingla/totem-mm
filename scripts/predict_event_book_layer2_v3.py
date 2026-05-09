"""Layer 2 v3 — quantile-based per-bucket calibration on mid-range only,
WITHOUT the worst-case margin shift (which v2 used). Pure quantile fit.

Strategies:
  A) Bucket (event, phase, innings)
  B) Bucket (event, phase, innings, price_bin)
  C) Logit-space bucket (event, phase, innings)
  D) Logit-space bucket (event, phase, innings, price_bin)
  F) Strict per-bucket min residual (envelope)

Sweep quantiles q ∈ {0.001, 0.005, 0.01, 0.025, 0.05}.
"""
from __future__ import annotations

from pathlib import Path
import numpy as np
import pandas as pd

RESID_CSV = Path("/Users/sobhagyaxd/DeepWork/totem-mm/captures/predict_dp_residuals.csv")


def logit(p, eps=1e-6):
    p = np.clip(p, eps, 1 - eps)
    return np.log(p / (1 - p))


def sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def make_pbin(p):
    if p < 0.20: return "0.10-0.20"
    if p < 0.35: return "0.20-0.35"
    if p < 0.50: return "0.35-0.50"
    if p < 0.65: return "0.50-0.65"
    if p < 0.80: return "0.65-0.80"
    return "0.80-0.90"


def load_split():
    df = pd.read_csv(RESID_CSV)
    df["date"] = df["slug"].str.extract(r"(\d{4}-\d{2}-\d{2})$")[0]
    df["resid_c"] = df["actual_delta_c"] - df["pred_delta_c"]
    df["pbin"] = df["t-40"].apply(make_pbin)
    df["t40_logit"] = logit(df["t-40"].values)
    df["t_logit"]   = logit(df["t"].values)
    df["pred_p"]    = np.clip(df["t-40"] + df["pred_delta_c"]/100, 1e-3, 1 - 1e-3)
    df["pred_logit"] = logit(df["pred_p"].values)
    df["actual_logit_delta"] = df["t_logit"] - df["t40_logit"]
    df["pred_logit_delta"]   = df["pred_logit"] - df["t40_logit"]
    df["resid_logit"] = df["actual_logit_delta"] - df["pred_logit_delta"]

    df = df[(df["t-40"] >= 0.10) & (df["t-40"] <= 0.90)].copy()
    df = df.sort_values(["date", "slug"]).reset_index(drop=True)
    sorted_dates = sorted(df["date"].unique())
    train_dates = set(sorted_dates[:-4])
    test_dates  = set(sorted_dates[-4:])
    return df[df["date"].isin(train_dates)].copy(), df[df["date"].isin(test_dates)].copy()


def metrics(actual, pred):
    err = actual - pred
    abs_err = err.abs()
    over = (err < 0).mean() * 100
    overshoot = abs_err[err < 0].mean() if (err < 0).any() else 0.0
    return dict(n=len(err), mean_abs=abs_err.mean(),
                median_abs=abs_err.median(),
                p90_abs=abs_err.quantile(0.9),
                p99_abs=abs_err.quantile(0.99),
                max_abs=abs_err.max(),
                pct_over=over, overshoot=overshoot)


# Strategy A: bucket (event, phase, innings) on raw resid_c
def strat_a(train, test, q):
    by3 = {}
    for k, g in train.groupby(["event", "phase", "innings"]):
        if len(g) >= 12: by3[k] = g["resid_c"].quantile(q)
    by2 = {}
    for k, g in train.groupby(["event", "innings"]):
        if len(g) >= 12: by2[k] = g["resid_c"].quantile(q)
    by1 = {}
    for k, g in train.groupby(["event"]):
        if len(g) >= 12: by1[(k,)] = g["resid_c"].quantile(q)
    glob = train["resid_c"].quantile(q)

    def pred(df):
        def lookup(r):
            k3 = (r["event"], r["phase"], int(r["innings"]))
            if k3 in by3: return by3[k3]
            k2 = (r["event"], int(r["innings"]))
            if k2 in by2: return by2[k2]
            if (r["event"],) in by1: return by1[(r["event"],)]
            return glob
        off = df.apply(lookup, axis=1)
        return df["pred_delta_c"] + off
    return pred(train), pred(test)


# Strategy B: bucket (event, phase, innings, pbin) on raw resid_c
def strat_b(train, test, q):
    by4 = {}
    for k, g in train.groupby(["event", "phase", "innings", "pbin"]):
        if len(g) >= 6: by4[k] = g["resid_c"].quantile(q)
    by3 = {}
    for k, g in train.groupby(["event", "phase", "innings"]):
        if len(g) >= 12: by3[k] = g["resid_c"].quantile(q)
    by2 = {}
    for k, g in train.groupby(["event", "innings"]):
        if len(g) >= 12: by2[k] = g["resid_c"].quantile(q)
    by1 = {}
    for k, g in train.groupby(["event"]):
        if len(g) >= 12: by1[(k,)] = g["resid_c"].quantile(q)
    glob = train["resid_c"].quantile(q)

    def pred(df):
        def lookup(r):
            k4 = (r["event"], r["phase"], int(r["innings"]), r["pbin"])
            if k4 in by4: return by4[k4]
            k3 = (r["event"], r["phase"], int(r["innings"]))
            if k3 in by3: return by3[k3]
            k2 = (r["event"], int(r["innings"]))
            if k2 in by2: return by2[k2]
            if (r["event"],) in by1: return by1[(r["event"],)]
            return glob
        off = df.apply(lookup, axis=1)
        return df["pred_delta_c"] + off
    return pred(train), pred(test)


# Strategy C: logit-space bucket (event, phase, innings)
def strat_c(train, test, q):
    by3 = {}
    for k, g in train.groupby(["event", "phase", "innings"]):
        if len(g) >= 12: by3[k] = g["resid_logit"].quantile(q)
    glob = train["resid_logit"].quantile(q)

    def pred(df):
        def lookup(r):
            k3 = (r["event"], r["phase"], int(r["innings"]))
            return by3.get(k3, glob)
        off = df.apply(lookup, axis=1)
        new_logit = df["pred_logit"] + off
        new_p = sigmoid(new_logit.values)
        return (new_p - df["t-40"]) * 100
    return pred(train), pred(test)


# Strategy D: logit-space bucket (event, phase, innings, pbin)
def strat_d(train, test, q):
    by4 = {}
    for k, g in train.groupby(["event", "phase", "innings", "pbin"]):
        if len(g) >= 6: by4[k] = g["resid_logit"].quantile(q)
    by3 = {}
    for k, g in train.groupby(["event", "phase", "innings"]):
        if len(g) >= 12: by3[k] = g["resid_logit"].quantile(q)
    by1 = {}
    for k, g in train.groupby(["event"]):
        if len(g) >= 12: by1[(k,)] = g["resid_logit"].quantile(q)
    glob = train["resid_logit"].quantile(q)

    def pred(df):
        def lookup(r):
            k4 = (r["event"], r["phase"], int(r["innings"]), r["pbin"])
            if k4 in by4: return by4[k4]
            k3 = (r["event"], r["phase"], int(r["innings"]))
            if k3 in by3: return by3[k3]
            if (r["event"],) in by1: return by1[(r["event"],)]
            return glob
        off = df.apply(lookup, axis=1)
        new_logit = df["pred_logit"] + off
        new_p = sigmoid(new_logit.values)
        return (new_p - df["t-40"]) * 100
    return pred(train), pred(test)


# Strategy F: strict per-bucket min (envelope)
def strat_f(train, test):
    by3 = {}
    for k, g in train.groupby(["event", "phase", "innings"]):
        if len(g) >= 5: by3[k] = g["resid_c"].min()
    glob = train["resid_c"].min()

    def pred(df):
        def lookup(r):
            return by3.get((r["event"], r["phase"], int(r["innings"])), glob)
        off = df.apply(lookup, axis=1)
        return df["pred_delta_c"] + off
    return pred(train), pred(test)


# Strategy G: price-bin strict envelope (min)
def strat_g(train, test):
    by4 = {}
    for k, g in train.groupby(["event", "phase", "innings", "pbin"]):
        if len(g) >= 4: by4[k] = g["resid_c"].min()
    by3 = {}
    for k, g in train.groupby(["event", "phase", "innings"]):
        if len(g) >= 5: by3[k] = g["resid_c"].min()
    glob = train["resid_c"].min()

    def pred(df):
        def lookup(r):
            k4 = (r["event"], r["phase"], int(r["innings"]), r["pbin"])
            if k4 in by4: return by4[k4]
            k3 = (r["event"], r["phase"], int(r["innings"]))
            if k3 in by3: return by3[k3]
            return glob
        off = df.apply(lookup, axis=1)
        return df["pred_delta_c"] + off
    return pred(train), pred(test)


def main() -> int:
    train, test = load_split()
    print(f"Mid-range only.  Train: {len(train)} events, Test: {len(test)} events.\n")

    print(f"{'strategy':<48}  {'TR mean':>7}  {'TR over%':>9}  "
          f"{'TE mean':>7}  {'TE med':>7}  {'TE p90':>7}  {'TE max':>7}  "
          f"{'TE over%':>9}  {'overshoot':>9}")
    print("-" * 130)

    runs = []
    for q in [0.001, 0.005, 0.01, 0.025, 0.05]:
        for label, fn in [
            ("A: 3-key bucket",    lambda q=q: strat_a(train, test, q)),
            ("B: 4-key + pbin",    lambda q=q: strat_b(train, test, q)),
            ("C: logit 3-key",     lambda q=q: strat_c(train, test, q)),
            ("D: logit 4-key",     lambda q=q: strat_d(train, test, q)),
        ]:
            tr_pred, te_pred = fn()
            m_tr = metrics(train["actual_delta_c"], tr_pred)
            m_te = metrics(test["actual_delta_c"], te_pred)
            name = f"{label} q={q}"
            runs.append((name, m_tr, m_te))
            print(f"{name:<48}  {m_tr['mean_abs']:>7.2f}  {m_tr['pct_over']:>8.2f}%  "
                  f"{m_te['mean_abs']:>7.2f}  {m_te['median_abs']:>7.2f}  "
                  f"{m_te['p90_abs']:>7.2f}  {m_te['max_abs']:>7.2f}  "
                  f"{m_te['pct_over']:>8.2f}%  {m_te['overshoot']:>9.2f}")
        print()

    # Strict envelopes
    for label, fn in [
        ("F: strict min 3-key",     lambda: strat_f(train, test)),
        ("G: strict min 4-key+pbin", lambda: strat_g(train, test)),
    ]:
        tr_pred, te_pred = fn()
        m_tr = metrics(train["actual_delta_c"], tr_pred)
        m_te = metrics(test["actual_delta_c"], te_pred)
        runs.append((label, m_tr, m_te))
        print(f"{label:<48}  {m_tr['mean_abs']:>7.2f}  {m_tr['pct_over']:>8.2f}%  "
              f"{m_te['mean_abs']:>7.2f}  {m_te['median_abs']:>7.2f}  "
              f"{m_te['p90_abs']:>7.2f}  {m_te['max_abs']:>7.2f}  "
              f"{m_te['pct_over']:>8.2f}%  {m_te['overshoot']:>9.2f}")

    # Find best at each over% tier
    print("\n=== BEST AT EACH OVER%-TIER (test set) ===")
    for tier_label, tier_max in [
        ("≤ 0.0% over",   0.0),
        ("≤ 0.5% over",   0.5),
        ("≤ 1.0% over",   1.0),
        ("≤ 2.0% over",   2.0),
    ]:
        eligible = [r for r in runs if r[2]["pct_over"] <= tier_max]
        if eligible:
            best = min(eligible, key=lambda r: r[2]["mean_abs"])
            print(f"  {tier_label}: {best[0]:<40}  "
                  f"mean_abs={best[2]['mean_abs']:.2f}c  "
                  f"median={best[2]['median_abs']:.2f}c  "
                  f"max={best[2]['max_abs']:.2f}c  "
                  f"actual over%={best[2]['pct_over']:.2f}%")
        else:
            print(f"  {tier_label}: no strategy qualifies")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
