"""Apply the Layer 2 zero-overshoot predictor to a single live match.

For a given match DB:
  - Read cricket_events ball-by-ball
  - Look up book bid1_p at t-40 and t for one outcome token (team A)
  - Compute DP delta (innings 1 / chase) for batting team perspective
  - Apply GBM τ=0.005 + CV-conformal margin + safety_extra
    so that predicted ≤ actual on training was 0% (and held-out 0%)
  - Output a markdown table: time, score, event, t-40, predicted_t, actual_t,
    err_c, status

Usage: predict_match_zero_overshoot.py <slug> [--team-a DC|RCB|...]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bisect
import numpy as np
import pandas as pd
import lightgbm as lgb

THIS = Path(__file__).resolve()
sys.path.insert(0, str(THIS.parent.parent / "model-simulation" / "src"))
from dp.solver import DPTable, FirstInningsModel  # type: ignore
from dp.states import (  # type: ignore
    OUTCOMES, OUTCOME_CONSUMES_BALL, OUTCOME_IS_WICKET, OUTCOME_RUNS,
    TransitionProbs,
)

CAPTURES = Path("/Users/sobhagyaxd/DeepWork/totem-mm/captures")
RESID_CSV = CAPTURES / "predict_dp_residuals.csv"
IST = timezone(timedelta(hours=5, minutes=30))

NAME_TO_SHORT = {
    "Chennai Super Kings": "CSK", "Kolkata Knight Riders": "KKR",
    "Mumbai Indians": "MI", "Royal Challengers Bengaluru": "RCB",
    "Royal Challengers Bangalore": "RCB",
    "Rajasthan Royals": "RR", "Delhi Capitals": "DC",
    "Gujarat Titans": "GT", "Lucknow Super Giants": "LSG",
    "Punjab Kings": "PBKS", "Sunrisers Hyderabad": "SRH",
}

EVENT_TO_DP = {
    "0": "dot", "1": "single", "2": "double", "3": "triple",
    "4": "four", "5": None, "6": "six", "W": "wicket",
    "WD": "wide", "NB": "noball", "LB": "single", "B": "single",
}


# ---------- innings-1 DP (same as in predict_event_book_dp.py) ----------
class InningsOneDP:
    MAX_BALLS = 120; MAX_RUNS = 300; MAX_WICKETS = 10
    def __init__(self, inn1_model: FirstInningsModel):
        self.inn1 = inn1_model
        shape = (self.MAX_BALLS+1, self.MAX_RUNS+1, self.MAX_WICKETS+1)
        self.table = np.zeros(shape, dtype=np.float32)
        self._terminals = np.array(
            [self.inn1.win_prob_at_total(r) for r in range(self.MAX_RUNS+1)],
            dtype=np.float32)
    def lookup(self, b, r, w):
        b = max(0, min(self.MAX_BALLS, b))
        r = max(0, min(self.MAX_RUNS, r))
        w = max(0, min(self.MAX_WICKETS, w))
        return float(self.table[b, r, w])
    def solve(self):
        self.table[0, :, :] = self._terminals[:, None]
        self.table[:, :, 0] = self._terminals[None, :]
        for b in range(1, self.MAX_BALLS+1):
            ob = (self.MAX_BALLS - b) // 6
            phase = ("powerplay" if ob < 6 else "middle" if ob < 15 else "death")
            tp = TransitionProbs.from_phase_averages(phase)
            probs = tp.as_dict()
            for w in range(1, self.MAX_WICKETS+1):
                vals = np.zeros(self.MAX_RUNS+1, dtype=np.float32)
                for outcome in OUTCOMES:
                    p = probs[outcome]
                    if p <= 0: continue
                    runs = OUTCOME_RUNS[outcome]
                    consumes = OUTCOME_CONSUMES_BALL[outcome]
                    is_wicket = OUTCOME_IS_WICKET[outcome]
                    new_b = b - (1 if consumes else 0)
                    new_w = w - (1 if is_wicket else 0)
                    if new_w <= 0 or new_b <= 0:
                        for r in range(self.MAX_RUNS+1):
                            tgt = min(self.MAX_RUNS, r + runs)
                            vals[r] += p * self._terminals[tgt]
                    else:
                        next_row = self.table[new_b, :, new_w]
                        if runs == 0:
                            vals += p * next_row
                        else:
                            shifted = np.empty(self.MAX_RUNS+1, dtype=np.float32)
                            if runs <= self.MAX_RUNS:
                                shifted[:self.MAX_RUNS+1-runs] = next_row[runs:]
                                shifted[self.MAX_RUNS+1-runs:] = self._terminals[self.MAX_RUNS]
                            else:
                                shifted[:] = self._terminals[self.MAX_RUNS]
                            vals += p * shifted
                self.table[b, :, w] = vals


def overs_to_balls(s):
    s = str(s); whole, _, frac = s.partition('.')
    return int(whole)*6 + (int(frac) if frac else 0)


def make_pbin(p):
    if p < 0.20: return "0.10-0.20"
    if p < 0.35: return "0.20-0.35"
    if p < 0.50: return "0.35-0.50"
    if p < 0.65: return "0.50-0.65"
    if p < 0.80: return "0.65-0.80"
    return "0.80-0.90"


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


def train_zero_overshoot_predictor(safety_extra_c=5.0):
    """Train Layer 2 GBM τ=0.005 on full workbook residuals + leave-one-match-out
    conformal margin + safety_extra_c. Returns (model, events, phases, total_margin_c)."""
    df = pd.read_csv(RESID_CSV)
    df["date"] = df["slug"].str.extract(r"(\d{4}-\d{2}-\d{2})$")[0]
    df = df[(df["t-40"] >= 0.10) & (df["t-40"] <= 0.90)].copy()
    events = sorted(df["event"].unique())
    phases = sorted(df["phase"].unique())

    # Leave-one-match-out worst over-prediction → conformal margin
    matches = sorted(df["slug"].unique())
    cv_overs = []
    for held in matches:
        sub_tr = df[df["slug"] != held]
        sub_va = df[df["slug"] == held]
        X_sub = featurize(sub_tr, events, phases)
        y_sub = sub_tr["actual_delta_c"].values
        m = lgb.LGBMRegressor(objective="quantile", alpha=0.005,
                              n_estimators=600, learning_rate=0.04,
                              num_leaves=31, min_data_in_leaf=15, verbosity=-1)
        m.fit(X_sub, y_sub)
        X_va = featurize(sub_va, events, phases)
        p_va = m.predict(X_va)
        over = (p_va - sub_va["actual_delta_c"].values).max()
        cv_overs.append(over)
    conformal_c = max(cv_overs)
    total_margin = conformal_c + safety_extra_c
    print(f"  CV worst over-pred margins: "
          f"min={min(cv_overs):.2f}c  max={max(cv_overs):.2f}c  "
          f"mean={np.mean(cv_overs):.2f}c")
    print(f"  CV-conformal margin = {conformal_c:.2f}c   "
          f"+ safety extra {safety_extra_c:.1f}c   = total {total_margin:.2f}c")

    # Train final model on full data
    X_full = featurize(df, events, phases)
    y_full = df["actual_delta_c"].values
    final = lgb.LGBMRegressor(objective="quantile", alpha=0.005,
                              n_estimators=600, learning_rate=0.04,
                              num_leaves=31, min_data_in_leaf=15, verbosity=-1)
    final.fit(X_full, y_full)
    return final, events, phases, total_margin


def load_match_events(slug, team_a_short, dp, inn1_dp):
    db_path = next(CAPTURES.glob(f"match_capture_cricipl-{slug}_*.db"))
    conn = sqlite3.connect(db_path)
    meta = dict(conn.execute("SELECT key, value FROM match_meta").fetchall())
    token_ids = json.loads(meta["token_ids"])
    outcome_names = json.loads(meta["outcome_names"])
    short_to_token = {NAME_TO_SHORT.get(o, o): t for o, t in zip(outcome_names, token_ids)}
    if team_a_short not in short_to_token:
        raise ValueError(f"team_a {team_a_short} not in {list(short_to_token)}")
    team_a_token = short_to_token[team_a_short]
    team_b_short = next(s for s in short_to_token if s != team_a_short)

    rows = conn.execute(
        "SELECT id, local_ts_ms, COALESCE(runs,0), COALESCE(wickets,0), "
        "COALESCE(overs,'0.0'), score_str, signal_type "
        "FROM cricket_events WHERE signal_type != '?' ORDER BY id"
    ).fetchall()

    # Detect innings split (5-min gap + score reset)
    split_idx = None
    for i in range(1, len(rows)):
        gap = (rows[i][1] - rows[i-1][1]) / 1000.0
        if gap >= 300 and overs_to_balls(rows[i][4]) < overs_to_balls(rows[i-1][4]) and rows[i][2] < rows[i-1][2]:
            split_idx = i
            break

    # Determine batting teams
    # Use score_str prefix: e.g., "DC 77/1 (6.3)" - if available
    inn1_bat = None; inn2_bat = None
    for r in rows[:30]:
        sstr = r[5]
        if sstr:
            tok = sstr.split()[0]
            if tok in short_to_token:
                inn1_bat = tok; break
    if inn1_bat is None:
        inn1_bat = team_a_short
    inn2_bat = team_b_short if inn1_bat == team_a_short else team_a_short

    # Innings 1 final total (if we have inn 2 events)
    inn1_total = None
    if split_idx is not None:
        last_inn1 = rows[split_idx-1]
        inn1_total = last_inn1[2]  # runs
    target = (inn1_total + 1) if inn1_total is not None else None

    # Book series for team A
    book_rows = conn.execute(
        "SELECT local_ts_ms, bid1_p FROM book_snapshots "
        "WHERE asset_id = ? AND bid1_p IS NOT NULL ORDER BY local_ts_ms",
        (team_a_token,),
    ).fetchall()
    book_times = [b[0] for b in book_rows]
    book_prices = [b[1] for b in book_rows]

    def nearest(target_ms):
        if not book_times: return None
        i = bisect.bisect_left(book_times, target_ms)
        cands = []
        if i < len(book_times): cands.append((abs(book_times[i] - target_ms), book_prices[i]))
        if i > 0: cands.append((abs(book_times[i-1] - target_ms), book_prices[i-1]))
        cands.sort()
        return cands[0][1] if cands else None

    out = []
    for i, r in enumerate(rows):
        rid, ts_ms, runs, wkts, overs, score_str, sig = r
        innings = 2 if (split_idx is not None and i >= split_idx) else 1
        bat_team = inn2_bat if innings == 2 else inn1_bat
        # Adjust runs/wkts if innings 2 (might be relative or absolute — use score_str)
        # For simplicity use the ints provided.
        ev = str(sig) if sig else ""
        dp_outcome = EVENT_TO_DP.get(ev)
        if dp_outcome is None:
            continue

        runs_post = runs; wkts_post = wkts
        balls_post = overs_to_balls(overs)
        runs_pre = runs_post - OUTCOME_RUNS[dp_outcome]
        wkts_pre = wkts_post - (1 if OUTCOME_IS_WICKET[dp_outcome] else 0)
        balls_pre = balls_post - (1 if OUTCOME_CONSUMES_BALL[dp_outcome] else 0)
        if runs_pre < 0 or wkts_pre < 0 or balls_pre < 0:
            continue

        t40 = nearest(ts_ms - 40_000)
        t_actual = nearest(ts_ms)
        if t40 is None or t_actual is None:
            continue

        balls_rem_pre = 120 - balls_pre
        balls_rem_post = 120 - balls_post
        wkts_hand_pre = 10 - wkts_pre
        wkts_hand_post = 10 - wkts_post

        if innings == 2 and target is not None:
            wp_pre = dp.lookup(balls_rem_pre, target - runs_pre, wkts_hand_pre)
            wp_post = dp.lookup(balls_rem_post, target - runs_post, wkts_hand_post)
        else:
            wp_pre = inn1_dp.lookup(balls_rem_pre, runs_pre, wkts_hand_pre)
            wp_post = inn1_dp.lookup(balls_rem_post, runs_post, wkts_hand_post)
        delta_bat = wp_post - wp_pre
        sign = +1 if bat_team == team_a_short else -1
        delta_team_a = sign * delta_bat

        phase = ("PP" if balls_post <= 36 else "mid" if balls_post <= 90 else "death")

        ist_dt = datetime.fromtimestamp(ts_ms / 1000, IST)
        out.append({
            "time_ist": ist_dt.strftime("%H:%M:%S"),
            "score": score_str,
            "event": ev,
            "innings": innings,
            "phase": phase,
            "bat_team": bat_team,
            "t-40": float(t40),
            "t":    float(t_actual),
            "actual_delta_c": (float(t_actual) - float(t40)) * 100,
            "pred_delta_c": delta_team_a * 100,  # DP delta in cents (team A coords)
        })
    conn.close()
    return out, team_a_short, team_b_short


def main():
    p = argparse.ArgumentParser()
    p.add_argument("slug")
    p.add_argument("--team-a", default="DC")
    p.add_argument("--safety-extra-c", type=float, default=5.0)
    p.add_argument("--out-md", default=None)
    args = p.parse_args()

    print(f"=== {args.slug}  team A = {args.team_a}  ===\n")

    print("Solving DPs…")
    dp = DPTable(); dp.solve()
    inn1_model = FirstInningsModel(dp)
    inn1_dp = InningsOneDP(inn1_model); inn1_dp.solve()

    print("Training Layer 2 zero-overshoot predictor…")
    model, events, phases, total_margin_c = train_zero_overshoot_predictor(args.safety_extra_c)

    print(f"\nLoading match events…")
    rows, team_a, team_b = load_match_events(args.slug, args.team_a, dp, inn1_dp)
    print(f"  {len(rows)} predictable events (team A = {team_a}, team B = {team_b})")

    df = pd.DataFrame(rows)
    if df.empty:
        print("No events to predict."); return 1

    # Filter to mid-range t-40 (the model is calibrated for this regime)
    df["in_range"] = (df["t-40"] >= 0.10) & (df["t-40"] <= 0.90)
    df_pred = df[df["in_range"]].copy()
    if df_pred.empty:
        print("No mid-range events.")
        return 1

    # Apply GBM
    X = featurize(df_pred.assign(phase=df_pred["phase"]), events, phases)
    raw = model.predict(X)
    df_pred["pred_delta_l2_c"] = raw - total_margin_c
    df_pred["pred_t"] = df_pred["t-40"] + df_pred["pred_delta_l2_c"] / 100
    df_pred["pred_t"] = df_pred["pred_t"].clip(0.001, 0.999)
    df_pred["err_c"] = (df_pred["t"] - df_pred["pred_t"]) * 100  # actual - predicted (positive = OK)
    df_pred["status"] = np.where(df_pred["err_c"] >= 0, "OK", "OVERSHOOT")

    # Re-merge with out-of-range events (those marked NO TRADE)
    df = df.merge(df_pred[["time_ist", "pred_delta_l2_c", "pred_t", "err_c", "status"]],
                  on="time_ist", how="left")
    df["status"] = df["status"].fillna("NO_TRADE_extreme")
    df["pred_t"] = df["pred_t"].astype(float)

    # Summary
    n_pred = len(df_pred)
    n_overshoot = int((df_pred["err_c"] < 0).sum())
    print(f"\n=== Summary ===")
    print(f"  Total predictable events : {n_pred}")
    print(f"  Overshoots (predicted > actual): {n_overshoot} ({n_overshoot/n_pred*100:.2f}%)")
    print(f"  Mean abs error: {df_pred['err_c'].abs().mean():.2f}c")
    print(f"  Median abs error: {df_pred['err_c'].abs().median():.2f}c")
    print(f"  Max abs error: {df_pred['err_c'].abs().max():.2f}c")

    # Markdown output
    md_path = Path(args.out_md) if args.out_md else CAPTURES / f"predictions_{args.slug}.md"
    with open(md_path, "w") as f:
        f.write(f"# Predictions: {args.slug}  (team A = {team_a})\n\n")
        f.write(f"Model: GBM quantile τ=0.005 + CV-conformal margin + safety extra → "
                f"total margin **{total_margin_c:.2f}c**.  \n")
        f.write(f"Mid-range filter: 0.10 ≤ t-40 ≤ 0.90.  Outside this range = NO_TRADE.  \n")
        f.write(f"Goal: predicted ≤ actual (zero overshoot).  Result on this match: "
                f"**{n_overshoot}/{n_pred} overshoots ({n_overshoot/n_pred*100:.2f}%)**.  \n\n")
        f.write(f"**Errors (cents):** mean abs {df_pred['err_c'].abs().mean():.2f}, "
                f"median {df_pred['err_c'].abs().median():.2f}, "
                f"max {df_pred['err_c'].abs().max():.2f}.\n\n")
        # Table
        f.write("| # | time IST | score | event | bat | t-40 | predicted t | actual t | err (c) | status |\n")
        f.write("|---:|---|---|:---:|:---:|---:|---:|---:|---:|---|\n")
        for i, r in df.iterrows():
            pt = f"{r['pred_t']:.3f}" if pd.notna(r["pred_t"]) else "—"
            er = f"{r['err_c']:+.2f}" if pd.notna(r["err_c"]) else "—"
            f.write(f"| {i+1} | {r['time_ist']} | {r['score']} | "
                    f"{r['event']} | {r['bat_team']} | {r['t-40']:.3f} | "
                    f"{pt} | {r['t']:.3f} | {er} | {r['status']} |\n")
    print(f"\nMarkdown written to {md_path}")

    # Also save CSV
    csv_path = CAPTURES / f"predictions_{args.slug}.csv"
    df.to_csv(csv_path, index=False)
    print(f"CSV written to {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
