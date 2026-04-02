use anyhow::Result;
use ethers::types::Address;
use ethers::utils::keccak256;
use rand::Rng;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use std::str::FromStr;
use crate::clob_auth::ClobAuth;
use crate::config::Config;
use crate::types::{FakOrder, Side};

const USDC_DECIMALS: u64 = 1_000_000;

/// Fee rate in basis points. Must match what the market expects.
/// Sports markets use 1000 (10%). Fetched from Gamma API during market setup.
fn fee_rate_bps(config: &Config) -> u32 {
    config.fee_rate_bps
}

fn side_to_u8(side: Side) -> u8 {
    match side {
        Side::Buy => 0,
        Side::Sell => 1,
    }
}

/// Convert a Decimal amount to 6-decimal base units (USDC / CTF token precision).
/// Uses Decimal::floor() for exact integer truncation — avoids the f64 precision
/// loss that the previous implementation had (f64 can't represent many 6-decimal
/// values exactly, causing off-by-one errors in order amounts).
pub(crate) fn to_base_units(amount: Decimal) -> u128 {
    let scaled = (amount * Decimal::from(USDC_DECIMALS)).floor();
    scaled.to_string().parse::<u128>().unwrap_or(0)
}

pub(crate) fn compute_amounts(side: Side, price: Decimal, size: Decimal) -> (String, String) {
    match side {
        Side::Buy => {
            let taker_amount = to_base_units(size);
            let maker_amount = to_base_units(size * price);
            (maker_amount.to_string(), taker_amount.to_string())
        }
        Side::Sell => {
            let maker_amount = to_base_units(size);
            let taker_amount = to_base_units(size * price);
            (maker_amount.to_string(), taker_amount.to_string())
        }
    }
}

