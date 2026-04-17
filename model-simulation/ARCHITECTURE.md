# Architecture — Cricket Win Probability Engine

## System Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                     PRE-MATCH (once per match)                   │
│                                                                  │
│  Playing XIs ──► Feature Store ──► Transition Probabilities       │
│       │              │                    │                       │
│       │              │            ┌───────▼────────┐             │
│       │              │            │  DP Backward    │             │
│       │              │            │  Induction      │             │
│       │              │            │  (~17ms)        │             │
│       │              │            └───────┬────────┘             │
│       │              │                    │                       │
│       │              │            DP Table (150MB)                │
│       │              │            18.7M states                    │
│       │              │            All targets 80-300              │
└──────┼──────────────┼────────────┼───────────────────────────────┘
       │              │            │
       ▼              ▼            ▼
┌──────────────────────────────────────────────────────────────────┐
│                     LIVE MATCH (per ball)                         │
│                                                                  │
│  Ball Event ──► Parse State ──► DP Lookup ──► Win Probability    │
│  (Cricbuzz/     (1ms)          (1ns)          │                  │
│   ESPN)                                       │                  │
│                                               ▼                  │
│                              ┌─────────────────────────┐         │
│                              │  Next-Ball Scenarios     │         │
│                              │  dot:    V(b-1, r, w)   │         │
│                              │  single: V(b-1, r-1, w) │         │
│                              │  four:   V(b-1, r-4, w) │         │
│                              │  six:    V(b-1, r-6, w) │         │
│                              │  wicket: V(b-1, r, w-1) │         │
│                              └───────────┬─────────────┘         │
│                                          │                       │
│                              ┌───────────▼─────────────┐         │
│                              │  Compare vs Market       │         │
│                              │  edge = model - market   │         │
│                              │  if |edge| > threshold   │         │
│                              │    → fire taker order    │         │
│                              └─────────────────────────┘         │
└──────────────────────────────────────────────────────────────────┘
```

## Module Breakdown

### 1. Data Pipeline (`src/data/`)

```
src/data/
  cricsheet_parser.py    — Parse Cricsheet JSON → flat deliveries DataFrame
  feature_store.py       — Compute all player/venue/matchup features
  bowler_types.py        — ESPNCricinfo lookup for pace/spin classification
  download.py            — Fetch latest Cricsheet zips
```

**Input:** Cricsheet JSON zip files (IPL + all T20)
**Output:** Parquet files in `data/`:
  - `deliveries.parquet` — all ball-by-ball data (~2M rows for all T20)
  - `batting_phase_stats.parquet` — per (batter, phase, as_of_date)
  - `bowling_phase_stats.parquet` — per (bowler, phase, as_of_date)
  - `h2h_matchups.parquet` — per (batter, bowler)
  - `venue_stats.parquet` — per venue
  - `bowler_types.json` — {player_name: "pace"|"spin"|"unknown"}

### 2. Transition Model (`src/transitions/`)

```
src/transitions/
  outcome_model.py       — LightGBM multinomial ball outcome predictor
  sequence_model.py      — GRU momentum correction
  bayesian_matchup.py    — Hierarchical Bayesian for sparse batter-vs-bowler
  builder.py             — Combine into per-(batter, bowler, phase) probability vectors
```

**Outcome space per legal delivery:**
```
outcomes = [dot, single, double, triple, four, six, wicket, wide, noball]
P(outcome | batter, bowler, phase, venue, conditions) → 9-dim probability vector
```

**LightGBM features (~35 total):**
```
# Batter features (by phase)
batter_sr, batter_dot_pct, batter_boundary_pct, batter_dismissal_rate
batter_vs_pace_sr, batter_vs_spin_sr
batter_balls_faced_this_innings  # settled vs fresh
batter_form_last_5_innings

# Bowler features (by phase)  
bowler_economy, bowler_dot_pct, bowler_wickets_per_match
bowler_boundary_concession_rate
bowler_type  # categorical: pace/spin

# Matchup
h2h_sr, h2h_dismissal_rate, h2h_balls  # with Bayesian shrinkage

# Match state
phase, balls_remaining, wickets_in_hand, runs_needed (2nd inn)
current_run_rate, required_run_rate
partnership_runs, partnership_balls

# Venue/conditions
venue_avg_score, venue_chase_success_rate
bat_first_or_chase, dew_factor, pitch_type
```

### 3. DP Engine (`src/dp/`)

```
src/dp/
  solver.py              — Backward induction solver (Python prototype)
  table.py               — DP table storage and lookup
  states.py              — State representation and transition logic
```

**State representation:**
```python
@dataclass
class MatchState:
    balls_remaining: int      # 0-120
    runs_needed: int          # 0-350 (0 = won for chase)
    wickets_in_hand: int      # 0-10
    striker_idx: int          # 0-10 (index in batting order)
    non_striker_idx: int      # 0-10

