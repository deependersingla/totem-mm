use anyhow::{Context, Result};
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use serde::{Deserialize, Serialize};
use std::path::Path;
use std::str::FromStr;

use crate::types::Team;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MakerConfig {
    pub enabled: bool,
    pub dry_run: bool,
    pub half_spread: Decimal,
    pub quote_size: Decimal,
    pub use_gtd: bool,
    pub gtd_expiry_secs: u64,
    pub refresh_interval_secs: u64,
    pub skew_kappa: Decimal,
    pub max_exposure: Decimal,
    pub t1_pct: f64,
    pub t2_pct: f64,
    pub t3_pct: f64,
}

impl Default for MakerConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            dry_run: true, // MUST start as true
            half_spread: dec!(0.01),
            quote_size: dec!(50),
            use_gtd: true,
            gtd_expiry_secs: 60,
            refresh_interval_secs: 45,
            skew_kappa: dec!(0.0005),
            max_exposure: dec!(200),
            t1_pct: 0.20,
            t2_pct: 0.50,
            t3_pct: 0.80,
        }
    }
}

const SETTINGS_FILE: &str = "settings.json";

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct SavedSettings {
    pub polymarket_private_key: Option<String>,
    pub polymarket_address: Option<String>,
    pub signature_type: Option<u8>,
    pub neg_risk: Option<bool>,
    pub team_a_name: Option<String>,
    pub team_b_name: Option<String>,
    pub team_a_token_id: Option<String>,
    pub team_b_token_id: Option<String>,
    pub condition_id: Option<String>,
    pub first_batting: Option<String>,
    pub total_budget_usdc: Option<String>,
    pub max_trade_usdc: Option<String>,
    pub safe_percentage: Option<u64>,
    pub revert_delay_ms: Option<u64>,
    pub fill_poll_interval_ms: Option<u64>,
    pub fill_poll_timeout_ms: Option<u64>,
    pub dry_run: Option<bool>,
    /// Pre-configured CLOB API credentials (from Polymarket.com → Settings → API).
    /// When set, ClobAuth::derive() skips the EIP-712 L1 derivation call entirely.
    pub api_key: Option<String>,
    pub api_secret: Option<String>,
    pub api_passphrase: Option<String>,
    /// Polymarket market slug (e.g. "crint-ind-wst-2026-03-01") for the embed widget.
    pub market_slug: Option<String>,
    /// Edge (profit margin) on revert GTC limit orders, in percentage points.
    /// e.g. 2 means 2% — if you sold at 28¢, revert buy limit at 28*(1-0.02)=27.44¢.
    pub edge_wicket: Option<f64>,
    pub edge_boundary_4: Option<f64>,
    pub edge_boundary_6: Option<f64>,
    pub fee_rate_bps: Option<u32>,
    pub order_min_size: Option<String>,
    pub fill_ws_timeout_ms: Option<u64>,
    pub breakeven_timeout_ms: Option<u64>,
    pub maker: Option<MakerConfig>,
    /// Builder API credentials (from polymarket.com/settings?tab=builder).
    /// Only used by the sweep engine for order attribution.
    pub builder_api_key: Option<String>,
    pub builder_api_secret: Option<String>,
    pub builder_api_passphrase: Option<String>,
}

impl SavedSettings {
    pub fn load() -> Self {
        let path = Path::new(SETTINGS_FILE);
        if path.exists() {
            match std::fs::read_to_string(path) {
                Ok(contents) => {
                    match serde_json::from_str(&contents) {
                        Ok(s) => return s,
                        Err(e) => tracing::warn!("failed to parse {SETTINGS_FILE}: {e}"),
                    }
                }
                Err(e) => tracing::warn!("failed to read {SETTINGS_FILE}: {e}"),
            }
        }
        Self::default()
    }

    pub fn save(&self) {
        match serde_json::to_string_pretty(self) {
            Ok(json) => {
                if let Err(e) = std::fs::write(SETTINGS_FILE, json) {
                    tracing::warn!("failed to write {SETTINGS_FILE}: {e}");
                }
            }
            Err(e) => tracing::warn!("failed to serialize settings: {e}"),
        }
    }

