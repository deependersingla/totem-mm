/// Tests for CricketSignal parsing and MatchState transitions.
use crate::types::{CricketSignal, MatchState, OrderBook, PriceLevel, Team};
use rust_decimal_macros::dec;

// ── CricketSignal::parse ──────────────────────────────────────────────────────

#[test]
fn parse_runs_zero_to_six() {
    for r in 0u8..=6 {
        let s = r.to_string();
        assert_eq!(CricketSignal::parse(&s), Some(CricketSignal::Runs(r)), "failed for {r}");
    }
}

#[test]
fn parse_runs_out_of_range_returns_none() {
    assert_eq!(CricketSignal::parse("7"), None);
    assert_eq!(CricketSignal::parse("99"), None);
}

#[test]
fn parse_wicket_plain() {
    assert_eq!(CricketSignal::parse("W"), Some(CricketSignal::Wicket(0)));
}

#[test]
fn parse_wicket_with_runs_zero_to_six() {
    for r in 1u8..=6 {
        let s = format!("W{r}");
        assert_eq!(CricketSignal::parse(&s), Some(CricketSignal::Wicket(r)), "failed for {s}");
    }
}

#[test]
fn parse_wicket_out_of_range_returns_none() {
    assert_eq!(CricketSignal::parse("W7"), None);
}

#[test]
fn parse_wide_plain() {
    assert_eq!(CricketSignal::parse("Wd"), Some(CricketSignal::Wide(0)));
}

#[test]
fn parse_wide_with_runs_zero_to_six() {
    for r in 0u8..=6 {
        let s = format!("Wd{r}");
        assert_eq!(CricketSignal::parse(&s), Some(CricketSignal::Wide(r)), "failed for {s}");
    }
}

#[test]
fn parse_no_ball_plain() {
    assert_eq!(CricketSignal::parse("N"), Some(CricketSignal::NoBall(0)));
}

#[test]
fn parse_no_ball_with_runs_zero_to_six() {
    for r in 0u8..=6 {
        let s = format!("N{r}");
        assert_eq!(CricketSignal::parse(&s), Some(CricketSignal::NoBall(r)), "failed for {s}");
    }
}

#[test]
fn parse_innings_over() {
    assert_eq!(CricketSignal::parse("IO"), Some(CricketSignal::InningsOver));
}

#[test]
fn parse_match_over() {
    assert_eq!(CricketSignal::parse("MO"), Some(CricketSignal::MatchOver));
}

#[test]
fn parse_garbage_returns_none() {
    assert_eq!(CricketSignal::parse(""), None);
    assert_eq!(CricketSignal::parse("X"), None);
    assert_eq!(CricketSignal::parse("WICKET"), None);
    assert_eq!(CricketSignal::parse("100"), None);
    assert_eq!(CricketSignal::parse("W99"), None);
}

#[test]
fn parse_trims_whitespace() {
    assert_eq!(CricketSignal::parse("  W  "), Some(CricketSignal::Wicket(0)));
    assert_eq!(CricketSignal::parse(" IO "), Some(CricketSignal::InningsOver));
    assert_eq!(CricketSignal::parse(" 4 "), Some(CricketSignal::Runs(4)));
}

// ── Display roundtrip ─────────────────────────────────────────────────────────

#[test]
fn display_roundtrip_for_all_signals() {
    let signals = vec![
        CricketSignal::Runs(0),
        CricketSignal::Runs(6),
        CricketSignal::Wicket(0),
        CricketSignal::Wicket(4),
        CricketSignal::Wide(0),
        CricketSignal::Wide(6),
        CricketSignal::NoBall(0),
        CricketSignal::NoBall(3),
        CricketSignal::InningsOver,
        CricketSignal::MatchOver,
    ];
    for sig in signals {
        let s = format!("{sig}");
        let parsed = CricketSignal::parse(&s);
        assert_eq!(parsed, Some(sig.clone()), "display→parse roundtrip failed for {s}");
    }
}

#[test]
fn wicket_is_detected_by_is_wicket() {
    assert!(CricketSignal::Wicket(0).is_wicket());
    assert!(CricketSignal::Wicket(4).is_wicket());
    assert!(!CricketSignal::Runs(4).is_wicket());
    assert!(!CricketSignal::InningsOver.is_wicket());
}

// ── MatchState ────────────────────────────────────────────────────────────────

#[test]
fn match_state_initial_batting() {
    let state = MatchState::new(Team::TeamA);
    assert_eq!(state.batting, Team::TeamA);
    assert_eq!(state.bowling(), Team::TeamB);
    assert_eq!(state.innings, 1);
}

#[test]
fn match_state_switch_swaps_teams() {
    let mut state = MatchState::new(Team::TeamA);
    state.switch_innings();
    assert_eq!(state.batting, Team::TeamB);
    assert_eq!(state.bowling(), Team::TeamA);
    assert_eq!(state.innings, 2);
}

#[test]
fn match_state_double_switch_returns_to_original_team() {
    let mut state = MatchState::new(Team::TeamA);
    state.switch_innings();
    state.switch_innings();
    assert_eq!(state.batting, Team::TeamA);
    assert_eq!(state.innings, 3);
}

#[test]
fn match_state_starts_with_team_b_batting() {
    let state = MatchState::new(Team::TeamB);
    assert_eq!(state.batting, Team::TeamB);
    assert_eq!(state.bowling(), Team::TeamA);
}

// ── OrderBook helpers ─────────────────────────────────────────────────────────

#[test]
fn empty_orderbook_best_bid_ask_are_none() {
    let book = OrderBook::default();
    assert!(book.best_bid().is_none());
    assert!(book.best_ask().is_none());
}

#[test]
fn orderbook_best_bid_returns_first_level() {
    let mut book = OrderBook::default();
    book.bids.levels.push(PriceLevel { price: dec!(0.60), size: dec!(100) });
    book.bids.levels.push(PriceLevel { price: dec!(0.55), size: dec!(200) });
    assert_eq!(book.best_bid().unwrap().price, dec!(0.60));
}

#[test]
fn orderbook_best_ask_returns_first_level() {
    let mut book = OrderBook::default();
    book.asks.levels.push(PriceLevel { price: dec!(0.62), size: dec!(50) });
    book.asks.levels.push(PriceLevel { price: dec!(0.70), size: dec!(100) });
    assert_eq!(book.best_ask().unwrap().price, dec!(0.62));
}
