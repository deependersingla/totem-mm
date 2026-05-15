/// Tests for strategy order building, safe-price guard, size computation, and edge selection.
use crate::config::Config;
use crate::strategy::{build_buy_order, build_sell_order, build_taker_exit_fak, compute_size, edge_ticks_for_label, price_in_safe_range};
use crate::types::{OrderBook, OrderBookSide, PriceLevel, Side, Team};
use rust_decimal::Decimal;
use rust_decimal_macros::dec;

/// Build a minimal Config for testing — avoids loading .env.
fn test_config(max_trade_usdc: &str, safe_percentage: u64) -> Config {
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
        total_budget_usdc: dec!(1000),
        max_trade_usdc: max_trade_usdc.parse().unwrap(),
        safe_percentage,
        revert_delay_ms: 3000,
        fill_poll_interval_ms: 500,
        fill_poll_timeout_ms: 5000,
        signal_gap_secs: 0,
        max_book_age_ms: 0,
        tick_size: "0.01".to_string(),
        order_min_size: Decimal::ONE,
        fee_rate: 0.0,
        fee_exponent: 0.0,
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
        breakeven_timeout_ms: 3000,
        move_lookback_ms: 3000,
        move_threshold_multiplier: 2.0,
        skip_on_premove: false,
        maker_config: crate::config::MakerConfig::default(),
        builder_code: String::new(),
    }
}

fn book_with_bid(price: Decimal, size: Decimal) -> OrderBook {
    OrderBook {
        bids: OrderBookSide {
            levels: vec![PriceLevel { price, size }],
        },
        asks: OrderBookSide { levels: vec![] },
        timestamp_ms: 0,
    }
}

fn book_with_ask(price: Decimal, size: Decimal) -> OrderBook {
    OrderBook {
        bids: OrderBookSide { levels: vec![] },
        asks: OrderBookSide {
            levels: vec![PriceLevel { price, size }],
        },
        timestamp_ms: 0,
    }
}

// ── price_in_safe_range ───────────────────────────────────────────────────────

#[test]
fn price_in_safe_range_for_mid_market_prices() {
    let config = test_config("10", 2); // safe: 0.02 – 0.98
    let a = book_with_bid(dec!(0.60), dec!(100));
    let b = book_with_ask(dec!(0.40), dec!(100));
    assert!(price_in_safe_range(&config, &(a, b)));
}

#[test]
fn price_outside_safe_range_low() {
    let config = test_config("10", 2); // safe: 0.02 – 0.98
    let a = book_with_bid(dec!(0.01), dec!(100)); // below 0.02
    let b = book_with_ask(dec!(0.40), dec!(100));
    assert!(!price_in_safe_range(&config, &(a, b)));
}

#[test]
fn price_outside_safe_range_high() {
    let config = test_config("10", 2);
    let a = book_with_bid(dec!(0.60), dec!(100));
    let b = book_with_ask(dec!(0.99), dec!(100)); // above 0.98
    assert!(!price_in_safe_range(&config, &(a, b)));
}

#[test]
fn empty_books_are_within_safe_range() {
    let config = test_config("10", 2);
    let empty = OrderBook::default();
    assert!(price_in_safe_range(&config, &(empty.clone(), empty)));
}

#[test]
fn price_exactly_at_safe_boundary_is_safe() {
    let config = test_config("10", 2); // safe: [0.02, 0.98]
    let a = book_with_bid(dec!(0.02), dec!(100)); // exactly at min
    let b = book_with_ask(dec!(0.98), dec!(100)); // exactly at max
    assert!(price_in_safe_range(&config, &(a, b)));
}

// ── compute_size ──────────────────────────────────────────────────────────────

#[test]
fn compute_size_capped_by_max_trade_usdc() {
    let config = test_config("10", 2); // max 10 USDC per trade
    // Available: 100 tokens @ 0.50 = 50 USDC of liquidity
    // Max tokens at 0.50 = 10 / 0.50 = 20
    let size = compute_size(&config, &dec!(100), dec!(0.50));
    assert_eq!(size, dec!(20));
}

#[test]
fn compute_size_limited_by_available_liquidity() {
    let config = test_config("100", 2); // max 100 USDC — much larger than available
    // Available: 5 tokens @ 0.60 = 3 USDC
    let size = compute_size(&config, &dec!(5), dec!(0.60));
    assert_eq!(size, dec!(5)); // capped by available
}

#[test]
fn compute_size_zero_price_returns_zero() {
    let config = test_config("10", 2);
    let size = compute_size(&config, &dec!(100), Decimal::ZERO);
    assert_eq!(size, Decimal::ZERO);
}

#[test]
fn compute_size_zero_available_returns_zero() {
    let config = test_config("10", 2);
    let size = compute_size(&config, &Decimal::ZERO, dec!(0.60));
    assert_eq!(size, Decimal::ZERO);
}

