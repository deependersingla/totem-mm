//! Polymarket CLOB V2 order signing primitives.
//!
//! Mirrors py-clob-client-v2's `exchange_order_builder_v2.py` exactly. Cross-checked
//! against `tests/fixtures/v2_signing_vectors.json` (generated via
//! `scripts/gen_v2_test_vectors.py` and verified against `eth_account.encode_typed_data`).
//!
//! V1 → V2 changes (signed-struct level):
//!   - removed: `taker`, `expiration`, `nonce`, `feeRateBps`
//!   - added:   `timestamp` (uint256, ms), `metadata` (bytes32), `builder` (bytes32)
//!   - domain version: "1" → "2" (Exchange domain only; ClobAuth login domain stays "1")
//!
//! Note: `expiration` is still in the JSON body sent to POST /order (server-side
//! metadata) but is NOT part of the EIP-712 struct hash.

use anyhow::{Context, Result};
use ethers::core::k256::ecdsa::SigningKey;
use ethers::signers::LocalWallet;
use ethers::types::{Address, Signature, H256, U256};
use ethers::utils::keccak256;
use serde::{Deserialize, Serialize};

/// V2 EIP-712 Order type string. The keccak256 of this is the V2 type hash.
pub const ORDER_TYPE_STRING: &[u8] = b"Order(uint256 salt,address maker,address signer,uint256 tokenId,uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,uint256 timestamp,bytes32 metadata,bytes32 builder)";

/// EIP-712 domain type string. Same shape as V1; only `version_hash` differs.
pub const DOMAIN_TYPE_STRING: &[u8] = b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)";

/// Domain name — identical for standard and neg-risk exchanges.
pub const DOMAIN_NAME: &[u8] = b"Polymarket CTF Exchange";

/// V2 domain version (was "1" in V1).
pub const DOMAIN_VERSION: &[u8] = b"2";

/// V2 Exchange contract addresses (Polygon mainnet, chain 137).
/// Source: py-clob-client-v2/config.py.
pub const EXCHANGE_V2_STANDARD: &str = "0xE111180000d2663C0091e4f400237545B87B996B";
pub const EXCHANGE_V2_NEG_RISK: &str = "0xe2222d279d744050d28e00520010520000310F59";

/// Resolve V2 exchange address based on neg-risk flag.
pub fn exchange_v2_address(neg_risk: bool) -> &'static str {
    if neg_risk { EXCHANGE_V2_NEG_RISK } else { EXCHANGE_V2_STANDARD }
}

/// V2 Order struct. Field order matches the EIP-712 type string.
///
/// `expiration` is included for JSON serialization but is NOT included in the
/// signed struct hash (V2 removed it from the on-chain commitment).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrderV2 {
    /// Random salt as decimal string (uint256).
    pub salt: String,
    /// Funds-holding address (proxy wallet for signature_type=1, EOA for type=0).
    pub maker: String,
    /// Signing EOA address.
    pub signer: String,
    /// CTF outcome token id as decimal string.
    pub token_id: String,
    pub maker_amount: String,
    pub taker_amount: String,
    /// 0 = BUY, 1 = SELL.
    pub side: u8,
    /// 0 = EOA, 1 = POLY_PROXY, 2 = POLY_GNOSIS_SAFE, 3 = POLY_1271.
    pub signature_type: u8,
    /// Order creation time in ms (uint256).
    pub timestamp: String,
    /// 32-byte hex (0x-prefixed). Default all-zero.
    pub metadata: String,
    /// 32-byte hex builderCode (0x-prefixed). Zero if no builder attribution.
    pub builder: String,
    /// Server-side expiration (NOT in struct hash). "0" for never-expires.
    pub expiration: String,
}

/// keccak256 of [`ORDER_TYPE_STRING`].
pub fn order_type_hash() -> [u8; 32] {
    keccak256(ORDER_TYPE_STRING)
}

