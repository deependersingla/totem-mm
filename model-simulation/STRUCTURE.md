# Directory Structure

```
model-simulation/
├── PLAN.md                    # Motive, architecture overview, development strategy
├── ARCHITECTURE.md            # Detailed system architecture, module breakdown, data flow
├── COMPETITIVE_EDGE.md        # Who we trade against, edge sources, fee analysis
├── DATA_SOURCES.md            # Cricsheet spec, feature store schema, live feeds
├── DP_MATH.md                 # Mathematical specification of the DP engine
├── VERIFICATION.md            # Testing strategy, metrics, regression suite
├── STRUCTURE.md               # This file
├── REFERENCES.md              # Academic papers, repos, data sources
│
├── configs/
│   └── default.toml           # All tunable parameters
│
├── data/
│   ├── raw/                   # Downloaded Cricsheet zips (gitignored)
│   │   ├── ipl_male_json.zip
│   │   ├── t20s_male_json.zip
│   │   └── people.csv
│   ├── deliveries.parquet     # Parsed ball-by-ball data
│   ├── features/              # Computed feature store
│   │   ├── batting_phase_stats.parquet
│   │   ├── bowling_phase_stats.parquet
│   │   ├── h2h_matchups.parquet
│   │   ├── vs_bowler_type.parquet
│   │   ├── venue_stats.parquet
│   │   ├── recent_form.parquet
│   │   └── bowler_types.json
│   └── calibration/           # Captured Polymarket data for calibration
│
├── models/                    # Trained model artifacts (gitignored)
│   ├── lgbm_ball_outcome.txt
│   └── gru_momentum.pt
│
├── src/
│   ├── data/                  # Data pipeline
│   │   ├── __init__.py
│   │   ├── cricsheet_parser.py
│   │   ├── feature_store.py
│   │   ├── bowler_types.py
│   │   └── download.py
│   │
│   ├── transitions/           # Ball outcome probability models
│   │   ├── __init__.py
│   │   ├── outcome_model.py   # LightGBM multinomial
│   │   ├── sequence_model.py  # GRU momentum
│   │   ├── bayesian_matchup.py
│   │   └── builder.py         # Combine into transition probability tables
│   │
│   ├── dp/                    # Dynamic programming engine
│   │   ├── __init__.py
│   │   ├── solver.py          # Backward induction solver
│   │   ├── table.py           # DP table storage and O(1) lookup
│   │   └── states.py          # State representation, transitions, strike rotation
│   │
│   ├── calibration/           # Probability calibration
│   │   ├── __init__.py
│   │   ├── calibrator.py      # Phase-specific isotonic regression
│   │   └── validator.py       # ECE, Brier score, reliability diagrams
│   │
│   └── live/                  # Live match pricing
│       ├── __init__.py
│       ├── match_pricer.py    # Main entry: state → probability + scenarios
│       ├── scenario_computer.py
│       └── state_parser.py    # Parse Cricbuzz/ESPN data into MatchState
│
├── scripts/
│   ├── download_data.py       # Fetch Cricsheet data
│   ├── build_features.py      # Compute feature store from raw data
│   ├── train_lgbm.py          # Train transition model
│   ├── train_gru.py           # Train sequence model
│   ├── solve_dp.py            # Pre-compute DP tables
│   ├── calibrate.py           # Fit calibration models
│   ├── backtest.py            # Replay captured matches, compare with market
│   └── validate.py            # Run all verification checks
│
├── tests/
│   ├── test_parser.py
│   ├── test_feature_store.py
│   ├── test_dp_terminal.py
│   ├── test_dp_monotonicity.py
│   ├── test_dp_vs_mc.py
│   ├── test_transitions.py
│   └── test_calibration.py
│
├── notebooks/                 # Exploration and analysis
│   ├── 01_data_exploration.ipynb
│   ├── 02_feature_analysis.ipynb
│   ├── 03_dp_visualization.ipynb
│   └── 04_market_comparison.ipynb
│
├── pyproject.toml
└── .gitignore
```

## Build Order

```
Phase 1: Data Pipeline
  scripts/download_data.py → data/raw/
  src/data/cricsheet_parser.py → data/deliveries.parquet
  src/data/bowler_types.py → data/features/bowler_types.json
  src/data/feature_store.py → data/features/*.parquet

Phase 2: DP Engine
  src/dp/states.py → state representation + transitions
  src/dp/solver.py → backward induction
  src/dp/table.py → storage + lookup

Phase 3: Transition Model
  src/transitions/outcome_model.py → LightGBM training
  src/transitions/sequence_model.py → GRU training
  src/transitions/bayesian_matchup.py → matchup shrinkage
  src/transitions/builder.py → combine into DP-ready probabilities

Phase 4: Calibration
  src/calibration/calibrator.py → isotonic regression
  src/calibration/validator.py → metrics + plots

Phase 5: Integration
  src/live/state_parser.py → parse live data
  src/live/match_pricer.py → probability output
  src/live/scenario_computer.py → next-ball pre-computation

Phase 6: Backtesting
  scripts/backtest.py → replay captured matches
  scripts/validate.py → full verification suite
```
