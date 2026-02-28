/// Tests for order amount computation, base unit conversion, EIP-712 struct
/// hashing, and order status helpers.
use crate::orders::{compute_amounts, order_struct_hash, to_base_units, ClobOrder, OpenOrder};
use crate::types::Side;
use rust_decimal::Decimal;
use rust_decimal_macros::dec;

// ── to_base_units ─────────────────────────────────────────────────────────────

#[test]
fn to_base_units_whole_usdc() {
    assert_eq!(to_base_units(dec!(1)), 1_000_000);
    assert_eq!(to_base_units(dec!(10)), 10_000_000);
    assert_eq!(to_base_units(dec!(100)), 100_000_000);
}

#[test]
fn to_base_units_common_prices() {
    assert_eq!(to_base_units(dec!(0.50)), 500_000);
    assert_eq!(to_base_units(dec!(0.63)), 630_000);
    assert_eq!(to_base_units(dec!(0.01)), 10_000);
    assert_eq!(to_base_units(dec!(0.99)), 990_000);
}

#[test]
fn to_base_units_six_decimal_precision() {
    assert_eq!(to_base_units(dec!(0.123456)), 123_456);
    assert_eq!(to_base_units(dec!(99.999999)), 99_999_999);
}

#[test]
fn to_base_units_floors_sub_usdc_remainder() {
    // 0.1234567 * 1_000_000 = 123456.7 — should floor to 123456
    assert_eq!(to_base_units(dec!(0.1234567)), 123_456);
}

#[test]
fn to_base_units_zero_returns_zero() {
    assert_eq!(to_base_units(Decimal::ZERO), 0);
}

// ── compute_amounts ───────────────────────────────────────────────────────────

#[test]
fn buy_maker_is_usdc_taker_is_tokens() {
    // BUY 10 tokens @ 0.65: maker pays 6.5 USDC, taker receives 10 tokens
    let (maker, taker) = compute_amounts(Side::Buy, dec!(0.65), dec!(10));
    assert_eq!(maker, "6500000");   // 6.5 USDC in base units
    assert_eq!(taker, "10000000"); // 10 tokens in base units
}

#[test]
fn sell_maker_is_tokens_taker_is_usdc() {
    // SELL 10 tokens @ 0.70: maker gives 10 tokens, taker pays 7 USDC
    let (maker, taker) = compute_amounts(Side::Sell, dec!(0.70), dec!(10));
    assert_eq!(maker, "10000000"); // 10 tokens in base units
    assert_eq!(taker, "7000000");  // 7 USDC in base units
}

#[test]
fn buy_at_price_050_symmetry() {
    // At 0.50: 2 tokens costs 1 USDC
    let (maker, taker) = compute_amounts(Side::Buy, dec!(0.50), dec!(2));
    assert_eq!(maker, "1000000");  // 1 USDC
    assert_eq!(taker, "2000000"); // 2 tokens
}

#[test]
fn zero_size_produces_zero_amounts() {
    let (maker, taker) = compute_amounts(Side::Buy, dec!(0.50), Decimal::ZERO);
    assert_eq!(maker, "0");
    assert_eq!(taker, "0");
}

// ── OpenOrder status helpers ──────────────────────────────────────────────────

fn make_order(status: &str, size_matched: Option<&str>, price: Option<&str>) -> OpenOrder {
    OpenOrder {
        id: Some("order-abc".to_string()),
        status: Some(status.to_string()),
        original_size: Some("100".to_string()),
        size_matched: size_matched.map(String::from),
        price: price.map(String::from),
    }
}

#[test]
fn is_terminal_for_matched_status() {
    assert!(make_order("matched", Some("10"), Some("0.65")).is_terminal());
}

#[test]
fn is_terminal_for_cancelled_status() {
    assert!(make_order("cancelled", None, None).is_terminal());
}

#[test]
fn is_terminal_for_expired_status() {
    assert!(make_order("expired", None, None).is_terminal());
}

#[test]
fn is_not_terminal_for_live_status() {
    assert!(!make_order("live", None, None).is_terminal());
}

#[test]
fn is_not_terminal_for_delayed_status() {
    // Sports markets: order in 3-second delay window — keep polling
    assert!(!make_order("delayed", None, None).is_terminal());
}

#[test]
fn is_not_terminal_for_unmatched_status() {
    // "unmatched" = order went through delay, found no taker, now resting live.
    // Still active — should not stop polling yet.
    assert!(!make_order("unmatched", None, None).is_terminal());
}

#[test]
fn filled_size_parses_decimal_string() {
    let order = make_order("matched", Some("15.5"), Some("0.63"));
    assert_eq!(order.filled_size(), dec!(15.5));
}

#[test]
fn filled_size_returns_zero_when_missing() {
    assert_eq!(make_order("live", None, None).filled_size(), Decimal::ZERO);
}

#[test]
fn filled_size_returns_zero_for_malformed_value() {
    assert_eq!(make_order("live", Some("bad"), None).filled_size(), Decimal::ZERO);
}

#[test]
fn fill_price_parses_decimal_string() {
    let order = make_order("matched", Some("10"), Some("0.67"));
    assert_eq!(order.fill_price(), dec!(0.67));
}

#[test]
fn fill_price_returns_zero_when_missing() {
    assert_eq!(make_order("matched", Some("10"), None).fill_price(), Decimal::ZERO);
}

// ── EIP-712 struct hash ───────────────────────────────────────────────────────

fn sample_order() -> ClobOrder {
    ClobOrder {
        salt: "12345".to_string(),
        maker: "0x1234567890123456789012345678901234567890".to_string(),
        signer: "0x1234567890123456789012345678901234567890".to_string(),
        taker: "0x0000000000000000000000000000000000000000".to_string(),
        token_id: "999".to_string(),
        maker_amount: "1000000".to_string(),
        taker_amount: "1538461".to_string(),
        side: 0,
        expiration: "0".to_string(),
        nonce: "0".to_string(),
        fee_rate_bps: "0".to_string(),
        signature_type: 1,
        signature: String::new(),
    }
}

#[test]
fn struct_hash_is_deterministic() {
    let order = sample_order();
    assert_eq!(order_struct_hash(&order), order_struct_hash(&order));
}

#[test]
fn struct_hash_is_non_zero() {
    assert_ne!(order_struct_hash(&sample_order()), [0u8; 32]);
}

#[test]
fn struct_hash_differs_by_side() {
    let mut order = sample_order();
    order.side = 0; // BUY
    let buy_hash = order_struct_hash(&order);
    order.side = 1; // SELL
    let sell_hash = order_struct_hash(&order);
    assert_ne!(buy_hash, sell_hash);
}

#[test]
fn struct_hash_differs_by_token_id() {
    let mut order = sample_order();
    let h1 = order_struct_hash(&order);
    order.token_id = "111".to_string();
    let h2 = order_struct_hash(&order);
    assert_ne!(h1, h2);
}

#[test]
fn struct_hash_differs_by_maker_amount() {
    let mut order = sample_order();
    let h1 = order_struct_hash(&order);
    order.maker_amount = "2000000".to_string();
    let h2 = order_struct_hash(&order);
    assert_ne!(h1, h2);
}

#[test]
fn struct_hash_differs_by_salt() {
    let mut order = sample_order();
    let h1 = order_struct_hash(&order);
    order.salt = "99999".to_string();
    let h2 = order_struct_hash(&order);
    assert_ne!(h1, h2, "same order with different salt must produce different hash");
}
