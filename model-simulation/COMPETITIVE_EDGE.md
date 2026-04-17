# Competitive Edge Analysis

## Who You're Trading Against

### Tier 1: Ground-Level Market Maker
- 1-3 operators with live TV or in-stadium access
- See events 5-15 seconds after ball is bowled
- Update Polymarket quotes within seconds (maker orders have NO delay)
- Collect Polymarket's $5M/month liquidity rewards as subsidy
- **Their edge: SPEED** — they see it first
- **Their weakness: ACCURACY** — they reprice by intuition, not structural math

### Tier 2: Latency Arb Bots (e.g., "swisstony")
- Use faster data feeds (TV streams, Betfair prices) to take against stale quotes
- "swisstony" turned $5 into $3.7M; another bot made $8M in 2 months
- **Their edge: faster data + automated execution**
- **Their weakness: only profitable on clearly mispriced events, no structural model**

### Tier 3: Retail / Crypto-Native Traders
- Trade based on vibes, team loyalty, delayed information
- Systematically overpay for low-probability outcomes (fan bias)
- **The ultimate source of profit in the ecosystem**

## Your Edge: Structural Accuracy

You cannot be faster than Tier 1 (they have eyes on the ground). But you can be more ACCURATE. Here's where human intuition fails and math wins:

### 1. Wicket Overreaction (Strongest, Academically Proven)

Norton, Gray & Faff (2015, Journal of Banking & Finance):
- Backing the batting team after a first-innings wicket on Betfair produced **20.8% returns**
- Significant at the 1% level across 1,098 ODIs
- The mechanism: humans overestimate wicket impact, especially for tail-enders

**How the model exploits this:**
- MM sees a wicket → moves price by ~5 cents
- DP model knows it was the #8 batter dismissed with 2 overs left → correct move is 2 cents
- Trade the 3-cent overreaction

### 2. Nonlinear Chase Math

Human intuition is linear; cricket chase math is deeply nonlinear:
- 36 off 12 balls with 8 wickets: ~72% win probability
- 36 off 12 balls with 3 wickets: ~28% win probability
- The SAME score/balls but wildly different probabilities

Humans underprice wickets-in-hand in the death overs because they anchor on required run rate.

**How the model exploits this:**
- DP table has exact values for every (runs_needed, balls_remaining, wickets) state
- No interpolation, no approximation — exact backward-induction solution

### 3. Cross-Event Pricing

The MM reprices event-by-event (reacts to a four, reacts to a wicket). Your model updates the full state holistically:
- After 3 consecutive dot balls: the DP probability shifts even though "nothing happened"
- The dot balls consumed resources (balls) without producing runs — this is information
- The MM's quote may not adjust for dot ball sequences until a dramatic event occurs

### 4. DLS / Rain Scenarios

When rain interrupts a match:
- Markets reprice based on run rate (intuitive but wrong)
- DLS uses resources (overs + wickets combined), which is structurally different
- A team 10 runs behind par with 8 wickets in hand has MORE DLS resources than a team at par with 4 wickets
- The DP model naturally handles interrupted matches via state lookup

### 5. Venue/Player Specificity

Generic MM rule: "wicket = -5 cents." Reality varies enormously:
- Wicket at Chinnaswamy (Bangalore, small ground, batting paradise): smaller impact
- Wicket at Chepauk (Chennai, spin-friendly, low scoring): larger impact
- Virat Kohli dismissed vs. #9 batter dismissed: completely different probabilities
- DP table with player identity captures all of this exactly

## The Latency Chain (Measured)

From your microstructure analysis (4 IPL matches, 260 events):

```
Ball bowled                    T = 0s
Stadium/scorer                 T + 0-1s
TV broadcast                   T + 5-15s
Polymarket MM requotes          T + 10-30s   ← they are here
Betfair reacts                 T + 10-25s   (+ 5s bet delay)
Cricket API (ESPN/Cricbuzz)    T + 60-120s  ← your data source is here
```

**Implication:** You are structurally ~60-90 seconds behind the market. Speed is not your game. But when the market moves, you can INSTANTLY compute whether the new price is correct using your pre-computed DP table. If it's wrong, you trade.

## Polymarket Infrastructure Details

- **CLOB hosted on AWS eu-west-2 (London)**
- **Maker orders: NO delay, zero fees**
- **Taker orders: 3-second delay, ~3% fee** (formula: `fee = shares * 0.03 * p * (1-p)`)
- **Tick size: $0.01** default; $0.001 at extremes (>$0.96 or <$0.04)
- **Rate limits: 3,500 orders/10s placements, 3,000 cancellations/10s**
- **25% daily rebate to makers** from collected taker fees

## Fee-Adjusted Edge Requirement

As a taker with 3% fee structure:
- At p=0.50: fee = $0.75 per 100 shares → need >1.5% edge to break even
- At p=0.70: fee = $0.63 per 100 shares → need >1.3% edge
- At p=0.90: fee = $0.27 per 100 shares → need >0.5% edge

**Minimum edge threshold: ~2-3% after calibration to reliably profit as a taker.**

The Norton paper found 20.8% returns on wicket overreaction — well above threshold.
