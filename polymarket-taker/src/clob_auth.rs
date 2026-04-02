use anyhow::{bail, Result};
use ethers::core::k256::ecdsa::SigningKey;
use ethers::signers::{LocalWallet, Signer};
use ethers::types::{Address, Signature, H256};
use ethers::utils::keccak256;
use hmac::{Hmac, Mac};
use reqwest::header::{HeaderMap, HeaderValue};
use serde::{Deserialize, Serialize};
use sha2::Sha256;
use crate::config::Config;

type HmacSha256 = Hmac<Sha256>;

#[derive(Debug, Clone)]
pub struct ClobAuth {
    pub api_key: String,
    pub api_secret: String,
    pub passphrase: String,
    wallet: LocalWallet,
    address: String,
    /// Funder / proxy wallet address. For signature types 1 (POLY_PROXY) and
    /// 2 (GNOSIS_SAFE), this is the on-chain proxy wallet that holds funds.
    /// For type 0 (EOA), this is empty and address is used instead.
    funder: String,
    http_client: reqwest::Client,
    clob_http: String,
}

#[derive(Debug, Serialize)]
#[allow(dead_code)]
struct L1AuthBody {
    #[serde(rename = "address")]
    address: String,
    #[serde(rename = "timestamp")]
    timestamp: String,
    #[serde(rename = "nonce")]
    nonce: u64,
    #[serde(rename = "message")]
    message: String,
    #[serde(rename = "signature")]
    signature: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ApiCredsResponse {
    api_key: Option<String>,
    secret: Option<String>,
    passphrase: Option<String>,
}

/// EIP-712 domain separator for ClobAuth
fn clob_auth_domain_separator(chain_id: u64) -> [u8; 32] {
    let type_hash = keccak256(b"EIP712Domain(string name,string version,uint256 chainId)");
    let name_hash = keccak256(b"ClobAuthDomain");
    let version_hash = keccak256(b"1");

    let mut encoded = Vec::with_capacity(128);
    encoded.extend_from_slice(&type_hash);
    encoded.extend_from_slice(&name_hash);
    encoded.extend_from_slice(&version_hash);
    let mut chain_buf = [0u8; 32];
    chain_buf[24..].copy_from_slice(&chain_id.to_be_bytes());
    encoded.extend_from_slice(&chain_buf);

    keccak256(encoded)
}

/// EIP-712 struct hash for ClobAuth message
fn clob_auth_struct_hash(address: &str, timestamp: &str, nonce: u64) -> [u8; 32] {
    let type_hash = keccak256(
        b"ClobAuth(address address,string timestamp,uint256 nonce,string message)",
    );
    let msg = "This message attests that I control the given wallet";
    let msg_hash = keccak256(msg.as_bytes());
    let ts_hash = keccak256(timestamp.as_bytes());

    let addr: Address = address.parse().unwrap_or_default();
    let mut addr_buf = [0u8; 32];
    addr_buf[12..].copy_from_slice(addr.as_bytes());

    let mut nonce_buf = [0u8; 32];
    nonce_buf[24..].copy_from_slice(&nonce.to_be_bytes());

    let mut encoded = Vec::with_capacity(192);
    encoded.extend_from_slice(&type_hash);
    encoded.extend_from_slice(&addr_buf);
    encoded.extend_from_slice(&ts_hash);
    encoded.extend_from_slice(&nonce_buf);
    encoded.extend_from_slice(&msg_hash);

    keccak256(encoded)
}

fn sign_eip712_hash(domain_sep: &[u8; 32], struct_hash: &[u8; 32], wallet: &LocalWallet) -> Result<String> {
    let mut digest_input = Vec::with_capacity(66);
    digest_input.extend_from_slice(b"\x19\x01");
    digest_input.extend_from_slice(domain_sep);
    digest_input.extend_from_slice(struct_hash);
    let hash = keccak256(&digest_input);

    let sig: Signature = wallet.sign_hash(H256::from(hash))?;
    let mut sig_bytes = [0u8; 65];
    sig.r.to_big_endian(&mut sig_bytes[0..32]);
    sig.s.to_big_endian(&mut sig_bytes[32..64]);
    sig_bytes[64] = sig.v as u8;
    Ok(format!("0x{}", hex::encode(sig_bytes)))
}

/// Build HMAC-SHA256 signature for L2 auth.
fn build_hmac_signature(
    secret: &str,
    timestamp: &str,
    method: &str,
    request_path: &str,
    body: Option<&str>,
) -> Result<String> {
    use base64::Engine;
    let decoded = base64::engine::general_purpose::URL_SAFE
        .decode(secret)
        .map_err(|e| anyhow::anyhow!("base64 decode error: {e}"))?;

    let mut message = format!("{timestamp}{method}{request_path}");
    if let Some(b) = body {
        message.push_str(b);
    }

    let mut mac =
        HmacSha256::new_from_slice(&decoded).map_err(|e| anyhow::anyhow!("hmac error: {e}"))?;
    mac.update(message.as_bytes());
    let result = mac.finalize().into_bytes();
    Ok(base64::engine::general_purpose::URL_SAFE.encode(result))
}

impl ClobAuth {
    pub async fn derive(config: &Config) -> Result<Self> {
        let key = config.polymarket_private_key.strip_prefix("0x")
            .unwrap_or(&config.polymarket_private_key);
        let key_bytes = hex::decode(key)?;
        let signing_key = SigningKey::from_bytes(key_bytes.as_slice().into())?;
        let wallet = LocalWallet::from(signing_key).with_chain_id(config.chain_id);

        let address = format!("{:#x}", wallet.address());
        let http_client = reqwest::Client::new();

        // If all three CLOB API credentials are pre-configured (pasted from
        // Polymarket.com → Settings → API), skip the L1 derivation entirely.
        // This avoids the EOA→proxy API-key mismatch that causes 401 errors.
        if !config.api_key.is_empty() && !config.api_secret.is_empty() && !config.api_passphrase.is_empty() {
            let funder = if config.signature_type > 0 && !config.polymarket_address.is_empty() {
                config.polymarket_address.clone()
            } else {
                String::new()
            };
            tracing::info!(
                signer = %address,
                funder = if funder.is_empty() { "(same as signer)" } else { &funder },
                "using pre-configured CLOB API credentials (skipping L1 derivation)"
            );
            return Ok(Self {
                api_key: config.api_key.clone(),
                api_secret: config.api_secret.clone(),
                passphrase: config.api_passphrase.clone(),
                wallet,
                address,
                funder,
                http_client,
                clob_http: config.clob_http.clone(),
            });
        }

        // For sig types 1/2, the funder (proxy wallet) is the polymarket_address
        let funder = if config.signature_type > 0 && !config.polymarket_address.is_empty() {
            config.polymarket_address.clone()
        } else {
            String::new()
        };

        tracing::info!(
            signer = %address,
            funder = if funder.is_empty() { "(same as signer)" } else { &funder },
            signature_type = config.signature_type,
            "wallet addresses"
        );

        // L1 auth always uses the EOA address (the actual signing key), even for proxy wallet
        // users. Polymarket's backend knows the EOA→proxy mapping and returns the proxy's API
        // key. The CLOB validates L1 auth by recovering the signer from the signature and
        // comparing it to POLY_ADDRESS — so POLY_ADDRESS must match the signing key (EOA).
        let timestamp = chrono::Utc::now().timestamp().to_string();
        let nonce: u64 = 0;

        let domain_sep = clob_auth_domain_separator(config.chain_id);
        let struct_hash = clob_auth_struct_hash(&address, &timestamp, nonce);
        let signature = sign_eip712_hash(&domain_sep, &struct_hash, &wallet)?;

        let l1_headers = {
            let mut h = HeaderMap::new();
            h.insert("POLY_ADDRESS", HeaderValue::from_str(&address)?);
            h.insert("POLY_SIGNATURE", HeaderValue::from_str(&signature)?);
            h.insert("POLY_TIMESTAMP", HeaderValue::from_str(&timestamp)?);
            h.insert("POLY_NONCE", HeaderValue::from_str(&nonce.to_string())?);
            h
        };

        let derive_url = format!("{}/auth/derive-api-key", config.clob_http);
        tracing::info!("deriving CLOB API key from {derive_url}");

        let resp = http_client
            .get(&derive_url)
            .headers(l1_headers.clone())
            .send()
            .await?;

        let status = resp.status();
        let body_text = resp.text().await?;

        if !status.is_success() {
            let create_url = format!("{}/auth/api-key", config.clob_http);
            tracing::info!("derive failed ({status}), trying create at {create_url}");

            let resp2 = http_client
                .post(&create_url)
                .headers(l1_headers)
                .send()
                .await?;

            let status2 = resp2.status();
            let body2 = resp2.text().await?;

            if !status2.is_success() {
                bail!("failed to create API key: {status2} {body2}");
            }

            let creds: ApiCredsResponse = serde_json::from_str(&body2)?;
            return Ok(Self {
                api_key: creds.api_key.unwrap_or_default(),
                api_secret: creds.secret.unwrap_or_default(),
                passphrase: creds.passphrase.unwrap_or_default(),
                wallet,
                address,
                funder,
                http_client,
                clob_http: config.clob_http.clone(),
            });
        }

        let creds: ApiCredsResponse = serde_json::from_str(&body_text)?;

        tracing::info!("CLOB API key derived successfully");

        Ok(Self {
            api_key: creds.api_key.unwrap_or_default(),
            api_secret: creds.secret.unwrap_or_default(),
            passphrase: creds.passphrase.unwrap_or_default(),
            wallet,
            address,
            funder,
            http_client,
            clob_http: config.clob_http.clone(),
        })
    }

