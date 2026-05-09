/// Tests for order amount computation, base unit conversion, and order status
/// helpers. EIP-712 struct hashing is covered by the cross-implementation test
/// suite in tests/v2_signing.rs.
use crate::orders::{compute_amounts, to_base_units, OpenOrder};
use crate::types::Side;
use rust_decimal::Decimal;
use rust_decimal_macros::dec;

// ── to_base_units ─────────────────────────────────────────────────────────────

#[test]
fn to_base_units_whole_usdc() {
    assert_eq!(to_base_units(dec!(1)).unwrap(), 1_000_000);
    assert_eq!(to_base_units(dec!(10)).unwrap(), 10_000_000);
    assert_eq!(to_base_units(dec!(100)).unwrap(), 100_000_000);
}

#[test]
fn to_base_units_common_prices() {
    assert_eq!(to_base_units(dec!(0.50)).unwrap(), 500_000);
    assert_eq!(to_base_units(dec!(0.63)).unwrap(), 630_000);
    assert_eq!(to_base_units(dec!(0.01)).unwrap(), 10_000);
    assert_eq!(to_base_units(dec!(0.99)).unwrap(), 990_000);
}

#[test]
fn to_base_units_six_decimal_precision() {
    assert_eq!(to_base_units(dec!(0.123456)).unwrap(), 123_456);
    assert_eq!(to_base_units(dec!(99.999999)).unwrap(), 99_999_999);
}

#[test]
fn to_base_units_floors_sub_usdc_remainder() {
    // 0.1234567 * 1_000_000 = 123456.7 — should floor to 123456
    assert_eq!(to_base_units(dec!(0.1234567)).unwrap(), 123_456);
}

#[test]
fn to_base_units_zero_returns_zero() {
    assert_eq!(to_base_units(Decimal::ZERO).unwrap(), 0);
}

#[test]
fn to_base_units_negative_returns_err() {
    // D4: negative amounts must error rather than silently encode as 0.
    assert!(to_base_units(dec!(-1)).is_err());
    assert!(to_base_units(dec!(-0.000001)).is_err());
}

// ── compute_amounts ───────────────────────────────────────────────────────────

#[test]
fn buy_maker_is_usdc_taker_is_tokens() {
    // BUY 10 tokens @ 0.65: maker pays 6.5 USDC, taker receives 10 tokens
    let (maker, taker) = compute_amounts(Side::Buy, dec!(0.65), dec!(10)).unwrap();
    assert_eq!(maker, "6500000");   // 6.5 USDC in base units
    assert_eq!(taker, "10000000"); // 10 tokens in base units
}

#[test]
fn sell_maker_is_tokens_taker_is_usdc() {
    // SELL 10 tokens @ 0.70: maker gives 10 tokens, taker pays 7 USDC
    let (maker, taker) = compute_amounts(Side::Sell, dec!(0.70), dec!(10)).unwrap();
    assert_eq!(maker, "10000000"); // 10 tokens in base units
    assert_eq!(taker, "7000000");  // 7 USDC in base units
}

#[test]
fn buy_at_price_050_symmetry() {
    // At 0.50: 2 tokens costs 1 USDC
    let (maker, taker) = compute_amounts(Side::Buy, dec!(0.50), dec!(2)).unwrap();
    assert_eq!(maker, "1000000");  // 1 USDC
    assert_eq!(taker, "2000000"); // 2 tokens
}

#[test]
fn zero_size_produces_zero_amounts() {
    let (maker, taker) = compute_amounts(Side::Buy, dec!(0.50), Decimal::ZERO).unwrap();
    assert_eq!(maker, "0");
    assert_eq!(taker, "0");
}

