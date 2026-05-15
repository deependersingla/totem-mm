/// Tests for `decide_entry_direction` — the conditional flip between
/// directional (current-strategy momentum) and reverse (mean-reversion) entry.
///
/// Rule: REVERSE only when BOTH sides we'd cross have already moved in the
/// news direction by ≥ threshold (default `2 × edge`). Otherwise DIRECTIONAL.
/// Cold start (no past snapshot) → DIRECTIONAL.

use std::time::Instant;

use rust_decimal::Decimal;
use rust_decimal_macros::dec;

use crate::price_history::TouchSnapshot;
use crate::strategy::{decide_entry_direction, premove_blocks_entry, EntryDirection, LegPair};
use crate::types::Team;

// ── helpers ──────────────────────────────────────────────────────────────────

fn touch(bid_a: Decimal, ask_a: Decimal, bid_b: Decimal, ask_b: Decimal) -> TouchSnapshot {
    TouchSnapshot {
        ts: Instant::now(),
        bid_a: Some(bid_a),
        ask_a: Some(ask_a),
        bid_b: Some(bid_b),
        ask_b: Some(ask_b),
    }
}

const T_A: Team = Team::TeamA;

// ── WICKET: directional = SELL batting + BUY bowling ─────────────────────────
//
// News on a wicket against batting team A:
//   batting (A) bid ↓ (we'd hit it on the directional sell)
//   bowling (B) ask ↑ (we'd lift it on the directional buy)
//
// Move detection compares past touches vs current:
//   drop_in_sell_bid = past.bid_a - current.bid_a
//   rise_in_buy_ask  = current.ask_b - past.ask_b
//
// Both ≥ threshold → REVERSE; otherwise DIRECTIONAL.

#[test]
fn wicket_reverse_when_both_sides_moved_by_at_least_threshold() {
    // Threshold = 2 × edge_wicket = 2 × 3¢ = 6¢
    let threshold = dec!(0.06);
    let past = touch(dec!(0.70), dec!(0.71), dec!(0.29), dec!(0.30));
    // Wicket on A: A bid drops 7¢ (0.70 → 0.63), B ask rises 7¢ (0.30 → 0.37)
    let now = touch(dec!(0.63), dec!(0.64), dec!(0.36), dec!(0.37));
    let dir = decide_entry_direction(LegPair::SellBattingBuyBowling, T_A, now, Some(past), threshold);
    assert_eq!(dir, EntryDirection::Reverse);
}

#[test]
fn wicket_reverse_at_exactly_threshold_inclusive() {
    let threshold = dec!(0.06);
    let past = touch(dec!(0.70), dec!(0.71), dec!(0.29), dec!(0.30));
    // Exactly 6¢ on each side
    let now = touch(dec!(0.64), dec!(0.65), dec!(0.35), dec!(0.36));
    let dir = decide_entry_direction(LegPair::SellBattingBuyBowling, T_A, now, Some(past), threshold);
    assert_eq!(dir, EntryDirection::Reverse, "threshold check is `>=`, not `>`");
}

#[test]
fn wicket_directional_just_below_threshold() {
    let threshold = dec!(0.06);
    let past = touch(dec!(0.70), dec!(0.71), dec!(0.29), dec!(0.30));
    // 5.9¢ on each side — under threshold
    let now = touch(dec!(0.641), dec!(0.651), dec!(0.349), dec!(0.359));
    let dir = decide_entry_direction(LegPair::SellBattingBuyBowling, T_A, now, Some(past), threshold);
    assert_eq!(dir, EntryDirection::Directional);
}

#[test]
fn wicket_directional_when_only_batting_dropped() {
    // Sell side moved enough but buy side didn't — book stutter, not a real
    // news move. Stay directional.
    let threshold = dec!(0.06);
    let past = touch(dec!(0.70), dec!(0.71), dec!(0.29), dec!(0.30));
    // A bid drops 7¢ but B ask only rises 1¢
    let now = touch(dec!(0.63), dec!(0.64), dec!(0.30), dec!(0.31));
    let dir = decide_entry_direction(LegPair::SellBattingBuyBowling, T_A, now, Some(past), threshold);
    assert_eq!(dir, EntryDirection::Directional);
}

#[test]
fn wicket_directional_when_only_bowling_rose() {
    let threshold = dec!(0.06);
    let past = touch(dec!(0.70), dec!(0.71), dec!(0.29), dec!(0.30));
    // A bid drops 1¢ but B ask rises 7¢
    let now = touch(dec!(0.69), dec!(0.70), dec!(0.36), dec!(0.37));
    let dir = decide_entry_direction(LegPair::SellBattingBuyBowling, T_A, now, Some(past), threshold);
    assert_eq!(dir, EntryDirection::Directional);
}

