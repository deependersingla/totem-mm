# Changes: Tier 1 + Tier 2 Infrastructure

## Summary

This changeset adds production infrastructure (Tier 1) and a market making engine (Tier 2) to the polymarket-taker. All changes are additive — existing taker behavior is unchanged.

**Test coverage:** 116 unit tests + 10 integration tests (up from 37 previously).

---

## Tier 1: Infrastructure (zero money risk)

### 1. User WebSocket for Fill Detection (`src/user_ws.rs`)

**What:** Replaces REST polling with push-based fill notifications via Polymarket's user WebSocket channel.

**Why:** The previous fill detection polled `GET /data/order/{id}` every 500ms after a 3.5-second matching delay. This added 4-10 seconds of latency per fill. The user WebSocket delivers trade/order events in real-time (~5ms).

**How it works:**
- Connects to `wss://ws-subscriptions-clob.polymarket.com/ws/user`
- Authenticates with API credentials in the subscription message
- Parses trade events (MATCHED/CONFIRMED status) and order events (partial fills)
- Feeds `FillEvent` structs through an mpsc channel to the strategy
- Falls back to REST polling if no WS fill arrives within `fill_ws_timeout_ms` (default 5000ms)
- Reconnects automatically with 2-second backoff

**Polymarket docs verified:**
- User channel endpoint and auth format: `docs/api-reference/wss/user`
- Trade status flow: MATCHED -> MINED -> CONFIRMED (we emit fills on MATCHED)

### 2. Latency Instrumentation (`src/latency.rs`)

**What:** Tracks timing for every critical path operation.

**Metrics tracked:**
| Metric | What it measures |
|--------|-----------------|
| `signal_to_decision` | Signal received -> order built |
| `sign_to_post` | Order signed -> HTTP POST sent |
| `post_to_response` | POST sent -> response received |
| `fill_detect_ws` | Order submitted -> fill via WebSocket |
| `fill_detect_poll` | Order submitted -> fill via REST poll |
| `e2e_signal_to_fill` | Signal received -> fill confirmed |

**API:** `GET /api/latency` returns p50/p95/p99/min/max in microseconds.

### 3. Build Optimizations

**Release profile (`Cargo.toml`):**
- `lto = "fat"` — full link-time optimization
- `codegen-units = 1` — maximum single-unit optimization
- `opt-level = 3` — aggressive optimization
- `strip = true` — smaller binary

**mimalloc (`src/main.rs`):**
- Swapped global allocator to mimalloc (~15% allocation performance improvement)
- Single `#[global_allocator]` line, zero behavior change

### 4. Criterion Benchmarks (`benches/hot_path.rs`)

Run with `cargo bench`. Benchmarks:
- `to_base_units` — Decimal to 6-decimal conversion
- `compute_amounts` — maker/taker amount calculation
- `compute_size` — order size computation
- `orderbook_json_parse` — WebSocket message parsing

### 5. Signal Broadcast Refactor

Changed signal distribution from `mpsc` (single consumer) to `broadcast` (multi-consumer) so both taker and maker can receive cricket signals independently. No behavioral change for the taker.

---

## Tier 2: Market Making Engine (DRY_RUN mandatory)

### 6. Maker Module (`src/maker.rs`)

**What:** 4-leg market making engine that maintains resting orders on both sides of both tokens.

**SAFETY: Starts disabled (`enabled: false`) and in dry-run (`dry_run: true`). Every order placement path checks `dry_run` — no API calls are made in dry-run mode.**

**How it works:**

1. **Fair value:** Computes mid-price from orderbook best bid/ask
2. **Reservation price (Avellaneda-Stoikov):** `r = mid - exposure * kappa` — shifts quotes to rebalance inventory
3. **Quote prices:** `bid = round_down(r - spread, tick)`, `ask = round_up(r + spread, tick)`
4. **Complementary pricing:** `fair_b = 1.0 - fair_a` for the other token
5. **4 resting orders:** TeamA BUY, TeamA SELL, TeamB BUY, TeamB SELL

**Event-driven cancellation matrix (from MM_STRATEGY.md):**

| Event | Cancel | Keep |
|-------|--------|------|
| Wicket | Batting BUY + Bowling SELL | Batting SELL + Bowling BUY |
| Boundary 4/6 | Batting SELL + Bowling BUY | Batting BUY + Bowling SELL |
| Dot / 1-3 runs | None | All |
| Innings Over | All 4 legs | None |
| Match Over | All 4 legs | None |

**Inventory management (tiered):**