pub(crate) fn order_struct_hash(order: &ClobOrder) -> [u8; 32] {
    let type_hash = keccak256(
        b"Order(uint256 salt,address maker,address signer,address taker,uint256 tokenId,uint256 makerAmount,uint256 takerAmount,uint256 expiration,uint256 nonce,uint256 feeRateBps,uint8 side,uint8 signatureType)",
    );

    fn pad_u256(val: &str) -> [u8; 32] {
        use ethers::types::U256;
        let v = U256::from_dec_str(val)
            .or_else(|_| {
                let s = val.strip_prefix("0x").unwrap_or(val);
                U256::from_str_radix(s, 16).map_err(|_| ())
            })
            .unwrap_or(U256::zero());
        let mut buf = [0u8; 32];
        v.to_big_endian(&mut buf);
        buf
    }

    fn pad_address(addr: &str) -> [u8; 32] {
        let a: Address = addr.parse().unwrap_or_default();
        let mut buf = [0u8; 32];
        buf[12..].copy_from_slice(a.as_bytes());
        buf
    }

    fn pad_u8(val: u8) -> [u8; 32] {
        let mut buf = [0u8; 32];
        buf[31] = val;
        buf
    }

    let mut encoded = Vec::with_capacity(13 * 32);
    encoded.extend_from_slice(&type_hash);
    encoded.extend_from_slice(&pad_u256(&order.salt));
    encoded.extend_from_slice(&pad_address(&order.maker));
    encoded.extend_from_slice(&pad_address(&order.signer));
    encoded.extend_from_slice(&pad_address(&order.taker));
    encoded.extend_from_slice(&pad_u256(&order.token_id));
    encoded.extend_from_slice(&pad_u256(&order.maker_amount));
    encoded.extend_from_slice(&pad_u256(&order.taker_amount));
    encoded.extend_from_slice(&pad_u256(&order.expiration));
    encoded.extend_from_slice(&pad_u256(&order.nonce));
    encoded.extend_from_slice(&pad_u256(&order.fee_rate_bps));
    encoded.extend_from_slice(&pad_u8(order.side));
    encoded.extend_from_slice(&pad_u8(order.signature_type));

    keccak256(encoded)
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct ClobOrder {
    pub salt: String,
    pub maker: String,
    pub signer: String,
    pub taker: String,
    pub token_id: String,
    pub maker_amount: String,
    pub taker_amount: String,
    pub side: u8,
    pub expiration: String,
    pub nonce: String,
    pub fee_rate_bps: String,
    pub signature_type: u8,
    pub signature: String,
}

/// Single-order request body — POST /order
#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct PostOrderRequest {
    order: PostOrderBody,
    owner: String,
    order_type: String,
    tick_size: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct BatchOrderItem {
    order: PostOrderBody,
    owner: String,
    order_type: String,
    tick_size: String,
    defer_exec: bool,
}

/// Response from POST /orders — one element per submitted order.
/// API also returns makingAmount, takingAmount, transactionsHashes, tradeIDs when relevant.
/// See: https://docs.polymarket.com/api-reference/trade/post-multiple-orders
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct BatchOrderResult {
    pub success: Option<bool>,
    #[serde(rename = "orderID")]
    pub order_id: Option<String>,
    pub status: Option<String>,
    pub error_msg: Option<String>,
}

/// Serialize salt as JSON number; API expects integer, not string.
fn serialize_salt_as_int<S>(s: &str, serializer: S) -> Result<S::Ok, S::Error>
where
    S: serde::Serializer,
{
    let n: u64 = s.parse().unwrap_or(0);
    serializer.serialize_u64(n)
}

/// Pass through the token ID as-is for the API payload.
/// The CLOB indexes orderbooks by the original token ID string (usually decimal).
/// The EIP-712 struct hash handles the uint256 encoding internally.
fn token_id_for_api(s: &str) -> String {
    s.trim().to_string()
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct PostOrderBody {
    #[serde(serialize_with = "serialize_salt_as_int")]
    salt: String,
    maker: String,
    signer: String,
    taker: String,
    token_id: String,
    maker_amount: String,
    taker_amount: String,
    side: String,
    expiration: String,
    nonce: String,
    fee_rate_bps: String,
    signature_type: u8,
    signature: String,
}

impl From<&ClobOrder> for PostOrderBody {
    fn from(o: &ClobOrder) -> Self {
        Self {
            salt: o.salt.clone(),
            maker: o.maker.clone(),
            signer: o.signer.clone(),
            taker: o.taker.clone(),
            token_id: token_id_for_api(&o.token_id),
            maker_amount: o.maker_amount.clone(),
            taker_amount: o.taker_amount.clone(),
            side: if o.side == 0 { "BUY".into() } else { "SELL".into() },
            expiration: o.expiration.clone(),
            nonce: o.nonce.clone(),
            fee_rate_bps: o.fee_rate_bps.clone(),
            signature_type: o.signature_type,
            signature: o.signature.clone(),
        }
    }
}

#[derive(Debug, Deserialize)]
pub struct PostOrderResponse {
    #[serde(rename = "orderID")]
    pub order_id: Option<String>,
    #[serde(alias = "errorMsg", alias = "error")]
    pub error_msg: Option<String>,
    pub status: Option<String>,
}

/// Internal helper that builds and signs a CLOB order with a given expiration.
/// `expiration` should be "0" for GTC/FAK or a unix timestamp string for GTD.
fn build_signed_order_inner(
    config: &Config,
    auth: &ClobAuth,
    order: &FakOrder,
    expiration: &str,
) -> Result<ClobOrder> {
    // Salt must fit in IEEE 754 double precision (backend parses as float).
    // Mask to <= 2^53 - 1 to avoid precision loss.
    let salt: u64 = rand::thread_rng().gen::<u64>() & ((1u64 << 53) - 1);
    let token_id = config.token_id(order.team).to_string();
    let (maker_amount, taker_amount) = compute_amounts(order.side, order.price, order.size);

    let signer_addr = auth.address().to_string();
    // maker = who provides the funds:
    //   type 0 (EOA):         maker == signer (both are the EOA)
    //   type 1 (POLY_PROXY):  maker == funder/proxy wallet address
    //   type 2 (GNOSIS_SAFE): maker == funder/proxy wallet address
    let maker_addr = auth.funder_address().to_string();

    let fee_rate_bps = fee_rate_bps(config);

    tracing::debug!(
        maker = %maker_addr,
        signer = %signer_addr,
        signature_type = config.signature_type,
        token_id = %token_id,
        side = %order.side,
        fee_rate_bps,
        "building signed order"
    );

    let mut clob_order = ClobOrder {
        salt: salt.to_string(),
        maker: maker_addr,
        signer: signer_addr,
        taker: "0x0000000000000000000000000000000000000000".to_string(),
        token_id,
        maker_amount,
        taker_amount,
        side: side_to_u8(order.side),
        expiration: expiration.to_string(),
        nonce: "0".to_string(),
        fee_rate_bps: fee_rate_bps.to_string(),
        signature_type: config.signature_type,
        signature: String::new(),
    };

    let struct_hash = order_struct_hash(&clob_order);
    let exchange = config.exchange_address();

    tracing::info!(
        struct_hash = %format!("0x{}", hex::encode(struct_hash)),
        exchange,
        chain_id = config.chain_id,
        neg_risk = config.neg_risk,
        salt = %clob_order.salt,
        maker = %clob_order.maker,
        signer = %clob_order.signer,
        token_id = %clob_order.token_id,
        maker_amount = %clob_order.maker_amount,
        taker_amount = %clob_order.taker_amount,
        side = clob_order.side,
        fee_rate_bps = %clob_order.fee_rate_bps,
        signature_type = clob_order.signature_type,
        "order EIP-712 signing"
    );

    let signature = auth.sign_order(&struct_hash, exchange, config.chain_id, config.neg_risk)?;
    clob_order.signature = signature;

    Ok(clob_order)
}

fn build_signed_order(config: &Config, auth: &ClobAuth, order: &FakOrder) -> Result<ClobOrder> {
    build_signed_order_inner(config, auth, order, "0")
}

/// Build a signed order with a GTD expiration (unix timestamp = now + expiry_secs).
fn build_signed_order_with_expiry(
    config: &Config,
    auth: &ClobAuth,
    order: &FakOrder,
    expiry_secs: u64,
) -> Result<ClobOrder> {
    let now_unix = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs();
    let expiration = (now_unix + expiry_secs).to_string();
    build_signed_order_inner(config, auth, order, &expiration)
}

async fn post_order(
    config: &Config,
    auth: &ClobAuth,
    clob_order: &ClobOrder,
    order_type: &str,
    tag: &str,
) -> Result<PostOrderResponse> {
    let body = PostOrderRequest {
        order: PostOrderBody::from(clob_order),
        owner: auth.api_key.clone(),
        order_type: order_type.to_string(),
        tick_size: config.tick_size.clone(),
    };

    let body_json = serde_json::to_string(&body)?;
    let path = "/order";
    let headers = auth.l2_headers("POST", path, Some(&body_json))?;
    let url = format!("{}{}", auth.clob_http_url(), path);

    let resp = auth
        .http_client()
        .post(&url)
        .headers(headers)
        .header("Content-Type", "application/json")
        .body(body_json)
        .send()
        .await?;

    let status = resp.status();
    let resp_body = resp.text().await?;

    if !status.is_success() {
        tracing::warn!(tag, status = %status, body = resp_body, "order HTTP error");
    }

    let result: PostOrderResponse = serde_json::from_str(&resp_body)
        .unwrap_or(PostOrderResponse {
            order_id: None,
            error_msg: Some(resp_body),
            status: None,
        });

    if let Some(ref oid) = result.order_id {
        tracing::info!(tag, order_id = oid, "order accepted");
    }
    if let Some(ref err) = result.error_msg {
        if result.order_id.is_none() {
            tracing::warn!(tag, error = err, "order rejected");
        }
    }

    Ok(result)
}

pub async fn post_fak_order(
    config: &Config,
    auth: &ClobAuth,
    order: &FakOrder,
    tag: &str,
) -> Result<PostOrderResponse> {
    tracing::info!(tag, side = %order.side, team = %config.team_name(order.team),
        price = %order.price, size = %order.size, "posting FAK order");

    let signed = build_signed_order(config, auth, order)?;
    post_order(config, auth, &signed, "FAK", tag).await
}

/// Post multiple FAK orders in a single HTTP call (POST /orders).
pub async fn post_fak_orders_batch(
    config: &Config,
    auth: &ClobAuth,
    orders: &[(FakOrder, &str)],
) -> Result<Vec<BatchOrderResult>> {
    let mut items = Vec::with_capacity(orders.len());
    for (order, tag) in orders {
        tracing::info!(tag, side = %order.side, team = %config.team_name(order.team),
            price = %order.price, size = %order.size, "batch FAK order");
        let signed = build_signed_order(config, auth, order)?;
        items.push(BatchOrderItem {
            order: PostOrderBody::from(&signed),
            owner: auth.api_key.clone(),
            order_type: "FAK".to_string(),
            tick_size: config.tick_size.clone(),
            defer_exec: false,
        });
    }
    let body_json = serde_json::to_string(&items)?;
    let path = "/orders";
    let headers = auth.l2_headers("POST", path, Some(&body_json))?;
    let url = format!("{}{}", auth.clob_http_url(), path);

    tracing::debug!(
        payload = %body_json,
        owner = %auth.api_key,
        "batch order request"
    );

    let resp = auth
        .http_client()
        .post(&url)
        .headers(headers)
        .header("Content-Type", "application/json")
        .body(body_json.clone())
        .send()
        .await?;

    let status = resp.status();
    let resp_body = resp.text().await?;

    if !status.is_success() {
        tracing::warn!(status = %status, body = resp_body, "batch order HTTP error");
        tracing::warn!(payload = %body_json, "batch order request payload (rejected)");
        anyhow::bail!("batch orders HTTP {status}: {resp_body}");
    }

    let results: Vec<BatchOrderResult> = serde_json::from_str(&resp_body).unwrap_or_else(|e| {
        tracing::warn!(error = %e, body = resp_body, "failed to parse batch order response");
        Vec::new()
    });

    for (i, r) in results.iter().enumerate() {
        let tag = orders.get(i).map(|(_, t)| *t).unwrap_or("?");
        let err = r.error_msg.as_deref().unwrap_or("");
        let oid = r.order_id.as_deref().filter(|s| !s.is_empty());
        if !err.is_empty() {
            tracing::warn!(tag, error = err, "batch order rejected");
            if err.contains("orderbook") && err.contains("does not exist") {
                tracing::info!("hint: if this market is neg-risk, set neg_risk=true in config (or try neg_risk=false)");
            }
        } else if let Some(oid) = oid {
            tracing::info!(tag, order_id = oid, status = ?r.status, "batch order accepted");
        }
    }

    Ok(results)
}

/// Post multiple GTC orders in a single HTTP call (POST /orders). Max 15 per batch.
pub async fn post_gtc_orders_batch(
    config: &Config,
    auth: &ClobAuth,
    orders: &[(FakOrder, &str)],
) -> Result<Vec<BatchOrderResult>> {
    let mut items = Vec::with_capacity(orders.len());
    for (order, tag) in orders {
        tracing::info!(tag, side = %order.side, team = %config.team_name(order.team),
            price = %order.price, size = %order.size, "batch GTC order");
        let signed = build_signed_order(config, auth, order)?;
        items.push(BatchOrderItem {
            order: PostOrderBody::from(&signed),
            owner: auth.api_key.clone(),
            order_type: "GTC".to_string(),
            tick_size: config.tick_size.clone(),
            defer_exec: false,
        });
    }
    let body_json = serde_json::to_string(&items)?;
    let path = "/orders";
    let headers = auth.l2_headers("POST", path, Some(&body_json))?;
    let url = format!("{}{}", auth.clob_http_url(), path);

    let resp = auth
        .http_client()
        .post(&url)
        .headers(headers)
        .header("Content-Type", "application/json")
        .body(body_json.clone())
        .send()
        .await?;

    let status = resp.status();
    let resp_body = resp.text().await?;

    if !status.is_success() {
        tracing::warn!(status = %status, body = resp_body, "batch GTC HTTP error");
        anyhow::bail!("batch GTC HTTP {status}: {resp_body}");
    }

    let results: Vec<BatchOrderResult> = serde_json::from_str(&resp_body).unwrap_or_else(|e| {
        tracing::warn!(error = %e, body = resp_body, "failed to parse batch GTC response");
        Vec::new()
    });

    for (i, r) in results.iter().enumerate() {
        let tag = orders.get(i).map(|(_, t)| *t).unwrap_or("?");
        let err = r.error_msg.as_deref().unwrap_or("");
        if !err.is_empty() {
            tracing::warn!(tag, error = err, "batch GTC rejected");
        } else if let Some(oid) = r.order_id.as_deref().filter(|s| !s.is_empty()) {
            tracing::info!(tag, order_id = oid, status = ?r.status, "batch GTC accepted");
        }
    }

    Ok(results)
}

pub async fn post_limit_order(
    config: &Config,
    auth: &ClobAuth,
    order: &FakOrder,
    tag: &str,
) -> Result<PostOrderResponse> {
    tracing::info!(tag, side = %order.side, team = %config.team_name(order.team),
        price = %order.price, size = %order.size, "posting GTC limit order");

    let signed = build_signed_order(config, auth, order)?;
    post_order(config, auth, &signed, "GTC", tag).await
}

#[derive(Debug, Deserialize)]
pub struct OpenOrder {
    pub id: Option<String>,
    pub status: Option<String>,
    pub original_size: Option<String>,
    pub size_matched: Option<String>,
    pub price: Option<String>,
}

impl OpenOrder {
    pub fn filled_size(&self) -> Decimal {
        self.size_matched.as_deref()
            .and_then(|s| Decimal::from_str(s).ok())
            .unwrap_or(Decimal::ZERO)
    }

    pub fn fill_price(&self) -> Decimal {
        self.price.as_deref()
            .and_then(|s| Decimal::from_str(s).ok())
            .unwrap_or(Decimal::ZERO)
    }

    pub fn is_terminal(&self) -> bool {
        self.status.as_deref().map_or(false, |s| {
            let lower = s.to_lowercase();
            matches!(lower.as_str(), "matched" | "cancelled" | "expired")
        })
    }
}

pub async fn get_order(
    auth: &ClobAuth,
    order_id: &str,
) -> Result<OpenOrder> {
    if order_id.is_empty() {
        anyhow::bail!("get_order: order_id must not be empty");
    }
    // Use /data/order/ endpoint — /order/ returns 404 for FAK orders
    // that have already been matched or cancelled.
    let path = format!("/data/order/{order_id}");
    let headers = auth.l2_headers("GET", &path, None)?;
    let url = format!("{}{}", auth.clob_http_url(), path);

    let resp = auth.http_client()
        .get(&url)
        .headers(headers)
        .send()
        .await?;

    let status = resp.status();
    let body = resp.text().await?;

    if !status.is_success() {
        anyhow::bail!("get_order failed: {status} {body}");
    }

    // The /data/order/ endpoint returns "null" before the order is indexed.
    // Treat it as a transient error so the poll loop retries.
    let trimmed = body.trim();
    if trimmed == "null" || trimmed.is_empty() {
        anyhow::bail!("order not yet indexed (null response)");
    }

    let order: OpenOrder = serde_json::from_str(&body)?;
    Ok(order)
}

pub async fn cancel_order(
    _config: &Config,
    auth: &ClobAuth,
    order_id: &str,
) -> Result<()> {
    let path = format!("/order/{order_id}");
    let headers = auth.l2_headers("DELETE", &path, None)?;
    let url = format!("{}{}", auth.clob_http_url(), path);

    let resp = auth.http_client()
        .delete(&url)
        .headers(headers)
        .send()
        .await?;

    let status = resp.status();
    if !status.is_success() {
        let body = resp.text().await?;
        tracing::warn!(order_id, status = %status, body, "cancel HTTP error");
        anyhow::bail!("cancel failed: {status}");
    }

    tracing::info!(order_id, "order cancelled");
    Ok(())
}

/// Cancel ALL open orders on the CLOB for this user.
/// Uses DELETE /cancel-all which cancels every open order regardless of market.
pub async fn cancel_all_open_orders(auth: &ClobAuth) -> Result<()> {
    let path = "/cancel-all";
    let headers = auth.l2_headers("DELETE", path, None)?;
    let url = format!("{}{}", auth.clob_http_url(), path);

    let resp = auth.http_client()
        .delete(&url)
        .headers(headers)
        .send()
        .await?;

    let status = resp.status();
    let body = resp.text().await?;

    if !status.is_success() {
        tracing::warn!(status = %status, body, "cancel-all HTTP error");
        anyhow::bail!("cancel-all failed: {status} {body}");
    }

    tracing::info!(response = body, "cancel-all completed");
    Ok(())
}

/// Fetch recent trades for the authenticated user from CLOB API.
/// Used as fallback when /data/order/{id} is slow to index.
pub async fn get_user_trades(
    auth: &ClobAuth,
    asset_id: Option<&str>,
) -> Result<Vec<serde_json::Value>> {
    let mut path = "/data/trades".to_string();
    if let Some(aid) = asset_id {
        path = format!("/data/trades?asset_id={aid}");
    }
    let headers = auth.l2_headers("GET", &path, None)?;
    let url = format!("{}{}", auth.clob_http_url(), path);

    let resp = auth.http_client()
        .get(&url)
        .headers(headers)
        .send()
        .await?;

    let status = resp.status();
    let body = resp.text().await?;

    if !status.is_success() {
        anyhow::bail!("get_user_trades failed: {status} {body}");
    }

    let trades: Vec<serde_json::Value> = serde_json::from_str(&body)
        .unwrap_or_default();
    Ok(trades)
}

/// Fetch all orders for the authenticated user from CLOB API.
/// Optionally filter by asset_id (token_id).
pub async fn get_user_orders(
    auth: &ClobAuth,
    asset_id: Option<&str>,
) -> Result<Vec<serde_json::Value>> {
    let mut path = "/data/orders".to_string();
    if let Some(aid) = asset_id {
        path = format!("/data/orders?asset_id={aid}");
    }
    let headers = auth.l2_headers("GET", &path, None)?;
    let url = format!("{}{}", auth.clob_http_url(), path);

    let resp = auth.http_client()
        .get(&url)
        .headers(headers)
        .send()
        .await?;

    let status = resp.status();
    let body = resp.text().await?;

    if !status.is_success() {
        anyhow::bail!("get_user_orders failed: {status} {body}");
    }

    let orders: Vec<serde_json::Value> = serde_json::from_str(&body)
        .unwrap_or_default();
    Ok(orders)
}

/// Post a GTD (Good-Till-Date) order with an explicit expiration.
pub async fn post_gtd_order(
    config: &Config,
    auth: &ClobAuth,
    order: &FakOrder,
    expiry_secs: u64,
    tag: &str,
) -> Result<PostOrderResponse> {
    tracing::info!(tag, side = %order.side, team = %config.team_name(order.team),
        price = %order.price, size = %order.size, expiry_secs, "posting GTD order");

    let signed = build_signed_order_with_expiry(config, auth, order, expiry_secs)?;
    post_order(config, auth, &signed, "GTD", tag).await
}

/// Response from DELETE /orders or DELETE /cancel-market-orders.
#[derive(Debug, Deserialize)]
pub struct CancelResponse {
    pub canceled: Option<Vec<String>>,
    pub not_canceled: Option<serde_json::Value>,
}

/// Cancel a batch of orders by ID.  DELETE /orders with body as JSON array.
pub async fn cancel_orders_batch(
    auth: &ClobAuth,
    order_ids: &[String],
) -> Result<CancelResponse> {
    let body_json = serde_json::to_string(order_ids)?;
    let path = "/orders";
    let headers = auth.l2_headers("DELETE", path, Some(&body_json))?;
    let url = format!("{}{}", auth.clob_http_url(), path);

    let resp = auth
        .http_client()
        .delete(&url)
        .headers(headers)
        .header("Content-Type", "application/json")
        .body(body_json)
        .send()
        .await?;

    let status = resp.status();
    let resp_body = resp.text().await?;

    if !status.is_success() {
        tracing::warn!(status = %status, body = resp_body, "cancel_orders_batch HTTP error");
        anyhow::bail!("cancel_orders_batch failed: {status} {resp_body}");
    }

    let result: CancelResponse = serde_json::from_str(&resp_body).unwrap_or(CancelResponse {
        canceled: None,
        not_canceled: Some(serde_json::Value::String(resp_body)),
    });

    tracing::info!(canceled = ?result.canceled, "cancel_orders_batch done");
    Ok(result)
}

/// Cancel all orders for a given market/asset.  DELETE /cancel-market-orders.
pub async fn cancel_market_orders(
    auth: &ClobAuth,
    condition_id: Option<&str>,
    asset_id: Option<&str>,
) -> Result<CancelResponse> {
    let mut body_map = serde_json::Map::new();
    if let Some(cid) = condition_id {
        body_map.insert("market".to_string(), serde_json::Value::String(cid.to_string()));
    }
    if let Some(aid) = asset_id {
        body_map.insert("asset_id".to_string(), serde_json::Value::String(aid.to_string()));
    }
    let body_json = serde_json::to_string(&body_map)?;
    let path = "/cancel-market-orders";
    let headers = auth.l2_headers("DELETE", path, Some(&body_json))?;
    let url = format!("{}{}", auth.clob_http_url(), path);

    let resp = auth
        .http_client()
        .delete(&url)
        .headers(headers)
        .header("Content-Type", "application/json")
        .body(body_json)
        .send()
        .await?;

    let status = resp.status();
    let resp_body = resp.text().await?;

    if !status.is_success() {
        tracing::warn!(status = %status, body = resp_body, "cancel_market_orders HTTP error");
        anyhow::bail!("cancel_market_orders failed: {status} {resp_body}");
    }

    let result: CancelResponse = serde_json::from_str(&resp_body).unwrap_or(CancelResponse {
        canceled: None,
        not_canceled: Some(serde_json::Value::String(resp_body)),
    });

    tracing::info!(canceled = ?result.canceled, "cancel_market_orders done");
    Ok(result)
}
