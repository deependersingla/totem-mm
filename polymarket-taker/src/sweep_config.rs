//! Sweep-specific configuration.
//!
//! Loads from `sweep.env` + `sweep_settings.json` — completely independent
//! from the taker's `.env` / `settings.json`. Own wallet, own market, own keys.

use anyhow::Result;
use ethers::core::k256::ecdsa::SigningKey;
use ethers::signers::{LocalWallet, Signer};
use ethers::types::Address;
use ethers::utils::keccak256;
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use serde::{Deserialize, Serialize};
use std::path::Path;
use std::str::FromStr;

// Polymarket proxy wallet factory constants (Polygon mainnet).
// Proxy addresses are derived deterministically via CREATE2 — no on-chain call needed.

/// Non-Safe proxy factory (signature_type=1, POLY_PROXY / Magic/email accounts)
const PROXY_FACTORY: &str = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052";
const PROXY_INIT_CODE_HASH: &str = "d21df8dc65880a8606f09fe0ce3df9b8869287ab0b058be05aa9e8af6330a00b";

/// Safe proxy factory (signature_type=2, GNOSIS_SAFE / browser wallets)
const SAFE_FACTORY: &str = "0xaacFeEa03eb1561C4e67d661e40682Bd20E3541b";
const SAFE_INIT_CODE_HASH: &str = "2bce2127ff07fb632d16c8347c4ebf501f4841168bed00d9e6ef715ddb6fcecf";

const SETTINGS_FILE: &str = "sweep_settings.json";
const ENV_FILE: &str = "sweep.env";

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct SweepSavedSettings {
    pub polymarket_private_key: Option<String>,
    pub polymarket_address: Option<String>,
    pub signature_type: Option<u8>,
    pub neg_risk: Option<bool>,
    pub team_a_name: Option<String>,
    pub team_b_name: Option<String>,
    pub team_a_token_id: Option<String>,
    pub team_b_token_id: Option<String>,
    pub condition_id: Option<String>,
    pub market_slug: Option<String>,
    pub tick_size: Option<String>,
    pub order_min_size: Option<f64>,
    pub dry_run: Option<bool>,
    pub api_key: Option<String>,
    pub api_secret: Option<String>,
    pub api_passphrase: Option<String>,
    pub builder_api_key: Option<String>,
    pub builder_api_secret: Option<String>,
    pub builder_api_passphrase: Option<String>,
    pub sweep_budget_usdc: Option<String>,
    pub grid_levels: Option<usize>,
    pub refresh_interval_secs: Option<u64>,
    pub http_port: Option<u16>,
}

impl SweepSavedSettings {
    pub fn load() -> Self {
        let path = Path::new(SETTINGS_FILE);
        if path.exists() {
            if let Ok(contents) = std::fs::read_to_string(path) {
                if let Ok(s) = serde_json::from_str(&contents) {
                    return s;
                }
            }
        }
        Self::default()
    }

    pub fn save(&self) {
        if let Ok(json) = serde_json::to_string_pretty(self) {
            if let Err(e) = std::fs::write(SETTINGS_FILE, json) {
                tracing::warn!("failed to write {SETTINGS_FILE}: {e}");
            }
        }
    }

    pub fn from_config(config: &SweepAppConfig) -> Self {
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
            market_slug: Some(config.market_slug.clone()).filter(|s| !s.is_empty()),
            tick_size: Some(config.tick_size.clone()),
            order_min_size: Some(config.order_min_size.to_string().parse().unwrap_or(1.0)),
            dry_run: Some(config.dry_run),
            api_key: Some(config.api_key.clone()).filter(|s| !s.is_empty()),
            api_secret: Some(config.api_secret.clone()).filter(|s| !s.is_empty()),
            api_passphrase: Some(config.api_passphrase.clone()).filter(|s| !s.is_empty()),
            builder_api_key: Some(config.builder_api_key.clone()).filter(|s| !s.is_empty()),
            builder_api_secret: Some(config.builder_api_secret.clone()).filter(|s| !s.is_empty()),
            builder_api_passphrase: Some(config.builder_api_passphrase.clone()).filter(|s| !s.is_empty()),
            sweep_budget_usdc: Some(config.sweep_budget_usdc.to_string()),
            grid_levels: Some(config.grid_levels),
            refresh_interval_secs: Some(config.refresh_interval_secs),
            http_port: Some(config.http_port),
        }
    }
}