# Total states with player identity: 121 * 351 * 11 * batting_pairs = 18.7M
# Memory: 150 MB (float64 per state)
```

**Bellman equation (chase innings):**
```
V(b, r, w, s, ns) = 
    P(dot)    * V(b-1, r,   w,   s', ns')    +
    P(single) * V(b-1, r-1, w,   ns', s')    +  # strike rotates on odd runs
    P(double) * V(b-1, r-2, w,   s', ns')    +
    P(triple) * V(b-1, r-3, w,   ns', s')    +
    P(four)   * V(b-1, r-4, w,   s', ns')    +
    P(six)    * V(b-1, r-6, w,   s', ns')    +
    P(wicket) * V(b-1, r,   w-1, next, alive) +
    P(wide)   * V(b,   r-1, w,   s,  ns)     +  # ball NOT consumed
    P(noball) * V(b,   r-1, w,   s,  ns)        # ball NOT consumed

where s', ns' apply end-of-over strike rotation if (120 - (b-1)) % 6 == 0

Terminal conditions:
    V(_, r<=0, _, _, _) = 1.0   # won
    V(_, r>0,  0, _, _) = 0.0   # all out
    V(0, r>0,  _, _, _) = 0.0   # balls exhausted
```

**First innings model:**
For the first innings, we compute expected win probability differently:
1. At each state (b, r, w, s, ns), compute the probability distribution over final total
2. For each possible total T, lookup chase win probability from the pre-computed chase DP table
3. First innings V(state) = sum_T P(total=T | state) * (1 - V_chase(120, T, 0, opener1, opener2))

### 4. Calibration (`src/calibration/`)

```
src/calibration/
  calibrator.py          — Phase-specific isotonic regression
  validator.py           — Brier score, ECE, reliability diagrams
  market_comparison.py   — Compare model vs captured Polymarket data
```

**Calibration pipeline:**
```
Raw DP probability
  → Phase-specific isotonic regression (trained on validation season)
  → Venue adjustment (historical pricing residuals at this ground)
  → Final calibrated probability
```

### 5. Live Engine (`src/live/`)

```
src/live/
  match_pricer.py        — Main entry point: match state → probability
  scenario_computer.py   — Pre-compute all next-ball outcomes
  state_parser.py        — Parse Cricbuzz/ESPN data into MatchState
```

**The core function:**
```python
def price_match(match_state: MatchState, dp_table: DPTable, calibrator: Calibrator) -> MatchPrice:
    # O(1) lookup
    raw_prob = dp_table.lookup(match_state)
    
    # Calibrate
    calibrated_prob = calibrator.transform(raw_prob, phase=match_state.phase)
    
    # Pre-compute all next-ball scenarios
    scenarios = {}
    for outcome in [Dot, Single, Double, Triple, Four, Six]:
        next_state = match_state.apply(outcome)
        scenarios[outcome] = calibrator.transform(dp_table.lookup(next_state), ...)
    
    # Wicket scenarios (depends on WHO gets out)
    for batter in [match_state.striker, match_state.non_striker]:
        next_state = match_state.apply_wicket(dismissed=batter)
        scenarios[f"wicket_{batter}"] = calibrator.transform(dp_table.lookup(next_state), ...)
    
    return MatchPrice(
        win_prob=calibrated_prob,
        scenarios=scenarios,
        compute_time_us=elapsed,
    )
```

### 6. Rust Production Engine (`src/rust/`) — Phase 2 Target

For production deployment, the DP engine ports to Rust:

```rust
// 14-byte state struct, fits in cache line
#[derive(Clone, Copy)]
struct SimState {
    runs_needed: u16,       // 2 bytes
    balls_remaining: u8,    // 1 byte
    wickets: u8,            // 1 byte
    striker_idx: u8,        // 1 byte
    non_striker_idx: u8,    // 1 byte
    // padding to 8-byte alignment
}

// DP table as flat array with computed indexing
struct DPTable {
    data: Vec<f32>,         // f32 sufficient (saves 50% memory: 75MB vs 150MB)
    dims: [usize; 5],      // (balls, runs, wickets, striker, non_striker)
}

impl DPTable {
    fn lookup(&self, state: &SimState) -> f32 {
        let idx = self.flat_index(state);
        self.data[idx]
    }
    
    fn precompute(&mut self, transitions: &TransitionTable) {
        // Backward induction, vectorized with SIMD
        // ~17ms on M-series chip
    }
}
```

Exposed to Python via PyO3 for integration with existing trading system.

## Data Flow

```
[Cricsheet JSON] ──parse──► [deliveries.parquet]
                                    │
                              ──compute──► [feature_store/*.parquet]
                                                   │
                                             ──train──► [LightGBM model]
                                                   │        │
                                                   │   ──predict──► [transition_probs.npz]
                                                   │                       │
                                                   │                 ──solve──► [DP table]
                                                   │                       │
[live ball event] ──parse──► [MatchState] ───lookup──► [win_probability]
                                                   │
                                             ──compare──► [edge vs market]
                                                   │
                                             ──decide──► [trade / no-trade]
```

## Hardware Requirements

| Component | RAM | Disk | CPU |
|---|---|---|---|
| Deliveries DataFrame (all T20) | ~1 GB | ~200 MB (parquet) | - |
| Feature store | ~200 MB | ~50 MB (parquet) | - |
| LightGBM model | ~10 MB | ~5 MB | - |
| GRU model | ~50 MB | ~2 MB | MPS for training |
| DP table (f32) | ~75 MB | - | ~17ms compute |
| Live match state | ~10 MB | - | - |
| **Total** | **~1.3 GB** | **~260 MB** | **Fits on 16GB Mac** |
