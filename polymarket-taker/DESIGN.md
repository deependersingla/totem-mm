# totem-taker — Design Document

Ultra low-latency Polymarket CLOB taker. Oracle-driven FAK order placement for cricket markets.

---

## 1. What This Is

A standalone Rust service that:
1. Subscribes to **Polymarket CLOB orderbook** via WebSocket (real-time L2 data)
2. Polls/streams a **cricket oracle** that predicts true odds
3. When the oracle price diverges from the CLOB price beyond a threshold, places a **FAK (Fill and Kill) limit order** to take mispriced liquidity
4. Monitors fills via the **User WebSocket channel**

This is a **pure taker** — no resting orders, no market making. Every order either fills immediately or is killed.

---

## 2. FAK Order Mechanics

**FAK = Fill and Kill (also called IOC — Immediate or Cancel)**

- A limit order with a price cap that executes against resting liquidity
- Whatever portion fills at or better than the limit price fills immediately
- The unfilled remainder is cancelled — never rests on the book
- Polymarket CLOB supports this as `time_in_force: "FOK"` for full-fill-or-nothing, or `"IOC"` semantics via GTD with instant expiry

**Why FAK over market orders:**
- Price protection — we set a max price we're willing to pay
- Prevents adverse fills during fast price moves
- The limit price = oracle price ± offset, so we only fill when edge exists

**Order flow per signal:**
```
Oracle says YES = 0.65
CLOB best ask for YES = 0.62

Edge = 0.65 - 0.62 = 0.03 > threshold (0.02) ✓

→ Place FAK BUY YES @ limit 0.645 (oracle - offset)
  Size = min(available_ask_liquidity * take_pct, max_order_size)
→ Fills whatever is available at ≤ 0.645, remainder killed
```

---

## 3. Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        totem-taker                              │
│                                                                 │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌───────────┐  │
│  │  Oracle   │───▶│ Strategy │───▶│  Orders  │───▶│ Polymarket│  │
│  │ Receiver  │    │  Engine  │    │  (FAK)   │    │ CLOB REST │  │
│  └──────────┘    └────▲─────┘    └──────────┘    └───────────┘  │
│                       │                                         │
│  ┌──────────┐         │          ┌──────────┐                   │
│  │ Market WS│─────────┘          │ User WS  │                   │
│  │ (L2 book)│                    │ (fills)  │                   │
│  └──────────┘                    └──────────┘                   │
│                                                                 │
│  ┌──────────┐    ┌──────────┐                                   │
│  │  Config  │    │ Position │                                   │
│  │ (.env)   │    │ Tracker  │                                   │
│  └──────────┘    └──────────┘                                   │
└─────────────────────────────────────────────────────────────────┘
```

### Components

| Module | File | Responsibility |
|--------|------|----------------|
| `config` | `config.rs` | Load `.env`, parse into typed config struct |
| `clob_auth` | `clob_auth.rs` | Polymarket CLOB API key derivation, L1/L2 headers, EIP-712 signing |
| `market_ws` | `market_ws.rs` | WebSocket to `wss://ws-subscriptions-clob.polymarket.com/ws/market` — maintains local L2 orderbook |
| `user_ws` | `user_ws.rs` | WebSocket to `/ws/user` — receives order/trade confirmations |
| `oracle` | `oracle.rs` | Connects to external oracle, receives predicted odds |
| `strategy` | `strategy.rs` | Core decision loop — compares oracle vs book, decides when/what to take |
| `orders` | `orders.rs` | Builds and posts FAK orders to CLOB REST API |
| `position` | `position.rs` | Tracks current exposure, fills, PnL |
| `types` | `types.rs` | Shared types: `OrderSide`, `OrderBook`, `OracleSignal`, etc. |

---

## 4. Data Flow (Hot Path)

```
1. Market WS receives L2 update
   → updates local orderbook (best bid/ask + depth)
   → pushes snapshot to strategy via channel

2. Oracle pushes new prediction
   → pushes OracleSignal{yes_prob, no_prob, ts} to strategy via channel

3. Strategy (select! loop) receives either:
   a. New book → re-evaluate with latest oracle
   b. New oracle → re-evaluate with latest book
   → compute edge = |oracle_price - clob_price|
   → if edge ≥ threshold AND position limits OK:
       → build FAK order (side, price=oracle±offset, size)
       → send to orders module

4. Orders module:
   → sign order (EIP-712)
   → POST /order to CLOB REST
   → return order_id

5. User WS receives fill confirmation
   → update position tracker
   → log PnL
```