/// Sweep binary's own config — mirrors the fields sweep needs from the taker Config,
/// but loads from its own env/settings files. Implements the same interface that
/// shared modules (orders, ctf, clob_auth) expect via `config::Config`.
#[derive(Debug, Clone, Serialize)]
pub struct SweepAppConfig {
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

    pub tick_size: String,
    pub order_min_size: Decimal,

    pub dry_run: bool,
    pub log_level: String,
    pub http_port: u16,

    #[serde(skip)]
    pub api_key: String,
    #[serde(skip)]
    pub api_secret: String,
    #[serde(skip)]
    pub api_passphrase: String,

    pub market_slug: String,

    #[serde(skip)]
    pub builder_api_key: String,
    #[serde(skip)]
    pub builder_api_secret: String,
    #[serde(skip)]
    pub builder_api_passphrase: String,

    pub sweep_budget_usdc: Decimal,
    pub grid_levels: usize,
    pub refresh_interval_secs: u64,
}

impl SweepAppConfig {
    pub fn from_env() -> Result<Self> {
        // Load sweep.env ONLY — fully independent wallet from taker
        dotenvy::from_filename(ENV_FILE).ok();
        let saved = SweepSavedSettings::load();

        let mut cfg = Self {
            polymarket_private_key: saved.polymarket_private_key
                .unwrap_or_else(|| env_or("POLYMARKET_PRIVATE_KEY", "")),
            // Will be auto-derived from private key below if empty
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

            team_a_name: saved.team_a_name.unwrap_or_else(|| "TEAM_A".into()),
            team_b_name: saved.team_b_name.unwrap_or_else(|| "TEAM_B".into()),
            team_a_token_id: saved.team_a_token_id.unwrap_or_default(),
            team_b_token_id: saved.team_b_token_id.unwrap_or_default(),
            condition_id: saved.condition_id.unwrap_or_default(),

            tick_size: saved.tick_size.unwrap_or_else(|| "0.01".into()),
            order_min_size: saved.order_min_size
                .map(|v| Decimal::from_str(&v.to_string()).unwrap_or(Decimal::ONE))
                .unwrap_or(Decimal::ONE),

            dry_run: saved.dry_run.unwrap_or(true),
            log_level: env_or("LOG_LEVEL", "info"),
            http_port: saved.http_port
                .unwrap_or_else(|| env_or("HTTP_PORT", "3001").parse().unwrap_or(3001)),

            api_key: saved.api_key.unwrap_or_default(),
            api_secret: saved.api_secret.unwrap_or_default(),
            api_passphrase: saved.api_passphrase.unwrap_or_default(),

            market_slug: saved.market_slug.unwrap_or_default(),

            builder_api_key: saved.builder_api_key
                .unwrap_or_else(|| env_or("POLYMARKET_BUILDER_API_KEY", "")),
            builder_api_secret: saved.builder_api_secret
                .unwrap_or_else(|| env_or("POLYMARKET_BUILDER_SECRET", "")),
            builder_api_passphrase: saved.builder_api_passphrase
                .unwrap_or_else(|| env_or("POLYMARKET_BUILDER_PASSPHRASE", "")),

            sweep_budget_usdc: saved.sweep_budget_usdc
                .and_then(|s| Decimal::from_str(&s).ok())
                .unwrap_or(dec!(50)),
            grid_levels: saved.grid_levels.unwrap_or(4),
            refresh_interval_secs: saved.refresh_interval_secs.unwrap_or(30),
        };

        // Auto-derive proxy address if private key is set but proxy is empty
        if cfg.has_wallet() && cfg.polymarket_address.is_empty() && cfg.signature_type > 0 {
            cfg.auto_derive_proxy();
            tracing::info!(
                eoa = ?cfg.eoa_address(),
                proxy = %cfg.polymarket_address,
                sig_type = cfg.signature_type,
                "auto-derived proxy address from private key"
            );
        }

        Ok(cfg)
    }

    pub fn persist(&self) {
        SweepSavedSettings::from_config(self).save();
    }

    pub fn has_wallet(&self) -> bool {
        !self.polymarket_private_key.is_empty()
            && self.polymarket_private_key != "0x0000000000000000000000000000000000000000000000000000000000000001"
    }

    pub fn has_tokens(&self) -> bool {
        !self.team_a_token_id.is_empty() && !self.team_b_token_id.is_empty()
    }