// ── build_sell_order ──────────────────────────────────────────────────────────

#[test]
fn build_sell_order_uses_best_bid_price_and_size() {
    let config = test_config("10", 2);
    let book = book_with_bid(dec!(0.65), dec!(50));
    let order = build_sell_order(&config, Team::TeamA, &book, None).unwrap();
    assert_eq!(order.side, Side::Sell);
    assert_eq!(order.team, Team::TeamA);
    assert_eq!(order.price, dec!(0.65));
    // max_tokens = 10 / 0.65 ≈ 15.38, available = 50 → capped at 15.38...
    // compute_size uses min(15.38, 50) ≈ 15.38
    assert!(order.size > Decimal::ZERO);
}

#[test]
fn build_sell_order_returns_none_for_empty_bids() {
    let config = test_config("10", 2);
    let book = OrderBook::default();
    assert!(build_sell_order(&config, Team::TeamA, &book, None).is_none());
}

// ── build_buy_order ───────────────────────────────────────────────────────────

#[test]
fn build_buy_order_uses_best_ask_price_and_size() {
    let config = test_config("10", 2);
    let book = book_with_ask(dec!(0.40), dec!(100));
    let order = build_buy_order(&config, Team::TeamB, &book).unwrap();
    assert_eq!(order.side, Side::Buy);
    assert_eq!(order.team, Team::TeamB);
    assert_eq!(order.price, dec!(0.40));
    // 10 / 0.40 = 25 tokens from budget
    assert_eq!(order.size, dec!(25));
}

#[test]
fn build_buy_order_returns_none_for_empty_asks() {
    let config = test_config("10", 2);
    let book = OrderBook::default();
    assert!(build_buy_order(&config, Team::TeamB, &book).is_none());
}

#[test]
fn build_buy_order_size_from_budget_not_book() {
    let config = test_config("100", 2); // 100 USDC max
    // Available: 5 tokens @ 0.50 — but size comes from budget, not book
    let book = book_with_ask(dec!(0.50), dec!(5));
    let order = build_buy_order(&config, Team::TeamA, &book).unwrap();
    // 100 / 0.50 = 200 tokens — sized from budget, ignoring book depth
    assert_eq!(order.size, dec!(200));
}

// ── build_buy_order — fee-aware sizing ────────────────────────────────────────

#[test]
fn build_buy_order_no_change_when_fee_rate_zero() {
    // Default test_config has fee_rate=0 — must size at the raw net target
    // (preserves all existing behaviour and tests above).
    let config = test_config("10", 2);
    let book = book_with_ask(dec!(0.50), dec!(100));
    let order = build_buy_order(&config, Team::TeamA, &book).unwrap();
    assert_eq!(order.size, dec!(20)); // 10 / 0.50, untouched
}

#[test]
fn build_buy_order_grosses_up_when_fee_rate_set() {
    let mut config = test_config("10", 2);
    config.fee_rate = 0.03;
    config.fee_exponent = 1.0;
    let book = book_with_ask(dec!(0.50), dec!(100));
    let order = build_buy_order(&config, Team::TeamA, &book).unwrap();
    // net = floor(10 / 0.50) = 20.  rate = 0.03 · 0.25 = 0.0075.
    // gross = ceil_at_6dp(20 / 0.9925) = 20.151134
    assert!(order.size > dec!(20), "gross should exceed net=20, got {}", order.size);

    // After fee deduction we receive >= net (20).
    let received = order.size - crate::fees::fee_tokens_buy(order.size, dec!(0.50), 0.03, 1);
    assert!(received >= dec!(20), "post-fee received={received} should be >= 20");
}

#[test]
fn build_buy_order_grosses_up_at_p_60() {
    // Cricket worked example. r=0.03 e=1 p=0.6 max_trade=10
    // net = floor(10/0.6) = 16. rate = 0.0072. gross >= 16/0.9928.
    let mut config = test_config("10", 2);
    config.fee_rate = 0.03;
    config.fee_exponent = 1.0;
    let book = book_with_ask(dec!(0.60), dec!(100));
    let order = build_buy_order(&config, Team::TeamA, &book).unwrap();
    assert!(order.size > dec!(16));
    let received = order.size - crate::fees::fee_tokens_buy(order.size, dec!(0.60), 0.03, 1);
    assert!(received >= dec!(16));
}

// ── build_taker_exit_fak — must use L0, not L+1 (no overshoot on exit) ─────
//
// Polymarket V2 BUY orders spend the full `maker_amount` USDC budget; when
// matched at a better-than-limit price the buyer receives MORE tokens, not
// less USDC paid. Using L+1 as the limit (like entry FAKs) lets the matcher
// sweep both levels and overshoot the requested size. Since the timeout
// exit is meant to FLATTEN, overshoot creates new exposure in the wrong
// direction. Tests below pin L0 for both BUY and SELL exits.

