/// Tests for strategy order building, safe-price guard, and size computation.
use crate::config::Config;
use crate::strategy::{build_buy_order, build_sell_order, compute_size, price_in_safe_range};
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
        tick_size: "0.01".to_string(),
        ws_ping_interval_secs: 10,
        dry_run: true,
        log_level: "info".to_string(),
        http_port: 3000,
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
    let order = build_sell_order(&config, Team::TeamA, &book).unwrap();
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
    assert!(build_sell_order(&config, Team::TeamA, &book).is_none());
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
    // max_tokens = 10 / 0.40 = 25, available = 100 → size = 25
    assert_eq!(order.size, dec!(25));
}

#[test]
fn build_buy_order_returns_none_for_empty_asks() {
    let config = test_config("10", 2);
    let book = OrderBook::default();
    assert!(build_buy_order(&config, Team::TeamB, &book).is_none());
}

#[test]
fn build_buy_order_size_limited_by_available() {
    let config = test_config("100", 2); // 100 USDC max
    // Available: 5 tokens @ 0.50 — only 5 tokens available
    let book = book_with_ask(dec!(0.50), dec!(5));
    let order = build_buy_order(&config, Team::TeamA, &book).unwrap();
    assert_eq!(order.size, dec!(5));
}
