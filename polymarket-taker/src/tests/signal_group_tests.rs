//! Unit tests for `SignalGroup` state — the per-signal 4-leg aggregator.

use crate::config::Config;
use crate::state::{AppState, GroupOutcome, LegRole, LegState, LegStatus, SignalGroup};
use crate::types::Team;
use rust_decimal_macros::dec;

fn min_config() -> Config {
    Config {
        polymarket_private_key: String::new(),
        polymarket_address: String::new(),
        signature_type: 1,
        neg_risk: false,
        chain_id: 137,
        polygon_rpc: String::new(),
        clob_http: String::new(),
        clob_ws: String::new(),
        team_a_name: "TeamA".to_string(),
        team_b_name: "TeamB".to_string(),
        team_a_token_id: String::new(),
        team_b_token_id: String::new(),
        condition_id: String::new(),
        first_batting: Team::TeamA,
        total_budget_usdc: dec!(100),
        max_trade_usdc: dec!(10),
        safe_percentage: 2,
        revert_delay_ms: 0,
        fill_poll_interval_ms: 500,
        fill_poll_timeout_ms: 5000,
        signal_gap_secs: 0,
        max_book_age_ms: 0,
        tick_size: "0.01".to_string(),
        order_min_size: rust_decimal::Decimal::ONE,
        fee_rate: 0.03,
        fee_exponent: 1.0,
        takers_only_fees: true,
        revert_timeout_ms: 15_000,
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
        breakeven_timeout_ms: 0,
        move_lookback_ms: 3000,
        move_threshold_multiplier: 2.0,
        maker_config: crate::config::MakerConfig::default(),
        builder_code: String::new(),
    }
}

fn fresh_group(correlation_id: &str, event_seq: u64, signal_tag: &str) -> SignalGroup {
    SignalGroup {
        correlation_id: correlation_id.to_string(),
        event_seq,
        signal_tag: signal_tag.to_string(),
        label: "WICKET".to_string(),
        ts_ist: "12:00:00".to_string(),
        batting: "IND".to_string(),
        bowling: "AUS".to_string(),
        legs: vec![
            LegStatus { role: LegRole::FakSell, state: LegState::Pending },
            LegStatus { role: LegRole::FakBuy, state: LegState::Pending },
            LegStatus { role: LegRole::RevertBuy, state: LegState::NotPlanned { reason: "awaiting fill".into() } },
            LegStatus { role: LegRole::RevertSell, state: LegState::NotPlanned { reason: "awaiting fill".into() } },
        ],
        total_fee_paid: rust_decimal::Decimal::ZERO,
        net_pnl: None,
        outcome: GroupOutcome::Open,
    }
}

// ── open / lookup ────────────────────────────────────────────────────────────

#[test]
fn open_signal_group_inserts_at_front() {
    let app = AppState::new(min_config());
    app.open_signal_group(fresh_group("1-W", 1, "W"));
    app.open_signal_group(fresh_group("2-R6", 2, "R6"));

    let snap = app.signal_groups_snapshot(10);
    assert_eq!(snap.len(), 2);
    // Newest first.
    assert_eq!(snap[0].correlation_id, "2-R6");
    assert_eq!(snap[1].correlation_id, "1-W");
}

#[test]
fn open_signal_group_caps_at_50_evicts_oldest() {
    let app = AppState::new(min_config());
    for i in 0..55 {
        app.open_signal_group(fresh_group(&format!("g-{i}"), i, "W"));
    }
    let snap = app.signal_groups_snapshot(100);
    assert_eq!(snap.len(), 50, "cap is 50, got {}", snap.len());
    assert_eq!(snap[0].correlation_id, "g-54");
    assert_eq!(snap[49].correlation_id, "g-5");
}

// ── update_leg ───────────────────────────────────────────────────────────────

#[test]
fn update_leg_mutates_state_in_place() {
    let app = AppState::new(min_config());
    app.open_signal_group(fresh_group("9-W", 9, "W"));

    app.update_leg("9-W", LegRole::FakSell, |leg| {
        leg.state = LegState::Posted {
            order_id: "ord-1".into(),
            price: dec!(0.62),
            size: dec!(10),
        };
    });

    let snap = app.signal_groups_snapshot(10);
    let leg = snap[0].legs.iter().find(|l| matches!(l.role, LegRole::FakSell)).unwrap();
    assert!(matches!(leg.state, LegState::Posted { .. }));
}

