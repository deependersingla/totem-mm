# Research Findings — Consolidated

## IPL Scoring Trends

| Season | Avg 1st Inn | 200+ % | Chase Win% |
|--------|------------|--------|-----------|
| 2008-2020 | 160 | 10% | 53% |
| 2023 | 184 | 33% | 45% |
| 2024 | 190 | 37% | 51% |
| 2025 | 191 | 46% | 51% |
| 2026 (21 matches) | 193 | 57% | 57% |

Impact player rule + batting-friendly conditions have fundamentally changed the game. Any model using pre-2023 data is outdated.

## Phase Scoring Rates (Modern Era 2023-2026)

| Phase | Strike Rate | Dot% | Four% | Six% | Wicket% |
|-------|------------|------|-------|------|---------|
| Powerplay | 147 | 39.7% | 18.2% | 6.2% | 4.2% |
| Middle | 141 | 25.8% | 10.3% | 6.7% | 4.7% |
| Death | 169 | 22.4% | 12.7% | 10.5% | 8.3% |

Compare to 2008-2020: PP SR was 119, death SR was 154. Modern IPL is 20-30% more aggressive.

## Ball-by-Ball Dependencies (From Literature)

Events are NOT independent. Key dependencies:

| Effect | Magnitude | Source |
|--------|-----------|--------|
| New batter vulnerability | 3-4x higher dismissal in first 5-15 balls | Bracewell & Ruggiero 2009 |
| Dot ball pressure | 15-25% higher wicket prob after 3+ dots | Kimber & Hansford 1993 |
| Partnership acceleration | 15-20% faster scoring when both settled | Lemmer 2011 |
| Within-over pattern | Ball 6 yields 10-15% more than ball 1 | Swartz et al. 2006 |
| Momentum (hot hand) | Weak: r=0.04-0.07 between consecutive balls | Davis-Perera-Swartz 2015 |

**Practical conclusion**: 2nd-order Markov (last 2 balls + state) captures virtually all exploitable structure (Dey-Ganguly-Saikia 2017). Higher-order adds negligible improvement.

## Actual Ball Outcomes in IPL

62 distinct outcomes observed across 283K deliveries. But for WIN PROBABILITY, only 4 things matter per delivery:
1. Runs added to total
2. Ball consumed or not (wides/no-balls don't consume)
3. Wicket or not
4. Who got out (determines next batter's quality)

The 62 outcomes collapse to ~15 functionally distinct states for DP purposes.

## Polymarket Cricket Market Structure

- CLOB (order book), not AMM
- Maker orders: zero fee, no delay
- Taker orders: 3% fee, 3-second delay
- Ground-level MM reprices by TV feed, 10-30 seconds after ball
- Market LEADS cricket APIs by 30-90 seconds (our data source is slowest in chain)
- $5M/month liquidity incentives from Polymarket
- Polymarket LEADS Betfair in price discovery for cricket (not the other way around)

## Where Edge Exists (Academic Evidence)

Norton-Gray-Faff (2015, Journal of Banking & Finance):
- First-innings wicket overreaction: **20.8% returns** on Betfair
- Market systematically overestimates wicket impact
- Statistically significant at 1% level across 1,098 matches

This edge likely exists on Polymarket too, especially for:
- Tail-ender wickets (market moves 5c, should move 2c)
- Nonlinear chase math in death overs
- DLS/reduced-overs mispricing

## DynaSim Paper (Mysore et al. 2023)

- 57 outcomes per ball, 20 features, IPL-specific
- Trained on 692 IPL matches (2008-2018), tested on 2019 season
- 76.47% accuracy (traditional ML) vs 76.13% (LSTM/GRU)
- Key finding: sequence models did NOT beat state-based models → Markov property holds with rich enough state
- Behind Springer paywall, no open access

## AlphaZero / RL — Not the Right Tool

Evaluated and rejected:
- Cricket is stochastic + imperfect information → not chess/Go
- RL produces strategies, not probabilities → wrong output for trading
- State space is small enough for exact DP → no need for function approximation
- One exception: Inverse RL (Markov Cricket, arxiv 2103.04349) for calibrating transition probabilities

## Reduced Overs Rules (Corrected)

**Rain BEFORE match**: Proper reduced-overs game. NOT DLS. Proportional powerplay, reduced max overs per bowler. Target = 1st innings score + 1.

**Rain DURING match**: DLS applies. Adjusted target based on resources remaining.

The RAJ vs MUM match in our test set was an 11-over match (rain before), not a DLS scenario. Our model treated it as 20 overs → catastrophic error.
