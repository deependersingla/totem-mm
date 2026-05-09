//! V2 `GET /clob-markets/{condition_id}` — single-call source of truth for
//! per-market parameters that matter for trading: tick size, min order size,
//! fee rate + exponent (V2 fee formula), neg-risk flag, taker-order-delay
//! flag (`itode`), and min-order-age (`oas`).
//!
//! Field abbreviations come straight from the response payload — the API uses
//! short keys (`mts`, `mos`, `fd.r`, `fd.e`, etc.) to keep payloads small.
//! See py-clob-client-v2 client.py::get_clob_market_info.

use anyhow::{Context, Result};
use serde::Deserialize;

#[derive(Debug, Clone, Deserialize)]
pub struct ClobMarketInfo {
    /// Minimum tick size (e.g. 0.01).
    #[serde(rename = "mts")]
    pub min_tick_size: f64,

    /// Minimum order size (e.g. 5.0).
    #[serde(rename = "mos")]
    pub min_order_size: f64,

    /// Maker base fee in bps. V1-style, vestigial in V2.
    #[serde(rename = "mbf", default)]
    pub maker_base_fee_bps: i64,

    /// Taker base fee in bps. V1-style, vestigial in V2.
    #[serde(rename = "tbf", default)]
    pub taker_base_fee_bps: i64,

    /// Neg-risk market flag.
    #[serde(rename = "nr", default)]
    pub neg_risk: bool,

    /// V2 fee details (per-market). Drives fee = C × feeRate × p × (1 − p).
    #[serde(rename = "fd", default)]
    pub fee_details: Option<FeeDetails>,

    /// Tokens array — `{ "t": <token_id>, "o": <outcome> }`.
    #[serde(rename = "t", default)]
    pub tokens: Vec<TokenInfo>,

    /// Game start time (RFC 3339) — useful for cricket game-state tracking.
    #[serde(rename = "gst", default)]
    pub game_start: Option<String>,

    /// RFQ-enabled market flag.
    #[serde(rename = "rfqe", default)]
    pub rfq_enabled: bool,

    /// Taker-order-delay enabled. **Markets with this true inject artificial
    /// latency on takers — our cricket strategy's edge depends on the absence
    /// of this. Refuse or alert when true.**
    #[serde(rename = "itode", default)]
    pub taker_order_delay_enabled: bool,

    /// Min order age (seconds). Server rejects fresher orders. 0 = no gate.
    #[serde(rename = "oas", default)]
    pub min_order_age_secs: u64,
}

#[derive(Debug, Clone, Deserialize)]
pub struct FeeDetails {
    /// Fee rate (per V2 formula). Float, not bps.
    #[serde(rename = "r", default)]
    pub rate: f64,

    /// Fee exponent. Drives the C constant in fee = C × rate × p × (1 − p).
    #[serde(rename = "e", default)]
    pub exponent: f64,

    /// Takers-only flag.
    #[serde(rename = "to", default)]
    pub takers_only: bool,
}

#[derive(Debug, Clone, Deserialize)]
pub struct TokenInfo {
    #[serde(rename = "t")]
    pub token_id: String,
    #[serde(rename = "o")]
    pub outcome: String,
}

impl ClobMarketInfo {
    pub fn fee_rate(&self) -> f64 {
        self.fee_details.as_ref().map(|fd| fd.rate).unwrap_or(0.0)
    }

    pub fn fee_exponent(&self) -> f64 {
        self.fee_details.as_ref().map(|fd| fd.exponent).unwrap_or(0.0)
    }

    /// V2 `fd.to` flag — when true, only takers pay the platform fee; makers
    /// pay zero. Defaults to **true** when the payload omits `fd` entirely
    /// (which corresponds to a fee-free market and is consistent with "no
    /// maker fee"). Production cricket markets always set this true.
    pub fn takers_only(&self) -> bool {
        self.fee_details.as_ref().map(|fd| fd.takers_only).unwrap_or(true)
    }
}

/// Fetch market parameters from `<base_url>/clob-markets/<condition_id>`.
pub async fn fetch(
    http_client: &reqwest::Client,
    base_url: &str,
    condition_id: &str,
) -> Result<ClobMarketInfo> {
    if condition_id.trim().is_empty() {
        anyhow::bail!("fetch_clob_market_info: condition_id must not be empty");
    }
    let url = format!("{}/clob-markets/{}", base_url.trim_end_matches('/'), condition_id);
    let resp = http_client
        .get(&url)
        .send()
        .await
        .context("clob-markets HTTP send")?;
    let status = resp.status();
    let body = resp.text().await.context("clob-markets HTTP read body")?;
    if !status.is_success() {
        anyhow::bail!("clob-markets HTTP {status}: {body}");
    }
    let info: ClobMarketInfo = serde_json::from_str(&body)
        .with_context(|| format!("clob-markets JSON decode failed; body={body}"))?;
    Ok(info)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Sample response shape we expect from the V2 endpoint. Decode round-trip.
    #[test]
    fn deserialize_full_response() {
        let raw = r#"{
            "mts": 0.01,
            "mos": 5.0,
            "mbf": 0,
            "tbf": 200,
            "nr": false,
            "fd": {"r": 0.02, "e": 4.0, "to": true},
            "t": [
                {"t": "71321", "o": "YES"},
                {"t": "44444", "o": "NO"}
            ],
            "gst": "2026-04-28T14:30:00Z",
            "rfqe": false,
            "itode": false,
            "oas": 0
        }"#;
        let info: ClobMarketInfo = serde_json::from_str(raw).unwrap();
        assert_eq!(info.min_tick_size, 0.01);
        assert_eq!(info.min_order_size, 5.0);
        assert_eq!(info.taker_base_fee_bps, 200);
        assert_eq!(info.fee_rate(), 0.02);
        assert_eq!(info.fee_exponent(), 4.0);
        assert_eq!(info.tokens.len(), 2);
        assert_eq!(info.tokens[0].outcome, "YES");
        assert!(!info.taker_order_delay_enabled);
    }

    #[test]
    fn deserialize_minimal_response_with_defaults() {
        // Server may omit optional fields. All defaults must work.
        let raw = r#"{"mts": 0.01, "mos": 5.0}"#;
        let info: ClobMarketInfo = serde_json::from_str(raw).unwrap();
        assert_eq!(info.fee_rate(), 0.0);
        assert_eq!(info.fee_exponent(), 0.0);
        assert!(!info.neg_risk);
        assert!(!info.rfq_enabled);
        assert!(info.tokens.is_empty());
    }

    #[test]
    fn flags_taker_delay_when_present() {
        let raw = r#"{"mts": 0.01, "mos": 5.0, "itode": true, "oas": 3}"#;
        let info: ClobMarketInfo = serde_json::from_str(raw).unwrap();
        assert!(info.taker_order_delay_enabled);
        assert_eq!(info.min_order_age_secs, 3);
    }
}