#[test]
fn wicket_directional_when_neither_moved_enough() {
    let threshold = dec!(0.06);
    let past = touch(dec!(0.70), dec!(0.71), dec!(0.29), dec!(0.30));
    let now = touch(dec!(0.69), dec!(0.70), dec!(0.30), dec!(0.31)); // 1¢ each side
    let dir = decide_entry_direction(LegPair::SellBattingBuyBowling, T_A, now, Some(past), threshold);
    assert_eq!(dir, EntryDirection::Directional);
}

#[test]
fn wicket_directional_when_pre_signal_moved_in_wrong_direction() {
    // Batting bid actually ROSE pre-signal (opposite of wicket news).
    // Definitely don't reverse — looks like a fake/early signal.
    let threshold = dec!(0.06);
    let past = touch(dec!(0.60), dec!(0.61), dec!(0.39), dec!(0.40));
    // A bid rises 7¢, B ask drops 7¢
    let now = touch(dec!(0.67), dec!(0.68), dec!(0.32), dec!(0.33));
    let dir = decide_entry_direction(LegPair::SellBattingBuyBowling, T_A, now, Some(past), threshold);
    assert_eq!(dir, EntryDirection::Directional);
}

#[test]
fn wicket_directional_when_no_past_snapshot_cold_start() {
    let threshold = dec!(0.06);
    let now = touch(dec!(0.63), dec!(0.64), dec!(0.36), dec!(0.37));
    let dir = decide_entry_direction(LegPair::SellBattingBuyBowling, T_A, now, None, threshold);
    assert_eq!(dir, EntryDirection::Directional, "cold-start defaults to directional");
}

#[test]
fn wicket_directional_when_past_missing_relevant_side() {
    let threshold = dec!(0.06);
    let past = TouchSnapshot {
        ts: Instant::now(),
        bid_a: None, // missing past bid for batting
        ask_a: Some(dec!(0.71)),
        bid_b: Some(dec!(0.29)),
        ask_b: Some(dec!(0.30)),
    };
    let now = touch(dec!(0.63), dec!(0.64), dec!(0.36), dec!(0.37));
    let dir = decide_entry_direction(LegPair::SellBattingBuyBowling, T_A, now, Some(past), threshold);
    assert_eq!(dir, EntryDirection::Directional);
}

#[test]
fn wicket_directional_when_current_missing_relevant_side() {
    let threshold = dec!(0.06);
    let past = touch(dec!(0.70), dec!(0.71), dec!(0.29), dec!(0.30));
    let now = TouchSnapshot {
        ts: Instant::now(),
        bid_a: Some(dec!(0.63)),
        ask_a: Some(dec!(0.64)),
        bid_b: Some(dec!(0.36)),
        ask_b: None, // missing current ask for bowling — can't compare
    };
    let dir = decide_entry_direction(LegPair::SellBattingBuyBowling, T_A, now, Some(past), threshold);
    assert_eq!(dir, EntryDirection::Directional);
}

// ── BOUNDARY (RUN/WD/NB): directional = SELL bowling + BUY batting ───────────
//
// News on a boundary by batting team A:
//   bowling (B) bid ↓ (we'd hit it on the directional sell)
//   batting (A) ask ↑ (we'd lift it on the directional buy)

#[test]
fn boundary_reverse_when_both_sides_moved_by_at_least_threshold() {
    // Threshold = 2 × edge_boundary_4 = 2 × 2¢ = 4¢
    let threshold = dec!(0.04);
    let past = touch(dec!(0.40), dec!(0.41), dec!(0.59), dec!(0.60));
    // Boundary by A: A ask rises 5¢ (0.41 → 0.46), B bid drops 5¢ (0.59 → 0.54)
    let now = touch(dec!(0.45), dec!(0.46), dec!(0.54), dec!(0.55));
    let dir = decide_entry_direction(LegPair::SellBowlingBuyBatting, T_A, now, Some(past), threshold);
    assert_eq!(dir, EntryDirection::Reverse);
}

#[test]
fn boundary_directional_when_below_threshold() {
    let threshold = dec!(0.04); // 4¢
    let past = touch(dec!(0.40), dec!(0.41), dec!(0.59), dec!(0.60));
    let now = touch(dec!(0.42), dec!(0.43), dec!(0.57), dec!(0.58)); // 2¢ each side
    let dir = decide_entry_direction(LegPair::SellBowlingBuyBatting, T_A, now, Some(past), threshold);
    assert_eq!(dir, EntryDirection::Directional);
}

