# Dynamic Programming — Mathematical Specification

## Why DP Works for Cricket

Cricket's state space is finite, bounded, and small:

| Dimension | Range | Values |
|---|---|---|
| Legal balls remaining | 0-120 | 121 |
| Runs needed (chase) | 0-350 | 351 |
| Wickets in hand | 0-10 | 11 |

**Basic state space: 121 * 351 * 11 = 467,181 states (3.7 MB)**

Compare to chess: ~10^43 legal positions. Cricket is ~10^37 times simpler.

## Adding Player Identity

With fixed batting order (batters come in sequence when wickets fall):

After `w` wickets fallen, `11-w` batters remain. Valid (striker, non_striker) ordered pairs:

```
w=0:  (11-0)(10-0) = 110 pairs
w=1:  (11-1)(10-1) =  90
w=2:  (11-2)(10-2) =  72
w=3:  (11-3)(10-3) =  56
w=4:  (11-4)(10-4) =  42
w=5:  (11-5)(10-5) =  30
w=6:  (11-6)(10-6) =  20
w=7:  (11-7)(10-7) =  12
w=8:  (11-8)(10-8) =   6
w=9:  (11-9)(10-9) =   2
w=10: terminal (all out)
Total valid configurations = 440
```

**Player-specific state space:**
```
121 (balls) * 351 (runs) * 440 (wickets + batting pair) = 18,686,040 states
Memory (float64): 150 MB
Memory (float32): 75 MB
Compute time (backward induction): ~17 ms on M-series chip
```

### Why Fixed Batting Order Works

In T20 cricket, the batting order is essentially fixed for each match:
- Positions 1-3: openers + #3 (almost never change mid-match)
- Positions 4-7: middle order (occasionally promoted/demoted, but largely fixed)
- Positions 8-11: lower order / bowlers (fixed)

When wicket `w` falls, the next batter is determined by the lineup. We can pre-set this before the match.