    /// Derive EOA address directly from the private key — no ClobAuth needed.
    pub fn eoa_address(&self) -> Option<String> {
        if !self.has_wallet() { return None; }
        let key = self.polymarket_private_key.strip_prefix("0x")
            .unwrap_or(&self.polymarket_private_key);
        let key_bytes = hex::decode(key).ok()?;
        let signing_key = SigningKey::from_bytes(key_bytes.as_slice().into()).ok()?;
        let wallet = LocalWallet::from(signing_key);
        // Use checksum format (EIP-55) for display
        Some(format!("{:?}", wallet.address()))
    }

    /// Derive Polymarket proxy wallet address from EOA via CREATE2.
    /// sig_type=1 → non-Safe proxy factory, sig_type=2 → Safe factory.
    /// sig_type=0 → no proxy (EOA direct), returns None.
    pub fn derive_proxy_address(&self) -> Option<String> {
        let eoa_str = self.eoa_address()?;
        let eoa: Address = eoa_str.parse().ok()?;

        let (factory_str, init_hash_hex) = match self.signature_type {
            1 => (PROXY_FACTORY, PROXY_INIT_CODE_HASH),
            2 => (SAFE_FACTORY, SAFE_INIT_CODE_HASH),
            _ => return None, // EOA mode, no proxy
        };

        let factory: Address = factory_str.parse().ok()?;
        let init_code_hash = hex::decode(init_hash_hex).ok()?;

        // salt = keccak256(abi.encode(eoa)) — address left-padded to 32 bytes
        let mut salt_input = [0u8; 32];
        salt_input[12..32].copy_from_slice(eoa.as_bytes());
        let salt = keccak256(salt_input);

        // CREATE2: keccak256(0xff ++ factory ++ salt ++ init_code_hash)[12:]
        let mut create2_input = Vec::with_capacity(1 + 20 + 32 + 32);
        create2_input.push(0xff);
        create2_input.extend_from_slice(factory.as_bytes());
        create2_input.extend_from_slice(&salt);
        create2_input.extend_from_slice(&init_code_hash);

        let hash = keccak256(&create2_input);
        let proxy: Address = Address::from_slice(&hash[12..]);
        Some(format!("{:?}", proxy))
    }

    /// Auto-derive and set the proxy address from the private key.
    /// Called when wallet is saved — fills in polymarket_address automatically.
    pub fn auto_derive_proxy(&mut self) {
        if self.signature_type == 0 {
            // EOA mode — no proxy
            self.polymarket_address = String::new();
            return;
        }
        if let Some(proxy) = self.derive_proxy_address() {
            self.polymarket_address = proxy;
        }
    }

    /// Convert to the shared `config::Config` that orders/ctf/clob_auth modules expect.
    /// This avoids duplicating all the shared module interfaces.
    pub fn to_shared_config(&self) -> crate::config::Config {
        crate::config::Config {
            polymarket_private_key: self.polymarket_private_key.clone(),
            polymarket_address: self.polymarket_address.clone(),
            signature_type: self.signature_type,
            neg_risk: self.neg_risk,
            chain_id: self.chain_id,
            polygon_rpc: self.polygon_rpc.clone(),
            clob_http: self.clob_http.clone(),
            clob_ws: self.clob_ws.clone(),
            team_a_name: self.team_a_name.clone(),
            team_b_name: self.team_b_name.clone(),
            team_a_token_id: self.team_a_token_id.clone(),
            team_b_token_id: self.team_b_token_id.clone(),
            condition_id: self.condition_id.clone(),
            first_batting: crate::types::Team::TeamA,
            total_budget_usdc: self.sweep_budget_usdc,
            max_trade_usdc: self.sweep_budget_usdc,
            safe_percentage: 2,
            revert_delay_ms: 3000,
            fill_poll_interval_ms: 500,
            fill_poll_timeout_ms: 10000,
            tick_size: self.tick_size.clone(),
            order_min_size: self.order_min_size,
            ws_ping_interval_secs: 10,
            dry_run: self.dry_run,
            log_level: self.log_level.clone(),
            http_port: self.http_port,
            api_key: self.api_key.clone(),
            api_secret: self.api_secret.clone(),
            api_passphrase: self.api_passphrase.clone(),
            market_slug: self.market_slug.clone(),
            edge_wicket: 0.0,
            edge_boundary_4: 0.0,
            edge_boundary_6: 0.0,
            fill_ws_timeout_ms: 5000,
            maker_config: crate::config::MakerConfig::default(),
            builder_api_key: self.builder_api_key.clone(),
            builder_api_secret: self.builder_api_secret.clone(),
            builder_api_passphrase: self.builder_api_passphrase.clone(),
        }
    }
}

fn env_or(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.to_string())
}
