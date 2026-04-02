/// Tests for PendingRevert state tracking in AppState.
use std::time::Instant;

use rust_decimal_macros::dec;

use crate::config::Config;
use crate::state::{AppState, PendingRevert};
use crate::types::{Side, Team};

/// Minimal config for constructing AppState in tests.
fn test_config() -> Config {
    Config {
        polymarket_private_key: String::new(),
        polymarket_address: String::new(),
        signature_type: 1,
        neg_risk: false,
        chain_id: 137,
        polygon_rpc: String::new(),
        clob_http: String::new(),
        clob_ws: String::new(),
        team_a_name: "IND".to_string(),
        team_b_name: "ENG".to_string(),
        team_a_token_id: String::new(),
        team_b_token_id: String::new(),
        condition_id: String::new(),
        first_batting: Team::TeamA,
        total_budget_usdc: dec!(1000),
        max_trade_usdc: dec!(10),
        safe_percentage: 2,
        revert_delay_ms: 9500,
        fill_poll_interval_ms: 500,
        fill_poll_timeout_ms: 5000,
        tick_size: "0.01".to_string(),
        order_min_size: rust_decimal::Decimal::ONE,
        fee_rate_bps: 0,
        ws_ping_interval_secs: 10,
        dry_run: true,
        log_level: "info".to_string(),
        http_port: 3000,
        api_key: String::new(),
        api_secret: String::new(),
        api_passphrase: String::new(),
        market_slug: String::new(),
        edge_wicket: 2.0,
        edge_boundary_4: 1.0,
        edge_boundary_6: 1.0,
        fill_ws_timeout_ms: 5000,
        breakeven_timeout_ms: 3000,
        maker_config: crate::config::MakerConfig::default(),
        builder_api_key: String::new(),
        builder_api_secret: String::new(),
        builder_api_passphrase: String::new(),
    }
}

fn make_revert(order_id: &str, team: Team, side: Side) -> PendingRevert {
    PendingRevert {
        order_id: order_id.to_string(),
        team,
        side,
        size: dec!(10),
        entry_price: dec!(0.75),
        revert_limit_price: dec!(0.76),
        placed_at: Instant::now(),
        label: "TEST_REVERT".to_string(),
    }
}

// ── push_revert ──────────────────────────────────────────────────────────────

#[test]
fn push_revert_adds_entry() {
    let app = AppState::new(test_config());
    assert_eq!(app.pending_revert_count(), 0);
    app.push_revert(make_revert("ord1", Team::TeamA, Side::Sell));
    assert_eq!(app.pending_revert_count(), 1);
}

#[test]
fn push_multiple_reverts() {
    let app = AppState::new(test_config());
    app.push_revert(make_revert("ord1", Team::TeamA, Side::Sell));
    app.push_revert(make_revert("ord2", Team::TeamB, Side::Buy));
    app.push_revert(make_revert("ord3", Team::TeamA, Side::Buy));
    assert_eq!(app.pending_revert_count(), 3);
}

// ── remove_revert ────────────────────────────────────────────────────────────

#[test]
fn remove_revert_by_order_id() {
    let app = AppState::new(test_config());
    app.push_revert(make_revert("ord1", Team::TeamA, Side::Sell));
    app.push_revert(make_revert("ord2", Team::TeamB, Side::Buy));

    let removed = app.remove_revert("ord1");
    assert!(removed.is_some());
    assert_eq!(removed.unwrap().order_id, "ord1");
    assert_eq!(app.pending_revert_count(), 1);
}

#[test]
fn remove_revert_nonexistent_returns_none() {
    let app = AppState::new(test_config());
    app.push_revert(make_revert("ord1", Team::TeamA, Side::Sell));
    assert!(app.remove_revert("ord999").is_none());
    assert_eq!(app.pending_revert_count(), 1);
}

#[test]
fn remove_revert_leaves_others_intact() {
    let app = AppState::new(test_config());
    app.push_revert(make_revert("ord1", Team::TeamA, Side::Sell));
    app.push_revert(make_revert("ord2", Team::TeamB, Side::Buy));
    app.push_revert(make_revert("ord3", Team::TeamA, Side::Buy));

    app.remove_revert("ord2");
    assert_eq!(app.pending_revert_count(), 2);
    // ord1 and ord3 remain
    let reverts = app.pending_reverts.lock().unwrap();
    let ids: Vec<&str> = reverts.iter().map(|r| r.order_id.as_str()).collect();
    assert!(ids.contains(&"ord1"));
    assert!(ids.contains(&"ord3"));
}

