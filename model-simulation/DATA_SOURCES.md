# Data Sources & Stats Pipeline

## Source of Truth: Cricsheet.org

### Downloads
- IPL: `https://cricsheet.org/downloads/ipl_male_json.zip`
- T20 Internationals: `https://cricsheet.org/downloads/t20s_male_json.zip`
- All T20 leagues: BBL, CPL, PSL, The Hundred, SA20 — individual zips at cricsheet.org/downloads/
- Player Registry: `https://cricsheet.org/register/` (maps to ESPNCricinfo IDs)

### JSON Structure Per Match

```json
{
  "info": {
    "dates": ["2026-04-04"],
    "venue": "Wankhede Stadium",
    "event": {"name": "Indian Premier League", "match_number": 23},
    "toss": {"decision": "field", "winner": "Mumbai Indians"},
    "players": {
      "Mumbai Indians": ["RG Sharma", "Ishan Kishan", ...],
      "Chennai Super Kings": ["MS Dhoni", ...]
    },
    "registry": {"people": {"RG Sharma": "abc12345"}},
    "outcome": {"winner": "Mumbai Indians", "by": {"runs": 8}}
  },
  "innings": [{
    "team": "Mumbai Indians",
    "overs": [{
      "over": 0,
      "deliveries": [{
        "batter": "RG Sharma",
        "bowler": "DJ Bravo",
        "non_striker": "Ishan Kishan",
        "runs": {"batter": 4, "extras": 0, "total": 4},
        "extras": {},
        "wickets": [{"player_out": "...", "kind": "caught", "fielders": [...]}]
      }]
    }]
  }]
}
```

### Fields Per Delivery
| Field | Description |
|---|---|
| `batter` | Name of batter on strike |
| `bowler` | Name of bowler |
| `non_striker` | Name of batter at non-striker end |
| `runs.batter` | Runs off bat: 0, 1, 2, 3, 4, 6 |
| `runs.extras` | Extra runs this delivery |
| `runs.total` | Total runs from delivery |
| `extras.wides` | Wide runs (if applicable) |
| `extras.noballs` | No-ball runs (if applicable) |
| `extras.byes` | Bye runs |
| `extras.legbyes` | Leg-bye runs |
| `wickets[].kind` | caught, bowled, lbw, stumped, run_out, hit_wicket |
| `wickets[].player_out` | Who got dismissed |
| `wickets[].fielders` | Fielders involved |

### Data Volume
| Source | Matches | Deliveries | Players |
|---|---|---|---|
| IPL only | ~1,200 | ~300K | ~600 |
| All T20 (IPL+T20I+BBL+CPL+PSL+etc) | ~8,000+ | ~2M+ | ~3,000+ |

### What's NOT in Cricsheet (Gaps)
- **Bowler type (pace/spin)** — must lookup via ESPNCricinfo
- Ball speed / trajectory / line & length
- Field placement
- Shot type / wagon wheel
- Weather conditions at match time
- Pitch type (batting/bowling/balanced)

## Bowler Type Classification

Cricsheet's `registry.people` provides ESPNCricinfo IDs via `people.csv`.
Use `python-espncricinfo` to get `bowling_style`:

```python
from espncricinfo.player import Player
p = Player('253802')  # ESPNCricinfo ID
# p.bowling_style → "Right-arm fast-medium"
# p.batting_style → "Right-hand bat"
# p.playing_role  → "Bowling Allrounder"
```

Classification rules:
```
"fast", "medium", "pace", "seam" → pace
"spin", "orthodox", "off break", "leg break", "wrist", "chinaman" → spin
```

Build once as `bowler_types.json`, refresh per season for new players.

## Feature Store Computation

All features computed with temporal integrity: only use data BEFORE the match date.

### Batting Stats by Phase
```python
# Phase: powerplay (overs 0-5), middle (6-14), death (15-19)
per (batter, phase):
  - strike_rate = runs / legal_balls * 100
  - dot_ball_pct = dots / legal_balls * 100
  - four_pct = fours / legal_balls * 100
  - six_pct = sixes / legal_balls * 100
  - boundary_pct = (fours + sixes) / legal_balls * 100
  - dismissal_rate = dismissals / legal_balls
  - balls_to_settle = modeled via SR(balls_faced) = SR_max * (1 - exp(-bf/tau))
```

### Bowling Stats by Phase
```python
per (bowler, phase):
  - economy = runs_conceded / (legal_balls / 6)
  - dot_ball_pct
  - wickets_per_match = total_wickets / matches
  - boundary_concession_rate
```

### Head-to-Head (Batter vs Bowler)
```python
per (batter, bowler):
  - balls, runs, dismissals, strike_rate
  - Apply Bayesian shrinkage: posterior = (n/(n+kappa)) * observed + (kappa/(n+kappa)) * prior
  - kappa = 40 balls (prior strength)
  - prior = batter's overall stats vs bowler_type (pace/spin) in same phase
```

### Batter vs Bowler Type
```python
per (batter, bowler_type={pace,spin}, phase):
  - strike_rate, dot_pct, boundary_pct, dismissal_rate
```

### Venue Stats
```python
per venue:
  - avg_1st_innings_score, avg_2nd_innings_score
  - chase_success_rate
  - powerplay_avg_score, death_avg_score
  - toss_field_win_rate
  - scoring_rate_by_phase
```

### Rolling Form
```python
per (batter, as_of_date):
  - last_5_innings_avg, last_10_innings_avg
  - last_5_innings_sr
  - Apply exponential decay: weight(match) = exp(-0.004 * days_since_match)
```

### Storage
All stored as Parquet files in `data/features/`:
```
data/features/
  batting_phase_stats.parquet
  bowling_phase_stats.parquet
  h2h_matchups.parquet
  vs_bowler_type.parquet
  venue_stats.parquet
  recent_form.parquet
  bowler_types.json
```

## Live Data Feed (For Real-Time Ball Events)

Existing infrastructure in `polymarket-simulator/`:
- `cricket_score.py` — Cricbuzz scraper (3s poll interval)
- `combined_score.py` — ESPN + Cricbuzz dual-source

These provide ball-by-ball events with: score, overs, batsman, bowler, run rate, required rate.

Latency: 60-120 seconds behind actual ball. This is fine — the model provides instant fair value at every state. When the API confirms an event, you already know your target probability from the pre-computed next-ball scenarios.

## Calibration Data

Captured Polymarket order books from IPL 2026 matches in `captures/`:
- 4 IPL matches (April 4-6, 2026)
- ~387K order book snapshots at 30-50ms granularity
- 5-level depth (bid/ask price+size) for both teams
- Used for calibrating model probabilities against actual market prices

## Python Dependencies for Data Pipeline

```
pandas>=2.0
pyarrow          # parquet support
polars           # fast alternative to pandas for large datasets
python-espncricinfo  # player metadata from ESPNCricinfo
httpx            # async HTTP for downloads
lightgbm
torch            # GRU model
scikit-learn     # calibration (isotonic regression)
```
