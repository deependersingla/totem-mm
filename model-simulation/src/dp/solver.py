"""Backward induction solver for cricket win probability."""

import time

import numpy as np

from .states import (
    OUTCOMES,
    OUTCOME_CONSUMES_BALL,
    OUTCOME_IS_WICKET,
    OUTCOME_RUNS,
    DPState,
    TransitionProbs,
)


class DPTable:
    """DP table for chase innings win probability.

    State: (balls_remaining, runs_needed, wickets_in_hand)
    Dimensions: (MAX_BALLS+1) x (MAX_RUNS+1) x (MAX_WICKETS+1)

    Stored as a 3D numpy array for O(1) lookup.
    """

    MAX_BALLS = 120
    MAX_RUNS = 350
    MAX_WICKETS = 10

    def __init__(self):
        shape = (self.MAX_BALLS + 1, self.MAX_RUNS + 2, self.MAX_WICKETS + 1)
        self.table = np.full(shape, -1.0, dtype=np.float32)
        self._solved = False

    def lookup(self, balls_remaining: int, runs_needed: int, wickets_in_hand: int) -> float:
        """O(1) lookup of win probability."""
        if runs_needed <= 0:
            return 1.0
        if wickets_in_hand <= 0 or balls_remaining <= 0:
            return 0.0
        if runs_needed > self.MAX_RUNS:
            return 0.0
        if balls_remaining > self.MAX_BALLS:
            balls_remaining = self.MAX_BALLS
        return float(self.table[balls_remaining, runs_needed, wickets_in_hand])

    def solve(self, get_transition_probs=None):
        """Solve the entire DP table via backward induction.

        Args:
            get_transition_probs: callable(balls_remaining, wickets_in_hand) -> TransitionProbs
                If None, uses phase-based defaults.
        """
        t0 = time.time()

        # Terminal states
        # runs_needed <= 0 → win (handled in lookup)
        # wickets = 0 → loss
        # balls = 0 → loss (if runs_needed > 0)
        self.table[:, 0, :] = 1.0   # runs_needed = 0 → won
        self.table[0, 1:, :] = 0.0  # balls = 0, runs > 0 → lost
        self.table[:, :, 0] = 0.0   # wickets = 0 → all out
        self.table[:, 0, :] = 1.0   # override: runs_needed=0 is always win

        # Backward induction: iterate balls from 1 to MAX_BALLS
        for b in range(1, self.MAX_BALLS + 1):
            # Determine phase based on balls remaining
            overs_bowled = (self.MAX_BALLS - b) // 6
            if overs_bowled < 6:
                phase = "powerplay"
            elif overs_bowled < 15:
                phase = "middle"
            else:
                phase = "death"

            for w in range(1, self.MAX_WICKETS + 1):
                if get_transition_probs:
                    tp = get_transition_probs(b, w)
                else:
                    tp = TransitionProbs.from_phase_averages(phase)

                probs = tp.as_dict()

                for r in range(1, self.MAX_RUNS + 2):  # +2 to cover full table dim
                    value = 0.0
                    for outcome in OUTCOMES:
                        p = probs[outcome]
                        if p <= 0:
                            continue

                        runs = OUTCOME_RUNS[outcome]
                        consumes = OUTCOME_CONSUMES_BALL[outcome]
                        is_wicket = OUTCOME_IS_WICKET[outcome]

                        new_b = b - (1 if consumes else 0)
                        new_r = r - runs
                        new_w = w - (1 if is_wicket else 0)

                        if new_r <= 0:
                            value += p * 1.0  # Won
                        elif new_w <= 0:
                            value += p * 0.0  # All out
                        elif new_b <= 0:
                            value += p * 0.0  # Balls exhausted
                        else:
                            value += p * self.table[new_b, new_r, new_w]

                    self.table[b, r, w] = value

        elapsed = time.time() - t0
        self._solved = True
        print(f"DP table solved in {elapsed:.2f}s")
        print(f"  Shape: {self.table.shape}")
        print(f"  Memory: {self.table.nbytes / 1024 / 1024:.1f} MB")
        return elapsed

    def solve_vectorized(self, get_transition_probs=None):
        """Faster vectorized solve using numpy operations."""
        t0 = time.time()

        # Initialize terminal states
        self.table[:, 0, :] = 1.0
        self.table[0, 1:, :] = 0.0
        self.table[:, :, 0] = 0.0
        self.table[:, 0, :] = 1.0

        for b in range(1, self.MAX_BALLS + 1):
            overs_bowled = (self.MAX_BALLS - b) // 6
            if overs_bowled < 6:
                phase = "powerplay"
            elif overs_bowled < 15:
                phase = "middle"
            else:
                phase = "death"

            for w in range(1, self.MAX_WICKETS + 1):
                if get_transition_probs:
                    tp = get_transition_probs(b, w)
                else:
                    tp = TransitionProbs.from_phase_averages(phase)

                probs = tp.as_dict()

                # For each outcome, build the value contribution across ALL runs
                vals = np.zeros(self.MAX_RUNS + 2, dtype=np.float32)  # +2 to match table dim

                for outcome in OUTCOMES:
                    p = probs[outcome]
                    if p <= 0:
                        continue

                    runs = OUTCOME_RUNS[outcome]
                    consumes = OUTCOME_CONSUMES_BALL[outcome]
                    is_wicket = OUTCOME_IS_WICKET[outcome]

                    new_b = b - (1 if consumes else 0)
                    new_w = w - (1 if is_wicket else 0)

                    if new_w <= 0 or new_b <= 0:
                        # All out or balls done — contributes 0 for all r>0
                        # But for r<=runs, it's a win
                        for r in range(1, min(runs + 1, self.MAX_RUNS + 1)):
                            vals[r] += p * 1.0
                        continue

                    # States where new_r <= 0 (won)
                    for r in range(1, min(runs + 1, self.MAX_RUNS + 2)):
                        vals[r] += p * 1.0

                    # States where new_r > 0 (look up table)
                    for r in range(max(1, runs + 1), self.MAX_RUNS + 2):
                        new_r = r - runs
                        vals[r] += p * self.table[new_b, new_r, new_w]

                self.table[b, 1:, w] = vals[1:]

        elapsed = time.time() - t0
        self._solved = True
        print(f"DP table solved (vectorized) in {elapsed:.2f}s")
        print(f"  Shape: {self.table.shape}")
        print(f"  Memory: {self.table.nbytes / 1024 / 1024:.1f} MB")
        return elapsed

    def get_scenarios(self, balls_remaining: int, runs_needed: int, wickets_in_hand: int) -> dict[str, float]:
        """Pre-compute win probability for each possible next-ball outcome."""
        scenarios = {}
        for outcome in OUTCOMES:
            runs = OUTCOME_RUNS[outcome]
            consumes = OUTCOME_CONSUMES_BALL[outcome]
            is_wicket = OUTCOME_IS_WICKET[outcome]

            new_b = balls_remaining - (1 if consumes else 0)
            new_r = runs_needed - runs
            new_w = wickets_in_hand - (1 if is_wicket else 0)

            scenarios[outcome] = self.lookup(new_b, new_r, new_w)

        return scenarios

    def verify_sanity(self) -> dict:
        """Run basic sanity checks on the solved table."""
        checks = {}

        # Check 1: 1 run needed, 120 balls, 10 wickets → near certain win
        v = self.lookup(120, 1, 10)
        checks["easy_chase"] = {"value": v, "expected": "> 0.99", "pass": v > 0.99}

        # Check 2: 300 runs needed, 1 ball → near zero
        v = self.lookup(1, 300, 10)
        checks["impossible_chase"] = {"value": v, "expected": "< 0.001", "pass": v < 0.001}

        # Check 3: Monotonicity — more balls = higher prob
        v1 = self.lookup(60, 80, 5)
        v2 = self.lookup(80, 80, 5)
        checks["more_balls_better"] = {"v60": v1, "v80": v2, "pass": v2 >= v1}

        # Check 4: Monotonicity — more wickets = higher prob
        v1 = self.lookup(60, 80, 3)
        v2 = self.lookup(60, 80, 7)
        checks["more_wickets_better"] = {"v3": v1, "v7": v2, "pass": v2 >= v1}

        # Check 5: Monotonicity — fewer runs needed = higher prob
        v1 = self.lookup(60, 100, 5)
        v2 = self.lookup(60, 60, 5)
        checks["fewer_runs_better"] = {"v100": v1, "v60": v2, "pass": v2 >= v1}

        # Check 6: Start of chase — typical IPL (target 170, 120 balls, 10 wkts)
        v = self.lookup(120, 170, 10)
        checks["typical_chase_start"] = {"value": v, "expected": "0.40-0.60", "pass": 0.30 <= v <= 0.70}

        # Check 7: Well-set chase — 50 needed, 40 balls, 7 wickets
        v = self.lookup(40, 50, 7)
        checks["wellset_chase"] = {"value": v, "expected": "0.50-0.95", "pass": 0.40 <= v <= 0.95}

        all_pass = all(c["pass"] for c in checks.values())
        checks["all_pass"] = all_pass

        return checks


class FirstInningsModel:
    """Expected total score predictor for first innings using DP."""

    def __init__(self, chase_dp: DPTable):
        self.chase_dp = chase_dp

    def win_prob_at_total(self, total: int) -> float:
        """P(batting first wins) given they score `total`."""
        # Chasing team needs total+1 to win (or total to tie → 50%)
        chase_prob = self.chase_dp.lookup(120, total + 1, 10)
        tie_prob = self.chase_dp.lookup(120, total, 10) - chase_prob
        # P(bat first wins) = 1 - P(chase wins) - 0.5 * P(tie)
        return 1.0 - chase_prob - 0.5 * max(0, tie_prob)
