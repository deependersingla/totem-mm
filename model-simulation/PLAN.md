# Cricket Win Probability Engine — Plan

## Motive

Build a real-time cricket win probability engine for IPL T20 matches that produces structurally accurate probabilities at every ball. The model replaces Betfair reference prices in the existing `QuoteEngine` with self-generated fair values, enabling:

1. **Detecting market overreactions** — wickets, boundaries, and phase transitions where Polymarket's ground-level MM reprices by intuition, not math
2. **Pre-computing all next-state probabilities** — know instantly what your fair value should be if the next ball is a dot, single, four, six, or wicket
3. **Pricing DLS/rain scenarios correctly** — where human intuition systematically fails
4. **Venue/player/matchup-specific pricing** — generic MMs use approximate rules; this model uses exact player distributions

## Why NOT Speed — Why Accuracy

Polymarket cricket markets react 30-52 seconds BEFORE any cricket API (someone at the ground or watching TV is making markets). The edge is NOT being faster. The edge is being MORE ACCURATE than a human MM's snap judgment on:
- Which wicket matters (tail-ender out = small move; star batter out = large move)
- Nonlinear chase math (36 off 12 with 8 wickets vs 3 wickets)
- Venue-specific scoring patterns
- Cumulative pressure from dot balls before a boundary

## Core Architecture: DP + ML Hybrid

### Layer 1: Dynamic Programming Engine (Exact, O(1) Lookup)

Cricket's state space is small enough to solve exactly via backward induction.

**State:** `(balls_remaining, runs_needed, wickets_in_hand, striker_id, non_striker_id)`

| Config | States | Memory | Compute Time | Lookup |
|---|---|---|---|---|
| Basic (b, r, w) | 467K | 3.7 MB | <1ms | O(1) |
| **+ Player identity (fixed batting order)** | **18.7M** | **150 MB** | **~17ms** | **O(1)** |

Pre-compute the entire table before each match (~17ms). Every ball update is a single array access (~1ns).

**Why DP over Monte Carlo:**
- Exact answers (zero variance) vs MC's +/-1% at 10K simulations
- One-time computation amortized across all 240+ lookups per match
- MC needs 10K+ simulations per query at ~12ms each; DP needs 0ms per query
- DP strictly dominates MC for state spaces under ~1B states

**Handling bowler identity without state explosion:**
Bowler identity is NOT a state dimension. Instead, use the known/expected bowling plan to make bowler a function of over number. This keeps the state space at 18.7M instead of exploding to billions.

**Handling wides/no-balls:**
Transitions where `balls_remaining` stays the same (ball not consumed) but `runs_needed` decreases. No cycles because runs strictly decrease. Process states in order of (balls ascending, runs descending).

### Layer 2: ML Correction Model (Player-Specific Transitions)

The DP engine's accuracy depends entirely on the transition probabilities:
`P(outcome | striker, bowler, phase, venue, conditions)`

These are estimated by ML models trained on historical ball-by-ball data:

**LightGBM** (primary): Predicts multinomial ball outcome distribution given ~30 features.
- Training: ~300K deliveries from IPL, ~2M from all T20. Trains in <30 seconds.
- Inference: 0.05ms per prediction. 5MB model.
- Why LightGBM: handles mixed features (categorical + continuous), native missing value support, `objective='cross_entropy'` directly optimizes calibration.

**GRU Sequence Model** (momentum correction): Captures over-by-over momentum patterns that per-ball tabular features miss.
- Architecture: 2-layer GRU, 128 hidden, 200K params, 2MB.
- Input: last 30 ball outcomes as sequence features.
- Output: adjustment factor for transition probabilities.
- Training: ~50 minutes on MPS. Inference: 0.2ms.

### Layer 3: Calibration

Raw model probabilities don't match market pricing convention. Calibrate against historical Polymarket data:
- Phase-specific isotonic regression (powerplay / middle / death overs)
- Validated against captured Polymarket order book data from IPL 2026 matches
- Target: ECE < 0.02, RMSE vs market < 0.03

### Why NOT AlphaZero/RL

Evaluated and rejected based on research:
- Cricket is stochastic + imperfect information (not chess/Go)
- RL produces optimal strategies, not probabilities — wrong output for trading
- State space is small enough for exact DP — no need for neural function approximation
- Simulator fidelity is the bottleneck — RL would just learn simulator quirks
- One exception: Inverse RL for calibrating transition probabilities (future work)

## Data Pipeline

### Source of Truth: Cricsheet.org

Free, comprehensive, ball-by-ball JSON for every IPL match since 2008.

**Per delivery fields:**
- `batter`, `bowler`, `non_striker` (names)
- `runs.batter` (0, 1, 2, 3, 4, 6), `runs.extras`, `runs.total`
- `extras.wides`, `extras.noballs`, `extras.byes`, `extras.legbyes`
- `wickets[].kind` (caught, bowled, lbw, stumped, run_out, etc.)
- `wickets[].player_out`, `wickets[].fielders`

**Match metadata:** venue, toss winner/decision, playing XIs, outcome, season, dates.

**Coverage:**
- IPL: ~1,200+ matches, ~300K+ deliveries
- All T20 worldwide: ~8,000+ matches, ~2M+ deliveries
- Player registry with ESPNCricinfo IDs for cross-referencing

**Gap: Bowler type (pace/spin)** — not in Cricsheet. Lookup via `python-espncricinfo` Player class using registry IDs. Build once, cache as `bowler_types.json`.

### Feature Store (Computed from Cricsheet)

