/// Tests for position tracking — budget checks, token balances, fill accounting.
use crate::position::PositionInner;
use crate::types::{FakOrder, Side, Team};
use rust_decimal::Decimal;
use rust_decimal_macros::dec;

fn make_position(budget: &str) -> PositionInner {
    PositionInner {
        team_a_tokens: Decimal::ZERO,
        team_b_tokens: Decimal::ZERO,
        total_spent: Decimal::ZERO,
        trade_count: 0,
        total_budget: budget.parse().unwrap(),
    }
}

fn buy_order(team: Team, price: Decimal, size: Decimal) -> FakOrder {
    FakOrder { team, side: Side::Buy, price, size }
}

fn sell_order(team: Team, price: Decimal, size: Decimal) -> FakOrder {
    FakOrder { team, side: Side::Sell, price, size }
}

// ── can_spend ─────────────────────────────────────────────────────────────────

#[test]
fn can_spend_within_budget() {
    let pos = make_position("100");
    assert!(pos.can_spend(dec!(50)));
}

#[test]
fn can_spend_exactly_at_budget() {
    let pos = make_position("100");
    assert!(pos.can_spend(dec!(100)));
}

#[test]
fn cannot_spend_over_budget() {
    let pos = make_position("100");
    assert!(!pos.can_spend(dec!(100.01)));
    assert!(!pos.can_spend(dec!(200)));
}

#[test]
fn can_spend_accounts_for_already_spent() {
    let mut pos = make_position("100");
    pos.total_spent = dec!(80);
    assert!(pos.can_spend(dec!(20)));
    assert!(!pos.can_spend(dec!(20.01)));
}

// ── remaining_budget ──────────────────────────────────────────────────────────

#[test]
fn remaining_budget_starts_at_total() {
    let pos = make_position("100");
    assert_eq!(pos.remaining_budget(), dec!(100));
}

#[test]
fn remaining_budget_decreases_after_buy_fill() {
    let mut pos = make_position("100");
    pos.on_fill(&buy_order(Team::TeamA, dec!(0.60), dec!(10)));
    // notional = 0.60 * 10 = 6 USDC
    assert_eq!(pos.total_spent, dec!(6));
    assert_eq!(pos.remaining_budget(), dec!(94));
}

#[test]
fn remaining_budget_clamps_at_zero_when_overspent() {
    let mut pos = make_position("10");
    pos.total_spent = dec!(15); // edge case guard
    assert_eq!(pos.remaining_budget(), Decimal::ZERO);
}

// ── on_fill — buy side ────────────────────────────────────────────────────────

#[test]
fn buy_fill_increases_team_a_tokens() {
    let mut pos = make_position("100");
    pos.on_fill(&buy_order(Team::TeamA, dec!(0.65), dec!(20)));
    assert_eq!(pos.team_a_tokens, dec!(20));
    assert_eq!(pos.team_b_tokens, Decimal::ZERO);
}

#[test]
fn buy_fill_increases_team_b_tokens() {
    let mut pos = make_position("100");
    pos.on_fill(&buy_order(Team::TeamB, dec!(0.35), dec!(10)));
    assert_eq!(pos.team_b_tokens, dec!(10));
    assert_eq!(pos.team_a_tokens, Decimal::ZERO);
    assert_eq!(pos.total_spent, dec!(3.5));
}

#[test]
fn buy_fill_adds_notional_to_total_spent() {
    let mut pos = make_position("100");
    pos.on_fill(&buy_order(Team::TeamA, dec!(0.63), dec!(15)));
    assert_eq!(pos.total_spent, dec!(9.45)); // 0.63 * 15
}

// ── on_fill — sell side ───────────────────────────────────────────────────────

#[test]
fn sell_fill_decreases_team_a_tokens() {
    let mut pos = make_position("100");
    pos.team_a_tokens = dec!(30);
    pos.on_fill(&sell_order(Team::TeamA, dec!(0.70), dec!(15)));
    assert_eq!(pos.team_a_tokens, dec!(15));
}

#[test]
fn sell_fill_does_not_change_total_spent() {
    // Selling recovers cash but the current implementation does not track
    // it in total_spent. This test documents that behaviour.
    let mut pos = make_position("100");
    pos.total_spent = dec!(20);
    pos.team_a_tokens = dec!(10);
    pos.on_fill(&sell_order(Team::TeamA, dec!(0.65), dec!(10)));
    assert_eq!(pos.total_spent, dec!(20), "sell should not change total_spent");
}

#[test]
fn sell_fill_decreases_team_b_tokens() {
    let mut pos = make_position("100");
    pos.team_b_tokens = dec!(50);
    pos.on_fill(&sell_order(Team::TeamB, dec!(0.40), dec!(20)));
    assert_eq!(pos.team_b_tokens, dec!(30));
}

// ── trade_count ───────────────────────────────────────────────────────────────

#[test]
fn trade_count_increments_on_every_fill() {
    let mut pos = make_position("1000");
    for _ in 0..7 {
        pos.on_fill(&buy_order(Team::TeamA, dec!(0.50), dec!(1)));
    }
    assert_eq!(pos.trade_count, 7);
}

#[test]
fn trade_count_increments_for_sells_too() {
    let mut pos = make_position("100");
    pos.team_a_tokens = dec!(10);
    pos.on_fill(&sell_order(Team::TeamA, dec!(0.60), dec!(5)));
    assert_eq!(pos.trade_count, 1);
}

// ── sequential buy + sell ────────────────────────────────────────────────────

#[test]
fn buy_then_sell_leaves_partial_position() {
    let mut pos = make_position("100");
    pos.on_fill(&buy_order(Team::TeamA, dec!(0.60), dec!(20)));
    pos.on_fill(&sell_order(Team::TeamA, dec!(0.70), dec!(10)));
    assert_eq!(pos.team_a_tokens, dec!(10));
    assert_eq!(pos.trade_count, 2);
}