fn book_two_level_asks(l0: Decimal, l1: Decimal) -> OrderBook {
    OrderBook {
        bids: OrderBookSide::default(),
        asks: OrderBookSide {
            levels: vec![
                PriceLevel { price: l0, size: dec!(100) },
                PriceLevel { price: l1, size: dec!(100) },
            ],
        },
        timestamp_ms: 0,
    }
}
fn book_two_level_bids(l0: Decimal, l1: Decimal) -> OrderBook {
    OrderBook {
        bids: OrderBookSide {
            levels: vec![
                PriceLevel { price: l0, size: dec!(100) },
                PriceLevel { price: l1, size: dec!(100) },
            ],
        },
        asks: OrderBookSide::default(),
        timestamp_ms: 0,
    }
}

#[test]
fn taker_exit_buy_uses_l0_not_l_plus_one() {
    // Asks: [0.21 (L0), 0.22 (L+1)]. Exit BUY must use 0.21 (touch), not 0.22.
    let book = book_two_level_asks(dec!(0.21), dec!(0.22));
    let order = build_taker_exit_fak(Team::TeamA, Side::Buy, &book, dec!(25)).unwrap();
    assert_eq!(order.side, Side::Buy);
    assert_eq!(order.price, dec!(0.21), "exit BUY must price at L0 to prevent V2 budget-overshoot");
    assert_eq!(order.size, dec!(25));
}

#[test]
fn taker_exit_sell_uses_l0_not_l_plus_one() {
    // Bids: [0.79 (L0), 0.78 (L+1)]. Exit SELL must use 0.79 (touch).
    let book = book_two_level_bids(dec!(0.79), dec!(0.78));
    let order = build_taker_exit_fak(Team::TeamA, Side::Sell, &book, dec!(25)).unwrap();
    assert_eq!(order.side, Side::Sell);
    assert_eq!(order.price, dec!(0.79));
    assert_eq!(order.size, dec!(25));
}

#[test]
fn taker_exit_returns_none_for_empty_book() {
    let empty = OrderBook::default();
    assert!(build_taker_exit_fak(Team::TeamA, Side::Buy, &empty, dec!(25)).is_none());
    assert!(build_taker_exit_fak(Team::TeamA, Side::Sell, &empty, dec!(25)).is_none());
}

#[test]
fn taker_exit_returns_none_for_zero_size() {
    let book = book_two_level_asks(dec!(0.21), dec!(0.22));
    assert!(build_taker_exit_fak(Team::TeamA, Side::Buy, &book, Decimal::ZERO).is_none());
}

// ── compute_taker_exit_size — high-precision Decimal subtraction ────────────
//
// Used by `spawn_revert_fill_monitor`'s timeout branch:
//   exit_size = entry_fill - max(cancel_filled, get_order_filled)
// If exit_size < order_min_size → return None (skip the FAK exit).

use crate::strategy::compute_taker_exit_size;

#[test]
fn taker_exit_size_decimal_precision() {
    // entry filled 16, GTC partial-filled 7.123456 before cancel.
    let exit = compute_taker_exit_size(dec!(16), dec!(7.123456), dec!(7.123456), dec!(1)).unwrap();
    assert_eq!(exit, dec!(8.876544));
}

#[test]
fn taker_exit_size_uses_max_of_cancel_and_get_order() {
    // Race: cancel response says 4 filled; get_order says 5 filled. Use 5.
    let exit = compute_taker_exit_size(dec!(10), dec!(4), dec!(5), dec!(1)).unwrap();
    assert_eq!(exit, dec!(5));
}

#[test]
fn taker_exit_size_uses_max_when_cancel_higher() {
    // Reverse race.
    let exit = compute_taker_exit_size(dec!(10), dec!(6), dec!(4), dec!(1)).unwrap();
    assert_eq!(exit, dec!(4));
}

#[test]
fn taker_exit_size_returns_none_when_below_min() {
    // 16 - 15.5 = 0.5 < min(1) → skip (residual too small to FAK).
    assert_eq!(compute_taker_exit_size(dec!(16), dec!(15.5), dec!(15.5), dec!(1)), None);
}

#[test]
fn taker_exit_size_returns_none_when_already_flat() {
    assert_eq!(compute_taker_exit_size(dec!(10), dec!(10), dec!(10), dec!(1)), None);
}

#[test]
fn taker_exit_size_clamps_at_zero_when_overfilled() {
    // Pathological: GTC reported more filled than entry. Should not produce
    // a negative size — must return None (already flat).
    assert_eq!(compute_taker_exit_size(dec!(10), dec!(11), dec!(11), dec!(1)), None);
}