    pub fn from_config(config: &Config) -> Self {
        Self {
            polymarket_private_key: if config.has_wallet() {
                Some(config.polymarket_private_key.clone())
            } else {
                None
            },
            polymarket_address: Some(config.polymarket_address.clone()).filter(|s| !s.is_empty()),
            signature_type: Some(config.signature_type),
            neg_risk: Some(config.neg_risk),
            team_a_name: Some(config.team_a_name.clone()).filter(|s| s != "TEAM_A"),
            team_b_name: Some(config.team_b_name.clone()).filter(|s| s != "TEAM_B"),
            team_a_token_id: Some(config.team_a_token_id.clone()).filter(|s| !s.is_empty()),
            team_b_token_id: Some(config.team_b_token_id.clone()).filter(|s| !s.is_empty()),
            condition_id: Some(config.condition_id.clone()).filter(|s| !s.is_empty()),
            first_batting: Some(format!("{}", config.first_batting)),
            total_budget_usdc: Some(config.total_budget_usdc.to_string()),
            max_trade_usdc: Some(config.max_trade_usdc.to_string()),
            safe_percentage: Some(config.safe_percentage),
            revert_delay_ms: Some(config.revert_delay_ms),
            fill_poll_interval_ms: Some(config.fill_poll_interval_ms),
            fill_poll_timeout_ms: Some(config.fill_poll_timeout_ms),
            dry_run: Some(config.dry_run),
            api_key: Some(config.api_key.clone()).filter(|s| !s.is_empty()),
            api_secret: Some(config.api_secret.clone()).filter(|s| !s.is_empty()),
            api_passphrase: Some(config.api_passphrase.clone()).filter(|s| !s.is_empty()),
            market_slug: Some(config.market_slug.clone()).filter(|s| !s.is_empty()),
            edge_wicket: Some(config.edge_wicket),
            edge_boundary_4: Some(config.edge_boundary_4),
            edge_boundary_6: Some(config.edge_boundary_6),
            fee_rate_bps: Some(config.fee_rate_bps),
            order_min_size: Some(config.order_min_size.to_string()),
            fill_ws_timeout_ms: Some(config.fill_ws_timeout_ms),
            breakeven_timeout_ms: Some(config.breakeven_timeout_ms),
            maker: Some(config.maker_config.clone()),
            builder_api_key: Some(config.builder_api_key.clone()).filter(|s| !s.is_empty()),
            builder_api_secret: Some(config.builder_api_secret.clone()).filter(|s| !s.is_empty()),
            builder_api_passphrase: Some(config.builder_api_passphrase.clone()).filter(|s| !s.is_empty()),
        }
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct Config {
    #[serde(skip)]
    pub polymarket_private_key: String,
    pub polymarket_address: String,
    pub signature_type: u8,
    pub neg_risk: bool,
    pub chain_id: u64,

    pub polygon_rpc: String,
    pub clob_http: String,
    pub clob_ws: String,

    pub team_a_name: String,
    pub team_b_name: String,
    pub team_a_token_id: String,
    pub team_b_token_id: String,
    pub condition_id: String,
    pub first_batting: Team,

    pub total_budget_usdc: Decimal,
    pub max_trade_usdc: Decimal,
    pub safe_percentage: u64,
    pub revert_delay_ms: u64,
    pub fill_poll_interval_ms: u64,
    pub fill_poll_timeout_ms: u64,
    pub tick_size: String,
    /// Min order size from Gamma (orderMinSize); enforced when placing orders.
    pub order_min_size: Decimal,
    /// Taker fee rate in basis points. Fetched from Gamma API (takerBaseFee).
    /// Sports markets typically use 1000 (10%). Default 0 for non-sports.
    pub fee_rate_bps: u32,

    pub ws_ping_interval_secs: u64,
    pub dry_run: bool,
    pub log_level: String,

    pub http_port: u16,

    /// Pre-configured CLOB API credentials. When all three are non-empty,
    /// ClobAuth::derive() uses them directly without hitting the L1 auth endpoint.
    #[serde(skip)]
    pub api_key: String,
    #[serde(skip)]
    pub api_secret: String,
    #[serde(skip)]
    pub api_passphrase: String,

    /// Polymarket market slug for the live embed widget.
    pub market_slug: String,

    /// Edge (profit margin %) on revert GTC orders per signal type.
    /// REVERT_SELL limit = buy_price * (1 + edge/100)
    /// REVERT_BUY limit = sell_price * (1 - edge/100)
    pub edge_wicket: f64,
    pub edge_boundary_4: f64,
    pub edge_boundary_6: f64,

    pub fill_ws_timeout_ms: u64,

    /// After placing a revert GTC, wait this long for it to fill.
    /// If still unfilled, cancel it and FAK exit at entry price (break-even).
    /// 0 = disabled (revert sits forever as GTC). Default: 3000ms.
    pub breakeven_timeout_ms: u64,

    pub maker_config: MakerConfig,

    /// Builder API credentials (sweep only). Separate from regular CLOB creds.
    #[serde(skip)]
    pub builder_api_key: String,
    #[serde(skip)]
    pub builder_api_secret: String,
    #[serde(skip)]
    pub builder_api_passphrase: String,
}

impl Config {
    pub fn from_env() -> Result<Self> {
        dotenvy::dotenv().ok();
        let saved = SavedSettings::load();

        let env_batting = env_or("FIRST_BATTING", "A");
        let first_batting_str = saved.first_batting.as_deref()
            .unwrap_or(&env_batting);
        let first_batting = match first_batting_str.to_uppercase().as_str() {
            "B" | "TEAM_B" => Team::TeamB,
            _ => Team::TeamA,
        };

        Ok(Self {
            polymarket_private_key: saved.polymarket_private_key
                .unwrap_or_else(|| env_or("POLYMARKET_PRIVATE_KEY", "")),
            polymarket_address: saved.polymarket_address
                .unwrap_or_else(|| env_or("POLYMARKET_ADDRESS", "")),
            // 0=EOA (no proxy), 1=POLY_PROXY (MetaMask+Polymarket proxy, most common),
            // 2=GNOSIS_SAFE. Default to 1 since most users connect via MetaMask which
            // creates a Polymarket proxy wallet.
            signature_type: saved.signature_type
                .unwrap_or_else(|| env_or("POLYMARKET_SIGNATURE_TYPE", "1").parse().unwrap_or(1)),
            neg_risk: saved.neg_risk
                .unwrap_or_else(|| env_or("NEG_RISK", "false").parse().unwrap_or(false)),
            chain_id: env_or("CHAIN_ID", "137").parse()?,

            polygon_rpc: env_or("POLYGON_RPC", "https://polygon-bor-rpc.publicnode.com"),
            clob_http: env_or("POLYMARKET_CLOB_HTTP", "https://clob.polymarket.com"),
            clob_ws: env_or(
                "POLYMARKET_CLOB_WS",
                "wss://ws-subscriptions-clob.polymarket.com/ws/market",
            ),

            team_a_name: saved.team_a_name
                .unwrap_or_else(|| env_or("TEAM_A_NAME", "TEAM_A")),
            team_b_name: saved.team_b_name
                .unwrap_or_else(|| env_or("TEAM_B_NAME", "TEAM_B")),
            team_a_token_id: saved.team_a_token_id
                .unwrap_or_else(|| env_or("TEAM_A_TOKEN_ID", "")),
            team_b_token_id: saved.team_b_token_id
                .unwrap_or_else(|| env_or("TEAM_B_TOKEN_ID", "")),
            condition_id: saved.condition_id
                .unwrap_or_else(|| env_or("CONDITION_ID", "")),
            first_batting,

            total_budget_usdc: decimal_env_or_saved(
                "TOTAL_BUDGET_USDC", "100", saved.total_budget_usdc.as_deref())?,
            max_trade_usdc: decimal_env_or_saved(
                "MAX_TRADE_USDC", "10", saved.max_trade_usdc.as_deref())?,
            safe_percentage: saved.safe_percentage
                .unwrap_or_else(|| env_or("SAFE_PERCENTAGE", "2").parse().unwrap_or(2)),
            revert_delay_ms: saved.revert_delay_ms
                .unwrap_or_else(|| env_or("REVERT_DELAY_MS", "0").parse().unwrap_or(0)),
            fill_poll_interval_ms: saved.fill_poll_interval_ms
                .unwrap_or_else(|| env_or("FILL_POLL_INTERVAL_MS", "500").parse().unwrap_or(500)),
            fill_poll_timeout_ms: saved.fill_poll_timeout_ms
                .unwrap_or_else(|| env_or("FILL_POLL_TIMEOUT_MS", "10000").parse().unwrap_or(10000)),
            tick_size: env_or("TICK_SIZE", "0.01"),
            order_min_size: saved.order_min_size.as_deref()
                .and_then(|s| Decimal::from_str(s).ok())
                .unwrap_or(Decimal::ONE),
            fee_rate_bps: saved.fee_rate_bps.unwrap_or(0),

            ws_ping_interval_secs: env_or("WS_PING_INTERVAL_SECS", "10").parse()?,
            dry_run: saved.dry_run
                .unwrap_or_else(|| env_or("DRY_RUN", "true").parse().unwrap_or(true)),
            log_level: env_or("LOG_LEVEL", "info"),

            http_port: env_or("HTTP_PORT", "3000").parse()?,

            api_key: saved.api_key.unwrap_or_else(|| env_or("POLYMARKET_API_KEY", "")),
            api_secret: saved.api_secret.unwrap_or_else(|| env_or("POLYMARKET_API_SECRET", "")),
            api_passphrase: saved.api_passphrase.unwrap_or_else(|| env_or("POLYMARKET_API_PASSPHRASE", "")),
            market_slug: saved.market_slug.unwrap_or_default(),

            edge_wicket: saved.edge_wicket.unwrap_or(2.0),
            edge_boundary_4: saved.edge_boundary_4.unwrap_or(1.0),
            edge_boundary_6: saved.edge_boundary_6.unwrap_or(1.0),

            fill_ws_timeout_ms: saved.fill_ws_timeout_ms
                .unwrap_or_else(|| env_or("FILL_WS_TIMEOUT_MS", "5000").parse().unwrap_or(5000)),
            breakeven_timeout_ms: saved.breakeven_timeout_ms
                .unwrap_or_else(|| env_or("BREAKEVEN_TIMEOUT_MS", "10000").parse().unwrap_or(10000)),
            maker_config: saved.maker.unwrap_or_default(),

            builder_api_key: saved.builder_api_key.unwrap_or_default(),
            builder_api_secret: saved.builder_api_secret.unwrap_or_default(),
            builder_api_passphrase: saved.builder_api_passphrase.unwrap_or_default(),
        })
    }

    pub fn persist(&self) {
        SavedSettings::from_config(self).save();
    }

    pub fn token_id(&self, team: Team) -> &str {
        match team {
            Team::TeamA => &self.team_a_token_id,
            Team::TeamB => &self.team_b_token_id,
        }
    }

    pub fn team_name(&self, team: Team) -> &str {
        match team {
            Team::TeamA => &self.team_a_name,
            Team::TeamB => &self.team_b_name,
        }
    }

    pub fn exchange_address(&self) -> &str {
        if self.neg_risk {
            match self.chain_id {
                137 => "0xC5d563A36AE78145C45a50134d48A1215220f80a",
                _ => "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
            }
        } else {
            match self.chain_id {
                137 => "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
                _ => "0xdFE02Eb6733538f8Ea35D585af8DE5958AD99E40",
            }
        }
    }

    pub fn safe_price_range(&self) -> (Decimal, Decimal) {
        let min = Decimal::new(self.safe_percentage as i64, 2);
        let max = Decimal::ONE - min;
        (min, max)
    }

    pub fn has_wallet(&self) -> bool {
        !self.polymarket_private_key.is_empty()
            && self.polymarket_private_key != "0x0000000000000000000000000000000000000000000000000000000000000001"
    }

    pub fn has_tokens(&self) -> bool {
        !self.team_a_token_id.is_empty() && !self.team_b_token_id.is_empty()
    }
}

fn env_or(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.to_string())
}

fn decimal_env(key: &str, default: &str) -> Result<Decimal> {
    let raw = env_or(key, default);
    Decimal::from_str(&raw).with_context(|| format!("invalid decimal for {key}: {raw}"))
}

fn decimal_env_or_saved(key: &str, default: &str, saved: Option<&str>) -> Result<Decimal> {
    if let Some(s) = saved {
        if let Ok(d) = Decimal::from_str(s) {
            return Ok(d);
        }
    }
    decimal_env(key, default)
}
