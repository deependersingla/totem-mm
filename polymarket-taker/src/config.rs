use anyhow::{Context, Result};
use rust_decimal::Decimal;
use serde::Serialize;
use std::str::FromStr;

use crate::types::Team;

#[derive(Debug, Clone, Serialize)]
pub struct Config {
    #[serde(skip)]
    pub polymarket_private_key: String,
    pub polymarket_address: String,
    pub signature_type: u8,
    pub neg_risk: bool,
    pub chain_id: u64,

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

        let first_batting = match env_or("FIRST_BATTING", "A").to_uppercase().as_str() {
            "B" => Team::TeamB,
            _ => Team::TeamA,
        };

        Ok(Self {
            polymarket_private_key: env_or("POLYMARKET_PRIVATE_KEY", ""),
            polymarket_address: env_or("POLYMARKET_ADDRESS", ""),
            signature_type: env_or("POLYMARKET_SIGNATURE_TYPE", "1").parse()?,
            neg_risk: env_or("NEG_RISK", "false").parse()?,
            chain_id: env_or("CHAIN_ID", "137").parse()?,

            clob_http: env_or("POLYMARKET_CLOB_HTTP", "https://clob.polymarket.com"),
            clob_ws: env_or(
                "POLYMARKET_CLOB_WS",
                "wss://ws-subscriptions-clob.polymarket.com/ws/market",
            ),

            team_a_name: env_or("TEAM_A_NAME", "TEAM_A"),
            team_b_name: env_or("TEAM_B_NAME", "TEAM_B"),
            team_a_token_id: env_or("TEAM_A_TOKEN_ID", ""),
            team_b_token_id: env_or("TEAM_B_TOKEN_ID", ""),
            condition_id: env_or("CONDITION_ID", ""),
            first_batting,

            total_budget_usdc: decimal_env("TOTAL_BUDGET_USDC", "100")?,
            max_trade_usdc: decimal_env("MAX_TRADE_USDC", "10")?,
            revert_delay_ms: env_or("REVERT_DELAY_MS", "3000").parse()?,
            tick_size: env_or("TICK_SIZE", "0.01"),

            ws_ping_interval_secs: env_or("WS_PING_INTERVAL_SECS", "10").parse()?,
            dry_run: env_or("DRY_RUN", "true").parse()?,
            log_level: env_or("LOG_LEVEL", "info"),

            http_port: env_or("HTTP_PORT", "3000").parse()?,
        })
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