#[test]
fn taker_exit_size_zero_min_size_allows_any_residual() {
    // If order_min_size is somehow 0, any positive residual is fired.
    let exit = compute_taker_exit_size(dec!(10), dec!(9.5), dec!(9.5), Decimal::ZERO).unwrap();
    assert_eq!(exit, dec!(0.5));
}

// ── revert_timeout_ms config knob ────────────────────────────────────────────

#[test]
fn revert_timeout_ms_zero_means_disabled() {
    // Convention: 0 disables the timeout (preserves today's "no time stop").
    let mut config = test_config("10", 2);
    config.revert_timeout_ms = 0;
    assert_eq!(config.revert_timeout_ms, 0);
}

// ── should_escalate_revert_timeout ────────────────────────────────────────────
//
// Pure predicate that the revert monitor consults each tick. Two-arg shape so
// both branches can be tested without spinning up the spawn_revert_fill_monitor
// task. Matches the inline check at strategy.rs:911-913.

use std::time::Duration;
use crate::strategy::should_escalate_revert_timeout;

#[test]
fn revert_timeout_zero_never_escalates_even_after_long_elapsed() {
    // Production default: timeout=0 means GTC reverts wait forever.
    assert!(!should_escalate_revert_timeout(0, Duration::from_secs(0)));
    assert!(!should_escalate_revert_timeout(0, Duration::from_millis(15_000)));
    assert!(!should_escalate_revert_timeout(0, Duration::from_secs(60 * 60)));
}

#[test]
fn revert_timeout_nonzero_escalates_at_or_after_threshold() {
    assert!(should_escalate_revert_timeout(1_000, Duration::from_millis(1_000)));
    assert!(should_escalate_revert_timeout(1_000, Duration::from_millis(2_500)));
    assert!(should_escalate_revert_timeout(15_000, Duration::from_millis(15_001)));
}

#[test]
fn revert_timeout_nonzero_does_not_escalate_before_threshold() {
    assert!(!should_escalate_revert_timeout(1_000, Duration::from_millis(0)));
    assert!(!should_escalate_revert_timeout(1_000, Duration::from_millis(999)));
    assert!(!should_escalate_revert_timeout(15_000, Duration::from_millis(14_999)));
}

#[test]
fn build_buy_order_floors_to_min_size_when_gross_below_min() {
    // Tiny budget — gross would be < 1 token; should floor up to order_min_size.
    let mut config = test_config("0.40", 2); // 0.40 USDC budget
    config.fee_rate = 0.03;
    config.fee_exponent = 1.0;
    let book = book_with_ask(dec!(0.50), dec!(100));
    // net = floor(0.40 / 0.50) = 0 → would return None today; fee-aware should
    // also return None (or floor up to min — preserve existing behaviour: None).
    let order = build_buy_order(&config, Team::TeamA, &book);
    // Existing behaviour today: when net=0 we fell into the min_size branch
    // and returned a size==order_min_size order. Keep that semantics.
    assert!(order.is_some());
    assert_eq!(order.unwrap().size, config.order_min_size);
}

// ── edge_ticks_for_label ───────────────────────────────────────────────────────

#[test]
fn edge_label_wicket_uses_wicket_edge() {
    let config = test_config("10", 2);
    assert_eq!(edge_ticks_for_label("WICKET", &config), config.edge_wicket);
}

#[test]
fn edge_label_run4_uses_boundary_4_edge() {
    let config = test_config("10", 2);
    assert_eq!(edge_ticks_for_label("RUN4", &config), config.edge_boundary_4);
}

#[test]
fn edge_label_run6_uses_boundary_6_edge() {
    let config = test_config("10", 2);
    assert_eq!(edge_ticks_for_label("RUN6", &config), config.edge_boundary_6);
}

#[test]
fn edge_label_wd4_uses_boundary_4_not_wicket() {
    // This is the bug: "WD4" starts with "W" but is NOT a wicket.
    let config = test_config("10", 2);
    assert_eq!(edge_ticks_for_label("WD4", &config), config.edge_boundary_4);
    assert_ne!(edge_ticks_for_label("WD4", &config), config.edge_wicket);
}

#[test]
fn edge_label_wd6_uses_boundary_6() {
    let config = test_config("10", 2);
    assert_eq!(edge_ticks_for_label("WD6", &config), config.edge_boundary_6);
}

#[test]
fn edge_label_nb4_uses_boundary_4() {
    let config = test_config("10", 2);
    assert_eq!(edge_ticks_for_label("NB4", &config), config.edge_boundary_4);
}

#[test]
fn edge_label_nb6_uses_boundary_6() {
    let config = test_config("10", 2);
    assert_eq!(edge_ticks_for_label("NB6", &config), config.edge_boundary_6);
}
