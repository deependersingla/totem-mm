//! Tests for the `DispatchedSignal` envelope and `AppState::make_dispatch`
//! introduced in B4 (TODO.md). Locks in:
//!   * monotonic event_seq allocation across calls
//!   * correlation_id format `{event_seq}-{short_tag}` for every signal
//!   * non-trade signals (dot ball, IO, MO) also receive an envelope so the
//!     ledger row stays joinable to whatever the strategy decides

use rust_decimal_macros::dec;

use crate::config::{Config, MakerConfig};
use crate::state::AppState;
use crate::types::{CricketSignal, Team};

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
        signal_gap_secs: 0, // most dispatch tests don't exercise the gate
        max_book_age_ms: 0,
        tick_size: "0.01".to_string(),
        order_min_size: rust_decimal::Decimal::ONE,
        fee_rate: 0.0,
        fee_exponent: 0.0,
        takers_only_fees: true,
        revert_timeout_ms: 0,
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
        move_lookback_ms: 3000,
        move_threshold_multiplier: 2.0,
        maker_config: MakerConfig::default(),
        builder_code: String::new(),
    }
}

#[test]
fn make_dispatch_assigns_monotonic_event_seq() {
    let app = AppState::new(test_config());
    let a = app.make_dispatch(CricketSignal::Wicket(0));
    let b = app.make_dispatch(CricketSignal::Runs(6));
    let c = app.make_dispatch(CricketSignal::Runs(0));
    assert_eq!(a.event_seq, 1);
    assert_eq!(b.event_seq, 2);
    assert_eq!(c.event_seq, 3);
}

#[test]
fn make_dispatch_correlation_id_format() {
    let app = AppState::new(test_config());
    let d = app.make_dispatch(CricketSignal::Wicket(0));
    assert_eq!(d.correlation_id, format!("{}-W", d.event_seq));

    let r = app.make_dispatch(CricketSignal::Runs(6));
    assert_eq!(r.correlation_id, format!("{}-R6", r.event_seq));

    let wd = app.make_dispatch(CricketSignal::Wide(4));
    assert_eq!(wd.correlation_id, format!("{}-Wd4", wd.event_seq));
}

#[test]
fn make_dispatch_envelopes_non_trade_signals_too() {
    // Dot balls, IO and MO must still receive envelopes — the handler
    // captures every receive into the ledger and the row needs an event_seq.
    let app = AppState::new(test_config());

    let dot = app.make_dispatch(CricketSignal::Runs(0));
    assert_eq!(dot.event_seq, 1);
    assert!(dot.correlation_id.ends_with("-R0"));

    let io = app.make_dispatch(CricketSignal::InningsOver);
    assert_eq!(io.event_seq, 2);
    assert!(io.correlation_id.ends_with("-IO"));

    let mo = app.make_dispatch(CricketSignal::MatchOver);
    assert_eq!(mo.event_seq, 3);
    assert!(mo.correlation_id.ends_with("-MO"));
}

#[test]
fn make_dispatch_preserves_signal_payload() {
    let app = AppState::new(test_config());
    let d = app.make_dispatch(CricketSignal::Wicket(2));
    assert_eq!(d.signal, CricketSignal::Wicket(2));
}

#[test]
fn make_dispatch_ts_ist_is_populated() {
    let app = AppState::new(test_config());
    let d = app.make_dispatch(CricketSignal::Runs(4));
    // ist_now() returns "HH:MM:SS" — 8 chars with two colons.
    assert_eq!(d.ts_ist.len(), 8, "ts_ist = {:?}", d.ts_ist);
    assert_eq!(d.ts_ist.matches(':').count(), 2);
}

// ── Dispatch-gap helper (A1) ────────────────────────────────────────────────

fn config_with_gap(secs: u64) -> Config {
    let mut c = test_config();
    c.signal_gap_secs = secs;
    c
}

#[test]
fn gap_disabled_when_zero_lets_every_signal_through() {
    let app = AppState::new(config_with_gap(0));
    for _ in 0..10 {
        assert!(app.check_and_update_dispatch_gap().is_none());
    }
}

#[test]
fn gap_blocks_rapid_consecutive_signals() {
    let app = AppState::new(config_with_gap(7));
    // First call passes the gate and stamps `last_dispatch_at`.
    assert!(app.check_and_update_dispatch_gap().is_none(), "first call must pass");
    // Subsequent calls within the 7s window must be rejected.
    for i in 0..4 {
        assert_eq!(
            app.check_and_update_dispatch_gap(),
            Some(7),
            "call #{i} should be GAP_REJECTED",
        );
    }
}

#[test]
fn gap_resets_on_new_match() {
    let app = AppState::new(config_with_gap(7));
    assert!(app.check_and_update_dispatch_gap().is_none());
    assert_eq!(app.check_and_update_dispatch_gap(), Some(7));
    // A new match should not inherit the previous match's dispatch timestamp.
    app.reset_for_new_match();
    assert!(
        app.check_and_update_dispatch_gap().is_none(),
        "first call after reset must pass",
    );
}

#[test]
fn gap_zero_via_runtime_reconfigure() {
    // Operator may toggle the gate off mid-match by setting signal_gap_secs=0
    // through /api/limits — verify the helper reads the live config each call.
    let app = AppState::new(config_with_gap(7));
    assert!(app.check_and_update_dispatch_gap().is_none());
    assert_eq!(app.check_and_update_dispatch_gap(), Some(7));

    app.config.write().unwrap().signal_gap_secs = 0;
    for _ in 0..3 {
        assert!(app.check_and_update_dispatch_gap().is_none());
    }
}
