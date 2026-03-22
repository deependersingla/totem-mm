# Informed Market Making Strategy — Cricket Prediction Markets

## Table of Contents

1. [Market Structure](#1-market-structure)
2. [The Edge: Fast Oracle](#2-the-edge-fast-oracle)
3. [Strategy Overview](#3-strategy-overview)
4. [Order Placement](#4-order-placement)
5. [Event-Driven Cancellation](#5-event-driven-cancellation)
6. [Inventory Management](#6-inventory-management)
7. [Spread Determination](#7-spread-determination)
8. [Parallel Maker + Taker Architecture](#8-parallel-maker--taker-architecture)
9. [Risk Management](#9-risk-management)
10. [P&L Model](#10-pl-model)
11. [Polymarket-Specific Constraints](#11-polymarket-specific-constraints)
12. [Edge Cases & Failure Modes](#12-edge-cases--failure-modes)

---

## 1. Market Structure

### Binary Outcome Tokens

Cricket match markets on Polymarket are binary:

```
Token_A (Team A wins) + Token_B (Team B wins) = $1.00

Example:
  India  (Token_A): bid 0.60 / ask 0.61
  England(Token_B): bid 0.38 / ask 0.40
```

Key properties:
- **Complementary pricing**: `P(A) + P(B) = 1.00` (minus spread)
- **Bounded in [0, 1]**: Volatility is highest at 50/50, lowest near extremes
- **Volatility model**: `sigma ~ sqrt(p * (1-p))` — a 60/40 market is less volatile than 50/50
- **Perfect negative correlation**: Every cent Team A gains, Team B loses

### Price Movement Drivers

Cricket prices move on **discrete ball-by-ball events**, not continuous flow:

| Event | Direction (for batting team) | Typical Move |
|-------|------------------------------|--------------|
| Dot ball | Neutral / slight negative | 0-0.2 cents |
| 1-3 runs | Slight positive | 0.1-0.5 cents |
| Four | Positive | 0.5-1.5 cents |
| Six | Strong positive | 1-3 cents |
| Wicket | Strong negative | 2-5 cents |
| Wide/No-ball | Slight positive (free run) | 0.1-0.5 cents |

Between deliveries (~30-40 seconds gap), prices are **mean-reverting** — any noise-driven displacement tends to revert. During events, prices **jump** discretely.

---

## 2. The Edge: Fast Oracle

### Why This Works

The entire strategy hinges on one asymmetry: **we know about ball outcomes before the market reprices.**

```
Timeline of a delivery:

t=0ms     Ball is bowled
t=200ms   Our oracle detects outcome (wicket/boundary/dot)
t=300ms   We cancel dangerous quotes
t=500ms+  Fastest retail/other traders start reacting
t=1000ms+ Majority of market reprices
t=2000ms+ New equilibrium established
```

**Stale quote window** = time between event and our cancel = ~100ms
**Market stale window** = time between event and full reprice = ~1-2 seconds

As long as our cancel lands before the fastest sniper fills our stale quote, we are safe. Our edge is the ~200-700ms gap where we've cancelled but others haven't repriced yet.

### What the Oracle Gives Us

1. **Safe quoting during dot balls**: ~60-70% of deliveries are dots. We know instantly that nothing happened, so our quotes remain valid. We collect spread while the market is quiet.
2. **Fast cancellation on events**: On the ~30-40% of deliveries with runs/wickets, we pull dangerous quotes before getting picked off.
3. **Taker opportunity**: After cancelling maker quotes, we can simultaneously take the other side (existing taker strategy) while stale liquidity still sits on the book.

---

## 3. Strategy Overview

### The Two Modes

The maker operates in two alternating modes:

```
┌─────────────────────────────────────────┐
│           QUOTING MODE                   │
│  (between deliveries, ~30-40s each)      │
│                                          │
│  4 resting GTC orders:                   │
│    Team A BUY  @ fair - spread           │
│    Team A SELL @ fair + spread           │
│    Team B BUY  @ fair - spread           │
│    Team B SELL @ fair + spread           │
│                                          │
│  Revenue: spread captured from retail    │
│  Risk: adverse selection (mitigated      │
│         by oracle speed)                 │
└──────────────┬──────────────────────────┘
               │ Oracle fires
               ▼
┌─────────────────────────────────────────┐
│         PROTECTION MODE                  │
│  (on event, ~2-5s duration)              │
│                                          │
│  1. Cancel dangerous legs (<100ms)       │
│  2. Optionally take stale liquidity      │
│  3. Wait for market to settle            │
│  4. Compute new fair value               │
│  5. Re-quote all 4 legs                  │
│                                          │
│  Revenue: none (or taker profit)         │
│  Risk: minimal (quotes are pulled)       │
└──────────────┬──────────────────────────┘
               │ Market settled
               ▼
         Back to QUOTING MODE
```

### The Profit Engine

Revenue comes from three sources:

1. **Spread capture** (primary): Retail buys Team A from us at `fair + spread`, retail buys Team B from us at `fair + spread`. If both sides fill, we collect `2 * spread` per pair of tokens and can merge them back to $1.00.

2. **Split-sell arbitrage**: If `best_bid_A + best_bid_B > 1.00`, we split USDC into A+B tokens and sell both into bids. Risk-free overround profit.

3. **Taker profit on events** (bonus): When oracle fires, our taker strategy captures mispriced stale liquidity. This runs independently with its own inventory.

---

## 4. Order Placement

### Order Types

| Order Type | When Used | Purpose |
|------------|-----------|---------|
| **GTC** (Good Til Cancelled) | Maker quotes | Resting liquidity, persists until filled or cancelled |
| **GTD** (Good Til Date) | Alternative maker quotes | Auto-expires, prevents stale quotes if cancel fails |
| **FAK** (Fill And Kill) | Taker trades on events | Immediate partial fill, remainder cancelled |
| **GTC + post-only** | Preferred maker quotes | Guaranteed to rest on book, rejected if it would cross |

### Quoting the 4 Legs

```
Given:
  fair_a = current fair value of Team A (from orderbook mid or oracle)
  fair_b = 1.00 - fair_a
  spread = our half-spread (e.g., 0.01 = 1 cent)
  tick = market tick size (0.01 for most markets)

Orders:
  TEAM_A_BUY:  price = round_down(fair_a - spread, tick),  size = quote_size
  TEAM_A_SELL: price = round_up(fair_a + spread, tick),     size = quote_size
  TEAM_B_BUY:  price = round_down(fair_b - spread, tick),   size = quote_size
  TEAM_B_SELL: price = round_up(fair_b + spread, tick),     size = quote_size
```

### Why Post-Only Matters

Without post-only, if we compute a quote that crosses the current best price, our "maker" order becomes a taker order and fills immediately — eating liquidity instead of providing it. Post-only prevents this: the order is rejected rather than crossing. We then re-quote at a valid price.

### Re-Quoting Frequency

- **On fill**: When any leg fills, immediately re-quote that leg (at same or adjusted price depending on inventory)
- **On event**: Cancel all → wait for settle → re-quote all 4 legs at new fair value
- **Periodic**: Every N seconds, check if fair value has drifted from our quotes. If drift > threshold, cancel and re-quote. This handles slow price changes between events.
- **On inventory threshold**: If inventory skew crosses a tier, adjust quotes (see Section 6)

### Batch Order Placement

Polymarket supports batch orders (`POST /orders`, up to 15 per request). Use this to:
- Place all 4 legs in a single API call
- Cancel and re-quote atomically (cancel batch + place batch)
- Reduce latency vs. 4 sequential calls

---

## 5. Event-Driven Cancellation

### The Cancellation Matrix

On every oracle signal, we must instantly decide which legs to cancel:

```
On WICKET (batting team probability drops):
  ┌─────────────────┬──────────────┬──────────────────────────────────┐
  │ Order            │ Action       │ Reason                           │
  ├─────────────────┼──────────────┼──────────────────────────────────┤
  │ Batting BUY      │ CANCEL       │ We'd buy at old high price       │
  │ Batting SELL     │ KEEP (safe)  │ No one buys at our high ask      │
  │ Bowling BUY      │ KEEP (safe)  │ No one sells at our low bid      │
  │ Bowling SELL     │ CANCEL       │ We'd sell at old low price       │
  └─────────────────┴──────────────┴──────────────────────────────────┘

On BOUNDARY/SIX (batting team probability rises):
  ┌─────────────────┬──────────────┬──────────────────────────────────┐
  │ Order            │ Action       │ Reason                           │
  ├─────────────────┼──────────────┼──────────────────────────────────┤
  │ Batting BUY      │ KEEP (safe)  │ No one sells at our low bid      │
  │ Batting SELL     │ CANCEL       │ We'd sell at old low price       │
  │ Bowling BUY      │ CANCEL       │ We'd buy at old high price       │
  │ Bowling SELL     │ KEEP (safe)  │ No one buys at our high ask      │
  └─────────────────┴──────────────┴──────────────────────────────────┘

On DOT BALL:
  All legs SAFE — no cancellation needed. Keep collecting spread.

On 1-3 RUNS:
  Technically a slight positive for batting team.
  If spread > expected move → KEEP all (move is within our spread).
  If spread < expected move → cancel same as boundary (conservative).

On WIDE / NO-BALL:
  Same as 1-3 runs (slight positive for batting team, extra delivery).

On INNINGS OVER:
  Cancel ALL legs. Batting/bowling swap. Re-quote after swap.

On MATCH OVER:
  Cancel ALL legs. Stop maker entirely.
```

### Cancellation Speed Budget

```
Oracle detection:     ~0ms   (live model, real-time)
Signal to strategy:   ~1ms   (in-process channel)
Cancel API call:      ~50ms  (REST to Polymarket CLOB)
Cancel confirmed:     ~100ms (round-trip)
─────────────────────────────
Total cancel latency: ~150ms

Fastest market sniper: ~500ms+ (they need to detect event, compute, and submit)

Safety margin: ~350ms — comfortable
```

### Fallback: GTD Expiry

As a safety net, instead of pure GTC orders, use **GTD with rolling expiry**:

```
Place order with expiry = now + 60 seconds
Every 45 seconds, cancel and re-quote with new expiry

If our cancel fails (network issue, API down):
  - GTC: quote sits forever, gets sniped on next event
  - GTD: quote auto-expires in at most 60 seconds, limiting damage
```

This is insurance against cancel failures. The cost is slightly more API calls for the periodic refresh.

---

## 6. Inventory Management

### Starting Position: The Split

Before quoting begins, **split USDC into equal YES + NO tokens**:

```
Split $500 USDC → 500 Token_A + 500 Token_B

Now we can:
  - SELL Token_A (we have 500 to sell)
  - SELL Token_B (we have 500 to sell)
  - BUY Token_A (we have USDC from sells + reserve)
  - BUY Token_B (we have USDC from sells + reserve)
```

Starting with tokens on both sides means we can quote all 4 legs immediately without needing initial fills.

### Inventory Tracking

```
State:
  q_a = Token_A balance  (starts at split amount)
  q_b = Token_B balance  (starts at split amount)
  usdc = USDC balance    (reserve for buy orders)

Net exposure:
  exposure = q_a - q_b

  exposure > 0 → long Team A (we benefit if A wins)
  exposure < 0 → long Team B (we benefit if B wins)
  exposure = 0 → perfectly hedged
```

### Skew-Based Quoting (Avellaneda-Stoikov Approach)

When inventory is imbalanced, shift quotes to encourage rebalancing:

```
Reservation price (inventory-adjusted fair value):

  r = fair_a - exposure * kappa

  where kappa = skew_intensity (tunable, e.g., 0.001 per token of imbalance)

When long Team A (exposure > 0):
  - r < fair_a (we value A less, want to sell it)
  - Team A ask tightens (more likely to sell A)
  - Team A bid widens (less likely to buy more A)
  - Team B bid tightens (more likely to buy B to hedge)
  - Team B ask widens (less likely to sell B)
```

Visually:

```
Balanced (exposure = 0):
  A: ----[BID]----mid----[ASK]----
  B: ----[BID]----mid----[ASK]----
  (symmetric)

Long Team A (exposure = +50):
  A: --[BID]------mid--[ASK]------    ← ask tighter (sell A), bid wider (don't buy A)
  B: ------[BID]--mid------[ASK]--    ← bid tighter (buy B), ask wider (don't sell B)
  (skewed to flatten)
```

### Tiered Inventory Response

| Tier | Condition | Action |
|------|-----------|--------|
| **Green** | `abs(exposure) < T1` (e.g., < 20% of split) | Normal symmetric quoting |
| **Yellow** | `T1 <= abs(exposure) < T2` (e.g., 20-50%) | Skew quotes per Avellaneda-Stoikov |
| **Orange** | `T2 <= abs(exposure) < T3` (e.g., 50-80%) | Aggressive skew + reduce quote size on heavy side |
| **Red** | `abs(exposure) >= T3` (e.g., > 80%) | One-sided quoting only (stop adding to heavy side) |
| **Emergency** | Inventory depleted on one side | Stop quoting that side entirely, actively rebalance |

### Active Rebalancing Methods

When passive skewing isn't fast enough:

**Method 1: Cross the spread**
- Send a FAK order to buy the deficit token at the current ask
- Cost: ~1 tick (the spread you pay)
- Fast, guaranteed fill (if liquidity exists)
- Use when exposure is critical and event risk is high

**Method 2: Merge excess pairs**
- If `q_a = 200, q_b = 150`, merge 150 pairs → recover $150 USDC
- Remaining: `q_a = 50, q_b = 0, usdc += 150`
- Then split some USDC to rebalance: split $50 → `q_a = 100, q_b = 50`

**Method 3: Wait for fills**
- If skewed long A, our tighter A-ask will get filled by retail eventually
- Slowest method but zero cost
- Only viable if there's enough time between events

### Maker vs Taker Inventory Separation

```
MAKER INVENTORY:
  - Persistent throughout the match
  - Starts with split tokens (both sides loaded)
  - Constantly being filled/rebalanced
  - Goal: stay near zero exposure at all times
  - Size: larger (e.g., 500-1000 tokens per side)

TAKER INVENTORY:
  - Active only during events
  - Starts near zero
  - Buys on event → reverts later → back to zero
  - Goal: capture event mispricings, flatten quickly
  - Size: smaller (e.g., max 50-100 tokens per trade)

WHY SEPARATE:
  - Taker needs to move fast, can't be constrained by maker's position limits
  - Maker needs continuous balanced positions, can't be disrupted by taker's directional bets
  - Different budget envelopes, different risk parameters
  - If taker has a bad trade, maker keeps running unaffected
```

---

## 7. Spread Determination

### Factors

The optimal spread balances profitability against fill rate and adverse selection:

```
spread = base_spread + volatility_component + inventory_component

where:
  base_spread      = minimum tick (0.01) — the structural floor
  volatility_comp  = f(match_phase, recent_event_frequency)
  inventory_comp   = kappa * abs(exposure) — widen when skewed
```

### Match Phase Adjustments

| Phase | Volatility | Spread | Reasoning |
|-------|-----------|--------|-----------|
| Powerplay (overs 1-6) | High | Wide (2-3 ticks) | More boundaries, aggressive batting |
| Middle overs (7-15) | Medium | Normal (1-2 ticks) | Steady accumulation |
| Death overs (16-20) | Very high | Wide (2-4 ticks) | Sixes, wickets, high variance |
| Between innings | Zero | Pull quotes | No events, price may gap on target |
| 2nd innings chase | Variable | Dynamic | Depends on required rate |

### Spread vs. Expected Event Move

**Critical rule**: Your spread must be profitable after accounting for the times you DO get picked off.

```
Expected P&L per delivery:

  E[PnL] = P(dot) * spread_revenue
          + P(event) * P(cancel_in_time) * 0    (successfully cancelled, no loss)
          + P(event) * P(late_cancel) * (-loss)  (picked off)
          - inventory_cost

where:
  spread_revenue = fill_probability * spread * quote_size
  loss = quote_size * (event_move - spread/2)

For this to be positive:
  P(dot) * fill_prob * spread * size  >  P(event) * P(late) * size * (move - spread/2)

With a fast oracle, P(late_cancel) ~ 0, so:
  E[PnL] ≈ P(dot) * fill_prob * spread * size  (nearly pure profit)
```

### Dynamic Spread Example

```
State: India batting, over 15, score 120/2, India at 0.65

base_spread = 0.01 (1 tick)
volatility  = "middle overs, moderate" → +0.00
inventory   = exposure = +20 tokens → +0.005 on A-ask, -0.005 on A-bid

Quotes:
  India BUY:   0.65 - 0.01 - 0.005 = 0.635  (wider, don't want more India)
  India SELL:  0.65 + 0.01 - 0.005 = 0.655  (tighter, want to sell India)
  England BUY: 0.35 - 0.01 + 0.005 = 0.345  (tighter, want to buy England)
  England SELL:0.35 + 0.01 + 0.005 = 0.365  (wider, don't want to sell England)
```

---

## 8. Parallel Maker + Taker Architecture

### Concurrency Model

```
                    Oracle Signal (broadcast)
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
         ┌─────────┐ ┌─────────┐ ┌──────────┐
         │  Maker  │ │  Taker  │ │ Book WS  │
         │  Task   │ │  Task   │ │  Task    │
         │         │ │         │ │          │
         │ GTC     │ │ FAK     │ │ L2 feed  │
         │ quotes  │ │ on event│ │          │
         │         │ │         │ │          │
         │ Maker   │ │ Taker   │ │ Shared   │
         │ Position│ │ Position│ │ OrderBook│
         └─────────┘ └─────────┘ └──────────┘
              │            │
              └────────────┘
            Independent, non-blocking
```

### Signal Distribution

Current system uses `mpsc` (single consumer). For parallel maker+taker, switch to `broadcast`:

```
signal_tx: broadcast::Sender<CricketSignal>

maker_rx = signal_tx.subscribe()   // maker gets every signal
taker_rx = signal_tx.subscribe()   // taker gets every signal independently

// Neither blocks the other. If maker is slow to process,
// taker still fires immediately (and vice versa).
```

### Execution Priority

On an event, both fire simultaneously, but their actions don't conflict:

```
Maker on WICKET:                    Taker on WICKET:
  1. Cancel batting BUY (GTC)         1. FAK sell batting team
  2. Cancel bowling SELL (GTC)        2. FAK buy bowling team
  3. Wait for reprice                 3. Poll fills
  4. Re-quote at new fair             4. Place revert orders

No contention — maker cancels its own order IDs,
taker places new FAK orders. Different order IDs, different flows.
```

### Separate Position State

```rust
// Two independent position trackers
maker_position: {
    team_a_tokens: Decimal,
    team_b_tokens: Decimal,
    usdc_reserve: Decimal,
    exposure: Decimal,        // q_a - q_b
    resting_order_ids: Vec<String>,
}

taker_position: {
    team_a_tokens: Decimal,
    team_b_tokens: Decimal,
    usdc_budget: Decimal,
    total_spent: Decimal,
}
```

---

## 9. Risk Management

### Position Limits

```
Maker:
  max_exposure = 200 tokens  (absolute, either direction)
  max_single_quote_size = 50 tokens
  max_total_inventory = 1000 tokens (per side)

Taker:
  max_trade_usdc = 10  (per event)
  total_budget = 100   (entire session)
```

### Price Safety Bounds

Never quote outside safe range:

```
safe_min = 0.03  (3%)
safe_max = 0.97  (97%)

If fair_a < safe_min or fair_a > safe_max:
  Stop maker quoting (spread is compressed, risk/reward poor)
  Taker can still operate
```

### Oracle Failure Handling

```
If oracle goes silent (no signal for > expected_delivery_time * 2):
  1. Widen spreads to maximum (defensive mode)
  2. If silence continues > 60s, cancel all maker quotes
  3. Log alert for manual intervention

If oracle sends contradictory signals:
  Cancel all quotes, enter manual mode
```

### Max Loss Circuit Breaker

```
If maker_pnl < -max_loss_threshold:
  Cancel all maker quotes
  Stop maker strategy
  Taker continues independently (different budget)

If taker_pnl < -max_loss_threshold:
  Stop taker strategy
  Maker continues (different inventory)
```

### Stale Quote Protection

```
For each resting order, track:
  placed_at: timestamp
  fair_value_at_placement: Decimal

Every heartbeat (1-2 seconds):
  current_fair = orderbook_mid()
  drift = abs(current_fair - fair_value_at_placement)

  If drift > max_drift (e.g., 0.02):
    Cancel and re-quote (fair value has moved, quotes are stale)
```

---

## 10. P&L Model

### Revenue Sources

```
1. SPREAD CAPTURE (maker):
   Revenue per round-trip = 2 * half_spread * size

   Example: half_spread = 0.01, size = 50 tokens
   Revenue = 2 * 0.01 * 50 = $1.00 per complete round-trip

   If we complete 10 round-trips per over (120 deliveries, ~60 dot balls with fills):
   Revenue = ~$10 per over, ~$200 per match (20 overs)

2. EVENT TAKING (taker):
   Revenue per event = edge_captured * size

   Example: wicket moves price 3 cents, we capture 2 cents, size = 50
   Revenue = 0.02 * 50 = $1.00 per wicket trade

   ~10 wickets per match + ~20 boundaries = ~30 events
   Revenue = ~$30 per match

3. MERGE PROFIT (arbitrage):
   If we sell Token_A at 0.61 and Token_B at 0.40:
   Revenue = (0.61 + 0.40) - 1.00 = $0.01 per pair
   After merge, pure profit locked in.
```

### Cost Sources

```
1. ADVERSE SELECTION (getting picked off):
   Cost per pick-off = size * (event_move - half_spread)
   With fast oracle: near zero occurrences

2. INVENTORY HOLDING COST:
   If exposed directionally when a large event hits
   Cost = exposure * unexpected_move
   Mitigated by: skew-based quoting, position limits

3. SPREAD CROSSING (active rebalance):
   Cost = 1 tick * rebalance_size
   Infrequent, small relative to spread revenue

4. GAS / ON-CHAIN FEES:
   Split/merge operations cost Polygon gas (~$0.01)
   Negligible
```

### Expected P&L Per Match (Conservative)

```
Assumptions:
  - 240 deliveries (2 innings x 20 overs x 6 balls)
  - ~60% dot balls (144 dots), ~40% scoring (96 events)
  - Fill rate on dots: 20% (fills on 29 deliveries)
  - Spread: 1 cent, size: 50 tokens
  - Pick-off rate: 0% (fast oracle)
  - Events captured by taker: 20 (significant ones)
  - Taker edge: 1.5 cents avg, size: 30 tokens

Maker revenue:  29 fills * $0.50 avg profit = $14.50
Taker revenue:  20 events * $0.45 avg profit = $9.00
Rebalance cost: 5 crosses * $0.50 = -$2.50
─────────────────────────────────────────────
Estimated P&L per match: ~$21.00

(Scale linearly with quote size. At 500 tokens/leg: ~$210/match)
```

---

## 11. Polymarket-Specific Constraints

### Rate Limits (relevant to maker)

| Action | Burst Limit | Sustained Limit |
|--------|-------------|-----------------|
| Place order (`POST /order`) | 3,500 / 10s | 36,000 / 10min |
| Place batch (`POST /orders`) | 1,000 / 10s | 15,000 / 10min |
| Cancel single (`DELETE /order`) | 3,000 / 10s | 30,000 / 10min |
| Cancel batch (`DELETE /orders`) | 1,000 / 10s | 15,000 / 10min |
| Cancel all (`DELETE /cancel-all`) | 250 / 10s | 6,000 / 10min |

**Our usage**: ~1 cancel-batch + 1 place-batch per delivery (every ~30s) = ~2 req/30s. Nowhere near limits.

### Fees

- **Cricket markets: ZERO fees.** No maker fee, no taker fee.
- This means our entire spread is profit — no fee drag.

### Balance Model

- BUY orders reserve USDC from available balance
- SELL orders reserve conditional tokens
- Both can coexist since they draw from different pools
- `max_order_size = balance - sum(open_order_sizes - filled_amounts)`

### No Self-Trade Prevention

Polymarket has no documented self-trade prevention. Your buy at 0.59 and sell at 0.61 on the same token will happily coexist. Just ensure they don't cross (use post-only flag).

### Anti-Abuse

One rule: "Any maker caught intentionally abusing balance checks will be blacklisted." This means don't post orders you can't back. As long as we have sufficient tokens/USDC for our quotes, no issue.

---

## 12. Edge Cases & Failure Modes

### Case 1: Oracle Detects Wrong Event

```
Scenario: Oracle says "dot ball" but it was actually a wicket.
Impact: Our stale quotes get sniped. We buy batting team at old high price.
Loss: quote_size * price_move (e.g., 50 * 0.03 = $1.50)

Mitigation:
  - Oracle accuracy is paramount. Test extensively before live.
  - Keep quote sizes small until oracle is proven.
  - GTD expiry as safety net.
```

### Case 2: Network Latency Spike

```
Scenario: Cancel request takes 500ms instead of 50ms.
Impact: Sniper fills our stale quote before cancel lands.

Mitigation:
  - Use GTD with short expiry (60s rolling) as backup.
  - Keep quote sizes conservative.
  - Monitor cancel latency; auto-widen spread if latency degrades.
```

### Case 3: One-Sided Flow (Everyone Buys India)

```
Scenario: India is dominating. Retail piles into India.
          We keep selling India tokens, accumulating USDC but depleting India inventory.

State: q_a = 50, q_b = 500 (started at 500 each)

Response:
  1. Tier system activates: yellow → orange → red
  2. Stop quoting India SELL (no more inventory to sell)
  3. Tighten India BUY (try to accumulate India back)
  4. If needed: split USDC → fresh tokens, or cross spread to buy India
  5. Eventually: India's price rises enough that retail stops buying
```

### Case 4: Price Near Extreme (India at 0.95)

```
Scenario: India almost certain to win. Price compressed near 1.00.

Problems:
  - Spread of 0.01 means ask at 0.96, bid at 0.94
  - England at 0.05 — almost no room for spread
  - Any wicket could move price 5+ cents (disproportionately large)

Response:
  - Reduce maker quote size significantly
  - Widen spread (2-3 ticks minimum)
  - Or stop maker entirely and let taker handle remaining events
```

### Case 5: Rain Delay / Match Interruption

```
Scenario: Play stops unexpectedly. No events for extended period.

Response:
  - If oracle signals "play stopped": cancel all quotes
  - If no signal but suspiciously long gap: auto-widen after timeout
  - Resume quoting when play resumes
  - Don't leave stale quotes sitting during interruptions
```

### Case 6: Both Legs Fill Simultaneously (Best Case)

```
Scenario: Retail buyer takes our India SELL at 0.61
          Retail buyer takes our England SELL at 0.40

Result:
  Collected: 0.61 + 0.40 = $1.01
  Cost: $1.00 (the pair was from a split)
  Profit: $0.01 per token, locked in immediately

  Merge remaining pairs to realize more profit.
  Split fresh USDC to reload inventory.
  This is the ideal steady-state cycle.
```

### Case 7: Taker and Maker Competing for Same Liquidity

```
Scenario: On a wicket, taker wants to FAK-buy bowling team.
          Maker also has a resting BUY on bowling team (safe leg, kept live).

Is there a conflict? No.
  - Maker's resting BUY is at fair_b - spread (below market ask)
  - Taker's FAK-BUY hits the current ask (at or above market)
  - They operate at different price levels
  - Maker's order remains resting; taker's fills immediately

If by chance they're at the same price: taker's FAK fills against
existing asks, not against our own resting buy. No self-matching issue.
```

---

## Appendix: Key Parameters to Tune

| Parameter | Description | Suggested Starting Value |
|-----------|-------------|--------------------------|
| `half_spread` | Distance from fair to quote | 0.01 (1 cent / 1 tick) |
| `quote_size` | Tokens per leg | 50 |
| `skew_kappa` | Inventory skew intensity | 0.0005 per token |
| `max_exposure` | Hard inventory limit | 200 tokens |
| `tier_1_threshold` | Green → Yellow | 20% of split |
| `tier_2_threshold` | Yellow → Orange | 50% of split |
| `tier_3_threshold` | Orange → Red | 80% of split |
| `requote_drift` | Fair value drift before re-quote | 0.02 |
| `gtd_expiry_seconds` | Rolling GTD expiry | 60 |
| `settle_wait_ms` | Wait after event before re-quote | 3000 |
| `split_amount` | Initial USDC to split | 500 |
| `maker_budget` | Total USDC allocated to maker | 1000 |
| `taker_budget` | Total USDC allocated to taker | 200 |

---

## Appendix: Theoretical Foundation

### Avellaneda-Stoikov Reservation Price

The optimal market maker quotes around an inventory-adjusted "reservation price" rather than the raw midpoint:

```
reservation_price = mid - q * gamma * sigma^2 * (T - t)

where:
  mid    = orderbook midpoint
  q      = signed inventory (positive = long)
  gamma  = risk aversion (higher = more aggressive skew)
  sigma  = price volatility
  T - t  = time remaining in match
```

For cricket prediction markets, we adapt:
- `sigma = sqrt(p * (1-p)) * event_frequency_factor` (bounded binary volatility)
- `T - t` = deliveries remaining / total deliveries
- `gamma` = tunable based on our risk tolerance

### Optimal Spread (AS Model)

```
total_spread = gamma * sigma^2 * (T - t) + (2/gamma) * ln(1 + gamma/k)

where:
  k = order flow elasticity (how much fill rate drops per tick of spread)
```

The first term is risk compensation (wider when volatile or early in match).
The second term is market structure (wider when order flow is inelastic).

### Mean Reversion Between Deliveries

Between events, prediction market prices tend to mean-revert:

```
dp = theta * (mu - p) * dt + sigma * sqrt(p*(1-p)) * dW

where:
  theta = mean-reversion speed
  mu    = current "consensus" probability
```

This is favorable for makers: temporary price displacements (noise trades) revert, meaning our inventory holdings are less risky than they would be under a random walk. We can quote tighter than a pure AS model would suggest.
