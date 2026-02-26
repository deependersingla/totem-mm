use anyhow::{Context, Result};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use std::path::Path;
use std::str::FromStr;

use crate::types::Team;

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
    pub revert_delay_ms: Option<u64>,
    pub dry_run: Option<bool>,
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
            revert_delay_ms: Some(config.revert_delay_ms),
            dry_run: Some(config.dry_run),
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
    pub revert_delay_ms: u64,
    pub tick_size: String,

    pub ws_ping_interval_secs: u64,
    pub dry_run: bool,
    pub log_level: String,

    pub http_port: u16,
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
            revert_delay_ms: saved.revert_delay_ms
                .unwrap_or_else(|| env_or("REVERT_DELAY_MS", "3000").parse().unwrap_or(3000)),
            tick_size: env_or("TICK_SIZE", "0.01"),

            ws_ping_interval_secs: env_or("WS_PING_INTERVAL_SECS", "10").parse()?,
            dry_run: saved.dry_run
                .unwrap_or_else(|| env_or("DRY_RUN", "true").parse().unwrap_or(true)),
            log_level: env_or("LOG_LEVEL", "info"),

            http_port: env_or("HTTP_PORT", "3000").parse()?,
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
