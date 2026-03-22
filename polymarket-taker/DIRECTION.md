# DIRECTION: Totem HFT — Polymarket High-Frequency Trading Firm

> **Last Updated**: 2026-03-17
> **Status**: Strategic Blueprint
> **North Star**: Sub-millisecond signal-to-wire execution on Polymarket CLOB, beating 99.99% of competitors

---

## Table of Contents

1. [What Exists Today](#1-what-exists-today)
2. [Gap Analysis](#2-gap-analysis)
3. [North Star Goals](#3-north-star-goals)
4. [Architecture Vision](#4-architecture-vision)
5. [Phase-Wise Execution Plan](#5-phase-wise-execution-plan)
6. [Core Engine Design](#6-core-engine-design)
7. [Latency Engineering](#7-latency-engineering)
8. [Strategy Framework](#8-strategy-framework)
9. [Risk & Safety Systems](#9-risk--safety-systems)
10. [Infrastructure & Deployment](#10-infrastructure--deployment)
11. [Competitive Intelligence](#11-competitive-intelligence)
12. [Open Questions & Decisions](#12-open-questions--decisions)

---

## 1. What Exists Today

### 1.1 Inventory of Components

| Component | Language | Location | Purpose | Maturity |
|-----------|----------|----------|---------|----------|
| **polymarket-taker** | Rust | `/polymarket-taker` | FAK taker bot for cricket markets | 70% — working, cricket-specific |
| **totem-mm (root)** | Python | `/` | Betfair→Polymarket RFQ market maker | 60% — functional but slow |
| **taker-builder** | Python | `/taker-builder` | Multi-wallet infra, proxy deployment, CLI | 80% — solid utility |
| **polymarket-simulator** | Python | `/polymarket-simulator` | CLOB simulator, live capture, replay | 50% — useful for testing |
| **Analytics suite** | Python | `/*.py` scripts | Trade extraction, match analysis, wallet ledger | 70% — strong analytics |
| **Ring simulator** | Python | `/taker-builder/ring_simulator` | Spread-based ring trading bot | 40% — experimental |

### 1.2 What the Rust Taker Already Has

**Working:**
- EIP-712 order signing (CLOB auth + CTF Exchange orders)
- L2 orderbook via WebSocket (`wss://ws-subscriptions-clob.polymarket.com`)
- FAK + GTC order placement and polling
- Three signature types (EOA, Polymarket proxy, Gnosis Safe)
- On-chain CTF operations (split, merge, redeem, proxy routing)
- Local orderbook maintenance with `tokio::sync::watch`
- Web UI for monitoring and manual control (Axum)
- Position tracking with budget enforcement
- Batch order support (POST `/orders`)
- Dry-run mode, configurable edge parameters

**Architecture (~6,700 lines Rust):**
```
market_ws → watch channel → strategy loop → orders → CLOB REST API
                                    ↑
                             signal (stdin/manual)
```

**Current Target Latency:** < 5ms signal-to-wire (design doc claim)

### 1.3 What the Python Stack Has

- Betfair streaming connector (WebSocket, ~200ms latency)
- Betfair polling connector (HTTP, ~2000ms latency)
- Polymarket RFQ listener (streaming + polling)
- Quote engine with mid-price calculation and spread application
- Comprehensive analytics: FIFO ledger, maker/taker analysis, adverse selection metrics, sniping detection
- Goldsky subgraph integration for historical trade extraction
- Grafana + Prometheus monitoring
- Data persistence (JSONL with rotation)

### 1.4 What the Wallet Infra Has

- EOA wallet generation and persistence
- Gnosis Safe 1-of-1 proxy deployment
- MATIC/USDC funding pipelines
- CTF token split/merge/redeem
- EOA ↔ proxy transfers
- Builder attribution for Polymarket rewards
- CLI interface (click-based)
- Multi-wallet lifecycle management

---

## 2. Gap Analysis

### 2.1 Critical Gaps (Must Fix)

| Gap | Impact | Current State |
|-----|--------|---------------|
| **No generic signal system** | Cricket-only, can't trade other markets | Hard-coded `CricketSignal` enum |
| **No user WebSocket** | Fill detection adds 500ms+ via polling | Uses `GET /data/order/{id}` polling |
| **No pre-signing pipeline** | Each order waits for signing (~0.5ms) | Signs on demand |
| **Python in hot path** | RFQ quoting is Python (ms-scale) | totem-mm root is Python |
| **No memory allocator tuning** | Default allocator, no arena/pool | Standard Rust allocator |
| **No CPU pinning** | OS scheduler moves threads randomly | Tokio default scheduler |
| **No connection pre-warming** | TCP handshake on first request | Connections created on demand |
| **Tokio work-stealing overhead** | Cross-thread task migration | Multi-threaded tokio runtime |
| **No market making logic** | Pure taker only, no quoting | Only FAK orders |
| **No backtesting framework** | Can't validate strategies offline | Simulator exists but disconnected |

### 2.2 Structural Gaps

| Gap | Impact |
|-----|--------|
| No unified codebase — Rust taker, Python MM, Python analytics are separate | Code duplication, inconsistent behavior |
| No database — all state in memory | Restart loses history, no analytics feedback loop |
| No multi-market support | One market at a time |
| No dynamic strategy selection | Hard-coded wicket/boundary logic |
| No automated deployment | Manual startup, no orchestration |
| No alerting/paging system | Failures go unnoticed |
| No formal risk limits engine | Only budget cap, no drawdown limits |

### 2.3 Latency Gaps

| Layer | Current | Target | Gap |
|-------|---------|--------|-----|
| WebSocket → local book | ~0.5ms | ~0.1ms | Custom parser, SIMD JSON |
| Signal → decision | ~0.5ms | ~0.05ms | Pre-computed decision tables |
| Order signing | ~0.5ms | ~0.01ms | Pre-signed order pool |
| HTTP POST to CLOB | ~2-5ms | ~1ms | Connection pooling, keep-alive, co-location |
| Fill detection | ~500ms (polling) | ~5ms (WebSocket push) | User WebSocket feed |
| **Total signal-to-wire** | **~5-10ms** | **< 1ms** | **Full pipeline redesign** |

---

## 3. North Star Goals

### 3.1 Performance Targets

```
Signal-to-wire latency:     < 500 microseconds (p50), < 1ms (p99)
Orderbook update latency:   < 100 microseconds
Fill confirmation:           < 10ms (via user WebSocket)
Order throughput:            1000+ orders/second sustained
Memory footprint:            < 100MB RSS steady-state
Zero memory leaks:           All allocations tracked, arena-reset per tick
Uptime:                      99.99% (< 52 minutes downtime/year)
```

### 3.2 Business Targets

```
Markets traded:              50+ concurrent markets
Strategies:                  Taking, market making, cross-market arb, event-driven
Daily volume:                $100K+ USDC
Win rate:                    > 55% on information-driven trades
Sharpe ratio:                > 3.0 annualized
Max drawdown:                < 5% of deployed capital
```

### 3.3 Engineering Targets

```
Thread safety:               Zero data races (compile-time guaranteed by Rust)
Memory safety:               Zero leaks, zero use-after-free (MIRI-clean)
Test coverage:               > 80% on core engine
Benchmark suite:             Criterion benchmarks for every hot-path function
CI/CD:                       Automated build, test, deploy on every commit
Observability:               Every order, fill, signal, decision logged with nanosecond timestamps
```

---

## 4. Architecture Vision

### 4.1 Target Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                        TOTEM HFT ENGINE (Rust)                       │
│                                                                      │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌────────────┐ │
│  │  FEED       │  │  SIGNAL     │  │  STRATEGY   │  │  EXECUTION │ │
│  │  HANDLER    │──│  PROCESSOR  │──│  ENGINE     │──│  ENGINE    │ │
│  │             │  │             │  │             │  │            │ │
│  │ • Market WS │  │ • Oracle    │  │ • Taker     │  │ • Signing  │ │
│  │ • User WS   │  │ • News/API  │  │ • Maker     │  │ • REST     │ │
│  │ • RPC feed  │  │ • Cross-mkt │  │ • Arb       │  │ • Batch    │ │
│  │ • Betfair   │  │ • On-chain  │  │ • Event     │  │ • Cancel   │ │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └─────┬──────┘ │
│         │                │                │                │        │
│  ┌──────┴────────────────┴────────────────┴────────────────┴──────┐ │
│  │                     SHARED BUS (lock-free SPSC rings)          │ │
│  └──────┬────────────────┬────────────────┬────────────────┬──────┘ │
│         │                │                │                │        │
│  ┌──────┴──────┐  ┌──────┴──────┐  ┌──────┴──────┐  ┌─────┴──────┐│
│  │  RISK       │  │  POSITION   │  │  ORDERBOOK  │  │  TELEMETRY ││
│  │  MANAGER    │  │  MANAGER    │  │  MANAGER    │  │  & LOGGING ││
│  │             │  │             │  │             │  │            ││
│  │ • Limits    │  │ • Inventory │  │ • Local L2  │  │ • Metrics  ││
│  │ • Drawdown  │  │ • PnL       │  │ • Depth     │  │ • Traces   ││
│  │ • Kill sw   │  │ • Multi-mkt │  │ • BBO cache │  │ • Alerts   ││
│  └─────────────┘  └─────────────┘  └─────────────┘  └────────────┘│
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │              WALLET INFRASTRUCTURE (multi-wallet)               │  │
│  │  • Proxy management  • Gas management  • Token operations      │  │
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
          │                    │                    │
    ┌─────┴─────┐      ┌──────┴──────┐     ┌──────┴──────┐
    │ Polymarket │      │  External   │     │  Analytics  │
    │ CLOB       │      │  Oracles    │     │  Pipeline   │
    │ (REST+WS)  │      │  (Betfair,  │     │  (Python)   │
    │            │      │   APIs)     │     │             │
    └────────────┘      └─────────────┘     └─────────────┘
```

### 4.2 Thread Architecture (Thread-Per-Core)

```
Core 0: Feed Handler
  - WebSocket read loop (market + user)
  - JSON parsing (simd-json)
  - Orderbook update
  - Writes to SPSC ring → Core 1

Core 1: Strategy Engine (HOT PATH — NO ALLOCATIONS)
  - Reads orderbook updates from Core 0
  - Reads signals from Core 2
  - Runs strategy logic
  - Writes order decisions to SPSC ring → Core 3
  - Arena allocator: reset every tick

Core 2: Signal Processor
  - External oracle connections
  - News/API polling
  - Cross-market correlation
  - Writes signals to SPSC ring → Core 1

Core 3: Execution Engine
  - Pre-signed order pool management
  - HTTP POST to CLOB (persistent connections)
  - Fill tracking via user WebSocket
  - Position updates → Core 1 (feedback)

Core 4: Risk + Telemetry (COLD PATH)
  - Position aggregation
  - Risk limit checks
  - Prometheus metrics export
  - Log flushing
  - Alerting
```

### 4.3 Data Flow (Hot Path)

```
Market WS msg arrives (Core 0)
  │  simd-json parse: ~50μs
  ▼
Update local orderbook (Core 0)
  │  Write to SPSC ring: ~10ns
  ▼
Strategy reads new BBO (Core 1)
  │  Decision logic: ~10μs
  ▼
Order decision written to ring (Core 1)
  │  ~10ns
  ▼
Pick pre-signed order from pool (Core 3)
  │  ~1μs
  ▼
HTTP POST via persistent connection (Core 3)
  │  Network RTT: ~500μs-2ms (co-located)
  ▼
CLOB accepts order
```

**Total in-process time: < 100μs**
**Total including network: < 500μs - 2ms**

---

## 5. Phase-Wise Execution Plan

### Phase 0: Foundation Hardening (Week 1-2)

**Goal:** Make the existing Rust taker production-grade and generic.

- [ ] **Decouple cricket logic** — Extract `CricketSignal` into a generic `Signal` trait with cricket as one implementation
- [ ] **Add user WebSocket** — Subscribe to fill notifications, eliminate polling
- [ ] **Switch to `mimalloc`** — Global allocator swap (one line in `main.rs`)
- [ ] **Add `simd-json`** — Replace `serde_json` for WebSocket message parsing
- [ ] **Connection pooling** — Use `reqwest` connection pool with keep-alive for CLOB REST
- [ ] **Criterion benchmarks** — Benchmark signing, orderbook update, strategy decision, JSON parsing
- [ ] **Fix Cargo.toml** — Add `lto = "fat"`, `codegen-units = 1`, `opt-level = 3` to release profile
- [ ] **Structured logging** — Add nanosecond timestamps to every critical path event

**Deliverable:** Generic taker bot that works on any Polymarket binary market, benchmarked.

### Phase 1: Latency Engineering (Week 3-4)

**Goal:** Achieve < 1ms signal-to-wire.

- [ ] **Thread-per-core architecture** — Replace tokio multi-thread with pinned threads + SPSC rings
- [ ] **Pre-signed order pool** — Pre-compute and cache EIP-712 signatures for likely orders (price levels ± N ticks from current)
- [ ] **Arena allocator** — `bumpalo` per-tick arena on the strategy thread, reset every cycle
- [ ] **Cache-line alignment** — `#[repr(align(64))]` on `OrderBook`, BBO struct, position state
- [ ] **TCP_NODELAY + SO_BUSY_POLL** — Socket options on all CLOB connections
- [ ] **Custom WebSocket frame parser** — Avoid tungstenite overhead for known message formats
- [ ] **Busy-poll strategy loop** — Replace `select!` with tight spin loop + SPSC reads
- [ ] **Profile-guided optimization** — Collect PGO data from live trading, rebuild with PGO

**Deliverable:** Benchmarked < 1ms in-process latency, < 3ms including network.

### Phase 2: Market Making Engine (Week 5-8)

**Goal:** Add quoting/market-making capability alongside taking.

- [ ] **Avellaneda-Stoikov implementation** — Reservation price, optimal spread, inventory skew
- [ ] **Quote management** — GTC order lifecycle: place, amend, cancel
- [ ] **Inventory manager** — Track positions across multiple markets, enforce limits
- [ ] **Adverse selection detector** — Monitor fill quality, widen spreads when being picked off
- [ ] **Dynamic spread model** — Time-to-resolution, volatility regime, orderbook depth
- [ ] **Multi-market support** — Trade N markets concurrently from one engine instance
- [ ] **Strategy selector** — Configuration-driven: taker-only, maker-only, or hybrid per market

**Deliverable:** Market making on 10+ markets with configurable strategy per market.

### Phase 3: Signal Intelligence (Week 9-12)

**Goal:** Build the information edge.

- [ ] **Oracle framework** — Generic trait for signal sources, pluggable adapters
- [ ] **Betfair connector (Rust)** — Port Python Betfair streaming to Rust for sports markets
- [ ] **News/social sentiment** — API integrations for real-time event detection
- [ ] **Cross-market correlation** — Monitor related markets, detect divergences
- [ ] **On-chain oracle monitoring** — Watch Chainlink/UMA feeds for resolution triggers
- [ ] **Historical signal database** — Store all signals with outcomes for backtesting
- [ ] **Backtesting engine** — Replay historical orderbook + signals through strategy

**Deliverable:** Multi-source signal pipeline feeding the strategy engine, backtesting capability.

### Phase 4: Multi-Wallet & Scale (Week 13-16)

**Goal:** Scale execution across multiple wallets and markets.

- [ ] **Port taker-builder to Rust** — Wallet generation, proxy deployment, funding in Rust
- [ ] **Multi-wallet execution** — Distribute orders across N wallets to avoid detection/limits
- [ ] **Gas management** — Automated MATIC top-up, gas price optimization
- [ ] **Market discovery** — Automated scanning for new markets with edge opportunity
- [ ] **Position aggregation** — Unified P&L across all wallets and markets
- [ ] **Builder attribution** — Polymarket builder program integration for rebates

**Deliverable:** 50+ markets, 10+ wallets, automated market discovery.

### Phase 5: Production Hardening (Week 17-20)

**Goal:** 99.99% uptime, zero-loss failure modes.

- [ ] **Kill switch** — Hardware-level kill (separate process, separate connection)
- [ ] **Graceful degradation** — If WebSocket drops, cancel all orders within 100ms
- [ ] **State persistence** — WAL (Write-Ahead Log) for position state, survives crashes
- [ ] **Disaster recovery** — Automated position reconciliation on restart
- [ ] **Alerting** — PagerDuty/Telegram for: connection loss, drawdown breach, abnormal fills
- [ ] **Chaos testing** — Simulate network partitions, API failures, stale data
- [ ] **Audit trail** — Immutable log of every decision with full context for post-mortem

**Deliverable:** Production system with SLA guarantees and operational runbooks.

### Phase 6: Advanced Strategies (Week 21+)

**Goal:** Expand edge beyond pure speed.

- [ ] **Resolution prediction** — ML model for predicting market resolution timing
- [ ] **Volatility surface** — Build implied vol surface across related prediction markets
- [ ] **Portfolio optimization** — Kelly criterion for position sizing across correlated markets
- [ ] **New market sniping** — Detect newly listed markets, provide initial liquidity at wide spread
- [ ] **Cross-platform arbitrage** — Polymarket vs Kalshi, Polymarket vs sportsbooks
- [ ] **RFQ responding** — Quote on Polymarket's RFQ system with the Rust engine (replace Python)

---

## 6. Core Engine Design

### 6.1 Message Types (Zero-Copy)

```rust
/// All messages on the internal bus. Fits in a cache line.
#[repr(C, align(64))]
pub enum BusMessage {
    /// Orderbook BBO update from feed handler
    BboUpdate {
        market_id: u64,           // 8 bytes — hashed market identifier
        best_bid: FixedPrice,     // 8 bytes — fixed-point price (6 decimals)
        best_ask: FixedPrice,     // 8 bytes
        bid_size: FixedSize,      // 8 bytes — fixed-point size
        ask_size: FixedSize,      // 8 bytes
        timestamp_ns: u64,        // 8 bytes — nanosecond timestamp
    },
    /// Signal from oracle/external source
    Signal {
        market_id: u64,
        signal_type: u16,
        fair_value: FixedPrice,
        confidence: u16,          // basis points (0-10000)
        timestamp_ns: u64,
    },
    /// Order decision from strategy
    OrderDecision {
        market_id: u64,
        side: u8,                 // 0=BUY, 1=SELL
        price: FixedPrice,
        size: FixedSize,
        order_type: u8,           // FAK, GTC, etc.
        pre_signed_idx: u32,      // index into pre-signed pool
        timestamp_ns: u64,
    },
    /// Fill notification from user WebSocket
    Fill {
        order_id: u64,
        filled_size: FixedSize,
        fill_price: FixedPrice,
        timestamp_ns: u64,
    },
}
```

### 6.2 Fixed-Point Arithmetic (No Floating Point on Hot Path)

```rust
/// 6-decimal fixed-point price (matches USDC precision)
/// 0.500000 = 500_000, 1.000000 = 1_000_000
#[derive(Copy, Clone, PartialEq, Eq, PartialOrd, Ord)]
#[repr(transparent)]
pub struct FixedPrice(u64);

impl FixedPrice {
    pub const SCALE: u64 = 1_000_000;
    pub const ONE: Self = Self(1_000_000);
    pub const ZERO: Self = Self(0);

    #[inline(always)]
    pub fn from_decimal(d: rust_decimal::Decimal) -> Self { /* ... */ }

    #[inline(always)]
    pub fn mul_size(self, size: FixedSize) -> u64 {
        // No floating point, no allocation
        (self.0 as u128 * size.0 as u128 / Self::SCALE as u128) as u64
    }
}
```

### 6.3 Orderbook (Cache-Optimized)

```rust
/// Compact orderbook optimized for BBO access.
/// Top-of-book is always at index 0 (no search needed).
#[repr(C, align(64))]
pub struct OrderBook {
    // Hot data — fits in 2 cache lines
    pub best_bid_price: FixedPrice,
    pub best_bid_size: FixedSize,
    pub best_ask_price: FixedPrice,
    pub best_ask_size: FixedSize,
    pub spread: FixedPrice,
    pub mid_price: FixedPrice,
    pub update_count: u64,
    pub timestamp_ns: u64,

    // Cold data — deeper levels, only accessed for sizing
    pub bid_levels: ArrayVec<PriceLevel, 32>,  // stack-allocated, no heap
    pub ask_levels: ArrayVec<PriceLevel, 32>,
}
```

### 6.4 Pre-Signed Order Pool

```rust
/// Pool of pre-computed EIP-712 signatures for rapid order submission.
/// On every BBO update, we pre-sign orders at the top N price levels.
pub struct PreSignedPool {
    /// Ring buffer of pre-signed orders, indexed by (side, tick_offset)
    pool: HashMap<(Side, i16), PreSignedOrder>,
    /// Current base price (mid or BBO)
    base_price: FixedPrice,
    /// Number of ticks above/below to pre-sign
    depth: u16,
}

pub struct PreSignedOrder {
    pub signed_payload: Vec<u8>,   // Ready-to-send HTTP body
    pub price: FixedPrice,
    pub max_size: FixedSize,       // Can partially fill up to this
    pub signature: [u8; 65],       // ECDSA signature
    pub salt: u64,
    pub created_ns: u64,
}
```

### 6.5 Thread-Safe State (No Mutex on Hot Path)

```rust
/// Position state shared between strategy and execution threads.
/// Updated atomically, no locks.
pub struct AtomicPosition {
    /// Packed: upper 32 bits = YES tokens (fixed-point), lower 32 = NO tokens
    tokens: AtomicU64,
    /// Total USDC deployed (fixed-point)
    total_spent: AtomicU64,
    /// Number of trades executed
    trade_count: AtomicU64,
    /// Unrealized P&L estimate (fixed-point, signed as i64)
    unrealized_pnl: AtomicI64,
}
```

---

## 7. Latency Engineering

### 7.1 Network Layer

| Optimization | Technique | Expected Impact |
|-------------|-----------|-----------------|
| **Co-location** | Deploy in same AWS region as Polymarket CLOB | -50-80% network RTT |
| **Persistent connections** | HTTP/2 with keep-alive, never close | -2-5ms per order |
| **TCP_NODELAY** | Disable Nagle's algorithm | -0.5ms on small packets |
| **SO_BUSY_POLL** | Kernel busy-polls NIC | -10-50μs on recv |
| **Pre-established WebSocket** | Connect on startup, never disconnect | Zero handshake cost |
| **Multiple CLOB endpoints** | Send to N endpoints, first response wins | Reduce tail latency |

### 7.2 Compute Layer

| Optimization | Technique | Expected Impact |
|-------------|-----------|-----------------|
| **simd-json** | SIMD-accelerated JSON parsing | -50-70% parse time |
| **Arena allocation** | bumpalo per-tick, reset at boundary | Zero GC pauses, better locality |
| **Fixed-point math** | No f64 on hot path | -30% compute, deterministic |
| **Pre-signing** | EIP-712 signatures cached | -500μs per order |
| **Branch prediction** | `#[likely]`/`#[unlikely]` on hot branches | -5-10% branch misses |
| **LTO + PGO** | Link-time + profile-guided optimization | -10-20% overall |
| **mimalloc** | Global allocator replacement | -15% allocation overhead |
| **CPU pinning** | `core_affinity` crate | Eliminate context-switch jitter |

### 7.3 Data Layer

| Optimization | Technique | Expected Impact |
|-------------|-----------|-----------------|
| **SPSC rings** | `rtrb` crate for inter-thread messaging | ~10ns per message |
| **Cache-line padding** | `#[repr(align(64))]` on hot structs | Eliminate false sharing |
| **Stack allocation** | `ArrayVec`, `SmallVec` instead of `Vec` | Zero heap allocation |
| **Zero-copy parsing** | Borrow from input buffer, don't clone | -50% allocation |

### 7.4 Latency Measurement

```rust
/// Instrument every critical path with rdtsc.
#[inline(always)]
fn rdtsc() -> u64 {
    unsafe { core::arch::x86_64::_rdtsc() }
}

// Usage:
let t0 = rdtsc();
// ... hot path code ...
let t1 = rdtsc();
let cycles = t1 - t0;
// At 3.5 GHz: 1 cycle ≈ 0.286 ns
```

---

## 8. Strategy Framework

### 8.1 Strategy Trait

```rust
pub trait Strategy: Send + 'static {
    /// Called on every BBO update. Must return in < 10μs.
    fn on_book_update(&mut self, book: &OrderBook) -> Option<OrderDecision>;

    /// Called on every external signal.
    fn on_signal(&mut self, signal: &Signal) -> Option<OrderDecision>;

    /// Called on every fill notification.
    fn on_fill(&mut self, fill: &Fill);

    /// Called periodically (every 100ms) for housekeeping.
    fn on_tick(&mut self) -> Vec<OrderDecision>;
}
```

### 8.2 Planned Strategies

#### Pure Taker (Existing, Generalized)
- Receives signal with fair value estimate
- Compares to current book prices
- If `book_ask < fair_value - threshold`: BUY (FAK)
- If `book_bid > fair_value + threshold`: SELL (FAK)
- Optional revert with GTC after configurable delay
- **Edge source:** Information speed (oracle faster than market)

#### Avellaneda-Stoikov Market Maker (New)
```
reservation_price = mid - q * γ * σ² * (T - t)
optimal_spread = γ * σ² * (T - t) + (2/γ) * ln(1 + γ/k)
bid = reservation_price - optimal_spread/2
ask = reservation_price + optimal_spread/2
```
- Inventory-skewed quoting
- Dynamic spread based on volatility, time-to-resolution, depth
- Adverse selection monitoring (widen on toxic flow)
- **Edge source:** Bid-ask spread capture

#### Cross-Market Arbitrage (New)
- Monitor correlated markets (e.g., "Will X win primary?" and "Will X win election?")
- Detect divergences in implied probabilities
- Execute offsetting positions
- **Edge source:** Price consistency across related markets

#### Event-Driven Taker (Existing, Enhanced)
- Monitor external events (sports scores, election results, economic data)
- React to events faster than the market
- Cricket-specific signals already work, generalize to any event type
- **Edge source:** Event detection speed

#### Resolution Sniper (New)
- Monitor on-chain oracle feeds (Chainlink, UMA) for resolution triggers
- When resolution is imminent, take positions at any price better than 0/1
- Extremely high edge, extremely high risk
- **Edge source:** On-chain data interpretation speed

### 8.3 Strategy Configuration

```toml
# Example: config/markets/btc-100k.toml
[market]
condition_id = "0x..."
token_a_id = "12345..."
token_b_id = "67890..."
tick_size = "0.01"

[strategy]
type = "market_maker"   # or "taker", "arb", "event_driven"

[strategy.market_maker]
gamma = 0.1             # risk aversion
sigma_window_secs = 300 # volatility estimation window
max_inventory = 1000    # max tokens per side
spread_floor_bps = 50   # minimum spread (basis points)
quote_levels = 3        # number of price levels to quote
quote_size_usdc = 50    # size per level

[risk]
max_position_usdc = 5000
max_drawdown_pct = 2.0
kill_switch_loss_usdc = 500
```

---

## 9. Risk & Safety Systems

### 9.1 Risk Hierarchy

```
Level 0: COMPILE-TIME SAFETY
  ├── Rust ownership model prevents data races
  ├── No null pointers, no buffer overflows
  └── Type system enforces valid state transitions

Level 1: PRE-TRADE CHECKS (< 1μs, inline)
  ├── Position limit check
  ├── Order size limit check
  ├── Price sanity check (within N% of mid)
  └── Budget remaining check

Level 2: REAL-TIME MONITORING (every 100ms)
  ├── Drawdown tracking (rolling window)
  ├── Fill rate analysis (adverse selection)
  ├── Connection health (WebSocket heartbeat)
  └── Gas balance monitoring

Level 3: KILL SWITCH (separate process)
  ├── Cancel all open orders
  ├── Flatten all positions (if possible)
  ├── Disconnect all feeds
  └── Alert via Telegram/PagerDuty
```

### 9.2 Kill Switch Design

```rust
/// Runs as a SEPARATE PROCESS with its own CLOB connection.
/// Monitors the main engine via shared memory or Unix socket.
/// Can kill all activity even if the main engine is hung/crashed.
pub struct KillSwitch {
    clob_auth: ClobAuth,
    max_drawdown_usdc: Decimal,
    max_position_usdc: Decimal,
    heartbeat_timeout_ms: u64,
    last_heartbeat: Instant,
}

impl KillSwitch {
    pub async fn run(&mut self) {
        loop {
            // Check heartbeat from main engine
            if self.last_heartbeat.elapsed() > Duration::from_millis(self.heartbeat_timeout_ms) {
                self.emergency_cancel_all().await;
                self.alert("Engine heartbeat lost").await;
            }

            // Check drawdown from shared position state
            if self.current_drawdown() > self.max_drawdown_usdc {
                self.emergency_cancel_all().await;
                self.alert("Max drawdown breached").await;
            }

            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    }
}
```

### 9.3 Memory Safety Guarantees

| Guarantee | Mechanism |
|-----------|-----------|
| No memory leaks | Arena allocator with per-tick reset; RAII for long-lived objects |
| No use-after-free | Rust ownership + lifetimes (compile-time) |
| No data races | `Send + Sync` bounds, SPSC rings, atomics |
| No unbounded growth | Fixed-capacity collections (`ArrayVec`), ring buffers with overwrite |
| No OOM | Pre-allocated buffers, max orderbook depth, max event log size |
| Leak detection | Run under Valgrind/MIRI in CI for every release |

---

## 10. Infrastructure & Deployment

### 10.1 Deployment Architecture

```
┌─────────────────────────────────────────┐
│          AWS us-east-1 (or nearest       │
│          to Polymarket CLOB infra)       │
│                                          │
│  ┌──────────────────────────────────┐    │
│  │  Bare-metal / Dedicated Host     │    │
│  │  (c7g.metal or c6i.metal)        │    │
│  │                                   │    │
│  │  ┌────────┐  ┌────────────────┐  │    │
│  │  │ Engine │  │ Kill Switch    │  │    │
│  │  │ (main) │  │ (watchdog)     │  │    │
│  │  └────────┘  └────────────────┘  │    │
│  │                                   │    │
│  │  ┌────────────────────────────┐  │    │
│  │  │ Monitoring Stack           │  │    │
│  │  │ Prometheus + Grafana       │  │    │
│  │  └────────────────────────────┘  │    │
│  └──────────────────────────────────┘    │
│                                          │
│  ┌──────────────────────────────────┐    │
│  │  Polygon Full Node (Bor)         │    │
│  │  (for on-chain operations)       │    │
│  └──────────────────────────────────┘    │
└─────────────────────────────────────────┘

┌─────────────────────────────────────────┐
│  Separate Region: Analytics + Dev        │
│                                          │
│  • Python analytics pipeline             │
│  • Backtesting infrastructure            │
│  • PostgreSQL for trade history           │
│  • Jupyter for research                  │
└─────────────────────────────────────────┘
```

### 10.2 Build & Release

```toml
# Cargo.toml [profile.release]
[profile.release]
opt-level = 3
lto = "fat"
codegen-units = 1
panic = "abort"
strip = true
```

```bash
# Build with PGO
RUSTFLAGS="-Cprofile-generate=/tmp/pgo-data" cargo build --release
# Run with representative workload...
RUSTFLAGS="-Cprofile-use=/tmp/pgo-data/merged.profdata" cargo build --release
```

### 10.3 Observability

```
Metrics (Prometheus):
  ├── totem_order_latency_ns{side,type}        — histogram
  ├── totem_fill_latency_ns{side}               — histogram
  ├── totem_book_update_latency_ns              — histogram
  ├── totem_signal_latency_ns{source}           — histogram
  ├── totem_position_tokens{market,side}        — gauge
  ├── totem_pnl_usdc{market}                    — gauge
  ├── totem_orders_sent_total{side,type}        — counter
  ├── totem_fills_total{side}                    — counter
  ├── totem_ws_reconnects_total                  — counter
  └── totem_risk_drawdown_usdc                   — gauge

Traces (structured logging):
  ├── Every order: {market, side, price, size, latency_ns, result}
  ├── Every fill: {order_id, fill_price, fill_size, slippage_bps}
  ├── Every signal: {source, market, fair_value, confidence}
  └── Every decision: {strategy, action, reason, book_state}
```

---

## 11. Competitive Intelligence

### 11.1 The Landscape

- **$40M** earned by arbitrage bots on Polymarket (Apr 2024 - Apr 2025)
- **73%** of arb profits captured by sub-100ms bots
- Average opportunity window: **2.7 seconds** (down from 12.3s in 2024)
- One bot: $313 → $414K in one month (98% win rate on BTC/ETH/SOL 15-min markets)
- Another: $2.2M in two months with ensemble probability models

### 11.2 Where We Can Win

| Edge Type | Difficulty | Our Advantage |
|-----------|-----------|--------------|
| **Pure speed** | Very Hard (arms race) | Rust engine vs Python bots = 10-100x faster |
| **Sports/cricket oracle** | Medium | Existing Betfair integration, domain expertise |
| **Cross-market arb** | Medium | Multi-market engine, correlation detection |
| **New market sniping** | Easy-Medium | Market discovery + fast first-mover quoting |
| **RFQ responding** | Medium | Replace Python with Rust, sub-ms quotes |
| **Resolution oracle** | Hard | On-chain monitoring, Chainlink/UMA feeds |
| **Information fusion** | Hard | Multiple signal sources into unified fair value |

### 11.3 What Competitors Do That We Don't (Yet)

- ML-based probability estimation from news/social data
- Multi-platform arb (Polymarket ↔ Kalshi ↔ sportsbooks)
- Sub-100ms execution pipelines (we're at ~5-10ms today)
- Automated market discovery and strategy deployment
- Institutional-grade risk management and reporting

---

## 12. Open Questions & Decisions

### Architecture Decisions Needed

1. **Tokio vs Custom Runtime** — Do we go full thread-per-core with `monoio`/`glommio`, or optimize Tokio with `current_thread` + CPU pinning? Thread-per-core is faster but harder to maintain.

2. **Single Binary vs Microservices** — One Rust binary with everything, or separate feed/strategy/execution processes? Single binary = lower latency (no IPC), multiple processes = better isolation.

3. **Database Choice** — PostgreSQL for analytics, but what about hot-path state? Options: memory-mapped file (fastest), RocksDB (durable), Redis (shared). Recommendation: WAL file for crash recovery, everything else in memory.

4. **Python Analytics: Keep or Port?** — The Python analytics suite is powerful. Port hot-path analytics to Rust, keep research/offline analytics in Python? Yes — this is the pragmatic choice.

5. **Multi-Wallet Strategy** — When to use multiple wallets? Anti-detection, parallel order submission, risk isolation? All three. Start with 3-5 wallets, scale to 10+.

### Research Questions

6. **Polymarket CLOB co-location** — Where exactly is the CLOB operator hosted? Need to determine optimal server location. Test latency from multiple AWS regions.

7. **Rate limit boundaries** — Exact rate limits per API key? Per IP? Can we increase by contacting Polymarket? Need empirical testing.

8. **User WebSocket reliability** — How reliable are fill notifications via WebSocket? Do we need polling as fallback? Start with WS + polling fallback, measure reliability, eventually drop polling.

9. **Market making profitability** — What spread is sustainable on Polymarket binary markets? Need backtesting with historical orderbook data. Use the simulator + Goldsky data.

10. **Regulatory considerations** — Any legal implications of automated trading on Polymarket? As a prediction market on Polygon, what jurisdictions apply? Consult legal counsel before scaling capital.

---

## Appendix A: Crate Recommendations

| Purpose | Crate | Why |
|---------|-------|-----|
| Global allocator | `mimalloc` | 15% faster than system allocator |
| Arena allocator | `bumpalo` | Bump allocation, mass free |
| JSON parsing | `simd-json` | SIMD-accelerated, 2-4x faster |
| SPSC channel | `rtrb` | Real-time safe ring buffer |
| Lock-free queue | `crossbeam` | Epoch-based reclamation |
| Fixed-point | Custom or `fixed` | No floating point on hot path |
| Stack collections | `arrayvec` | No heap allocation |
| CPU pinning | `core_affinity` | Pin threads to cores |
| Benchmarking | `criterion` | Statistical benchmarks |
| HTTP client | `hyper` (direct) | Lower overhead than reqwest |
| WebSocket | `tokio-tungstenite` or `fastwebsockets` | `fastwebsockets` is faster |
| EIP-712 signing | `ethers` or `alloy` | `alloy` is the modern replacement |
| Metrics | `prometheus` | Standard metrics export |
| Logging | `tracing` | Structured, async-aware |

## Appendix B: Key Polymarket Contract Addresses

| Contract | Address |
|----------|---------|
| CTF Exchange | `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982e` |
| Neg Risk CTF Exchange | `0xC5d563A36AE78145C45a50134d48A1215220f80a` |
| Neg Risk Adapter | `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296` |
| USDC (Polygon) | `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` |

## Appendix C: Polymarket API Reference

| Endpoint | Method | Purpose | Latency-Critical |
|----------|--------|---------|-------------------|
| `/order` | POST | Place single order | YES |
| `/orders` | POST | Batch (up to 15) | YES |
| `/order/{id}` | DELETE | Cancel order | YES |
| `/cancel-all` | DELETE | Cancel all | EMERGENCY |
| `/data/order/{id}` | GET | Order status | FALLBACK |
| `wss://ws-subscriptions-clob.polymarket.com` | WS | Market data | YES |
| User WS channel | WS | Fill notifications | YES |

---

*This document is a living blueprint. Update as decisions are made and phases are completed.*
