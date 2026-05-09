//! Unit tests for `crate::fees` — V2 taker fee formula.
//!
//! Cricket production reference: `r=0.03, e=1` (sports tier). Worked example
//! locked in `FEES_AND_LEGS_CURRENT.md §1`.

use crate::fees::*;
use rust_decimal::Decimal;
use rust_decimal_macros::dec;

// ── k_factor ─────────────────────────────────────────────────────────────────

#[test]
fn k_factor_e0_is_one_everywhere() {
    assert_eq!(k_factor(dec!(0.5), 0), Decimal::ONE);
    assert_eq!(k_factor(dec!(0.7), 0), Decimal::ONE);
    assert_eq!(k_factor(dec!(0.01), 0), Decimal::ONE);
}

#[test]
fn k_factor_e1_is_p_times_one_minus_p() {
    assert_eq!(k_factor(dec!(0.5), 1), dec!(0.25));
    assert_eq!(k_factor(dec!(0.6), 1), dec!(0.24));
    assert_eq!(k_factor(dec!(0.4), 1), dec!(0.24)); // symmetry
    assert_eq!(k_factor(dec!(0.7), 1), dec!(0.21));
}

#[test]
fn k_factor_e2_squares() {
    assert_eq!(k_factor(dec!(0.5), 2), dec!(0.0625));
    // 0.6 * 0.4 = 0.24, 0.24^2 = 0.0576
    assert_eq!(k_factor(dec!(0.6), 2), dec!(0.0576));
}

#[test]
fn k_factor_e4_for_extreme_exponent() {
    // 0.5 → 0.25^4 = 0.00390625
    assert_eq!(k_factor(dec!(0.5), 4), dec!(0.00390625));
}

#[test]
fn k_factor_at_extremes_is_zero() {
    assert_eq!(k_factor(dec!(0.0), 1), Decimal::ZERO);
    assert_eq!(k_factor(dec!(1.0), 1), Decimal::ZERO);
    assert_eq!(k_factor(dec!(-0.1), 1), Decimal::ZERO);
    assert_eq!(k_factor(dec!(1.1), 1), Decimal::ZERO);
}

// ── platform_fee_rate ────────────────────────────────────────────────────────

#[test]
fn rate_zero_when_r_is_zero() {
    assert_eq!(platform_fee_rate(0.0, dec!(0.5), 1), Decimal::ZERO);
    assert_eq!(platform_fee_rate(0.0, dec!(0.5), 4), Decimal::ZERO);
}

#[test]
fn rate_cricket_peak() {
    // r=0.03, e=1, p=0.5 → 0.03 * 0.25 = 0.0075
    assert_eq!(platform_fee_rate(0.03, dec!(0.5), 1), dec!(0.0075));
}

#[test]
fn rate_cricket_at_p_60() {
    // r=0.03, e=1, p=0.6 → 0.03 * 0.24 = 0.0072
    assert_eq!(platform_fee_rate(0.03, dec!(0.6), 1), dec!(0.0072));
}

// ── fee_tokens_buy / fee_usdc_sell ───────────────────────────────────────────

#[test]
fn cricket_worked_example_buy() {
    // r=0.03, e=1, p=0.6, shares=10 → fee_tokens = 10 · 0.0072 = 0.072
    let fee = fee_tokens_buy(dec!(10), dec!(0.6), 0.03, 1);
    assert_eq!(fee, dec!(0.072));
}

#[test]
fn cricket_worked_example_sell() {
    // r=0.03, e=1, p=0.6, shares=10 → fee_usdc = 10 · 0.6 · 0.0072 = 0.0432
    let fee = fee_usdc_sell(dec!(10), dec!(0.6), 0.03, 1);
    assert_eq!(fee, dec!(0.0432));
}

#[test]
fn buy_sell_fee_symmetry_holds() {
    // fee_tokens_buy * p == fee_usdc_sell  for the same (shares, p, r, e)
    let p = dec!(0.55);
    let shares = dec!(7);
    let r = 0.03;
    let e = 1;
    assert_eq!(fee_tokens_buy(shares, p, r, e) * p, fee_usdc_sell(shares, p, r, e));
}

#[test]
fn zero_shares_zero_fee() {
    assert_eq!(fee_tokens_buy(Decimal::ZERO, dec!(0.5), 0.03, 1), Decimal::ZERO);
    assert_eq!(fee_usdc_sell(Decimal::ZERO, dec!(0.5), 0.03, 1), Decimal::ZERO);
}

#[test]
fn zero_rate_zero_fee() {
    assert_eq!(fee_tokens_buy(dec!(10), dec!(0.5), 0.0, 1), Decimal::ZERO);
    assert_eq!(fee_usdc_sell(dec!(10), dec!(0.5), 0.0, 1), Decimal::ZERO);
}

// ── gross_up_buy ─────────────────────────────────────────────────────────────