If the captain promotes a batter (sends #6 before #5), we can recompute the DP table for the remaining states from the current position in ~1ms (only need to recompute states reachable from current state forward).

## Bellman Equation — Chase Innings

Let `V(b, r, w, s, ns)` = probability of winning from state:
- `b` = legal balls remaining
- `r` = runs still needed to win
- `w` = wickets in hand (batters remaining - 2, since 2 are currently batting)
- `s` = striker index in batting order
- `ns` = non-striker index in batting order

### Terminal Conditions

```
V(b, r, w, s, ns) = 1.0   if r <= 0          (target reached: WIN)
V(b, r, w, s, ns) = 0.0   if r > 0 and w < 0 (all out: LOSE)  
V(0, r, w, s, ns) = 0.0   if r > 0           (balls exhausted: LOSE)
```

### Recursive Case (b > 0, r > 0, w >= 0)

```
V(b, r, w, s, ns) = 
    p_dot     * V(b-1, r,   w,   eov(s, ns, b-1))          +
    p_single  * V(b-1, r-1, w,   eov(ns, s, b-1))          +  // odd runs: swap
    p_double  * V(b-1, r-2, w,   eov(s, ns, b-1))          +
    p_triple  * V(b-1, r-3, w,   eov(ns, s, b-1))          +  // odd runs: swap
    p_four    * V(b-1, r-4, w,   eov(s, ns, b-1))          +
    p_six     * V(b-1, r-6, w,   eov(s, ns, b-1))          +
    p_wicket  * V(b-1, r,   w-1, wicket_transition(s, ns, w)) +
    p_wide    * V(b,   r-1, w,   s, ns)                    +  // ball NOT consumed
    p_noball  * V(b,   r-1, w,   s, ns)                       // ball NOT consumed
```

Where:
- `p_x = P(outcome=x | batter=s, bowler=bowler_at_over(b), phase=phase(b), venue, conditions)`
- `eov(s, ns, b')` applies end-of-over strike rotation: if `(120 - b') % 6 == 0`, swap s and ns
- `wicket_transition(s, ns, w)` = `(next_batter(w), ns)` if striker out, or `(s, next_batter(w))` if non-striker out (run-out)

### End-of-Over Strike Rotation

```python
def end_of_over_swap(striker, non_striker, balls_remaining_after):
    # After this delivery, how many legal balls have been bowled?
    balls_bowled = 120 - balls_remaining_after
    # Is this the last ball of an over?
    if balls_bowled > 0 and balls_bowled % 6 == 0:
        return non_striker, striker  # swap
    return striker, non_striker
```

### Wicket Transition

When a wicket falls:
- Typically the striker is dismissed (caught, bowled, LBW, stumped)
- Run-out can dismiss either batter
- For simplification: model P(striker_out | wicket) ≈ 0.90, P(non_striker_out | wicket) ≈ 0.10

```python
def wicket_transition(striker, non_striker, wickets_in_hand):
    next_batter = batting_order[10 - wickets_in_hand + 2]  # next in line
    # Striker dismissed (90% of wickets):
    # new pair = (next_batter, non_striker) — new batter takes dismissed batter's end
    # Non-striker dismissed (10%, run-outs):
    # new pair = (striker, next_batter)
    return weighted_combination  # or split into two sub-transitions
```

### Processing Order (No Cycles)

Wides and no-balls don't consume a ball but DO reduce runs_needed. Process:
1. Iterate `b` from 0 to 120 (ascending)
2. Within each `b`, iterate `r` from 0 to max (ascending — but terminal at r<=0)

Since wide/noball transitions go to `(b, r-1, ...)` and we process `r` ascending, the dependency is on a state we've already computed. No cycles.

## First Innings Model

For the first innings, we don't have a target to chase. Instead, we compute the probability distribution over final total.

**Approach 1: Score Distribution DP**

Let `F(b, r, w, s, ns)` = probability of being in state (b balls bowled, r runs scored, w wickets lost) during the first innings.

Start: `F(0, 0, 0, opener1, opener2) = 1.0`
Propagate forward using transition probabilities.
At terminal states (b=120 or w=10), collect the run distribution.

**Approach 2: Expected Runs DP (simpler)**

Let `E(b, w, s, ns)` = expected additional runs from state (b balls remaining, w wickets in hand, batters s and ns).

```
E(b, w, s, ns) = sum_outcome P(outcome) * [runs(outcome) + E(b', w', s', ns')]
```

Terminal: `E(0, _, _, _) = 0`, `E(_, 0, _, _) = 0` (assuming last batter out ends innings).

The expected total = runs_already_scored + E(current_state).

**Combining innings:**
For each possible first innings total T:
```
P(batting_first_wins | T) = 1 - V_chase(120, T+1, 0, chase_opener1, chase_opener2)
```

Where V_chase is the chase DP table (adding 1 because chasing team needs T+1 to win, or T to tie which usually goes to super over).

## Bowler Assignment

Bowler is NOT a state variable (would explode to billions). Instead:

**Pre-match:** Define expected bowling plan:
```python
bowling_plan = {
    0: "Bumrah",    1: "Shami",     2: "Bumrah",    3: "Ashwin",
    4: "Jadeja",    5: "Shami",     6: "Ashwin",    7: "Jadeja",
    8: "Hardik",    9: "Bumrah",   10: "Ashwin",   11: "Jadeja",
   12: "Shami",    13: "Hardik",   14: "Bumrah",   15: "Shami",
   16: "Hardik",   17: "Ashwin",   18: "Bumrah",   19: "Shami",
}
```

**During match:** If actual bowling deviates from plan, update transition probabilities for remaining overs and recompute DP table for remaining states (~1ms from current state forward).

## Convergence and Accuracy

| Property | DP | Monte Carlo (10K sims) | Monte Carlo (50K sims) |
|---|---|---|---|
| Error | 0 (exact) | ±1.0% | ±0.44% |
| Per-query time | ~1 ns (lookup) | ~12 ms | ~60 ms |
| One-time compute | ~17 ms | 0 | 0 |
| Queries per match | ~240 | ~240 | ~240 |
| Total time/match | 17 ms + 240 ns | 2,880 ms | 14,400 ms |

DP is 170x-850x faster than MC over a full match AND gives exact answers.

## Memory Layout (Flat Array)

```python
# 5-dimensional array flattened to 1D
# Index: [balls_rem][runs_needed][wickets][striker][non_striker]
# With bounds: 121 * 351 * 11 * 11 * 10

def flat_index(b, r, w, s, ns):
    # Adjust ns index to avoid s==ns (invalid: same batter can't be both)
    ns_adj = ns if ns < s else ns - 1
    return ((((b * 351 + r) * 11 + w) * 11 + s) * 10 + ns_adj)

# Alternative: use validity-aware indexing to compress from 51.4M to 18.7M
# by only storing states where the batting pair is consistent with wickets fallen
```

## Handling Super Overs and Ties

If r == 0 at end of innings (scores level), the match goes to a super over. Model this as:
- `V(0, 0, w, s, ns) = 0.5` (coin flip approximation)
- Or: pre-compute super over win probability based on remaining batters/bowlers

## Phase-Dependent Transition Probabilities

The same (batter, bowler) pair has different outcome distributions in different phases:
- Powerplay (overs 0-5): fielding restrictions → more boundaries, fewer dots
- Middle (overs 6-14): more dots, more spin bowling
- Death (overs 15-19): maximum intent → more sixes, more wickets

Phase is determined by `over_number = (120 - balls_remaining) / 6`, which is derivable from state. The transition probabilities `P(outcome | ...)` naturally vary by phase through the ML model's features.