**Latency targets:**
- Market WS → strategy decision: < 1ms
- Strategy decision → order POST sent: < 2ms
- Total signal-to-wire: < 5ms

---

## 5. Polymarket CLOB API Reference

### 5.1 Authentication

Polymarket CLOB uses API key authentication derived from the wallet:

```
POST /auth/api-key  (derive or create API key)

Headers for authenticated requests:
  POLY-ADDRESS: <wallet_address>
  POLY-SIGNATURE: <signature>
  POLY-TIMESTAMP: <unix_ts>
  POLY-NONCE: <nonce>
  POLY-API-KEY: <api_key>
  POLY-PASSPHRASE: <passphrase>
```

### 5.2 Order Placement

```
POST /order

{
  "order": {
    "salt": <random_u256>,
    "maker": "<address>",
    "signer": "<address>",
    "taker": "0x0000000000000000000000000000000000000000",
    "tokenId": "<token_id>",
    "makerAmount": "<amount_in_base_units>",
    "takerAmount": "<amount_in_base_units>",
    "side": "BUY" | "SELL",
    "expiration": "0",
    "nonce": "0",
    "feeRateBps": "0",
    "signatureType": 1,
    "signature": "<eip712_signature>"
  },
  "orderType": "FOK",
  "tickSize": "0.01"
}
```

**orderType values:**
- `"GTC"` — Good Till Cancelled (rests on book)
- `"GTD"` — Good Till Date (rests until expiration)
- `"FOK"` — Fill or Kill (full fill or nothing)
- For FAK/IOC semantics: use `"FOK"` with the order — it either fills fully or cancels

### 5.3 Market WebSocket

```
Endpoint: wss://ws-subscriptions-clob.polymarket.com/ws/market

Subscribe:
{
  "assets_ids": ["<TOKEN_YES>", "<TOKEN_NO>"],
  "type": "market"
}

Events:
- "book"             → full L2 orderbook snapshot
- "price_change"     → incremental L2 updates
- "last_trade_price" → tick-by-tick trades
```

### 5.4 User WebSocket

```
Endpoint: wss://ws-subscriptions-clob.polymarket.com/ws/user

Auth: API key headers on connect

Events:
- "order"  → order placed/updated/cancelled
- "trade"  → trade MATCHED → CONFIRMED
```

### 5.5 Ping/Pong

Send literal string `"PING"` every 10 seconds on both websockets or the server drops the connection.

---

## 6. Oracle Interface

The oracle is an external service (not part of this repo). It exposes predicted probabilities.

**Expected interface (HTTP poll):**
```
GET <ORACLE_URL>

Response:
{
  "yes_probability": 0.65,
  "no_probability": 0.35,
  "confidence": 0.92,
  "timestamp_ms": 1708900000000,
  "match_id": "IND_vs_AUS_T20_2026"
}
```

**Future: WebSocket push** — oracle pushes updates, zero poll latency. The `oracle.rs` module will support both modes.

---

## 7. Strategy Logic

```rust
// pseudocode
fn evaluate(book: &OrderBook, oracle: &OracleSignal, position: &Position) -> Option<FakOrder> {
    let oracle_yes = oracle.yes_probability;
    let best_ask_yes = book.best_ask(YES);
    let best_bid_yes = book.best_bid(YES);

    // BUY YES: oracle thinks YES is worth more than what the book is asking
    if oracle_yes - best_ask_yes.price >= EDGE_THRESHOLD {
        if position.can_buy(side=YES, notional) {
            return Some(FakOrder {
                side: BUY,
                token: YES,
                price: oracle_yes - PRICE_OFFSET,  // limit below oracle
                size: compute_size(best_ask_yes.depth),
            });
        }
    }

    // SELL YES (= BUY NO): oracle thinks YES is worth less than what bidders pay
    if best_bid_yes.price - oracle_yes >= EDGE_THRESHOLD {
        if position.can_sell(side=YES, notional) {
            return Some(FakOrder {
                side: SELL,
                token: YES,
                price: oracle_yes + PRICE_OFFSET,  // limit above oracle
                size: compute_size(best_bid_yes.depth),
            });
        }
    }

    // Same logic mirrored for NO token...
    None
}
```