#[test]
fn update_leg_filled_accumulates_total_fee_paid() {
    let app = AppState::new(min_config());
    app.open_signal_group(fresh_group("9-W", 9, "W"));

    // Two legs filled, each with a fee.
    app.update_leg("9-W", LegRole::FakSell, |leg| {
        leg.state = LegState::Filled {
            order_id: "s".into(), size: dec!(10), avg_price: dec!(0.62), fee: dec!(0.04),
        };
    });
    app.update_leg("9-W", LegRole::FakBuy, |leg| {
        leg.state = LegState::Filled {
            order_id: "b".into(), size: dec!(10), avg_price: dec!(0.40), fee: dec!(0.03),
        };
    });

    let snap = app.signal_groups_snapshot(10);
    assert_eq!(snap[0].total_fee_paid, dec!(0.07));
}

#[test]
fn update_leg_unknown_correlation_id_is_noop() {
    let app = AppState::new(min_config());
    app.update_leg("does-not-exist", LegRole::FakSell, |leg| {
        leg.state = LegState::Killed { order_id: "x".into() };
    });
    assert_eq!(app.signal_groups_snapshot(10).len(), 0);
}

// ── set_group_outcome ────────────────────────────────────────────────────────

#[test]
fn set_outcome_marks_group_terminal() {
    let app = AppState::new(min_config());
    app.open_signal_group(fresh_group("9-W", 9, "W"));
    app.set_group_outcome("9-W", GroupOutcome::Wait);
    let snap = app.signal_groups_snapshot(10);
    assert_eq!(snap[0].outcome, GroupOutcome::Wait);
}

// ── set_group_net_pnl ────────────────────────────────────────────────────────

#[test]
fn set_group_net_pnl_writes_through() {
    let app = AppState::new(min_config());
    app.open_signal_group(fresh_group("9-W", 9, "W"));
    app.set_group_net_pnl("9-W", dec!(0.15));
    let snap = app.signal_groups_snapshot(10);
    assert_eq!(snap[0].net_pnl, Some(dec!(0.15)));
}

// ── mark_group_closed_if_terminal ────────────────────────────────────────────

#[test]
fn group_stays_open_while_any_leg_pending() {
    let app = AppState::new(min_config());
    app.open_signal_group(fresh_group("9-W", 9, "W"));
    // Move both FAKs to terminal but reverts still NotPlanned... actually
    // NotPlanned is a terminal state. So a Wicket where both reverts never
    // post would close immediately. Use Pending → Posted to keep it Open.
    app.update_leg("9-W", LegRole::FakSell, |l| {
        l.state = LegState::Posted { order_id: "s".into(), price: dec!(0.62), size: dec!(10) };
    });
    app.update_leg("9-W", LegRole::FakBuy, |l| {
        l.state = LegState::Posted { order_id: "b".into(), price: dec!(0.40), size: dec!(10) };
    });
    app.mark_group_closed_if_terminal("9-W");
    let snap = app.signal_groups_snapshot(10);
    assert_eq!(snap[0].outcome, GroupOutcome::Open);
}

#[test]
fn group_stays_open_after_fak_fills_when_reverts_not_yet_posted() {
    // PREMATURE-CLOSE REGRESSION GUARD. After both FAKs fill, the strategy
    // hasn't yet posted the GTC reverts — revert legs are still NotPlanned
    // with the "awaits FILL" reason. The group MUST stay Open until the
    // reverts have been posted and resolved. Closing here would mislead
    // the operator into thinking the round-trip is done.
    let app = AppState::new(min_config());
    app.open_signal_group(fresh_group("9-W", 9, "W"));
    app.update_leg("9-W", LegRole::FakSell, |l| {
        l.state = LegState::Filled { order_id: "s".into(), size: dec!(10), avg_price: dec!(0.62), fee: dec!(0.04) };
    });
    app.update_leg("9-W", LegRole::FakBuy, |l| {
        l.state = LegState::Filled { order_id: "b".into(), size: dec!(10), avg_price: dec!(0.40), fee: dec!(0.03) };
    });
    app.mark_group_closed_if_terminal("9-W");
    let snap = app.signal_groups_snapshot(10);
    assert_eq!(snap[0].outcome, GroupOutcome::Open,
        "group must stay Open while paired FAK is filled but revert NotPlanned (not yet posted)");
}

#[test]
fn group_closes_when_fak_killed_and_revert_unplanned() {
    // FAK didn't fill → no revert needed → revert leg NotPlanned is
    // legitimately terminal. Group must close.
    let app = AppState::new(min_config());
    app.open_signal_group(fresh_group("9-W", 9, "W"));
    app.update_leg("9-W", LegRole::FakSell, |l| {
        l.state = LegState::Killed { order_id: "s".into() };
    });
    app.update_leg("9-W", LegRole::FakBuy, |l| {
        l.state = LegState::Killed { order_id: "b".into() };
    });
    app.mark_group_closed_if_terminal("9-W");
    let snap = app.signal_groups_snapshot(10);
    assert_eq!(snap[0].outcome, GroupOutcome::Closed,
        "killed FAK pair with NotPlanned reverts is fully terminal");
}