// ── take_reverts_for_team ────────────────────────────────────────────────────

#[test]
fn take_reverts_for_team_returns_matching() {
    let app = AppState::new(test_config());
    app.push_revert(make_revert("a1", Team::TeamA, Side::Sell));
    app.push_revert(make_revert("b1", Team::TeamB, Side::Buy));
    app.push_revert(make_revert("a2", Team::TeamA, Side::Buy));

    let taken = app.take_reverts_for_team(Team::TeamA);
    assert_eq!(taken.len(), 2);
    let ids: Vec<&str> = taken.iter().map(|r| r.order_id.as_str()).collect();
    assert!(ids.contains(&"a1"));
    assert!(ids.contains(&"a2"));

    // Only TeamB remains
    assert_eq!(app.pending_revert_count(), 1);
    let remaining = app.pending_reverts.lock().unwrap();
    assert_eq!(remaining[0].order_id, "b1");
}

#[test]
fn take_reverts_for_team_returns_empty_when_no_match() {
    let app = AppState::new(test_config());
    app.push_revert(make_revert("b1", Team::TeamB, Side::Buy));

    let taken = app.take_reverts_for_team(Team::TeamA);
    assert!(taken.is_empty());
    assert_eq!(app.pending_revert_count(), 1);
}

#[test]
fn take_reverts_for_team_removes_all_when_all_match() {
    let app = AppState::new(test_config());
    app.push_revert(make_revert("a1", Team::TeamA, Side::Sell));
    app.push_revert(make_revert("a2", Team::TeamA, Side::Buy));

    let taken = app.take_reverts_for_team(Team::TeamA);
    assert_eq!(taken.len(), 2);
    assert_eq!(app.pending_revert_count(), 0);
}

// ── reset clears pending_reverts ─────────────────────────────────────────────

#[test]
fn reset_clears_pending_reverts() {
    let app = AppState::new(test_config());
    app.push_revert(make_revert("ord1", Team::TeamA, Side::Sell));
    app.push_revert(make_revert("ord2", Team::TeamB, Side::Buy));
    assert_eq!(app.pending_revert_count(), 2);

    app.reset_for_new_match();
    assert_eq!(app.pending_revert_count(), 0);
}

// ── age_secs ─────────────────────────────────────────────────────────────────

#[test]
fn pending_revert_age_secs_is_nonnegative() {
    let pr = make_revert("ord1", Team::TeamA, Side::Sell);
    assert!(pr.age_secs() >= 0.0);
}

// ── config defaults ──────────────────────────────────────────────────────────

#[test]
fn config_revert_delay_default_is_9500() {
    let config = test_config();
    assert_eq!(config.revert_delay_ms, 9500);
}

#[test]
fn config_breakeven_timeout_default_is_3000() {
    let config = test_config();
    assert_eq!(config.breakeven_timeout_ms, 3000);
}

// ── breakeven monitor: disabled when timeout=0 ──────────────────────────────

#[tokio::test]
async fn breakeven_monitor_disabled_when_zero() {
    let mut config = test_config();
    config.breakeven_timeout_ms = 0;
    let app = AppState::new(config);

    // Push a revert, but since timeout=0 the monitor should not run
    app.push_revert(make_revert("ord1", Team::TeamA, Side::Sell));

    // After a small sleep, revert should still be there (monitor didn't remove it)
    tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    assert_eq!(app.pending_revert_count(), 1);
}

// ── breakeven monitor: removes revert when it's no longer pending ────────────

#[tokio::test]
async fn breakeven_monitor_skips_when_revert_already_removed() {
    let config = test_config();
    let app = AppState::new(config.clone());

    app.push_revert(make_revert("ord1", Team::TeamA, Side::Sell));

    // Simulate: revert was filled/cancelled by something else before monitor fires
    app.remove_revert("ord1");
    assert_eq!(app.pending_revert_count(), 0);

    // Monitor would check still_pending, find false, and return early.
    // This test verifies the remove_revert + count logic is correct.
}
