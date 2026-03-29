//! Shared manual trading functions.
//!
//! Place individual GTC limit orders — buy or sell a specific token
//! at a specific price and size. Used by sweep UI, reusable by taker.

use anyhow::Result;
use rust_decimal::Decimal;

use crate::clob_auth::ClobAuth;
use crate::config::Config;
use crate::orders;
use crate::types::{FakOrder, Side, Team};

/// Place a GTC limit BUY order. Returns (order_id, latency_ms).
pub async fn limit_buy(
    config: &Config,
    auth: &ClobAuth,
    team: Team,
    price: Decimal,
    size: Decimal,
) -> Result<(String, u64)> {
    let order = FakOrder { team, side: Side::Buy, price, size };
    let tag = format!("manual-buy-{}", price);
    let t0 = tokio::time::Instant::now();
    let resp = orders::post_limit_order(config, auth, &order, &tag).await?;
    let ms = t0.elapsed().as_millis() as u64;
    match resp.order_id {
        Some(oid) if !oid.is_empty() => Ok((oid, ms)),
        _ => {
            let msg = resp.error_msg.unwrap_or_else(|| "no order_id returned".into());
            anyhow::bail!("order rejected: {msg}");
        }
    }
}

/// Place a GTC limit SELL order. Returns (order_id, latency_ms).
pub async fn limit_sell(
    config: &Config,
    auth: &ClobAuth,
    team: Team,
    price: Decimal,
    size: Decimal,
) -> Result<(String, u64)> {
    let order = FakOrder { team, side: Side::Sell, price, size };
    let tag = format!("manual-sell-{}", price);
    let t0 = tokio::time::Instant::now();
    let resp = orders::post_limit_order(config, auth, &order, &tag).await?;
    let ms = t0.elapsed().as_millis() as u64;
    match resp.order_id {
        Some(oid) if !oid.is_empty() => Ok((oid, ms)),
        _ => {
            let msg = resp.error_msg.unwrap_or_else(|| "no order_id returned".into());
            anyhow::bail!("order rejected: {msg}");
        }
    }
}