#[test]
fn boundary_directional_when_only_one_side_moved() {
    let threshold = dec!(0.04);
    let past = touch(dec!(0.40), dec!(0.41), dec!(0.59), dec!(0.60));
    // B bid drops 5¢, A ask only rises 1¢
    let now = touch(dec!(0.41), dec!(0.42), dec!(0.54), dec!(0.55));
    let dir = decide_entry_direction(LegPair::SellBowlingBuyBatting, T_A, now, Some(past), threshold);
    assert_eq!(dir, EntryDirection::Directional);
}

#[test]
fn boundary_directional_when_pre_signal_moved_against_news_direction() {
    // Boundary by A should push A up, B down. If pre-signal A actually went
    // DOWN (negative drop_in_sell_bid for our framing), don't reverse.
    let threshold = dec!(0.04);
    let past = touch(dec!(0.45), dec!(0.46), dec!(0.54), dec!(0.55));
    // A ask drops 5¢ (wrong direction), B bid rises 5¢ (also wrong direction)
    let now = touch(dec!(0.40), dec!(0.41), dec!(0.59), dec!(0.60));
    let dir = decide_entry_direction(LegPair::SellBowlingBuyBatting, T_A, now, Some(past), threshold);
    assert_eq!(dir, EntryDirection::Directional);
}

// ── batting team B path (mirror) ─────────────────────────────────────────────
//
// Same logic but with the team roles flipped — verifies the helper isn't
// hardcoded to batting=TeamA.

#[test]
fn wicket_reverse_when_batting_is_team_b() {
    let threshold = dec!(0.06);
    // Team B is batting; wicket against B → B bid ↓, A ask ↑
    let past = touch(dec!(0.29), dec!(0.30), dec!(0.70), dec!(0.71));
    let now = touch(dec!(0.36), dec!(0.37), dec!(0.63), dec!(0.64));
    let dir = decide_entry_direction(LegPair::SellBattingBuyBowling, Team::TeamB, now, Some(past), threshold);
    assert_eq!(dir, EntryDirection::Reverse);
}

#[test]
fn boundary_reverse_when_batting_is_team_b() {
    let threshold = dec!(0.04);
    // Team B is batting; boundary by B → B ask ↑, A bid ↓
    let past = touch(dec!(0.59), dec!(0.60), dec!(0.40), dec!(0.41));
    let now = touch(dec!(0.54), dec!(0.55), dec!(0.45), dec!(0.46));
    let dir = decide_entry_direction(LegPair::SellBowlingBuyBatting, Team::TeamB, now, Some(past), threshold);
    assert_eq!(dir, EntryDirection::Reverse);
}

// ── threshold passed via parameter, not derived inside helper ─────────────────

#[test]
fn helper_uses_caller_supplied_threshold_not_a_constant() {
    // With threshold = 1¢, even a small move triggers reverse.
    let small_threshold = dec!(0.01);
    let past = touch(dec!(0.70), dec!(0.71), dec!(0.29), dec!(0.30));
    let now = touch(dec!(0.69), dec!(0.70), dec!(0.30), dec!(0.31)); // 1¢ each
    let dir = decide_entry_direction(LegPair::SellBattingBuyBowling, T_A, now, Some(past), small_threshold);
    assert_eq!(dir, EntryDirection::Reverse);
}

// ── premove_blocks_entry: the "signal arrived late" skip guard ────────────────
//
// Unlike decide_entry_direction (REVERSE only when BOTH sides moved), this
// trips when EITHER leg moved ≥ threshold in the news direction. When it
// returns true the strategy skips the trade entirely (config.skip_on_premove).
// Cold start / missing data / adverse moves → false (keep trading).

#[test]
fn premove_blocks_when_both_sides_moved() {
    // Superset of the REVERSE case — must also block here.
    let threshold = dec!(0.06);
    let past = touch(dec!(0.70), dec!(0.71), dec!(0.29), dec!(0.30));
    let now = touch(dec!(0.63), dec!(0.64), dec!(0.36), dec!(0.37)); // 7¢ each
    assert!(premove_blocks_entry(LegPair::SellBattingBuyBowling, T_A, now, Some(past), threshold));
}