#[test]
fn group_closes_when_fak_killed_one_side_revert_filled_other() {
    // Asymmetric: SELL FAK killed (no fill), BUY FAK filled and its revert
    // (RevertSell) eventually filled. The RevertBuy stays NotPlanned because
    // its paired FakSell never filled. Group must close.
    let app = AppState::new(min_config());
    app.open_signal_group(fresh_group("9-W", 9, "W"));
    app.update_leg("9-W", LegRole::FakSell, |l| {
        l.state = LegState::Killed { order_id: "s".into() };
    });
    app.update_leg("9-W", LegRole::FakBuy, |l| {
        l.state = LegState::Filled { order_id: "b".into(), size: dec!(10), avg_price: dec!(0.40), fee: dec!(0.03) };
    });
    app.update_leg("9-W", LegRole::RevertSell, |l| {
        l.state = LegState::Filled { order_id: "rs".into(), size: dec!(10), avg_price: dec!(0.42), fee: rust_decimal::Decimal::ZERO };
    });
    // RevertBuy left at NotPlanned: paired FakSell was Killed, so terminal.
    app.mark_group_closed_if_terminal("9-W");
    let snap = app.signal_groups_snapshot(10);
    assert_eq!(snap[0].outcome, GroupOutcome::Closed);
}

#[test]
fn skipped_outcome_not_overridden_by_terminal_check() {
    let app = AppState::new(min_config());
    app.open_skipped_signal_group(
        "9-W".into(), 9, "W", "WICKET", "IND", "AUS",
        GroupOutcome::Wait, "pending revert opposes",
    );
    // Even though all four legs are NotPlanned (terminal), the `Wait` outcome
    // must persist through the close check.
    app.mark_group_closed_if_terminal("9-W");
    let snap = app.signal_groups_snapshot(10);
    assert_eq!(snap[0].outcome, GroupOutcome::Wait);
}

// ── End-to-end lifecycle: signal → post → fill → revert → close ──────────────

#[test]
fn full_wicket_lifecycle_round_trip_closes_group() {
    let app = AppState::new(min_config());

    // Open: both FAK legs pending, both reverts NotPlanned.
    app.open_signal_group(fresh_group("42-W", 42, "W"));

    // FAKs posted.
    app.update_leg("42-W", LegRole::FakSell, |l| {
        l.state = LegState::Posted { order_id: "fak-s".into(), price: dec!(0.62), size: dec!(10) };
    });
    app.update_leg("42-W", LegRole::FakBuy, |l| {
        l.state = LegState::Posted { order_id: "fak-b".into(), price: dec!(0.40), size: dec!(10) };
    });

    // FAKs filled.
    app.update_leg("42-W", LegRole::FakSell, |l| {
        l.state = LegState::Filled { order_id: "fak-s".into(), size: dec!(10), avg_price: dec!(0.62), fee: dec!(0.0432) };
    });
    app.update_leg("42-W", LegRole::FakBuy, |l| {
        l.state = LegState::Filled { order_id: "fak-b".into(), size: dec!(10), avg_price: dec!(0.40), fee: dec!(0.0288) };
    });

    // Reverts posted (makers).
    app.update_leg("42-W", LegRole::RevertBuy, |l| {
        l.state = LegState::Posted { order_id: "rev-b".into(), price: dec!(0.60), size: dec!(10) };
    });
    app.update_leg("42-W", LegRole::RevertSell, |l| {
        l.state = LegState::Posted { order_id: "rev-s".into(), price: dec!(0.42), size: dec!(10) };
    });

    // Group is still Open while reverts are Posted.
    app.mark_group_closed_if_terminal("42-W");
    assert_eq!(app.signal_groups_snapshot(10)[0].outcome, GroupOutcome::Open);

    // Reverts fill (zero fee — makers under takers_only_fees).
    app.update_leg("42-W", LegRole::RevertBuy, |l| {
        l.state = LegState::Filled { order_id: "rev-b".into(), size: dec!(10), avg_price: dec!(0.60), fee: rust_decimal::Decimal::ZERO };
    });
    app.update_leg("42-W", LegRole::RevertSell, |l| {
        l.state = LegState::Filled { order_id: "rev-s".into(), size: dec!(10), avg_price: dec!(0.42), fee: rust_decimal::Decimal::ZERO };
    });
    app.set_group_net_pnl("42-W", dec!(0.128));
    app.mark_group_closed_if_terminal("42-W");

    let snap = app.signal_groups_snapshot(10);
    let g = &snap[0];
    assert_eq!(g.outcome, GroupOutcome::Closed);
    assert_eq!(g.total_fee_paid, dec!(0.0720)); // 0.0432 + 0.0288 + 0 + 0
    assert_eq!(g.net_pnl, Some(dec!(0.128)));
}
