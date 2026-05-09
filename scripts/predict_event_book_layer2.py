"""Layer 2 calibrated predictor on top of the DP delta.

Reads the residuals CSV produced by predict_event_book_dp.py and fits a
per-bucket conservative offset so that:
    predicted_delta = DP_delta + offset_bucket
    predicted_delta <= actual_delta  (no false-positive over-predictions)

Bucket key: (event, phase, innings). Sparse buckets fall back to
(event, innings), then (event), then 0 — with a configurable safety margin
in cents subtracted on top.

Two evaluations:
  - in-sample: fit & test on all 17 matches (sanity / upper bound)
  - out-of-sample: fit on first 13 matches by date, test on last 4
"""
from __future__ import annotations

from pathlib import Path
import numpy as np
import pandas as pd

RESID_CSV = Path("/Users/sobhagyaxd/DeepWork/totem-mm/captures/predict_dp_residuals.csv")
SAFETY_MARGIN_C = 0.5  # cents subtracted from offset for out-of-sample buffer
QUANTILE = 0.005       # 0.5% quantile = nearly the min, robust to one outlier
MIN_BUCKET_N = 12      # require this many train rows or fall back


def date_from_slug(slug: str) -> str:
    return slug.rsplit("-", 3)[-3] + "-" + slug.rsplit("-", 3)[-2] + "-" + slug.rsplit("-", 3)[-1]


def fit_offsets(train: pd.DataFrame, q: float, safety_c: float, min_n: int) -> dict:
    """Return nested dict of offsets at three granularity levels."""
    train = train.copy()
    train["resid"] = train["actual_delta_c"] - train["pred_delta_c"]

    by_evpi = {}
    by_evi  = {}
    by_ev   = {}

    g = train.groupby(["event", "phase", "innings"])
    for k, sub in g:
        if len(sub) >= min_n:
            by_evpi[k] = sub["resid"].quantile(q) - safety_c

    g = train.groupby(["event", "innings"])
    for k, sub in g:
        if len(sub) >= min_n:
            by_evi[k] = sub["resid"].quantile(q) - safety_c

    g = train.groupby(["event"])
    for k, sub in g:
        if len(sub) >= min_n:
            by_ev[k] = sub["resid"].quantile(q) - safety_c

    # Global fallback
    global_off = train["resid"].quantile(q) - safety_c

    return {
        "evpi": by_evpi,
        "evi": by_evi,
        "ev": by_ev,
        "global": global_off,
    }


def lookup_offset(offsets: dict, ev: str, phase: str, innings: int) -> float:
    if (ev, phase, innings) in offsets["evpi"]:
        return offsets["evpi"][(ev, phase, innings)]
    if (ev, innings) in offsets["evi"]:
        return offsets["evi"][(ev, innings)]
    if (ev,) in offsets["ev"]:
        return offsets["ev"][(ev,)]
    return offsets["global"]


def evaluate(test: pd.DataFrame, offsets: dict, label: str) -> pd.DataFrame:
    df = test.copy()
    df["offset_c"] = df.apply(
        lambda r: lookup_offset(offsets, r["event"], r["phase"], int(r["innings"])),
        axis=1,
    )
    # Layer 2 prediction in cents: pred_delta_c + offset_c
    df["pred_delta_c_l2"] = df["pred_delta_c"] + df["offset_c"]
    df["error_c_l2"] = df["actual_delta_c"] - df["pred_delta_c_l2"]
    df["abs_err_c_l2"] = df["error_c_l2"].abs()

    over = (df["error_c_l2"] < 0).mean() * 100
    over_mag = df.loc[df["error_c_l2"] < 0, "abs_err_c_l2"].mean()

    print(f"\n=== {label} (n={len(df)}) ===")
    print(f"  Mean signed error  : {df['error_c_l2'].mean():+.3f} c")
    print(f"  Mean absolute error: {df['abs_err_c_l2'].mean():.3f} c")
    print(f"  Median abs error   : {df['abs_err_c_l2'].median():.3f} c")
    print(f"  P90 abs error      : {df['abs_err_c_l2'].quantile(0.90):.3f} c")
    print(f"  P99 abs error      : {df['abs_err_c_l2'].quantile(0.99):.3f} c")
    print(f"  Max abs error      : {df['abs_err_c_l2'].max():.3f} c")
    print(f"  predicted > actual (BAD) : {over:.2f}%   "
          f"avg over-pred magnitude: {over_mag if not np.isnan(over_mag) else 0:.3f} c")
    return df


