"""Layer 1 deterministic predictor: takes (state at t-40, current odds at t-40,
ball outcome) and predicts odds at t using pure DP win-probability delta.

For each event row in captures/team_a_event_book.xlsx:
  1. Recover pre-ball state from the post-ball score column + the outcome.
  2. Compute fair WP for the BATTING team at pre- and post-ball state.
       - Innings 2 (chase): direct chase DP lookup.
       - Innings 1: median projected total via binary search on chase DP, then
         FirstInningsModel.win_prob_at_total.
  3. delta_team_a = sign * (wp_post - wp_pre)   sign=+1 if batting=team A else -1
  4. predicted_t = clip(t-40 odds + delta_team_a, 0, 1)
  5. error = actual_t - predicted_t            (cents)

Reports overall error, by event type, by innings, by phase, and the %% of
rows where predicted > actual (the case the user wants to avoid).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "model-simulation" / "src"))
from dp.solver import DPTable, FirstInningsModel  # type: ignore
from dp.states import (  # type: ignore
    OUTCOMES,
    OUTCOME_CONSUMES_BALL,
    OUTCOME_IS_WICKET,
    OUTCOME_RUNS,
    TransitionProbs,
)


class InningsOneDP:
    """Backward DP for innings-1 win probability.

    State: (balls_remaining_in_inn1, runs_scored, wickets_in_hand).
    Terminal value: FirstInningsModel.win_prob_at_total(final_score), where
    final_score = runs at the moment innings ends (balls run out or all out).
    """
    MAX_BALLS = 120
    MAX_RUNS = 300
    MAX_WICKETS = 10

    def __init__(self, inn1_model: FirstInningsModel):
        self.inn1 = inn1_model
        shape = (self.MAX_BALLS + 1, self.MAX_RUNS + 1, self.MAX_WICKETS + 1)
        self.table = np.zeros(shape, dtype=np.float32)
        self._terminals = np.array(
            [self.inn1.win_prob_at_total(r) for r in range(self.MAX_RUNS + 1)],
            dtype=np.float32,
        )

    def lookup(self, balls_remaining: int, runs_scored: int, wkts_in_hand: int) -> float:
        b = max(0, min(self.MAX_BALLS, balls_remaining))
        r = max(0, min(self.MAX_RUNS, runs_scored))
        w = max(0, min(self.MAX_WICKETS, wkts_in_hand))
        return float(self.table[b, r, w])

    def solve(self) -> float:
        import time
        t0 = time.time()
        # Terminal: balls = 0 (innings over) → WP = f(runs_scored)
        self.table[0, :, :] = self._terminals[:, None]
        # Terminal: wkts = 0 (all out) → WP = f(runs_scored)
        self.table[:, :, 0] = self._terminals[None, :]

        for b in range(1, self.MAX_BALLS + 1):
            overs_bowled = (self.MAX_BALLS - b) // 6
            phase = ("powerplay" if overs_bowled < 6
                     else "middle" if overs_bowled < 15
                     else "death")
            tp = TransitionProbs.from_phase_averages(phase)
            probs = tp.as_dict()
            # Vectorized over runs dimension
            for w in range(1, self.MAX_WICKETS + 1):
                vals = np.zeros(self.MAX_RUNS + 1, dtype=np.float32)
                for outcome in OUTCOMES:
                    p = probs[outcome]
                    if p <= 0:
                        continue
                    runs = OUTCOME_RUNS[outcome]
                    consumes = OUTCOME_CONSUMES_BALL[outcome]
                    is_wicket = OUTCOME_IS_WICKET[outcome]
                    new_b = b - (1 if consumes else 0)
                    new_w = w - (1 if is_wicket else 0)

                    # next-state values for every starting r
                    if new_w <= 0:
                        # Wicket → all out → terminal at runs+runs_added
                        for r in range(self.MAX_RUNS + 1):
                            tgt = min(self.MAX_RUNS, r + runs)
                            vals[r] += p * self._terminals[tgt]
                    elif new_b <= 0:
                        # Wide/no-ball can't reach new_b<=0 (they don't consume),
                        # but for legals they can. Final score = r + runs.
                        for r in range(self.MAX_RUNS + 1):
                            tgt = min(self.MAX_RUNS, r + runs)
                            vals[r] += p * self._terminals[tgt]
                    else:
                        # Vectorized lookup of self.table[new_b, r+runs, new_w]
                        next_row = self.table[new_b, :, new_w]
                        if runs == 0:
                            vals += p * next_row
                        else:
                            shifted = np.empty(self.MAX_RUNS + 1, dtype=np.float32)
                            if runs <= self.MAX_RUNS:
                                shifted[: self.MAX_RUNS + 1 - runs] = next_row[runs:]
                                # For r where r+runs > MAX_RUNS, cap at terminal
                                shifted[self.MAX_RUNS + 1 - runs:] = self._terminals[self.MAX_RUNS]
                            else:
                                shifted[:] = self._terminals[self.MAX_RUNS]
                            vals += p * shifted
                self.table[b, :, w] = vals

        elapsed = time.time() - t0
        print(f"InningsOne DP solved in {elapsed:.2f}s")
        return elapsed

WB = Path("/Users/sobhagyaxd/DeepWork/totem-mm/captures/team_a_event_book.xlsx")

# Map workbook event codes to DP outcome names. LB/B treated as singles
# (consume ball, batting team gets run, no batsman credit but DP doesn't model
# strike rotation either, so this is fine for WP delta).
EVENT_TO_DP = {
    "0": "dot",
    "1": "single",
    "2": "double",
    "3": "triple",
    "4": "four",
    "5": None,        # 5 runs is rare (overthrow); skip
    "6": "six",
    "W": "wicket",
    "WD": "wide",
    "NB": "noball",
    "LB": "single",
    "B": "single",
}


def overs_to_balls(s: str) -> int:
    whole, _, frac = str(s).partition(".")
    return int(whole) * 6 + (int(frac) if frac else 0)


def parse_score(s: str) -> tuple[str, int, int, int] | None:
    m = re.match(r"^(\w+)\s+(\d+)/(\d+)\s+\(([\d.]+)\)", str(s).strip())
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3)), overs_to_balls(m.group(4))


def parse_inn1_meta(s: str) -> tuple[str, int, int] | None:
    m = re.match(r"^(\w+)\s+(\d+)/(\d+)", str(s).strip())
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3))


def median_runs_scoreable(dp: DPTable, balls_left: int, wkts_in_hand: int) -> int:
    """Binary search target where chase WP ≈ 0.5 — median expected runs the
    batting team will add given balls and wickets remaining."""
    if balls_left <= 0 or wkts_in_hand <= 0:
        return 0
    lo, hi = 0, dp.MAX_RUNS
    while lo < hi:
        mid = (lo + hi + 1) // 2
        wp = dp.lookup(balls_left, mid, wkts_in_hand)
        if wp >= 0.5:
            lo = mid
        else:
            hi = mid - 1
    return lo


def main() -> int:
    print("Solving chase DP table…")
    dp = DPTable()
    dp.solve()  # non-vectorized; vectorized has a wide/noball self-reference bug
    sanity = dp.verify_sanity()
    print(f"  DP sanity all_pass = {sanity['all_pass']}")
    inn1_model = FirstInningsModel(dp)

    print("Solving innings-1 DP table…")
    inn1_dp = InningsOneDP(inn1_model)
    inn1_dp.solve()

    print(f"Loading {WB}…")
    xl = pd.ExcelFile(WB)
    sheets = [s for s in xl.sheet_names if s != "_summary"]

    rows: list[dict] = []
    for slug in sheets:
        raw = pd.read_excel(xl, sheet_name=slug, header=3, dtype=object)
        meta = pd.read_excel(xl, sheet_name=slug, header=None, nrows=2, dtype=object)
        team_a = str(meta.iloc[0, 1]).split(":")[-1].strip()
        team_b = str(meta.iloc[0, 2]).split(":")[-1].strip()
        inn1_meta = parse_inn1_meta(str(meta.iloc[1, 0]))
        if inn1_meta is None:
            continue
        inn1_team, inn1_total, _inn1_wkts = inn1_meta
        target = inn1_total + 1

        raw = raw.dropna(subset=["time_ist", "event"])
        for _, r in raw.iterrows():
            ev = str(r["event"])
            dp_outcome = EVENT_TO_DP.get(ev)
            if dp_outcome is None:
                continue
            try:
                innings = int(r["innings"])
            except (TypeError, ValueError):
                continue
            parsed = parse_score(r["score"])
            if parsed is None:
                continue
            bat_team, runs_post, wkts_post, balls_post = parsed

            # Reverse to pre-state.
            runs_pre = runs_post - OUTCOME_RUNS[dp_outcome]
            wkts_pre = wkts_post - (1 if OUTCOME_IS_WICKET[dp_outcome] else 0)
            balls_pre = balls_post - (1 if OUTCOME_CONSUMES_BALL[dp_outcome] else 0)
            if runs_pre < 0 or wkts_pre < 0 or balls_pre < 0:
                continue

            t_minus_40 = r.get("t-40")
            t_actual = r.get("t")
            if pd.isna(t_minus_40) or pd.isna(t_actual):
                continue
            t_minus_40 = float(t_minus_40)
            t_actual = float(t_actual)

            balls_rem_pre = 120 - balls_pre
            balls_rem_post = 120 - balls_post
            wkts_hand_pre = 10 - wkts_pre
            wkts_hand_post = 10 - wkts_post

            if innings == 2:
                wp_pre = dp.lookup(balls_rem_pre, target - runs_pre, wkts_hand_pre)
                wp_post = dp.lookup(balls_rem_post, target - runs_post, wkts_hand_post)
            else:
                # Innings 1: proper backward DP over (balls_rem, runs, wkts).
                wp_pre = inn1_dp.lookup(balls_rem_pre, runs_pre, wkts_hand_pre)
                wp_post = inn1_dp.lookup(balls_rem_post, runs_post, wkts_hand_post)

            delta_bat = wp_post - wp_pre
            sign = +1 if bat_team == team_a else -1
            delta_team_a = sign * delta_bat

            predicted = max(0.0, min(1.0, t_minus_40 + delta_team_a))
            err_c = (t_actual - predicted) * 100  # cents, positive = under-predicted

            phase = "PP" if balls_post <= 36 else ("mid" if balls_post <= 90 else "death")
            rows.append({
                "slug": slug,
                "innings": innings,
                "event": ev,
                "phase": phase,
                "bat_is_A": (bat_team == team_a),
                "t-40": t_minus_40,
                "t": t_actual,
                "pred": predicted,
                "actual_delta_c": (t_actual - t_minus_40) * 100,
                "pred_delta_c": delta_team_a * 100,
                "error_c": err_c,  # signed: actual - predicted
                "abs_err_c": abs(err_c),
            })

    df = pd.DataFrame(rows)
    print(f"\n=== Predictor ran on {len(df)} events from {df['slug'].nunique()} matches ===\n")

    # Overall
    print("=== OVERALL ===")
    print(f"  Mean signed error  : {df['error_c'].mean():+.3f} c   (positive = under-predicted)")
    print(f"  Mean absolute error: {df['abs_err_c'].mean():.3f} c")
    print(f"  Median abs error   : {df['abs_err_c'].median():.3f} c")
    print(f"  P90 abs error      : {df['abs_err_c'].quantile(0.90):.3f} c")
    print(f"  P99 abs error      : {df['abs_err_c'].quantile(0.99):.3f} c")
    print(f"  Max abs error      : {df['abs_err_c'].max():.3f} c")
    over_pct = (df["error_c"] < 0).mean() * 100
    eq_pct = (df["error_c"] == 0).mean() * 100
    under_pct = (df["error_c"] > 0).mean() * 100
    print(f"  predicted > actual (BAD): {over_pct:5.1f}%   "
          f"==: {eq_pct:5.1f}%   predicted < actual (OK): {under_pct:5.1f}%")
    print(f"  Mean over-pred magnitude (when over): "
          f"{df.loc[df['error_c'] < 0, 'abs_err_c'].mean():.3f} c")

    # By event type
    print("\n=== By event type ===")
    g = df.groupby("event").agg(
        n=("event", "count"),
        mean_signed=("error_c", "mean"),
        mean_abs=("abs_err_c", "mean"),
        median_abs=("abs_err_c", "median"),
        p90_abs=("abs_err_c", lambda x: x.quantile(0.9)),
        max_abs=("abs_err_c", "max"),
        pct_overpred=("error_c", lambda x: (x < 0).mean() * 100),
    ).round(3)
    g = g.sort_values("n", ascending=False)
    print(g.to_string())

    # By innings
    print("\n=== By innings ===")
    g = df.groupby("innings").agg(
        n=("innings", "count"),
        mean_signed=("error_c", "mean"),
        mean_abs=("abs_err_c", "mean"),
        p90_abs=("abs_err_c", lambda x: x.quantile(0.9)),
        pct_overpred=("error_c", lambda x: (x < 0).mean() * 100),
    ).round(3)
    print(g.to_string())

    # By phase
    print("\n=== By phase ===")
    g = df.groupby("phase").agg(
        n=("phase", "count"),
        mean_signed=("error_c", "mean"),
        mean_abs=("abs_err_c", "mean"),
        p90_abs=("abs_err_c", lambda x: x.quantile(0.9)),
        pct_overpred=("error_c", lambda x: (x < 0).mean() * 100),
    ).round(3)
    print(g.to_string())

    # Big-event focus: 4 / 6 / W only, by innings × phase
    print("\n=== 4/6/W by innings × phase ===")
    big = df[df["event"].isin(["4", "6", "W"])]
    g = big.groupby(["event", "innings", "phase"]).agg(
        n=("event", "count"),
        mean_signed=("error_c", "mean"),
        mean_abs=("abs_err_c", "mean"),
        p90_abs=("abs_err_c", lambda x: x.quantile(0.9)),
        pct_overpred=("error_c", lambda x: (x < 0).mean() * 100),
    ).round(3)
    print(g.to_string())

    # Worst over-predictions (predicted ran higher than reality) — top 10
    print("\n=== TOP 10 WORST OVER-PREDICTIONS (predicted > actual) ===")
    worst = df.sort_values("error_c").head(10)[
        ["slug", "innings", "event", "phase", "t-40", "t", "pred",
         "actual_delta_c", "pred_delta_c", "error_c"]
    ]
    print(worst.round(3).to_string(index=False))

    # Save full residuals
    out = WB.parent / "predict_dp_residuals.csv"
    df.to_csv(out, index=False)
    print(f"\nFull residuals saved to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
