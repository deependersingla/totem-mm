# Current State — What Exists, What Works, What Doesn't

## What's Built (v1)

### Data Pipeline ✓
- Cricsheet parser: 1,191 IPL matches → 283,229 deliveries in parquet
- Feature store: batting/bowling stats by phase, 30K H2H matchups, 59 venues
- All working, tested, committed

### DP Engine ✓  
- Backward induction solver: 467K states (balls × runs × wickets)
- Solves in 0.8s, O(1) lookup, all sanity checks pass
- Parameterizable transition probabilities (can swap in any source)
- Supports per-phase transitions
- Working, tested, committed

### LightGBM Transition Model ✓
- 9-class ball outcome classifier, trained on 278K samples
- Test log loss: 1.55, accuracy: 40.8%
- Produces phase-level transition probability vectors
- Working, committed, but too generic (phase averages only)

### Calibration ✓
- Isotonic regression calibrator
- Trained on 17K historical predictions
- Working, committed

### Honest Test Suite ✓
- Tests against actual captured Polymarket order book data
- Correct chasing team identification via event-reaction method
- Rain-reduced match detection (skips matches with < 80 2nd-inn balls)
- Per-match RMSE breakdown
- Working, committed

## Honest Performance

### Where It Works (RMSE < 0.10)
- Typical IPL chases (target 150-175): tracks Polymarket within 3-5 cents
- Death overs across all matches: RMSE 0.089
- Example: SUN vs LUC (target 157) — nearly perfect, ~3 cent error

### Where It's Decent (RMSE 0.10-0.20)  
- Moderate targets (target 175-200): ~10-15 cent systematic underestimate
- Example: DEL vs MUM (target 163) — 5 cent error, converges in middle overs

### Where It Fails (RMSE > 0.30)
- High targets (200+): model underestimates chase probability by 25-35 cents
- Reduced-overs matches: model doesn't know the match is shortened
- Tough pitch + low target: model overestimates because it uses avg conditions
- Example: GUJ vs RAJ (target 211) — 30 cent error early, converges late
- Example: RAJ vs MUM (11-over match) — catastrophic, skipped

### Root Causes (Ordered by Impact)
1. No player identity → treats Kohli same as #11
2. No pitch/conditions awareness → uses avg scoring regardless
3. No reduced-overs support → assumes all matches are 120 balls
4. No batter settling-in effect → misses 3-4x wicket risk for new batters
5. No recent-ball history → misses pressure/momentum effects

## File Layout

```
model-simulation/
├── PLAN.md                 # Original architecture plan
├── ARCHITECTURE.md         # System design (still valid for structure)
├── COMPETITIVE_EDGE.md     # Market analysis (still valid)
├── DATA_SOURCES.md         # Cricsheet spec, features (still valid)  
├── DP_MATH.md              # Bellman equations (still valid)
├── VERIFICATION.md         # Test strategy (needs updating for honest tests)
├── LESSONS_LEARNED.md      # What went wrong, don't repeat
├── V2_SPEC.md              # What to build next
├── CURRENT_STATE.md        # This file
├── REFERENCES.md           # Papers, repos, data sources
├── HONEST_SCORES.json      # Real test results
├── configs/default.toml    # Configuration
├── src/
│   ├── data/               # Parser + feature store (working)
│   ├── dp/                 # DP solver (working, needs total_overs param)
│   ├── transitions/        # LightGBM model (working, needs player-specific)
│   ├── calibration/        # Isotonic regression (working)
│   └── live/               # Empty — not built yet
├── scripts/
│   ├── runner.py           # Full pipeline (working)
│   └── honest_test.py      # Real validation (working)
├── data/
│   ├── raw/                # Cricsheet JSONs (gitignored)
│   ├── deliveries.parquet  # Parsed data (gitignored)
│   └── features/           # Feature store (gitignored)
└── models/                 # Trained models (gitignored)
```

## Git State
- Branch: `model-simulation`
- 5 commits
- All code committed and working