    /// Build L2 headers for authenticated requests (HMAC-signed).
    pub fn l2_headers(&self, method: &str, path: &str, body: Option<&str>) -> Result<HeaderMap> {
        let timestamp = chrono::Utc::now().timestamp().to_string();
        let hmac_sig = build_hmac_signature(&self.api_secret, &timestamp, method, path, body)?;

        let mut headers = HeaderMap::new();
        headers.insert("POLY_ADDRESS", HeaderValue::from_str(&self.address)?);
        headers.insert("POLY_SIGNATURE", HeaderValue::from_str(&hmac_sig)?);
        headers.insert("POLY_TIMESTAMP", HeaderValue::from_str(&timestamp)?);
        headers.insert("POLY_API_KEY", HeaderValue::from_str(&self.api_key)?);
        headers.insert("POLY_PASSPHRASE", HeaderValue::from_str(&self.passphrase)?);
        Ok(headers)
    }

    /// Sign an order using EIP-712 (Order struct for CTF Exchange)
    pub fn sign_order(&self, order_hash: &[u8; 32], exchange_address: &str, chain_id: u64, neg_risk: bool) -> Result<String> {
        let domain_sep = order_domain_separator(chain_id, exchange_address, neg_risk);
        tracing::debug!(
            domain_sep = %format!("0x{}", hex::encode(domain_sep)),
            exchange = exchange_address,
            chain_id,
            signer = %self.address,
            "EIP-712 order domain separator"
        );
        sign_eip712_hash(&domain_sep, order_hash, &self.wallet)
    }

