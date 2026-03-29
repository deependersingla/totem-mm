# SWEEP — Endgame Position Resolution

## Quick Start

```bash
# From the polymarket-taker directory:
cargo run --bin sweep

# Or release build (faster execution):
cargo build --release --bin sweep
./target/release/sweep
```

Open **http://localhost:3001** in your browser.

The sweep binary is fully independent from the taker. You can run both simultaneously:
```bash
# Terminal 1 — taker on port 3000
cargo run

# Terminal 2 — sweep on port 3001
cargo run --bin sweep
```

---

## Config Files

| File | Purpose |
|------|---------|
| `sweep.env` | Wallet keys, chain config, builder keys, port |
| `sweep_settings.json` | Auto-saved UI state (market, wallet, sweep params) |

Both are gitignored. The sweep binary loads ONLY `sweep.env` — it does NOT read the taker's `.env`.

---

## What Sweep Does

Sweep is for the endgame of a cricket match when the result is effectively decided (one team's token near 99¢, the other near 1¢).

**Goal:** Maximize value by selling worthless (losing) tokens and accumulating winning tokens before resolution.

### Strategy

```
1. DUMP — FAK sell all losing team tokens (hits bids, accepts 3s sports delay)
2. GRID — Place resting GTC orders on both sides:
     BUY winning team at prices just below best ask (0.995, 0.994, 0.993, ...)
     SELL losing team at prices just above best bid (0.005, 0.006, 0.007, ...)
3. REFRESH — Every N seconds, cancel old grid + place new grid tracking market
4. HEARTBEAT — POST /heartbeats every 10s to keep GTC orders alive
```

### Why Resting Orders

| | Resting (maker) | Crossing (taker/FAK) |
|---|---|---|
| Matching delay | **None** | **3 seconds** (sports markets) |
| Fees | Lower (25% maker rebate) | Full taker fee |
| Book status | Goes `live` immediately | Goes `delayed` for 3s |

At endgame prices (near 0¢/100¢), fees are negligible either way. But the **zero delay** on resting orders is the key advantage — 3 seconds is an eternity in a fast-moving endgame.

### Safety Gates

| Losing team price | Behavior |
|---|---|
| >= 5¢ | **BLOCKED** — too early to call winner |
| >= 1¢ | **WARNING** — shown in events, sweep proceeds |
| < 1¢ | **PROCEED** — safe to resolve |

### Order Grid

The CLOB tracks available balance per order — you can't double-book tokens. So the grid splits quantity across price levels:

```
Example: 1000 losing tokens, 4 grid levels, best bid = 0.004

  SELL 250 @ 0.005   (1 tick above best bid — resting)
  SELL 250 @ 0.006
  SELL 250 @ 0.007
  SELL 250 @ 0.008

Example: $50 USDC budget, 4 levels, winning best ask = 0.996

  BUY 12 @ 0.995   ($11.94 reserved — 1 tick below best ask — resting)
  BUY 12 @ 0.994   ($11.93 reserved)
  BUY 12 @ 0.993   ($11.92 reserved)
  BUY 12 @ 0.992   ($11.90 reserved)
```

### Latency

Order placement latency is measured as the HTTP POST round-trip time. For resting orders, this IS the time-to-live-on-book — no additional delay. Shown per-order in the event log and as aggregate stats (last/avg/min/max) in the status endpoint.

---

## Architecture

```
sweep binary (src/bin/sweep.rs)
  │
  ├── sweep_config.rs    — loads sweep.env + sweep_settings.json
  ├── sweep_state.rs     — own AppState (position, orders, auth, latency)
  ├── sweep_server.rs    — HTTP routes + sweep loop + grid logic
  │
  └── shared library (zero duplication):
      ├── orders.rs      — post_limit_order, post_fak_order, cancel_*
      ├── ctf.rs         — split, merge, balance_of, move_tokens_*
      ├── clob_auth.rs   — EIP-712 signing, API key derivation
      ├── market_ws.rs   — orderbook WebSocket
      ├── heartbeat.rs   — keep GTC orders alive
      ├── types.rs       — OrderBook, FakOrder, Team, Side
      └── web.rs         — SWEEP_HTML UI
```

---

## UI Features

| Section | What it does |
|---|---|
| **Wallet** | Private key + proxy address. Greyed out once saved, "Edit" to unlock |
| **Market** | Fetch by Polymarket slug. Populates token IDs, tick size, team names |
| **Balances** | Live EOA + proxy USDC + token balances (5s auto-refresh) |
| **Order Book** | 5-level bid/ask for both tokens (500ms refresh) |
| **Split** | Split X USDC into X YES + X NO tokens (on-chain) |
| **Move** | Transfer tokens/USDC between EOA and proxy |
| **Builder Keys** | Polymarket builder API credentials for order attribution |
| **Sweep Controls** | Winning team, budget, grid levels, refresh interval, dry-run toggle |
| **Cancel All** | Emergency cancel ALL open orders |
| **Events** | Live log of all actions, fills, errors |

---

## Builder Keys

Builder keys attribute orders to your builder account (fee rebates). Get them from [polymarket.com/settings?tab=builder](https://polymarket.com/settings?tab=builder).

Set in `sweep.env`:
```
POLYMARKET_BUILDER_API_KEY=...
POLYMARKET_BUILDER_SECRET=...
POLYMARKET_BUILDER_PASSPHRASE=...
```

Or configure via UI (Builder Keys section).

---

## Required On-Chain Approvals

Before the sweep can place orders, the wallet needs these one-time approvals:

For **neg-risk markets** (cricket):
1. USDC.e `approve(NegRiskExchange, MAX)` — for BUY orders
2. CTF `setApprovalForAll(NegRiskExchange, true)` — for SELL orders
3. USDC.e `approve(NegRiskAdapter, MAX)` — for split/merge
4. CTF `setApprovalForAll(NegRiskAdapter, true)` — for merge/redeem

These are usually done once via the Polymarket web UI when you first connect your wallet.

---

## Fees (Sports Markets)

Current fee formula: `fee = C * feeRate * (p * (1-p))^exponent`

At endgame prices (p near 0.01 or 0.99):
- `p * (1-p)` approaches 0
- **Fees are near-zero**
- Maker orders get additional 25% rebate