#[test]
fn premove_blocks_when_only_sell_bid_dropped() {
    // The key difference vs decide_entry_direction: one leg is enough.
    // Here decide_entry_direction would say Directional (book stutter).
    let threshold = dec!(0.06);
    let past = touch(dec!(0.70), dec!(0.71), dec!(0.29), dec!(0.30));
    // A (sell) bid drops 7¢; B (buy) ask only rises 1¢.
    let now = touch(dec!(0.63), dec!(0.64), dec!(0.30), dec!(0.31));
    assert_eq!(
        decide_entry_direction(LegPair::SellBattingBuyBowling, T_A, now, Some(past), threshold),
        EntryDirection::Directional,
        "sanity: the both-sides rule would still trade here"
    );
    assert!(
        premove_blocks_entry(LegPair::SellBattingBuyBowling, T_A, now, Some(past), threshold),
        "either-side rule must block on the single confirmed leg move"
    );
}

#[test]
fn premove_blocks_when_only_buy_ask_rose() {
    let threshold = dec!(0.06);
    let past = touch(dec!(0.70), dec!(0.71), dec!(0.29), dec!(0.30));
    // A (sell) bid drops 1¢; B (buy) ask rises 7¢.
    let now = touch(dec!(0.69), dec!(0.70), dec!(0.36), dec!(0.37));
    assert!(premove_blocks_entry(LegPair::SellBattingBuyBowling, T_A, now, Some(past), threshold));
}

#[test]
fn premove_blocks_at_exactly_threshold_inclusive() {
    let threshold = dec!(0.06);
    let past = touch(dec!(0.70), dec!(0.71), dec!(0.29), dec!(0.30));
    // Sell bid drops exactly 6¢; buy ask flat.
    let now = touch(dec!(0.64), dec!(0.65), dec!(0.29), dec!(0.30));
    assert!(
        premove_blocks_entry(LegPair::SellBattingBuyBowling, T_A, now, Some(past), threshold),
        "check is `>=`, not `>`"
    );
}

#[test]
fn premove_does_not_block_just_below_threshold() {
    let threshold = dec!(0.06);
    let past = touch(dec!(0.70), dec!(0.71), dec!(0.29), dec!(0.30));
    // 5.9¢ on each side — under threshold, both legs.
    let now = touch(dec!(0.641), dec!(0.651), dec!(0.349), dec!(0.359));
    assert!(!premove_blocks_entry(LegPair::SellBattingBuyBowling, T_A, now, Some(past), threshold));
}

#[test]
fn premove_does_not_block_on_cold_start() {
    let threshold = dec!(0.06);
    let now = touch(dec!(0.63), dec!(0.64), dec!(0.36), dec!(0.37));
    assert!(
        !premove_blocks_entry(LegPair::SellBattingBuyBowling, T_A, now, None, threshold),
        "no past snapshot → no evidence of a pre-move → keep trading"
    );
}

#[test]
fn premove_does_not_block_on_missing_relevant_side() {
    let threshold = dec!(0.06);
    let past = TouchSnapshot {
        ts: Instant::now(),
        bid_a: None, // missing past bid for the sell (batting) leg
        ask_a: Some(dec!(0.71)),
        bid_b: Some(dec!(0.29)),
        ask_b: Some(dec!(0.30)),
    };
    let now = touch(dec!(0.63), dec!(0.64), dec!(0.36), dec!(0.37));
    assert!(!premove_blocks_entry(LegPair::SellBattingBuyBowling, T_A, now, Some(past), threshold));
}

#[test]
fn premove_does_not_block_on_adverse_move() {
    // Book moved the OPPOSITE way pre-signal — not "we're late on this news".
    let threshold = dec!(0.06);
    let past = touch(dec!(0.60), dec!(0.61), dec!(0.39), dec!(0.40));
    // A bid rises 7¢, B ask drops 7¢ (wrong direction for a wicket on A).
    let now = touch(dec!(0.67), dec!(0.68), dec!(0.32), dec!(0.33));
    assert!(!premove_blocks_entry(LegPair::SellBattingBuyBowling, T_A, now, Some(past), threshold));
}

#[test]
fn premove_blocks_boundary_legpair_on_single_leg() {
    // Boundary by batting A: directional = SELL bowling(B) + BUY batting(A).
    // News: B bid ↓, A ask ↑. Only the buy-side (A ask) moves ≥ threshold.
    let threshold = dec!(0.04);
    let past = touch(dec!(0.40), dec!(0.41), dec!(0.59), dec!(0.60));
    // A (buy) ask rises 5¢; B (sell) bid flat.
    let now = touch(dec!(0.40), dec!(0.46), dec!(0.59), dec!(0.60));
    assert!(premove_blocks_entry(LegPair::SellBowlingBuyBatting, T_A, now, Some(past), threshold));
}