#[test]
fn gross_up_no_op_when_rate_zero() {
    assert_eq!(gross_up_buy(dec!(10), dec!(0.5), 0.0, 1), dec!(10));
}

#[test]
fn gross_up_no_op_for_zero_net() {
    assert_eq!(gross_up_buy(Decimal::ZERO, dec!(0.5), 0.03, 1), Decimal::ZERO);
}

#[test]
fn gross_up_at_peak_rate_returns_at_least_net_after_fee() {
    // r=0.03, e=1, p=0.5: rate=0.0075. gross = ceil(10 / 0.9925) at 6dp.
    // 10 / 0.9925 = 10.0755667...  → ceil_at_6dp → 10.075567
    let net = dec!(10);
    let p = dec!(0.5);
    let r = 0.03;
    let e = 1;
    let gross = gross_up_buy(net, p, r, e);
    let fee = fee_tokens_buy(gross, p, r, e);
    let received = gross - fee;
    assert!(received >= net, "received={received} should be >= net={net}, gross={gross}, fee={fee}");
}

#[test]
fn gross_up_round_trips_at_p_60() {
    let net = dec!(16); // 16 tokens at p=0.6 = $9.60 — typical max_trade
    let p = dec!(0.6);
    let r = 0.03;
    let e = 1;
    let gross = gross_up_buy(net, p, r, e);
    let received = gross - fee_tokens_buy(gross, p, r, e);
    // Post-fee net must be at least the requested net.
    assert!(received >= net);
    // Integer ceil overshoots by < 1 token (the pre-ceil raw was net/(1-rate)
    // and ceiling adds at most 1 unit of rounding).
    assert!(gross - net < dec!(2));
    // Result must be integer (V2 BUY precision constraint).
    assert_eq!(gross.fract(), Decimal::ZERO);
}

#[test]
fn gross_up_strictly_greater_than_net_when_fee_positive() {
    let gross = gross_up_buy(dec!(10), dec!(0.6), 0.03, 1);
    assert!(gross > dec!(10));
}

// V2 BUY-order precision constraint: `taker_amount` has ≤4 decimals,
// `maker_amount` (= size × price) has ≤2 decimals. With tick_size=0.01 (price
// has 2 decimals), the only sizing that satisfies BOTH is **integer size**.
// `gross_up_buy` must therefore return integer values — non-integer would
// produce maker_amount with 3+ decimals and Polymarket rejects with
// "invalid amounts, market buy orders maker amount supports a max accuracy
// of 2 decimals, taker amount a max of 4 decimals".

#[test]
fn gross_up_buy_returns_integer_for_v2_precision() {
    // 21 / (1 − 0.03·0.23·0.77) = 21.112 → ceil to integer = 22
    let g = gross_up_buy(dec!(21), dec!(0.23), 0.03, 1);
    assert_eq!(g, dec!(22), "must be integer for V2 BUY precision constraint");
    assert_eq!(g.fract(), Decimal::ZERO);
}

#[test]
fn gross_up_buy_integer_at_peak_rate() {
    // p=0.5, peak rate 0.0075. 10/0.9925 = 10.0755 → ceil = 11.
    let g = gross_up_buy(dec!(10), dec!(0.5), 0.03, 1);
    assert_eq!(g, dec!(11));
}

#[test]
fn gross_up_buy_integer_already_no_change_at_zero_rate() {
    // r=0 path is unaffected — preserves existing zero-fee behaviour.
    assert_eq!(gross_up_buy(dec!(20), dec!(0.5), 0.0, 1), dec!(20));
}

#[test]
fn gross_up_buy_zero_input_stays_zero() {
    assert_eq!(gross_up_buy(Decimal::ZERO, dec!(0.5), 0.03, 1), Decimal::ZERO);
}

#[test]
fn gross_up_buy_size_times_price_has_at_most_two_decimals() {
    // Property check: for any (net, price-with-2-decimals, rate, e), the
    // returned gross times the price must have ≤2 decimal places (the V2
    // BUY maker_amount constraint).
    for &(net_int, p_str) in &[
        (5u32, "0.23"), (10, "0.23"), (16, "0.23"),
        (5, "0.27"), (10, "0.27"), (16, "0.27"),
        (5, "0.50"), (10, "0.50"), (21, "0.50"),
        (5, "0.62"), (10, "0.62"), (21, "0.62"),
        (5, "0.99"), (10, "0.99"),
    ] {
        let net = Decimal::from(net_int);
        let p: Decimal = p_str.parse().unwrap();
        let g = gross_up_buy(net, p, 0.03, 1);
        let notional = g * p;
        // Notional must be representable in 2 decimals.
        let scaled = notional * dec!(100);
        assert_eq!(scaled.fract(), Decimal::ZERO,
            "notional {notional} (= {g} × {p_str}) must have ≤ 2 decimals; net={net_int}");
    }
}