/// EIP-712 domain separator for the V2 Exchange.
///
/// `verifying_contract` should be one of [`EXCHANGE_V2_STANDARD`] or
/// [`EXCHANGE_V2_NEG_RISK`].
pub fn domain_separator(chain_id: u64, verifying_contract: &str) -> [u8; 32] {
    let type_hash = keccak256(DOMAIN_TYPE_STRING);
    let name_hash = keccak256(DOMAIN_NAME);
    let version_hash = keccak256(DOMAIN_VERSION);

    let mut chain_buf = [0u8; 32];
    chain_buf[24..].copy_from_slice(&chain_id.to_be_bytes());

    let addr: Address = verifying_contract.parse().unwrap_or_default();
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

/// EIP-712 struct hash of an [`OrderV2`].
pub fn order_struct_hash(order: &OrderV2) -> [u8; 32] {
    let mut buf = Vec::with_capacity(12 * 32);
    buf.extend_from_slice(&order_type_hash());
    buf.extend_from_slice(&pad_uint256_dec(&order.salt));
    buf.extend_from_slice(&pad_address(&order.maker));
    buf.extend_from_slice(&pad_address(&order.signer));
    buf.extend_from_slice(&pad_uint256_dec(&order.token_id));
    buf.extend_from_slice(&pad_uint256_dec(&order.maker_amount));
    buf.extend_from_slice(&pad_uint256_dec(&order.taker_amount));
    buf.extend_from_slice(&pad_uint8(order.side));
    buf.extend_from_slice(&pad_uint8(order.signature_type));
    buf.extend_from_slice(&pad_uint256_dec(&order.timestamp));
    buf.extend_from_slice(&parse_bytes32(&order.metadata));
    buf.extend_from_slice(&parse_bytes32(&order.builder));
    keccak256(buf)
}

fn pad_uint256_dec(s: &str) -> [u8; 32] {
    let v = U256::from_dec_str(s.trim()).unwrap_or_else(|_| {
        let stripped = s.trim().strip_prefix("0x").unwrap_or(s.trim());
        U256::from_str_radix(stripped, 16).unwrap_or(U256::zero())
    });
    let mut out = [0u8; 32];
    v.to_big_endian(&mut out);
    out
}

fn pad_address(a: &str) -> [u8; 32] {
    let addr: Address = a.parse().unwrap_or_default();
    let mut out = [0u8; 32];
    out[12..].copy_from_slice(addr.as_bytes());
    out
}

fn pad_uint8(v: u8) -> [u8; 32] {
    let mut out = [0u8; 32];
    out[31] = v;
    out
}

fn parse_bytes32(hex_str: &str) -> [u8; 32] {
    let stripped = hex_str.trim().strip_prefix("0x").unwrap_or(hex_str.trim());
    let mut padded = String::with_capacity(64);
    if stripped.len() < 64 {
        padded.extend(std::iter::repeat('0').take(64 - stripped.len()));
    }
    padded.push_str(stripped);
    let bytes = hex::decode(&padded).unwrap_or_else(|_| vec![0u8; 32]);
    let mut out = [0u8; 32];
    let n = bytes.len().min(32);
    out[..n].copy_from_slice(&bytes[..n]);
    out
}

/// Final EIP-712 digest: keccak256(0x1901 || domain_separator || struct_hash).
pub fn eip712_digest(domain_sep: &[u8; 32], struct_hash: &[u8; 32]) -> [u8; 32] {
    let mut buf = Vec::with_capacity(2 + 32 + 32);
    buf.extend_from_slice(&[0x19, 0x01]);
    buf.extend_from_slice(domain_sep);
    buf.extend_from_slice(struct_hash);
    keccak256(buf)
}

/// Sign an [`OrderV2`] with the given private key, returning the 65-byte
/// `r || s || v` signature as a 0x-prefixed hex string (v ∈ {27, 28}).
pub fn sign_order(
    privkey_hex: &str,
    order: &OrderV2,
    verifying_contract: &str,
    chain_id: u64,
) -> Result<String> {
    let wallet = wallet_from_privkey(privkey_hex)?;
    let dom = domain_separator(chain_id, verifying_contract);
    let struct_h = order_struct_hash(order);
    let digest = eip712_digest(&dom, &struct_h);
    sign_digest(&digest, &wallet)
}

/// A signed V2 order — pairs an [`OrderV2`] with the EIP-712 signature.
#[derive(Debug, Clone)]
pub struct SignedOrderV2 {
    pub order: OrderV2,
    pub signature: String,
}

impl SignedOrderV2 {
    /// Sign `order` with `privkey_hex` against the given exchange + chain.
    pub fn build(
        privkey_hex: &str,
        order: OrderV2,
        verifying_contract: &str,
        chain_id: u64,
    ) -> Result<Self> {
        let signature = sign_order(privkey_hex, &order, verifying_contract, chain_id)?;
        Ok(Self { order, signature })
    }
}

/// Top-level body for `POST /order`. Mirrors py-clob-client-v2 `order_to_json_v2`.
///
/// Use [`OrderSubmission::new`] to construct from a [`SignedOrderV2`].
#[derive(Debug, Clone, Serialize)]
pub struct OrderSubmission {
    pub order: PostOrderBodyV2,
    pub owner: String,
    #[serde(rename = "orderType")]
    pub order_type: String,
    #[serde(rename = "deferExec")]
    pub defer_exec: bool,
    #[serde(rename = "postOnly")]
    pub post_only: bool,
}

impl OrderSubmission {
    pub fn new(signed: &SignedOrderV2, owner: impl Into<String>, order_type: impl Into<String>) -> Self {
        Self::with_post_only(signed, owner, order_type, false)
    }

    /// Build a submission with the `postOnly` wire flag explicitly set.
    ///
    /// `post_only=true` instructs the matcher to **reject** the order if it
    /// would cross the spread (rather than crossing and becoming a taker).
    /// Used for revert GTCs that must rest as makers; rejection on cross is
    /// detected by [`crate::orders::is_post_only_cross_reject`] and triggers
    /// a fall-back to a plain GTC retry.
    ///
    /// Polymarket V2 only accepts `post_only=true` with `order_type` of
    /// `"GTC"` or `"GTD"` — the matcher rejects FAK + post_only as nonsense.
    pub fn with_post_only(
        signed: &SignedOrderV2,
        owner: impl Into<String>,
        order_type: impl Into<String>,
        post_only: bool,
    ) -> Self {
        Self {
            order: PostOrderBodyV2::from_signed(signed),
            owner: owner.into(),
            order_type: order_type.into(),
            defer_exec: false,
            post_only,
        }
    }
}

/// Inner `"order": { ... }` object in the POST body.
///
/// JSON shape (verified against `tests/fixtures/v2_signing_vectors.json`):
///   - `salt` is a JSON number, every other amount/id is a decimal string
///   - `side` is the uppercase string "BUY" or "SELL" (NOT the int)
///   - `signatureType` is a JSON number
///   - `metadata` and `builder` are 0x-prefixed 32-byte hex strings
///   - `expiration` is in this body but NOT in the signed struct hash
#[derive(Debug, Clone, Serialize)]
pub struct PostOrderBodyV2 {
    #[serde(serialize_with = "serialize_dec_as_int")]
    pub salt: String,
    pub maker: String,
    pub signer: String,
    #[serde(rename = "tokenId")]
    pub token_id: String,
    #[serde(rename = "makerAmount")]
    pub maker_amount: String,
    #[serde(rename = "takerAmount")]
    pub taker_amount: String,
    pub side: String,
    pub expiration: String,
    #[serde(rename = "signatureType")]
    pub signature_type: u8,
    pub timestamp: String,
    pub metadata: String,
    pub builder: String,
    pub signature: String,
}

impl PostOrderBodyV2 {
    fn from_signed(s: &SignedOrderV2) -> Self {
        Self {
            salt: s.order.salt.clone(),
            maker: s.order.maker.clone(),
            signer: s.order.signer.clone(),
            token_id: s.order.token_id.clone(),
            maker_amount: s.order.maker_amount.clone(),
            taker_amount: s.order.taker_amount.clone(),
            side: if s.order.side == 0 { "BUY".into() } else { "SELL".into() },
            expiration: s.order.expiration.clone(),
            signature_type: s.order.signature_type,
            timestamp: s.order.timestamp.clone(),
            metadata: s.order.metadata.clone(),
            builder: s.order.builder.clone(),
            signature: s.signature.clone(),
        }
    }
}

fn serialize_dec_as_int<S>(s: &str, serializer: S) -> std::result::Result<S::Ok, S::Error>
where
    S: serde::Serializer,
{
    // py-clob-client-v2 generates salts as random.random() * time_ms — always u64-safe.
    let n: u64 = s.parse().map_err(serde::ser::Error::custom)?;
    serializer.serialize_u64(n)
}

fn wallet_from_privkey(privkey_hex: &str) -> Result<LocalWallet> {
    let stripped = privkey_hex.trim().strip_prefix("0x").unwrap_or(privkey_hex.trim());
    let key_bytes = hex::decode(stripped).context("decode privkey hex")?;
    let signing_key = SigningKey::from_bytes(key_bytes.as_slice().into())
        .context("invalid privkey bytes")?;
    Ok(LocalWallet::from(signing_key))
}

fn sign_digest(digest: &[u8; 32], wallet: &LocalWallet) -> Result<String> {
    let sig: Signature = wallet
        .sign_hash(H256::from(*digest))
        .context("ECDSA sign")?;
    let mut bytes = [0u8; 65];
    sig.r.to_big_endian(&mut bytes[..32]);
    sig.s.to_big_endian(&mut bytes[32..64]);
    bytes[64] = sig.v as u8;
    Ok(format!("0x{}", hex::encode(bytes)))
}