def main() -> int:
    df = pd.read_csv(RESID_CSV)
    df["date"] = df["slug"].str.extract(r"(\d{4}-\d{2}-\d{2})$")[0]
    df = df.sort_values(["date", "slug"]).reset_index(drop=True)

    print(f"Loaded {len(df)} events from {df['slug'].nunique()} matches "
          f"({df['date'].min()} … {df['date'].max()})")

    # ---- 1. In-sample fit & evaluation (all 17 matches) ----
    print("\n" + "=" * 70)
    print("IN-SAMPLE  (fit & evaluate on all 17 matches)")
    print("=" * 70)
    offsets = fit_offsets(df, QUANTILE, SAFETY_MARGIN_C, MIN_BUCKET_N)
    insamp = evaluate(df, offsets, "in-sample (all 17)")

    # Per-event breakdown in-sample
    print("\n  By event type:")
    g = insamp.groupby("event").agg(
        n=("event", "count"),
        mean_abs=("abs_err_c_l2", "mean"),
        median_abs=("abs_err_c_l2", "median"),
        p90_abs=("abs_err_c_l2", lambda x: x.quantile(0.9)),
        max_abs=("abs_err_c_l2", "max"),
        pct_over=("error_c_l2", lambda x: (x < 0).mean() * 100),
    ).round(3).sort_values("n", ascending=False)
    print(g.to_string())

    # ---- 2. Out-of-sample: fit on first 13 matches, test on last 4 ----
    print("\n" + "=" * 70)
    print("OUT-OF-SAMPLE  (fit on first 13 matches by date, test on last 4)")
    print("=" * 70)
    sorted_dates = sorted(df["date"].unique())
    train_dates = set(sorted_dates[:-4])
    test_dates  = set(sorted_dates[-4:])
    train_df = df[df["date"].isin(train_dates)]
    test_df  = df[df["date"].isin(test_dates)]
    print(f"Train: {len(train_df)} events, {train_df['slug'].nunique()} matches "
          f"({sorted(train_dates)[0]}..{sorted(train_dates)[-1]})")
    print(f"Test : {len(test_df)} events, {test_df['slug'].nunique()} matches "
          f"({sorted(test_dates)[0]}..{sorted(test_dates)[-1]})")

    offsets_oos = fit_offsets(train_df, QUANTILE, SAFETY_MARGIN_C, MIN_BUCKET_N)
    evaluate(train_df, offsets_oos, "training set (in-sample)")
    oos = evaluate(test_df, offsets_oos, "TEST set (held-out)")

    # OOS event breakdown
    print("\n  Held-out test by event type:")
    g = oos.groupby("event").agg(
        n=("event", "count"),
        mean_abs=("abs_err_c_l2", "mean"),
        median_abs=("abs_err_c_l2", "median"),
        max_abs=("abs_err_c_l2", "max"),
        pct_over=("error_c_l2", lambda x: (x < 0).mean() * 100),
    ).round(3).sort_values("n", ascending=False)
    print(g.to_string())

    # ---- 3. Print the fitted offsets so we can read the model ----
    print("\n" + "=" * 70)
    print("FITTED OFFSETS — (event × phase × innings) buckets, training set")
    print("=" * 70)
    rows = []
    for (ev, ph, inn), off in offsets_oos["evpi"].items():
        n_train = len(train_df[(train_df["event"] == ev)
                                & (train_df["phase"] == ph)
                                & (train_df["innings"] == inn)])
        # Mean DP delta + offset for this bucket = mean Layer 2 delta
        mean_dp = train_df[(train_df["event"] == ev) & (train_df["phase"] == ph)
                           & (train_df["innings"] == inn)]["pred_delta_c"].mean()
        mean_actual = train_df[(train_df["event"] == ev) & (train_df["phase"] == ph)
                               & (train_df["innings"] == inn)]["actual_delta_c"].mean()
        rows.append({
            "event": ev, "phase": ph, "innings": inn, "n": n_train,
            "mean_dp_delta_c": round(mean_dp, 2),
            "offset_c": round(off, 2),
            "mean_l2_delta_c": round(mean_dp + off, 2),
            "mean_actual_delta_c": round(mean_actual, 2),
        })
    offset_df = pd.DataFrame(rows).sort_values(["innings", "phase", "event"])
    print(offset_df.to_string(index=False))

    # Save Layer 2 residuals
    out = RESID_CSV.parent / "predict_layer2_residuals.csv"
    insamp.to_csv(out, index=False)
    print(f"\nLayer 2 residuals saved to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
