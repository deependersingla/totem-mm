# Lessons Learned — What NOT to Do Again

## Mistake 1: Fake Metrics

The first build scored "100/100" by validating the model against itself (predicting historical outcomes using data from the same distribution). This is meaningless. Real validation must be against:
- Actual Polymarket prices (captured in `captures/`)
- Actual match outcomes on held-out matches the model has never seen

Never celebrate a metric that isn't measured against external ground truth.

## Mistake 2: 18-Year Averaged Transitions

Used 2008-2026 averaged scoring rates. IPL has transformed:
- 2008-2020 avg 1st innings: ~160
- 2023-2026 avg 1st innings: ~189
- 2026 alone: ~193, with 57% of scores over 200

Using old data dilutes modern reality. At minimum, use only 2023+ data. Ideally, weight recent matches exponentially.

## Mistake 3: Ignoring Match Context

The model had NO awareness of:
- **Reduced overs matches**: Rain BEFORE the match = proper reduced-overs game (NOT DLS). Different powerplay length, different max overs per bowler. The RAJ vs MUM match was 11 overs per side — model assumed 120 balls and gave 95% chase prob when market gave 25%.
- **Pitch conditions**: 1st innings score of 150 in a 193-average era signals a difficult pitch. The model used the same transitions regardless.
- **Who is batting/bowling**: The single biggest gap. Virat Kohli vs a #11 tail-ender produce completely different outcome distributions.

## Mistake 4: Obsessing Over Outcome Granularity

Spent time enumerating 62 distinct ball outcomes when win probability doesn't care whether a run came from a leg-bye or a bat shot. The score is the score. What matters:
- **Runs added to total** (affects runs_needed)
- **Ball consumed or not** (affects balls_remaining)  
- **Wicket or not** (affects wickets_in_hand)
- **Who got out** (affects future transition probabilities)

Everything else (stumped vs bowled vs caught, bye vs leg-bye) is irrelevant for win probability.

## Mistake 5: Not Understanding Cricket Rules

- DLS applies when rain interrupts an IN-PROGRESS match
- Rain before the match = reduced overs game with modified playing conditions (proportional powerplay, max overs per bowler reduced)
- Free hit after no-ball (next ball: only run-out dismissal possible)
- Super over rules for ties (1 over per side, different playing conditions)

These aren't edge cases — they're fundamental rules that change the entire state space.

## Mistake 6: Building Before Thinking

Jumped straight into coding a DP solver before properly understanding:
- Whether ball events are independent (they're not — 2nd order Markov with rich state)
- What features actually drive win probability (player identity is #1, not phase averages)
- What the DynaSim paper found (57 outcomes, 20 features, 76% accuracy with temporal split)
- How Polymarket actually prices cricket (ground-level MM with TV feed, not API-driven)

## What Actually Matters for Edge

The Polymarket MM reprices by human intuition after watching TV. The edge comes from:
1. **Knowing the math better than intuition** — nonlinear chase scenarios, DLS situations, wickets-in-hand effects
2. **Player-specific impact** — which wicket matters (star vs tail-ender)
3. **Conditions awareness** — pitch difficulty inferred from 1st innings score and venue history

The model doesn't need 62 outcome classes. It needs to answer: "given THIS batter, THIS bowler, THIS pitch, THIS state — what's P(chase team wins)?"

## The DP Architecture is Sound

The backward induction approach works:
- 467K states (basic) or 18.7M states (with player identity)
- Solves in <1 second
- O(1) lookup
- Exact answers (no MC variance)

The problem was never the DP structure. It's the INPUTS — the transition probabilities — that were too generic.
