# Verification & Testing Strategy

## Verification Layers

### 1. Data Pipeline Verification

**Spot-check against ESPNCricinfo Statsguru:**
```
For 10 well-known players (Kohli, Bumrah, Dhoni, etc.):
  - Compare computed career IPL SR with Statsguru
  - Compare total IPL runs, wickets, matches
  - Tolerance: exact match for counting stats, <0.5% for rates
```

**Cross-check delivery counts:**
```
For 5 random matches:
  - Verify total deliveries parsed matches ball-by-ball commentary
  - Verify extras are correctly categorized (wide vs noball vs bye)
  - Verify wicket events match official scorecards
```

**Temporal integrity:**
```
For every computed feature:
  - Assert: feature(batter, date=D) uses ONLY deliveries with match_date < D
  - Test: compute stats for a player as-of 2024-03-15 and 2024-03-16 (no match on 16th)
  - Assert: both produce identical results
```

### 2. DP Engine Verification

**Terminal state correctness:**
```
Assert V(b, r<=0, w, s, ns) == 1.0 for all b, w, s, ns  (won)
Assert V(0, r>0,  w, s, ns) == 0.0 for all r, w, s, ns  (balls out)
Assert V(b, r>0,  -1, s, ns) == 0.0                     (all out)
```

**Boundary case sanity:**
```
# 1 run needed, 120 balls, 10 wickets = near-certain win
Assert V(120, 1, 10, any, any) > 0.99

# 300 runs needed, 1 ball, 1 wicket = near-impossible
Assert V(1, 300, 1, any, any) < 0.001

# 1 run needed, 1 ball, 1 wicket = depends on P(scoring at least 1)
# With typical P(dot)=0.40, P(score>=1) ≈ 0.60
Assert 0.50 < V(1, 1, 1, any, any) < 0.75
```

**Monotonicity checks:**
```
# More balls remaining → higher win prob (holding runs, wickets constant)
Assert V(b+1, r, w, s, ns) >= V(b, r, w, s, ns)

# Fewer runs needed → higher win prob
Assert V(b, r-1, w, s, ns) >= V(b, r, w, s, ns)

# More wickets in hand → higher win prob
Assert V(b, r, w+1, s, ns) >= V(b, r, w, s, ns)
```

**Comparison against Monte Carlo:**
```
For 1000 random states:
  - Run DP lookup
  - Run MC simulation (100K iterations for low variance)
  - Assert |DP - MC| < 0.005 (0.5%)
  - This validates the DP implementation against an independent method
```

**Comparison against DLS par scores:**
```
For known DLS scenarios (published in ICC playing conditions):
  - Compute DP-implied par score (runs_needed where V ≈ 0.50)
  - Compare with DLS par score
  - Document systematic differences (DLS is player-agnostic; DP is player-specific)
```

**Known match replay:**
```
For 10 completed IPL matches:
  - Replay ball-by-ball, computing V at each delivery
  - At match end: winning team's V should approach 1.0, losing team's V → 0.0
  - Check that V moves in the correct direction on every event:
    - Boundary → V increases for batting team
    - Wicket → V decreases for batting team
    - Dot ball → V decreases for batting team (marginal)
```

### 3. Transition Model Verification

**LightGBM output validation:**
```
For all predictions:
  - Assert probabilities sum to 1.0 (within floating point tolerance)
  - Assert all probabilities in [0, 1]
  - Assert P(dot) is highest for defensive bowlers in middle overs
  - Assert P(six) is highest for power hitters in death overs
```

**Out-of-sample accuracy:**
```
Train on IPL 2008-2023, test on IPL 2024:
  - Log loss < 1.80 (9-class multinomial)
  - Per-outcome calibration: predicted P(four) ≈ actual four_rate per bin
```

**Bayesian matchup shrinkage validation:**
```
For matchups with >100 balls of data:
  - Compare Bayesian posterior with raw observed stats
  - Assert they converge (posterior ≈ observed when n >> kappa)

For matchups with <10 balls of data:
  - Assert posterior is close to the prior (career stats vs bowler type)
```

### 4. Calibration Verification

**Expected Calibration Error (ECE):**
```
Bin predictions into 10 deciles.
For each bin:
  actual_frequency = fraction of matches won
  predicted_mean = mean predicted probability in bin
  |actual - predicted| < 0.02 per bin

Overall ECE = weighted average of |actual - predicted| across bins
Target: ECE < 0.02
```

**Brier Score:**
```
BS = mean((predicted - actual)^2)
Target: BS < 0.20

For reference:
  Always predict 0.50 → BS = 0.25
  Perfect model → BS = 0.00
  Good sports model → BS = 0.18-0.22
```

**Reliability Diagram:**
```
Plot predicted probability bins (x-axis) vs actual win frequency (y-axis).
A perfectly calibrated model lies on the y=x diagonal.
Visual inspection + quantitative deviation measurement.
```

**Phase-specific calibration:**
```
Compute ECE separately for:
  - Powerplay (overs 0-5)
  - Middle overs (6-14)
  - Death overs (15-19)
  - First innings vs second innings
Each should have ECE < 0.03
```

### 5. Market Comparison Verification

**Against captured Polymarket data (IPL 2026):**
```
For each captured match:
  - Replay ball events
  - Compute model probability at each ball
  - Compare with Polymarket mid-price at closest timestamp

Metrics:
  - Correlation: target > 0.95
  - RMSE: target < 0.03 (within 3 cents)
  - Mean edge: should be near zero (model not systematically biased)
  - Edge distribution: should be symmetric around zero
```

**Wicket overreaction test (key trading edge):**
```
For each wicket event in captured data:
  - Compute model delta (model probability change)
  - Compute market delta (Polymarket price change)
  - If |market_delta| > |model_delta| consistently → overreaction confirmed
  - Measure: mean excess move = mean(|market_delta| - |model_delta|)
  - Target: statistically significant positive excess move
```

**Next-ball scenario accuracy:**
```
For each ball in test data:
  - Pre-compute model's predicted probability for each outcome
  - After the ball is bowled, check: did the model's scenario for the ACTUAL outcome
    match the market's subsequent price?
  - Measure: |model_scenario[actual_outcome] - market_price_after_event| < 0.02
```

### 6. Integration Verification

**End-to-end latency:**
```
Measure time from ball event received to trade decision:
  - State parsing: < 1ms
  - DP lookup: < 0.001ms
  - Calibration: < 0.1ms
  - Market comparison: < 0.1ms
  - Total: < 2ms
```

**Paper trading validation:**
```
Run model live alongside Polymarket for 5+ matches:
  - Log every edge signal (model vs market > threshold)
  - Track which signals would have been profitable
  - Compute hypothetical P&L after fees
  - Requirement: positive expected P&L before going live
```

## Regression Test Suite

After each code change, run:
1. DP terminal state tests (fast, <1s)
2. DP monotonicity tests (fast, <1s)
3. DP vs MC comparison for 100 states (<10s)
4. Feature store temporal integrity (<5s)
5. LightGBM prediction sanity (<1s)
6. Calibration ECE on validation set (<5s)

Total regression suite: < 30 seconds.