**Size computation:**
```
size = min(
    available_depth_at_or_better * LIQUIDITY_TAKE_PCT,
    MAX_ORDER_SIZE_USDC / price,
    remaining_exposure_room,
)
clamp to >= MIN_ORDER_SIZE_USDC / price, else skip
```

---

## 8. Position Tracking

```
Position {
    yes_tokens: f64,      // net YES token balance (+ = long, - = short)
    no_tokens: f64,       // net NO token balance
    cash_deployed: f64,   // total USDC spent (unsigned)
    total_pnl: f64,       // realized PnL from fills
    open_orders: u32,     // number of inflight orders (should be 0 or 1)
}
```

Exposure check: `cash_deployed <= MAX_EXPOSURE_USDC`

---

## 9. Directory Structure

```
totem-taker/
├── Cargo.toml
├── .env.example
├── .gitignore
├── DESIGN.md
└── src/
    ├── main.rs           # entry point, tokio runtime, spawn tasks
    ├── config.rs         # .env → typed Config struct
    ├── types.rs          # OrderSide, OrderBook, OracleSignal, FakOrder, etc.
    ├── clob_auth.rs      # API key derivation, request signing, EIP-712
    ├── market_ws.rs      # market websocket — L2 orderbook maintenance
    ├── user_ws.rs        # user websocket — fill/order notifications
    ├── oracle.rs         # oracle client (HTTP poll / WS push)
    ├── strategy.rs       # core decision engine
    ├── orders.rs         # build + POST FAK orders to CLOB
    └── position.rs       # exposure tracking, PnL
```

---

## 10. Concurrency Model

```
tokio::spawn(market_ws_task)    ──▶ tx_book   ──┐
tokio::spawn(oracle_task)       ──▶ tx_oracle ──┤
                                                ├──▶ strategy_loop (select!)
tokio::spawn(user_ws_task)      ──▶ tx_fills  ──┤     │
                                                      ▼
                                               orders::post_fak()
                                                      │
                                               position::update()
```

- `tokio::sync::watch` for book state (latest-value semantics, strategy always reads freshest)
- `tokio::sync::mpsc` for oracle signals and fill events (queue semantics, don't drop)
- Strategy loop is single-threaded — no lock contention on the hot path

---

## 11. Implementation Order

| Phase | What | Details |
|-------|------|---------|
| **P0** | Config + Types | `.env` loading, all shared types |
| **P1** | Market WebSocket | Connect, subscribe, parse L2, maintain local book |
| **P2** | Oracle client | Poll oracle HTTP, parse response |
| **P3** | CLOB Auth | API key derivation, EIP-712 order signing |
| **P4** | Orders (FAK) | Build signed order, POST to CLOB |
| **P5** | Strategy | Wire book + oracle → FAK decision → order placement |
| **P6** | User WebSocket | Fill notifications, position updates |
| **P7** | Position/PnL | Exposure limits, PnL logging |
| **P8** | Dry run + logging | Full trace logging, dry_run mode |

---

## 12. Key Design Decisions

1. **FAK not FOK**: We want partial fills — if 70% of our size is available at our limit, take it. FOK would reject the entire order. Polymarket may not have a native IOC type, so we may implement this as a GTD with a 1-second expiry (effectively IOC).

2. **Single-token focus**: Each order targets one side (YES or NO). We are not doing arb (buy both). Pure directional taking based on oracle edge.

3. **No resting orders**: Every order is fire-and-forget. Nothing sits on the book. This means no heartbeat is needed.

4. **Oracle as truth**: The oracle's predicted probability IS the fair price. We take when the CLOB deviates from it.

5. **Position limits, not stop losses**: We cap total exposure in USDC. No stop-loss orders — the oracle signal drives exit timing too.