All features are computed using only data BEFORE the match date (no leakage):

**Batting stats by phase (powerplay/middle/death):**
- Strike rate, dot ball %, boundary % (4s and 6s separate)
- Dismissal rate per ball
- Balls to settle (strike rate curve vs balls faced)

**Bowling stats by phase:**
- Economy rate, dot ball %, wickets per match
- Boundary concession rate

**Matchup stats:**
- Batter vs bowler head-to-head (with Bayesian shrinkage, kappa=40)
- Batter vs bowler TYPE (pace/spin) by phase

**Venue stats:**
- Average 1st/2nd innings scores
- Chase success rate
- Phase-specific scoring patterns
- Toss-elect-to-field win rate

**Form (rolling):**
- Last 5/10 innings: average runs, SR
- Exponential decay weighting (lambda ~0.004/day, half-life ~170 days)

**Storage:** Parquet files, refreshed before each match day.

## Development Strategy: Claude Code + Codex

### Agent Architecture

**Claude Code (primary builder):**
- Writes all code, tests, iterates
- Has full context of the codebase and trading system
- Handles architecture decisions

**Codex (reviewer):**
- Reviews PRs for correctness, edge cases, performance
- Catches issues Claude Code might miss
- Second opinion on math/probability logic

### Build Phases (6 modules, sequential)

**Phase 1: Data Pipeline** (~1-2 sessions)
- Download Cricsheet IPL + all T20 data
- Parse JSON into flat deliveries Parquet table
- Build bowler type lookup from ESPNCricinfo
- Compute feature store (batting/bowling/venue/matchup stats)
- Verification: spot-check computed stats against Statsguru

**Phase 2: DP Engine** (~2-3 sessions)
- Implement backward induction solver in Python (prototype)
- Port to Rust for production (14-byte SimState struct, rayon parallelism)
- Pre-compute tables for all possible targets (80-300)
- Verification: compare against DLS par scores, known match outcomes

**Phase 3: Transition Probability Model** (~2-3 sessions)
- Train LightGBM on ball-by-ball outcomes (multinomial classification)
- Train GRU sequence model for momentum
- Bayesian hierarchical model for sparse matchups
- Verification: calibration plots, Brier score on held-out season

**Phase 4: Calibration** (~1-2 sessions)
- Phase-specific isotonic regression
- Calibrate against captured Polymarket data (IPL 2026 matches)
- Verification: ECE, reliability diagrams, RMSE vs market

**Phase 5: Integration** (~1-2 sessions)
- Wire into existing QuoteEngine as alternative to Betfair reference
- Pre-compute all next-ball scenarios for instant reaction
- Live probability output via the existing Cricbuzz/ESPN scraper
- Verification: paper trade against live Polymarket data

**Phase 6: Backtesting** (~1-2 sessions)
- Replay captured IPL 2026 event books
- Compare model probability vs Polymarket mid at each ball
- Measure: correlation, RMSE, edge detection accuracy
- Identify systematic mispricings (wicket overreaction, DLS gaps)

## End Goal

A function that takes current match state and returns:

```python
{
    "win_prob_batting_team": 0.623,        # calibrated probability
    "confidence_interval": [0.610, 0.636], # from DP exactness + calibration uncertainty
    "next_ball_scenarios": {
        "dot":    0.598,  # if next ball is a dot
        "single": 0.612,  # if next ball is a single
        "four":   0.661,  # if next ball is a four
        "six":    0.689,  # if next ball is a six
        "wicket": 0.498,  # if next ball is a wicket (depends on WHO is out)
        "wicket_striker":     0.471,  # if striker is out
        "wicket_non_striker": 0.523,  # if non-striker is out (run-out)
    },
    "edge_vs_market": 0.023,  # model - market midpoint
    "model_source": "dp_player_specific",
    "compute_time_us": 1200,  # microseconds
}
```

This feeds directly into the taker's decision logic: if `|edge_vs_market| > threshold`, fire an order.

## Verification Criteria

| Metric | Target | Measured Against |
|---|---|---|
| Brier Score | < 0.20 | Held-out IPL 2025 season |
| ECE (Expected Calibration Error) | < 0.02 | Per-phase calibration |
| RMSE vs Polymarket | < 0.03 | Captured IPL 2026 data |
| Correlation with market | > 0.95 | Ball-by-ball comparison |
| Inference latency | < 1ms | O(1) DP lookup |
| Pre-match computation | < 10 seconds | Full DP table for both innings |

## Key References

### Academic
- Davis, Perera & Swartz (2015) — "A Simulator for Twenty20 Cricket" (T20 simulation with Bayesian player models)
- Norton, Gray & Faff (2015) — "In-Play Cricket Trading Strategies" (20.8% returns from wicket overreaction on Betfair)
- Valerio (2021) — "Markov Cricket" (Forward + Inverse RL for cricket, arxiv 2103.04349)
- Asif & McHale (2016) — "In-play forecasting of win probability in ODI cricket"
- Brooker & Hogan (2011) — WASP model (DP for cricket win probability)

### Data
- Cricsheet.org — ball-by-ball JSON, IPL + all T20 (source of truth for training data)
- Cricsheet Register — player IDs mapped to ESPNCricinfo
- Captured Polymarket order books — IPL 2026 (source of truth for calibration)

### Open Source
- dr00bot T20 Ball Simulation — neural net + Monte Carlo (reference implementation)
- flumine (betcode-org) — betting framework with Polymarket + CricketMatch support
- LightGBM — primary ML model
- Rayon (Rust) — parallel Monte Carlo / DP computation
