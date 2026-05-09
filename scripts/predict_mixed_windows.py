"""Single rising-team predictor that uses different entry/exit windows per
event type:
  - 4/6/NB: t-40 entry, t exit (markets react inside this window)
  - W:      t-50 entry, t-10 exit (markets react earlier — DRS delays the API
                                    tag, ground spectators react instantly)

Trained as one Layer 2 quantile model with one conformal margin.
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
from dp.states import OUTCOME_CONSUMES_BALL, OUTCOME_IS_WICKET, OUTCOME_RUNS  # type: ignore
from predict_event_book_dp import InningsOneDP  # type: ignore

CAPTURES = Path("/Users/sobhagyaxd/DeepWork/totem-mm/captures")
WB = CAPTURES / "team_a_event_book.xlsx"

EVENT_TO_DP = {
    "0": "dot", "1": "single", "2": "double", "3": "triple",
    "4": "four", "5": None, "6": "six", "W": "wicket",
    "WD": "wide", "NB": "noball", "LB": "single", "B": "single",
}

# Per-event window: (entry_col, exit_col)
EVENT_WINDOW = {
    "4":  ("t-40", "t"),
    "6":  ("t-40", "t"),
    "NB": ("t-40", "t"),
    "W":  ("t-50", "t-10"),
}
SIGNAL_EVENTS = list(EVENT_WINDOW.keys())


def overs_to_balls(s):
    s = str(s); whole, _, frac = s.partition(".")
    return int(whole)*6 + (int(frac) if frac else 0)


def parse_score(s):
    m = re.match(r"^(\w+)\s+(\d+)/(\d+)\s+\(([\d.]+)\)", str(s).strip())
    if not m: return None
    return m.group(1), int(m.group(2)), int(m.group(3)), overs_to_balls(m.group(4))


def parse_inn1(s):
    m = re.match(r"^(\w+)\s+(\d+)/(\d+)", str(s).strip())
    if not m: return None
    return m.group(1), int(m.group(2)), int(m.group(3))


def phase_of(balls):
    return "PP" if balls <= 36 else ("mid" if balls <= 90 else "death")


def build_dataset(workbook_path: Path, dp: DPTable, inn1_dp: InningsOneDP) -> pd.DataFrame:
    """Per event: compute DP delta + entry & exit prices using event-specific
    window, in rising-team coords."""
    xl = pd.ExcelFile(workbook_path)
    sheets = [s for s in xl.sheet_names if s != "_summary"]
    rows = []
    for slug in sheets:
        meta = pd.read_excel(xl, sheet_name=slug, header=None, nrows=2, dtype=object)
        team_a = str(meta.iloc[0,1]).split(":")[-1].strip()
        team_b = str(meta.iloc[0,2]).split(":")[-1].strip()
        inn1m = parse_inn1(str(meta.iloc[1,0]))
        if inn1m is None: continue
        _, inn1_total, _ = inn1m
        target = inn1_total + 1

        df = pd.read_excel(xl, sheet_name=slug, header=3, dtype=object).dropna(
            subset=["time_ist", "event"])
        for c in ("t-50","t-40","t-30","t-20","t-10","t","t+10","t+20"):
            df[c] = pd.to_numeric(df[c], errors="coerce")

        for _, r in df.iterrows():
            ev = str(r["event"])
            if ev not in EVENT_WINDOW: continue
            entry_col, exit_col = EVENT_WINDOW[ev]
            if pd.isna(r[entry_col]) or pd.isna(r[exit_col]): continue
            dp_outcome = EVENT_TO_DP.get(ev)
            if dp_outcome is None: continue
            try: innings = int(r["innings"])
            except Exception: continue
            parsed = parse_score(r["score"])
            if parsed is None: continue
            bat_team, runs_post, wkts_post, balls_post = parsed

            # Pre-state from outcome
            runs_pre = runs_post - OUTCOME_RUNS[dp_outcome]
            wkts_pre = wkts_post - (1 if OUTCOME_IS_WICKET[dp_outcome] else 0)
            balls_pre = balls_post - (1 if OUTCOME_CONSUMES_BALL[dp_outcome] else 0)
            if runs_pre < 0 or wkts_pre < 0 or balls_pre < 0: continue

            # DP fair WP for batting team pre & post
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
            delta_bat = wp_post - wp_pre  # batting team WP change

            # Translate to TEAM A coords (sign), then to rising-team coords
            sign_to_A = +1 if bat_team == team_a else -1
            delta_team_a = sign_to_A * delta_bat
            dp_rises_A = (delta_team_a > 0)
            dp_rise_c = abs(delta_team_a) * 100

            # Entry & exit prices (team A coords from workbook, then convert)
            entry_a = float(r[entry_col]); exit_a = float(r[exit_col])
            entry_rising = entry_a if dp_rises_A else (1.0 - entry_a)
            exit_rising  = exit_a  if dp_rises_A else (1.0 - exit_a)
            actual_rise_c = (exit_rising - entry_rising) * 100

            rows.append({
                "slug": slug, "innings": innings, "event": ev,
                "phase": phase_of(balls_post),
                "bat_team": bat_team,
                "team_a": team_a, "team_b": team_b,
                "rising_team": team_a if dp_rises_A else team_b,
                "entry_col": entry_col, "exit_col": exit_col,
                "entry_rising": entry_rising,
                "exit_rising":  exit_rising,
                "dp_rise_c": dp_rise_c,
                "actual_rise_c": actual_rise_c,
                "score": r["score"], "time_ist": r["time_ist"],
            })
    return pd.DataFrame(rows)


def featurize(d, events, phases):
    feats = {
        "dp_rise_c":     d["dp_rise_c"].values,
        "entry_rising":  d["entry_rising"].values,
        "innings":       d["innings"].astype(float).values,
    }
    for e in events: feats[f"ev_{e}"] = (d["event"]==e).astype(float).values
    for p in phases: feats[f"ph_{p}"] = (d["phase"]==p).astype(float).values
    return pd.DataFrame(feats)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--safety-extra-c", type=float, default=2.0)
    ap.add_argument("--out-md", default=None)
    args = ap.parse_args()

    print("Solving DPs…")
    dp = DPTable(); dp.solve()
    inn1_model = FirstInningsModel(dp)
    inn1_dp = InningsOneDP(inn1_model); inn1_dp.solve()

    print("\nBuilding dataset (mixed windows)…")
    df = build_dataset(WB, dp, inn1_dp)
    print(f"  Total events with valid windows: {len(df)}")
    df = df[(df["entry_rising"] >= 0.10) & (df["entry_rising"] <= 0.90)].copy()
    df["date"] = df["slug"].str.extract(r"(\d{4}-\d{2}-\d{2})$")[0]
    print(f"  Mid-range only: {len(df)}")

    # Sanity: direction-mismatch by event type
    print("\nDirection mismatch (% with actual_rise < 0) — should be lowER for W now:")
    for ev in SIGNAL_EVENTS:
        for inn in [1, 2]:
            sub = df[(df["event"]==ev) & (df["innings"]==inn)]
            if len(sub) < 5: continue
            mm = (sub["actual_rise_c"] < 0).mean()*100
            mn = sub["actual_rise_c"].mean()
            print(f"  {ev} inn{inn}: n={len(sub):>3}  mismatch={mm:>5.1f}%  mean_rise={mn:>+5.2f}c")

    # Train Layer 2 with LOMO conformal
    events_list = sorted(df["event"].unique())
    phases_list = sorted(df["phase"].unique())
    matches = sorted(df["slug"].unique())
    cv_overs = []
    for held in matches:
        tr = df[df["slug"]!=held]; va = df[df["slug"]==held]
        if va.empty: continue
        Xt = featurize(tr, events_list, phases_list); yt = tr["actual_rise_c"].values
        m = lgb.LGBMRegressor(objective="quantile", alpha=0.005,
                              n_estimators=600, learning_rate=0.04,
                              num_leaves=31, min_data_in_leaf=15, verbosity=-1)
        m.fit(Xt, yt)
        Xv = featurize(va, events_list, phases_list)
        cv_overs.append((m.predict(Xv) - va["actual_rise_c"].values).max())
    margin_c = max(cv_overs) + args.safety_extra_c
    print(f"\nLOMO conformal: max={max(cv_overs):.2f}c  + safety {args.safety_extra_c}c = total {margin_c:.2f}c")

    # Final model trained on all data (in-sample for evaluation)
    Xf = featurize(df, events_list, phases_list)
    final = lgb.LGBMRegressor(objective="quantile", alpha=0.005,
                              n_estimators=600, learning_rate=0.04,
                              num_leaves=31, min_data_in_leaf=15, verbosity=-1)
    final.fit(Xf, df["actual_rise_c"].values)

    # LOMO-honest predictions for evaluation: predict each match using a model
    # trained on the other 16
    df["pred_rise_c"] = np.nan
    for held in matches:
        tr = df[df["slug"]!=held]; va = df[df["slug"]==held]
        Xt = featurize(tr, events_list, phases_list); yt = tr["actual_rise_c"].values
        m = lgb.LGBMRegressor(objective="quantile", alpha=0.005,
                              n_estimators=600, learning_rate=0.04,
                              num_leaves=31, min_data_in_leaf=15, verbosity=-1)
        m.fit(Xt, yt)
        Xv = featurize(va, events_list, phases_list)
        pred = np.maximum(0.0, m.predict(Xv) - margin_c)
        df.loc[df["slug"]==held, "pred_rise_c"] = pred
    df["err_c"] = df["actual_rise_c"] - df["pred_rise_c"]
    df["fill_price"] = df["entry_rising"] + df["pred_rise_c"]/100
    df["pnl_$"] = 100 * (df["exit_rising"] / df["entry_rising"] - 1)  # bid at entry
    df["pnl_$_atquote"] = 100 * (df["exit_rising"] / df["fill_price"] - 1)  # bid at quote
    df["status"] = np.where(df["err_c"] >= 0, "OK", "OVERSHOOT")

    print("\n" + "="*70)
    print(f"=== PER-EVENT TYPE (LOMO honest, $100/trade @ ENTRY price) ===")
    print("="*70)
    print(f"{'event':<5} {'n':>5} {'overshoots':>11} {'over%':>7} "
          f"{'mean_rise':>10} {'mean_pnl_$':>11} {'total_pnl_$':>12} "
          f"{'wins':>5} {'losses':>7}")
    for ev in SIGNAL_EVENTS:
        sub = df[df["event"]==ev]
        if sub.empty: continue
        n = len(sub); over = (sub["err_c"]<0).sum()
        print(f"{ev:<5} {n:>5} {over:>11} {over/n*100:>6.2f}% "
              f"{sub['actual_rise_c'].mean():>+10.2f} "
              f"{sub['pnl_$'].mean():>+11.2f} "
              f"{sub['pnl_$'].sum():>+12.2f} "
              f"{(sub['actual_rise_c']>0).sum():>5} "
              f"{(sub['actual_rise_c']<0).sum():>7}")

    print("\n=== Wickets by innings (with new t-50→t-10 window) ===")
    for inn in [1, 2]:
        sub = df[(df["event"]=="W") & (df["innings"]==inn)]
        if sub.empty: continue
        n = len(sub); over = (sub["err_c"]<0).sum()
        print(f"  inn {inn}: n={n:>3}  overshoots={over:>3} ({over/n*100:>5.1f}%)  "
              f"mean_rise={sub['actual_rise_c'].mean():>+5.2f}c  "
              f"mean_pnl=${sub['pnl_$'].mean():>+5.2f}  "
              f"total_pnl=${sub['pnl_$'].sum():>+7.2f}  "
              f"win/loss={(sub['actual_rise_c']>0).sum()}/{(sub['actual_rise_c']<0).sum()}")

    print(f"\n=== COMBINED (4/6/NB/W with mixed windows) ===")
    n = len(df); over = (df["err_c"]<0).sum()
    print(f"  Total trades: {n}")
    print(f"  Overshoots:   {over} ({over/n*100:.2f}%)")
    print(f"  Win/loss/flat: {(df['actual_rise_c']>0).sum()} / "
          f"{(df['actual_rise_c']<0).sum()} / "
          f"{(df['actual_rise_c']==0).sum()}")
    print(f"  Total PnL @ $100/trade (entry-priced fills): ${df['pnl_$'].sum():.2f}")
    print(f"  Mean PnL/trade: ${df['pnl_$'].mean():.2f}")
    print(f"  Mean abs error: {df['err_c'].abs().mean():.2f}c")

    # Compare with old (4/6 only, t-40→t)
    only46 = df[df["event"].isin(["4","6"])]
    print(f"\n=== Comparison: 4/6 only (t-40→t) ===")
    print(f"  n={len(only46)}, overshoots={(only46['err_c']<0).sum()}, "
          f"PnL=${only46['pnl_$'].sum():.2f}")

    # Save full markdown
    md = Path(args.out_md) if args.out_md else CAPTURES / "predictions_mixed_windows.md"
    with open(md, "w") as f:
        f.write("# Mixed-window predictor (4/6/NB use t-40→t, W uses t-50→t-10)\n\n")
        f.write(f"LOMO conformal margin: **{margin_c:.2f}c**  "
                f"(worst LOMO over-pred {max(cv_overs):.2f}c + safety {args.safety_extra_c}c).\n\n")
        f.write(f"**Honest LOMO eval — {n} trades, {over} overshoots ({over/n*100:.2f}%), "
                f"total PnL @ $100/trade = ${df['pnl_$'].sum():.2f}**.\n\n")
        f.write("## By event type\n\n")
        f.write("| event | n | overshoots | over% | mean rise | mean PnL | total PnL | wins | losses |\n")
        f.write("|---|---:|---:|---:|---:|---:|---:|---:|---:|\n")
        for ev in SIGNAL_EVENTS:
            sub = df[df["event"]==ev]
            if sub.empty: continue
            f.write(f"| {ev} | {len(sub)} | {(sub['err_c']<0).sum()} | "
                    f"{(sub['err_c']<0).mean()*100:.1f}% | "
                    f"{sub['actual_rise_c'].mean():+.2f}c | "
                    f"${sub['pnl_$'].mean():+.2f} | "
                    f"${sub['pnl_$'].sum():+.2f} | "
                    f"{(sub['actual_rise_c']>0).sum()} | "
                    f"{(sub['actual_rise_c']<0).sum()} |\n")
        f.write("\n## Wickets by innings\n\n")
        f.write("| innings | n | overshoots | over% | mean rise | total PnL |\n")
        f.write("|---|---:|---:|---:|---:|---:|\n")
        for inn in [1, 2]:
            sub = df[(df["event"]=="W") & (df["innings"]==inn)]
            if sub.empty: continue
            f.write(f"| {inn} | {len(sub)} | {(sub['err_c']<0).sum()} | "
                    f"{(sub['err_c']<0).mean()*100:.1f}% | "
                    f"{sub['actual_rise_c'].mean():+.2f}c | "
                    f"${sub['pnl_$'].sum():+.2f} |\n")
        f.write("\n## Per-match PnL\n\n")
        f.write("| match | trades | overshoots | total PnL |\n")
        f.write("|---|---:|---:|---:|\n")
        for slug, sub in df.groupby("slug"):
            f.write(f"| {slug} | {len(sub)} | {(sub['err_c']<0).sum()} | "
                    f"${sub['pnl_$'].sum():+.2f} |\n")
    df.to_csv(CAPTURES / "predictions_mixed_windows.csv", index=False)
    print(f"\nMarkdown: {md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
