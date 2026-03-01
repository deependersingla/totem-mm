# Running the live feeds (polling + streaming)

Two scripts write real-time odds to `.txt` files. You can run them in **two separate virtual environments** in parallel.

## Output files

| Script | Output file | Update frequency |
|--------|-------------|------------------|
| **Polling** | `data/live_odds_polling.txt` | One row every 1 second |
| **Streaming** | `data/live_odds_streaming.txt` | One row per Betfair or Polymarket update (queue, no drops) |

## Prerequisites

- `.env` in project root with:
  - **Polling:** `BETFAIR_MARKET_IDS`, `BETFAIR_APP_KEY`, `BETFAIR_SESSION_TOKEN`, `TOKEN_MAP`
  - **Streaming:** Same, plus Betfair **streaming** credentials: `BETFAIR_USERNAME`, `BETFAIR_PASSWORD`, `BETFAIR_APP_KEY`, and SSL certs (`BETFAIR_CERTS` or `BETFAIR_CERT_FILE`)

## Run polling (e.g. in `.venv`)

From project root:

```bash
# Activate first venv (e.g. .venv)
source .venv/bin/activate

# Install deps if needed
pip install -e .

# Run polling (writes to data/live_odds_polling.txt every 1s)
python scripts/live_feed_polling.py
```

Leave this running. To watch the file:

```bash
tail -f data/live_odds_polling.txt
```

## Run streaming (e.g. in `venv`)

In a **second terminal**, from project root:

```bash
# Activate second venv (e.g. venv)
source venv/bin/activate

# Install deps if needed (includes betfairlightweight for streaming)
pip install -e .

# Run streaming (writes to data/live_odds_streaming.txt on every update)
python scripts/live_feed_streaming.py
```

Leave this running. To watch the file:

```bash
tail -f data/live_odds_streaming.txt
```

## Summary

| Venv | Command |
|------|--------|
| `.venv` | `source .venv/bin/activate` → `python scripts/live_feed_polling.py` |
| `venv` | `source venv/bin/activate` → `python scripts/live_feed_streaming.py` |

Both scripts use the same `.env` and write to different files, so they do not interfere. Stop with `Ctrl+C` in each terminal.
