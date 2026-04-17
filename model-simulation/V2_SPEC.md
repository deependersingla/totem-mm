# V2 Specification — What To Build Next

## The Only Question That Matters

Given current match state → P(chasing team wins)?

Everything serves this single output.

## What Moves Win Probability (Ranked by Impact)

### Tier 1 — Must Have (accounts for ~80% of error)

1. **Player identity in transitions**
   - WHO is batting changes everything. Career SR, phase SR, matchup vs bowler type.
   - WHO is bowling: death specialist vs spinner in death = completely different game.
   - The DP table supports player identity (18.7M states, 150MB). The missing piece is feeding player-specific transition probabilities into the solver.
   - Implementation: Before each match, compute per-(batter, bowler_type, phase) outcome distributions from Cricsheet. Feed into DP solver. Pre-compute for the specific playing XI.

2. **Match-specific conditions**
   - **Total overs**: Not every match is 20 overs. Reduced-overs matches have different rules:
     - Powerplay proportionally reduced (e.g., 2 overs in an 11-over game)
     - Max overs per bowler reduced (e.g., 2 overs instead of 4)
     - The DP solver MUST take total_overs as a parameter, not hardcode 120 balls
   - **Pitch/venue proxy**: Use 1st innings score relative to venue average as a multiplier on scoring transitions. If 1st innings was 150 at a venue that averages 190, scale all scoring probabilities down by ~20%.

3. **Batter balls faced (settling-in effect)**
   - New batter: dismissal probability 3-4x higher in first 5-15 balls
   - This is the single biggest dependency effect in cricket
   - Add `batter_balls_faced` as a state variable or as a modifier on transition probabilities

### Tier 2 — Should Have (accounts for ~15% of error)

4. **Recent ball history (2nd order Markov)**
   - After 3+ consecutive dots: wicket probability increases 15-25%
   - After a boundary: scoring probability slightly elevated for next 2-3 balls
   - Track last 2-3 ball outcomes as input to transition model

5. **Free hit tracking**
   - After a no-ball, next ball is a free hit (only run-out possible)
   - 0.46% of deliveries are no-balls → 0.46% of next deliveries are free hits
   - On free hits: wicket probability drops to ~0.5% (run-out only), scoring goes up
   - Add boolean `is_free_hit` as state or modifier

6. **Partnership state**
   - Two settled batters (both 15+ balls) score 15-20% faster
   - One new + one settled = lower scoring rate
   - Partnership runs/balls as input features

### Tier 3 — Nice to Have (accounts for ~5% of error)

7. **Bowling resources tracking**
   - Which bowlers have overs remaining
   - Quality of remaining bowling options (best death bowler used up vs available)
   - Handled as modifier on transition probs, not state dimension

8. **Within-over patterns**
   - Ball 1 of an over: bowler finding length, ~10% fewer runs
   - Ball 6: slightly more attacking, ~10% more runs
   - Minor effect, easily added as modifier

9. **Super over handling**
   - When scores are tied: separate 1-over game with different dynamics
   - Rare but affects tie probability calculation

## Data Requirements

### Already Have
- Cricsheet ball-by-ball: 1,191 IPL matches, 283K deliveries ✓
- Feature store: batting/bowling phase stats, H2H matchups, venue stats ✓
- DP solver: backward induction, 467K-18.7M states ✓
- Captured Polymarket data: 5+ IPL 2026 matches with order books ✓

### Need to Build
- Per-player outcome distributions by phase (from Cricsheet) — compute from existing data
- Venue average scores for condition proxy — already in venue_stats.parquet
- Reduced-overs DP mode — parameterize solver by total_overs
- Batter balls-faced modifier — add to transition probability function

## Architecture Change

Current: `phase_average_transitions → DP solver → win_prob`

V2: `(batter_stats, bowler_stats, venue_factor, balls_faced_modifier, recent_history) → player_specific_transitions → DP solver(total_overs) → win_prob`

The DP solver itself doesn't change. Only the inputs change.

## Validation Plan

### Test Against Captured Matches (honest)
For each 2nd innings event in captured Polymarket data:
1. Look up actual batter/bowler from the cricket score data (Cricbuzz/ESPN captures in `data/_scores/`)
2. Compute player-specific transition probabilities
3. Run DP with correct total overs
4. Compare model probability vs Polymarket mid-price
5. Measure RMSE, correlation, per-event error

### Target Metrics (realistic)
- RMSE vs Polymarket < 0.10 (currently 0.15 on full matches, 0.03-0.05 on typical targets)
- Correlation > 0.95 (currently 0.93)
- No catastrophic failures on reduced-overs or extreme targets

### What Success Looks Like
The model doesn't need to beat the market on every ball. It needs to:
1. Track the market within ~5 cents on normal plays (dots, singles)
2. Disagree meaningfully on specific events where human intuition fails:
   - Wicket of a tail-ender (market overreacts, model knows it's low-impact)
   - Nonlinear death-over math (36 off 12 balls, 8 wkts vs 3 wkts)
   - Reduced-overs chase probability (market may price DLS wrong)

## Key References for Implementation

- **DynaSim (Mysore 2023)**: 57 outcomes, 20 features, 76% accuracy. IPL-specific. Behind paywall but methodology reconstructable. Key insight: ML and sequence models performed similarly (76% both) → Markov state is sufficient.
- **Davis-Perera-Swartz (2015)**: Hierarchical Bayesian player models for T20 simulation. The gold standard for player-specific outcome distributions.
- **Dey-Ganguly-Saikia (2017)**: Tested higher-order Markov models, found 2nd-order captures virtually all structure.
- **Norton-Gray-Faff (2015)**: Proved 20.8% returns from first-innings wicket overreaction on Betfair. Direct evidence of exploitable market inefficiency.

## Dependency Question — Resolved

Ball events are NOT independent. They are approximately Markov given a rich enough state:
- Match state (score, wickets, balls) ✓ already in DP
- Batter state (balls faced, runs scored) — NEED TO ADD
- Recent history (last 2-3 ball outcomes) — NEED TO ADD
- Partnership state (runs, balls together) — NICE TO HAVE

A 2nd-order Markov model (last 2 balls + state) captures virtually all exploitable structure. Going deeper yields diminishing returns.

## Reduced Overs Rules (Correct Understanding)

**Rain BEFORE match starts:**
- Match is officially reduced (e.g., 11 overs per side)
- NOT a DLS situation
- Powerplay proportionally reduced (e.g., 2 overs PP in 11-over game)
- Max overs per bowler proportionally reduced (e.g., 2 overs max in 11-over game)  
- Target is simply: 1st innings score + 1

**Rain DURING match (interrupting play):**
- DLS method applies to adjust target
- Par scores based on resources remaining (overs + wickets)
- The DP model needs DLS tables to handle this correctly

**Super Over (tie):**
- 1 over per side (6 balls)
- 3 batters, 1 bowler from each team
- Separate mini-game, not part of main match DP