    pub fn wallet(&self) -> &LocalWallet {
        &self.wallet
    }

    pub fn clob_http_url(&self) -> &str {
        &self.clob_http
    }

    pub fn http_client(&self) -> &reqwest::Client {
        &self.http_client
    }

    pub fn address(&self) -> &str {
        &self.address
    }

    /// Returns the funder (proxy wallet) address if set, otherwise the EOA.
    pub fn funder_address(&self) -> &str {
        if !self.funder.is_empty() {
            &self.funder
        } else {
            &self.address
        }
    }
}

/// EIP-712 domain separator for Polymarket CTF Exchange orders.
/// The domain name is "Polymarket CTF Exchange" for BOTH standard and neg-risk
/// exchanges. Only the verifyingContract address differs.
fn order_domain_separator(chain_id: u64, exchange_address: &str, _neg_risk: bool) -> [u8; 32] {
    let type_hash = keccak256(
        b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)",
    );
    let name_hash = keccak256(b"Polymarket CTF Exchange");
    let version_hash = keccak256(b"1");

    let mut chain_buf = [0u8; 32];
    chain_buf[24..].copy_from_slice(&chain_id.to_be_bytes());

    let addr: Address = exchange_address.parse().unwrap_or_default();
    let mut addr_buf = [0u8; 32];
    addr_buf[12..].copy_from_slice(addr.as_bytes());

    let mut encoded = Vec::with_capacity(160);
    encoded.extend_from_slice(&type_hash);
    encoded.extend_from_slice(&name_hash);
    encoded.extend_from_slice(&version_hash);
    encoded.extend_from_slice(&chain_buf);
    encoded.extend_from_slice(&addr_buf);

    keccak256(encoded)
}
