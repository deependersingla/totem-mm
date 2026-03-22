# Backtest — Taker Flow

## Process

1. Get market slug from Polymarket (e.g. `crint-nzl-zaf-2026-03-22`)
2. Capture live orderbook + trades during match
3. Capture live score events during match
4. Parse special events (wickets, boundaries) from score data
5. Find market movements for each event
6. Run backtest to compute taker P&L

## Setup

```bash
cd /Users/sobhagyaxd/DeepWork/totem-mm/polymarket-simulator
source .venv/bin/activate
```

## Step 1 — Capture live data (run during match in tmux)

```bash
tmux new-session -s capture -d

# Pane 1: orderbook + trade capture
tmux send-keys -t capture "cd /Users/sobhagyaxd/DeepWork/totem-mm/polymarket-simulator && .venv/bin/python capture.py crint-nzl-zaf-2026-03-22 --duration 18000" Enter

# Pane 2: ESPN score logger
tmux split-window -h -t capture
tmux send-keys -t capture "cd /Users/sobhagyaxd/DeepWork/totem-mm/polymarket-simulator && .venv/bin/python score_log.py 1491738 --duration 18000" Enter

tmux attach -t capture
# Ctrl+B, D to detach
```

Output:
- `data/crint-nzl-zaf-2026-03-22/<timestamp>_crint-nzl-zaf-2026-03-22.jsonl` (orderbook)
- `captures/<timestamp>_scores_<match_id>.jsonl` (scores)

## Step 2 — Parse special events

```bash
cd /Users/sobhagyaxd/DeepWork/totem-mm/scripts

.venv/bin/python parse_events.py \
  ../polymarket-simulator/captures/<scores_file>.jsonl \
  SA NZ \
  --slug crint-nzl-zaf-2026-03-22
```

Output: `data/crint-nzl-zaf-2026-03-22/<scores_stem>_events.json`

## Step 3 — Find market movements

```bash
.venv/bin/python fill_market_movement.py \
  ../data/crint-nzl-zaf-2026-03-22/<events_file>.json \
  ../data/crint-nzl-zaf-2026-03-22/<capture_file>.jsonl \
  NZ \
  --match-date 2026-03-22
```

Updates the events JSON in-place with `market_movement` field.

## Step 4 — Run backtest

```bash
.venv/bin/python backtest_taker.py \
  ../data/crint-nzl-zaf-2026-03-22/<events_file>.json \
  ../data/crint-nzl-zaf-2026-03-22/<capture_file>.jsonl \
  crint-nzl-zaf-2026-03-22 \
  NZ SA \
  --settlement sa
```

Output: `data/crint-nzl-zaf-2026-03-22/backtest_results_crint-nzl-zaf-2026-03-22.json`

## Step 5 — Generate report (optional)

```bash
.venv/bin/python _viz.py \
  ../data/crint-nzl-zaf-2026-03-22/<events_file>.json \
  crint-nzl-zaf-2026-03-22
```

Output: `data/crint-nzl-zaf-2026-03-22/match_events_report_crint-nzl-zaf-2026-03-22.xlsx`
