# Analysis — Match & Wallet Reporting

## Process

1. Provide market slug and match timings
2. Run match analytics — fetches all trades from Goldsky, computes per-wallet PnL, maker/taker splits
3. Run wallet report — deep dive on a specific wallet's activity

## Setup

```bash
cd /Users/sobhagyaxd/DeepWork/totem-mm/scripts
source ../venv/bin/activate  # or ../.venv/bin/activate
```

## Step 1 — Match-level analytics

Edit config at top of `match_analytics.py`:

```python
SLUG = "crint-nzl-zaf-2026-03-22"
TEAM_A = "New Zealand"
TEAM_B = "South Africa"
WINNER = "South Africa"
MATCH_DATE = "2026-03-22"
INN1_START = "11:45"
INN1_END   = "13:15"
INN2_START = "13:30"
MATCH_END  = "14:50"
```

Run:

```bash
python match_analytics.py
```

Output: `data/crint-nzl-zaf-2026-03-22/match_analytics_crint-nzl-zaf-2026-03-22.xlsx`

Sheets: Event Log, Wallet PnL, Maker/Taker, Snipers, Overlap Detection

## Step 2 — Wallet-level report

Edit config at top of `wallet_match_report.py`:

```python
WALLET = "0x4a3d9401..."
SLUG = "crint-nzl-zaf-2026-03-22"
TEAM_A = "New Zealand"
TEAM_B = "South Africa"
WINNER = "South Africa"
MATCH_DATE = "2026-03-22"
INN1_START = "11:45"
INN1_END   = "13:15"
INN2_START = "13:30"
MATCH_END  = "14:50"
```

Run:

```bash
python wallet_match_report.py
```

Output: `data/crint-nzl-zaf-2026-03-22/report_<wallet>_<slug>.xlsx`

Sheets: Trades, Position, PnL by Phase, Snipe Detection

## Cross-match comparison

Run `match_analytics.py` for multiple slugs, then compare the per-wallet PnL sheets across matches to identify consistent snipers or profitable wallets.