#[test]
fn compute_amounts_negative_size_errors() {
    // D4: bubbling up the to_base_units error.
    assert!(compute_amounts(Side::Buy, dec!(0.50), dec!(-1)).is_err());
    assert!(compute_amounts(Side::Sell, dec!(0.50), dec!(-1)).is_err());
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
fn is_terminal_for_unmatched_status() {
    // "unmatched" = sports market delay expired with no match — order is killed.
    assert!(make_order("unmatched", None, None).is_terminal());
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

// EIP-712 struct hash invariants (incl. the u256 large-token-id regressions)
// are now covered by tests/v2_signing.rs, which cross-checks every byte against
// py-clob-client-v2 fixtures. No need to re-test here.

// ── post-only cross-reject detection ─────────────────────────────────────────
//
// Polymarket V2 rejects post-only orders that would cross the spread with a
// `success:false` body whose `errorMsg` contains "cross". The strategy uses
// `is_post_only_cross_reject` to differentiate this from other rejection
// reasons (insufficient balance, malformed signature, etc) — the cross-reject
// path retries with a plain GTC, all other rejects are terminal.

use crate::orders::is_post_only_cross_reject;

#[test]
fn cross_reject_recognised_from_canonical_phrasings() {
    assert!(is_post_only_cross_reject("post-only order would cross"));
    assert!(is_post_only_cross_reject("would cross the spread"));
    assert!(is_post_only_cross_reject("Order would cross"));
    assert!(is_post_only_cross_reject("post-only would cross the touch"));
}

#[test]
fn cross_reject_case_insensitive() {
    assert!(is_post_only_cross_reject("POST-ONLY ORDER WOULD CROSS"));
    assert!(is_post_only_cross_reject("Would Cross"));
}

#[test]
fn cross_reject_rejects_unrelated_errors() {
    assert!(!is_post_only_cross_reject("insufficient balance"));
    assert!(!is_post_only_cross_reject("invalid signature"));
    assert!(!is_post_only_cross_reject("rate limited"));
    assert!(!is_post_only_cross_reject(""));
}

#[test]
fn cross_reject_avoids_false_positives_on_substring_cross() {
    // These contain "cross" but are not post-only-would-cross errors.
    assert!(!is_post_only_cross_reject("across the spread"));
    assert!(!is_post_only_cross_reject("cross-market mismatch"));
    assert!(!is_post_only_cross_reject("crossover detected"));
    // "post-only" alone (without "cross") shouldn't match either.
    assert!(!is_post_only_cross_reject("post-only orders not supported on this market"));
}

// ── post_only field plumbing on OrderSubmission ──────────────────────────────

#[test]
fn order_submission_with_post_only_sets_field() {
    use crate::orders_v2::{OrderSubmission, OrderV2, SignedOrderV2};
    let order = OrderV2 {
        salt: "1".into(), maker: "0x".into(), signer: "0x".into(),
        token_id: "1".into(), maker_amount: "1".into(), taker_amount: "1".into(),
        side: 0, signature_type: 1, timestamp: "1".into(),
        metadata: "0x0000000000000000000000000000000000000000000000000000000000000000".into(),
        builder: "0x0000000000000000000000000000000000000000000000000000000000000000".into(),
        expiration: "0".into(),
    };
    let signed = SignedOrderV2 {
        order: order.clone(),
        signature: "0x00".into(),
    };
    let plain = OrderSubmission::new(&signed, "owner", "GTC");
    assert!(!plain.post_only, "default new() must keep post_only=false");

    let post_only = OrderSubmission::with_post_only(&signed, "owner", "GTC", true);
    assert!(post_only.post_only, "with_post_only(true) must set the field");
    assert!(!post_only.defer_exec);
    assert_eq!(post_only.order_type, "GTC");
}

#[test]
fn order_submission_post_only_serializes_to_correct_field_name() {
    use crate::orders_v2::{OrderSubmission, OrderV2, SignedOrderV2};
    let order = OrderV2 {
        salt: "1".into(), maker: "0xa".into(), signer: "0xa".into(),
        token_id: "9".into(), maker_amount: "1000000".into(),
        taker_amount: "2000000".into(), side: 0, signature_type: 1,
        timestamp: "1700000000000".into(),
        metadata: "0x0000000000000000000000000000000000000000000000000000000000000000".into(),
        builder: "0x0000000000000000000000000000000000000000000000000000000000000000".into(),
        expiration: "0".into(),
    };
    let signed = SignedOrderV2 { order: order.clone(), signature: "0x00".into() };
    let body = OrderSubmission::with_post_only(&signed, "owner", "GTC", true);
    let json = serde_json::to_string(&body).unwrap();
    assert!(json.contains("\"postOnly\":true"), "wire field must be camelCase 'postOnly': {json}");
    assert!(!json.contains("post_only"), "snake_case must not leak into wire format");
}
