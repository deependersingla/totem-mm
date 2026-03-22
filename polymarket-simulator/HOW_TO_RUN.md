# Polymarket Simulator — How to Run

## Setup (one time)

```bash
cd /Users/sobhagyaxd/DeepWork/totem-mm/polymarket-simulator
source .venv/bin/activate
```

---

## Option 1: Live Event Capture (JSONL + Excel)

Captures every WS event at nanosecond precision, exports to Excel.
Run this BEFORE the match starts and let it run throughout.

```bash
source .venv/bin/activate

# For a cricket match (replace slug with actual match):
python capture.py crint-nzl-zaf-2026-03-17 --duration 14400

# Duration examples:
#   --duration 14400   = 4 hours (full T20 match)
#   --duration 21600   = 6 hours (ODI)
#   --duration 600     = 10 minutes (quick test)

# To stop early: Ctrl+C (Excel still exports)
```

**Output files** (in `captures/` directory):
- `captures/YYYYMMDD_HHMMSS_<slug>.jsonl` — raw events, every line is one event
- `captures/YYYYMMDD_HHMMSS_<slug>.xlsx` — Excel with 5 sheets:
  - **All Events** — every event with IST timestamp, type, outcome, side, price, size, wallet
  - **Trades** — only trades (WS ticks + REST with wallets)
  - **Level Changes** — every book level mutation (adds, cancels, fills)
  - **Book Snapshots** — L2 snapshots with depth stats
  - **Summary** — stats + data source documentation

**What gets captured:**
- Every WS `price_change` (book level add/remove) — nanosecond timestamp
- Every WS `last_trade_price` (trade tick) — with tx hash
- Every REST trade (2s poll) — with taker wallet address
- Every WS `book` snapshot — full L2 state

---

## Option 2: Live Trading Simulator (Web UI)

Full web UI with live order book, mock trading, wallet tracking, sniping detection.
Also records all events to `recordings/` directory.

```bash
source .venv/bin/activate
python -m simulator
# Open http://localhost:8899
```

**In the browser:**
1. Search for market in the top bar (e.g. "New Zealand South Africa")
2. Click the result to load it — live order book appears
3. Place simulated orders:
   - **FAK** — Fill-And-Kill (sweep book, kill unfilled)
   - **FOK** — Fill-Or-Kill (all or nothing)
   - **GTC** — Good-Til-Cancelled (rests with queue position tracking)
   - **GTD** — Good-Til-Date (rests until expiration)
4. Right panel: live wallet tracker, market trades, wallet subset books, inventory graphs

**Recording:** All events auto-saved to `recordings/` while the simulator runs.

---

## Option 3: Both at the Same Time

Run capture in one terminal, simulator in another:

**Terminal 1 (capture):**
```bash
cd /Users/sobhagyaxd/DeepWork/totem-mm/polymarket-simulator
source .venv/bin/activate
python capture.py crint-nzl-zaf-2026-03-17 --duration 14400
```

**Terminal 2 (simulator UI):**
```bash
cd /Users/sobhagyaxd/DeepWork/totem-mm/polymarket-simulator
source .venv/bin/activate
python -m simulator
# Open http://localhost:8899, search for same market
```

---

## Finding Market Slugs

Cricket matches use `crint-` prefix:
```
crint-ind-nzl-2026-03-20       # India vs New Zealand, March 20
crint-nzl-zaf-2026-03-17       # NZ vs SA, March 17
crint-gbr-ind-2026-03-05       # England vs India, March 5
```

The slug is the last part of the Polymarket URL:
`https://polymarket.com/sports/crint/crint-nzl-zaf-2026-03-17` → slug = `crint-nzl-zaf-2026-03-17`

---

## Data Accuracy Notes

**What is REAL (definitive from Polymarket):**
- `trade` events — a fill happened at this exact price/size
- `rest_trade` events — same fill but with taker's wallet address
- `book_snapshot` — full L2 state at that moment

**What is INFERRED (heuristic, ~95% accurate):**
- `level_increase` — "something was added at this price" (could be 1 or 10 orders)
- `level_decrease_cancel` — "liquidity left without a matching trade" (probably cancel or GTD expiry)
- `level_decrease_fill` — "liquidity left WITH a matching trade" (probably filled)

**What is NOT available from Polymarket Market WS:**
- Order type (GTC/GTD/FOK/FAK) for other people's orders
- Individual order IDs
- Wallet addresses on book events
- Explicit cancel events
