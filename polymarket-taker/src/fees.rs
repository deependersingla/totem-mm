//! Polymarket V2 taker fee math.
//!
//! Canonical formula (taker only — `fd.to=true` on every V2 cricket market):
//!
//! ```text
//!   k(p)              = (p · (1 − p))^e
//!   platform_fee_rate = r · k(p)
//!   BUY  fee_in_tokens = shares · platform_fee_rate
//!   SELL fee_in_usdc   = shares · p · platform_fee_rate
//! ```
//!
//! Where `r = fd.r`, `e = fd.e` (integer in production V2), `p` ∈ (0, 1) is
//! the fill price, and `shares` is the matched size in tokens.
//!
//! Source of truth: `Polymarket/rs-clob-client-v2/src/clob/utilities.rs:493`,
//! `clob-client-v2/src/client.ts:161`. Production cricket: `r=0.03, e=1`
//! (peak rate 0.75 % at p=0.5).
//!
//! All math is `rust_decimal` end-to-end. `f64` parameters from `Config` are
//! routed through their `Display` representation (shortest-round-trip) →
//! `Decimal::from_str` so `0.03_f64` becomes exactly `dec!(0.03)`.

use rust_decimal::Decimal;
use rust_decimal_macros::dec;

/// Decimal places for token / USDC base units. CLOB matches at this precision.
/// Kept for reference; the production sizing path uses integer ceiling because
/// V2 BUY orders impose a tighter 2-decimal `maker_amount` constraint.
#[allow(dead_code)]
const TOKEN_DP: u32 = 6;

/// Convert an `f64` to `Decimal` without binary-float drift.
///
/// `Decimal::from_f64_retain(0.01_f64)` returns
/// `0.0100000000000000002081668171` because `0.01` isn't exactly
/// representable in IEEE-754. This helper instead routes the value through
/// its `Display` representation (which Rust formats as the shortest
/// round-trip string, e.g. `"0.01"`), then parses that as `Decimal`. Result
/// is the *intended* clean decimal literal.
///
/// Use this helper at every f64→Decimal boundary — config-loaded fee rates,
/// Gamma-API tick sizes, edge-tick floats, etc. — instead of
/// `Decimal::from_f64_retain` or `Decimal::from_f64`. Returns `Decimal::ZERO`
/// for non-finite (`NaN`, ±∞) and on parse failure.
pub fn dec_from_f64(x: f64) -> Decimal {
    if !x.is_finite() {
        return Decimal::ZERO;
    }
    format!("{x}").parse().unwrap_or(Decimal::ZERO)
}

/// Cast `f64` exponent to `u32`. Production V2 always uses integer `e`.
/// Negative or non-finite → 0. Non-integer `e` → floor + warn.
pub fn fee_exponent_as_u32(e: f64) -> u32 {
    if !e.is_finite() || e <= 0.0 {
        return 0;
    }
    if (e - e.round()).abs() > 1e-9 {
        tracing::warn!(
            e,
            "fee_exponent has fractional part — flooring; production V2 uses integer e",
        );
    }
    e.floor() as u32
}

/// `(p · (1 − p))^e`. Returns `1` for `e == 0`. Saturates at `0` for `p ≤ 0`
/// or `p ≥ 1`.
pub fn k_factor(p: Decimal, e: u32) -> Decimal {
    if e == 0 {
        return Decimal::ONE;
    }
    if p <= Decimal::ZERO || p >= Decimal::ONE {
        return Decimal::ZERO;
    }
    let base = p * (Decimal::ONE - p);
    let mut acc = Decimal::ONE;
    for _ in 0..e {
        acc *= base;
    }
    acc
}

/// `r · k(p)` — dimensionless taker fee rate. Returns `0` if `r == 0`.
pub fn platform_fee_rate(r: f64, p: Decimal, e: u32) -> Decimal {
    if r == 0.0 {
        return Decimal::ZERO;
    }
    dec_from_f64(r) * k_factor(p, e)
}

/// BUY: fee deducted from tokens received. Units: tokens.
///
/// `shares` is the matched size in tokens (the `taker_amount` of the order).
pub fn fee_tokens_buy(shares: Decimal, p: Decimal, r: f64, e: u32) -> Decimal {
    if r == 0.0 || shares.is_zero() {
        return Decimal::ZERO;
    }
    shares * platform_fee_rate(r, p, e)
}

/// SELL: fee deducted from USDC received. Units: USDC.
///
/// Mathematically equivalent to `fee_tokens_buy(shares, p, r, e) * p`.
pub fn fee_usdc_sell(shares: Decimal, p: Decimal, r: f64, e: u32) -> Decimal {
    if r == 0.0 || shares.is_zero() {
        return Decimal::ZERO;
    }
    shares * p * platform_fee_rate(r, p, e)
}

/// Smallest **integer** gross size such that `gross · (1 − rate) ≥ net`.
///
/// Use when posting a BUY: request `gross` tokens so that after the matcher
/// deducts the per-fill fee we still receive at least `net` tokens.
///
/// **Integer rounding is required by Polymarket V2 BUY-order precision
/// constraints** — the matcher rejects with "invalid amounts, market buy
/// orders maker amount supports a max accuracy of 2 decimals, taker amount
/// a max of 4 decimals" when either side has too many decimal places. With
/// `tick_size = 0.01` (price has 2 decimals), the only sizing that
/// satisfies the maker-amount = `size × price` ≤ 2-decimal rule for
/// arbitrary prices like 0.23, 0.27, 0.62 (denominators coprime to 10) is
/// integer size.
///
/// Returns `net` unchanged when `r == 0`. Trade-off: integer ceil
/// overshoots the post-fee-net target by up to ~1 token, vs. the original
/// 6-decimal ceiling. Acceptable: a tiny inventory overshoot is far better
/// than a rejected order that ships zero of the leg.
pub fn gross_up_buy(net_shares: Decimal, p: Decimal, r: f64, e: u32) -> Decimal {
    if r == 0.0 || net_shares.is_zero() {
        return net_shares;
    }
    let rate = platform_fee_rate(r, p, e);
    let denom = Decimal::ONE - rate;
    if denom <= Decimal::ZERO {
        // pathological — would require rate ≥ 1; just return net.
        return net_shares;
    }
    let raw = net_shares / denom;
    // Ceiling to the nearest whole token. Decimal::ceil() rounds to integer.
    raw.ceil()
}

/// Round `x` UP to `dp` decimal places. (Decimal's built-in `ceil` rounds to
/// integer; we want token-precision ceil.)
#[allow(dead_code)]
fn ceil_at_dp(x: Decimal, dp: u32) -> Decimal {
    let scale = match dp {
        0 => dec!(1),
        1 => dec!(10),
        2 => dec!(100),
        3 => dec!(1_000),
        4 => dec!(10_000),
        5 => dec!(100_000),
        6 => dec!(1_000_000),
        _ => {
            // Generic fall-back; shouldn't be hit for production dp ≤ 6.
            let mut s = Decimal::ONE;
            for _ in 0..dp { s *= dec!(10); }
            s
        }
    };
    (x * scale).ceil() / scale
}

#[cfg(test)]
pub(crate) fn ceil_at_dp_for_tests(x: Decimal, dp: u32) -> Decimal {
    ceil_at_dp(x, dp)
}