| Tier | Condition | Response |
|------|-----------|----------|
| Green | exposure < 20% of split | Symmetric quotes |
| Yellow | 20-50% | Apply skew via kappa |
| Orange | 50-80% | Aggressive skew + halve size on heavy side |
| Red | >80% | One-sided quoting only |

**Configuration (`MakerConfig`):**
```
enabled: false              (must explicitly enable)
dry_run: true               (must explicitly disable)
half_spread: 0.01           (1 cent / 1 tick)
quote_size: 50              (tokens per leg)
use_gtd: true               (auto-expiring orders)
gtd_expiry_secs: 60         (orders expire in 60s)
refresh_interval_secs: 45   (re-quote every 45s)
skew_kappa: 0.0005          (inventory skew intensity)
max_exposure: 200           (max directional exposure)
t1/t2/t3_pct: 0.20/0.50/0.80  (tier boundaries)
```

### 7. Heartbeat (`src/heartbeat.rs`)

**What:** Sends `POST /heartbeats` every 10 seconds with L2 auth headers.

**Why:** Polymarket auto-cancels all open orders if heartbeat stops. Required for GTC revert orders and maker quotes.

**Polymarket docs verified:** `docs/api-reference/trade/send-heartbeat`

### 8. GTD Orders & Batch Cancel (`src/orders.rs`)

**Added:**
- `post_gtd_order()` — Good-Til-Date orders with configurable expiry (min 60s per Polymarket docs)
- `cancel_orders_batch()` — Cancel multiple orders in one API call (`DELETE /orders`)
- `cancel_market_orders()` — Cancel all orders for a specific market (`DELETE /cancel-market-orders`)

**Polymarket docs verified:**
- GTD expiration: Unix timestamp, minimum 60s in future
- Cancel batch: `DELETE /orders` with JSON array body (max 3000)
- Cancel market: `DELETE /cancel-market-orders` with condition_id/asset_id

---

## New API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/latency` | GET | Latency percentiles for all metrics |
| `/api/maker/status` | GET | Maker config, enabled/dry_run state |
| `/api/maker/config` | POST | Update maker configuration |

---

## Files Added

| File | Purpose |
|------|---------|
| `src/user_ws.rs` | User WebSocket for fill detection |
| `src/latency.rs` | Latency tracking and percentile computation |
| `src/heartbeat.rs` | CLOB heartbeat for order keep-alive |
| `src/maker.rs` | 4-leg market making engine |
| `src/tests/user_ws_tests.rs` | 7 tests for WS message parsing |
| `src/tests/latency_tests.rs` | 4 tests for latency tracker |
| `src/tests/heartbeat_tests.rs` | 1 test for endpoint verification |
| `src/tests/maker_tests.rs` | 14 tests for maker logic |
| `benches/hot_path.rs` | Criterion benchmarks |

## Files Modified

| File | Changes |
|------|---------|
| `Cargo.toml` | Release profile, mimalloc, criterion |
| `src/main.rs` | Global allocator |
| `src/lib.rs` | New module declarations |
| `src/types.rs` | FillEvent struct |
| `src/state.rs` | Broadcast channels, latency tracker, maker state |
| `src/signal.rs` | Broadcast sender |
| `src/orders.rs` | GTD orders, batch cancel, refactored signing |
| `src/config.rs` | MakerConfig, fill_ws_timeout_ms |
| `src/server.rs` | New endpoints, maker/heartbeat wiring |
| `src/strategy.rs` | Broadcast receiver |

---

## How to Enable Maker (after validation)

1. Start with dry-run to observe behavior:
   ```
   POST /api/maker/config
   {"enabled": true, "dry_run": true}
   ```
2. Start innings normally via UI
3. Watch logs for `[MAKER] [DRY_RUN]` entries showing would-be quotes
4. Check `/api/maker/status` for state
5. Check `/api/latency` for timing data
6. Only after extensive dry-run validation, disable dry_run:
   ```
   POST /api/maker/config
   {"dry_run": false}
   ```

---

## Polymarket API Compliance

All implementations verified against https://docs.polymarket.com/:

- User WS auth format matches `docs/market-data/websocket/user-channel`
- Trade status flow: MATCHED -> MINED -> CONFIRMED
- Heartbeat endpoint: POST `/heartbeats` with L2 headers
- GTD expiration: Unix timestamp, min 60s future
- Cancel batch: DELETE `/orders` with array body
- Cancel market: DELETE `/cancel-market-orders`
- Cricket markets: feeRateBps = 0 (zero fees)
- Rate limits: maker operates well within burst limits (1-2 batch ops per event, ~45s refresh cycle)