#[test]
fn gross_up_at_e_4_still_returns_integer() {
    // e=4 makes the rate vanishingly small (0.0001 at p=0.6); raw gross is
    // 10.000996. With integer ceiling required by the V2 BUY precision
    // constraint, gross becomes 11 — a tiny-fee overshoot is the cost of
    // staying on the integer grid. Acceptable: e=4 is not a production
    // tier (sports/politics/culture/crypto all use e=1).
    let gross = gross_up_buy(dec!(10), dec!(0.6), 0.03, 4);
    let received = gross - fee_tokens_buy(gross, dec!(0.6), 0.03, 4);
    assert!(received >= dec!(10));
    assert_eq!(gross.fract(), Decimal::ZERO);
    assert_eq!(gross, dec!(11));
}

// ── fee_exponent_as_u32 ──────────────────────────────────────────────────────

#[test]
fn exponent_cast_handles_clean_integers() {
    assert_eq!(fee_exponent_as_u32(0.0), 0);
    assert_eq!(fee_exponent_as_u32(1.0), 1);
    assert_eq!(fee_exponent_as_u32(2.0), 2);
    assert_eq!(fee_exponent_as_u32(4.0), 4);
}

#[test]
fn exponent_cast_zero_for_negative_or_nan() {
    assert_eq!(fee_exponent_as_u32(-1.0), 0);
    assert_eq!(fee_exponent_as_u32(f64::NAN), 0);
    assert_eq!(fee_exponent_as_u32(f64::INFINITY), 0);
}

#[test]
fn exponent_cast_floors_fractional() {
    // No assertion on the warn — just confirm we don't panic and we floor.
    assert_eq!(fee_exponent_as_u32(1.5), 1);
    assert_eq!(fee_exponent_as_u32(2.99), 2);
}

// ── dec_from_f64 — boundary at every f64→Decimal site ───────────────────────

#[test]
fn dec_from_f64_handles_classic_unrepresentable_floats() {
    // The exact bug that corrupted the tick cache: from_f64_retain(0.01_f64)
    // returns 0.0100000000000000002081668171. Our helper must give 0.01.
    assert_eq!(crate::fees::dec_from_f64(0.01), dec!(0.01));
    assert_eq!(crate::fees::dec_from_f64(0.001), dec!(0.001));
    assert_eq!(crate::fees::dec_from_f64(0.03), dec!(0.03));
    assert_eq!(crate::fees::dec_from_f64(0.1), dec!(0.1));
    assert_eq!(crate::fees::dec_from_f64(0.2), dec!(0.2));
    assert_eq!(crate::fees::dec_from_f64(0.7), dec!(0.7));
}

#[test]
fn dec_from_f64_clean_for_integers() {
    assert_eq!(crate::fees::dec_from_f64(0.0), Decimal::ZERO);
    assert_eq!(crate::fees::dec_from_f64(1.0), Decimal::ONE);
    assert_eq!(crate::fees::dec_from_f64(2.0), dec!(2));
    assert_eq!(crate::fees::dec_from_f64(3.0), dec!(3));
}

#[test]
fn dec_from_f64_handles_non_finite() {
    assert_eq!(crate::fees::dec_from_f64(f64::NAN), Decimal::ZERO);
    assert_eq!(crate::fees::dec_from_f64(f64::INFINITY), Decimal::ZERO);
    assert_eq!(crate::fees::dec_from_f64(f64::NEG_INFINITY), Decimal::ZERO);
}

#[test]
fn dec_from_f64_does_not_match_from_f64_retain_for_unrepresentable_values() {
    // Document the actual divergence between our helper and the Decimal
    // crate's `from_f64_retain` so future readers know why we don't use it.
    let retained = Decimal::from_f64_retain(0.01).unwrap();
    let safe = crate::fees::dec_from_f64(0.01);
    assert_ne!(retained, safe, "from_f64_retain produces drift; dec_from_f64 doesn't");
    assert_eq!(safe.to_string(), "0.01");
    // The retained value has 28 trailing decimals (the IEEE-754 bits).
    assert!(retained.to_string().len() > "0.01".len());
}

// ── ceil_at_dp helper ────────────────────────────────────────────────────────

#[test]
fn ceil_at_dp_rounds_up_only() {
    assert_eq!(ceil_at_dp_for_tests(dec!(1.0000001), 6), dec!(1.000001));
    assert_eq!(ceil_at_dp_for_tests(dec!(1.000_000), 6), dec!(1.000_000));
    assert_eq!(ceil_at_dp_for_tests(dec!(10.075566708), 6), dec!(10.075_567));
}

#[test]
fn ceil_at_dp_handles_zero() {
    assert_eq!(ceil_at_dp_for_tests(Decimal::ZERO, 6), Decimal::ZERO);
}
