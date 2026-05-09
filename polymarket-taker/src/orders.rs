use anyhow::Result;
use rand::Rng;
use rust_decimal::Decimal;
use serde::Deserialize;
use std::str::FromStr;
use std::time::{SystemTime, UNIX_EPOCH};
use crate::clob_auth::ClobAuth;
use crate::config::Config;
use crate::orders_v2::{OrderSubmission, OrderV2, SignedOrderV2};
use crate::types::{FakOrder, Side};

const USDC_DECIMALS: u64 = 1_000_000;
const BYTES32_ZERO: &str = "0x0000000000000000000000000000000000000000000000000000000000000000";

/// V2 alias — external callers and the order cache hold this opaque type.
pub type ClobOrder = SignedOrderV2;

fn side_to_u8(side: Side) -> u8 {
    match side {
        Side::Buy => 0,
        Side::Sell => 1,
    }
}

fn now_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis()
}

/// Builder code from config, validated to a 0x-prefixed 32-byte hex.
/// Empty config value → all-zero bytes32 (no attribution).
fn builder_code(config: &Config) -> String {
    let raw = config.builder_code.trim();
    if raw.is_empty() {
        return BYTES32_ZERO.to_string();
    }
    if raw.starts_with("0x") { raw.to_string() } else { format!("0x{raw}") }
}

/// Convert a Decimal amount to 6-decimal base units (USDC / CTF token precision).
/// Uses Decimal::floor() for exact integer truncation — avoids the f64 precision
/// loss that the previous implementation had (f64 can't represent many 6-decimal
/// values exactly, causing off-by-one errors in order amounts).
///
/// D4 (TODO.md): returns `Err` on negative amounts and on values that exceed
/// `u128::MAX` (the previous `unwrap_or(0)` silently produced 0-size orders
/// that the CLOB then rejected with no signal to the strategy).
pub(crate) fn to_base_units(amount: Decimal) -> Result<u128> {
    if amount.is_sign_negative() {
        anyhow::bail!("to_base_units: negative amount {amount}");
    }
    let scaled = (amount * Decimal::from(USDC_DECIMALS)).floor();
    scaled
        .to_string()
        .parse::<u128>()
        .map_err(|e| anyhow::anyhow!("to_base_units: cannot encode {amount} as u128: {e}"))
}

pub(crate) fn compute_amounts(
    side: Side,
    price: Decimal,
    size: Decimal,
) -> Result<(String, String)> {
    match side {
        Side::Buy => {
            let taker_amount = to_base_units(size)?;
            let maker_amount = to_base_units(size * price)?;
            Ok((maker_amount.to_string(), taker_amount.to_string()))
        }
        Side::Sell => {
            let maker_amount = to_base_units(size)?;
            let taker_amount = to_base_units(size * price)?;
            Ok((maker_amount.to_string(), taker_amount.to_string()))
        }
    }
}

/// True iff `error_msg` indicates a post-only order was rejected because it
/// would cross the spread. The match is intentionally tight to avoid false
/// positives on unrelated errors that happen to contain "cross" as a
/// substring (e.g., "across the spread", "cross-market", "crossover").
///
/// Recognised phrasings (case-insensitive):
///   - "would cross"          (canonical Polymarket V2 reject)
///   - "post-only" + "cross"  (compound — covers wording variants)
///   - "post_only" + "cross"  (snake-case variant)
///
/// Used by the revert post path: on a cross-reject we immediately retry the
/// same order without `postOnly` so the crossing portion fills as taker at
/// the now-better market price, residual rests as a passive maker.
pub fn is_post_only_cross_reject(error_msg: &str) -> bool {
    if error_msg.is_empty() {
        return false;
    }
    let lower = error_msg.to_lowercase();
    lower.contains("would cross")
        || (lower.contains("post-only") && lower.contains("cross"))
        || (lower.contains("post_only") && lower.contains("cross"))
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

#[derive(Debug, Deserialize)]
pub struct PostOrderResponse {
    #[serde(rename = "orderID")]
    pub order_id: Option<String>,
    #[serde(alias = "errorMsg", alias = "error")]
    pub error_msg: Option<String>,
    pub status: Option<String>,
}

/// Internal helper that builds and signs a V2 CLOB order with a given expiration.
/// `expiration` should be "0" for GTC/FAK or a unix-seconds timestamp string for GTD.
///
/// V2 changes vs V1:
///   - removed from signed struct: `taker`, `nonce`, `feeRateBps`
///   - added: `timestamp` (ms), `metadata` (bytes32 zero), `builder` (bytes32 builderCode)
///   - `expiration` stays in the JSON body but is NOT part of the signed struct hash
fn build_signed_order_inner(
    config: &Config,
    auth: &ClobAuth,
    order: &FakOrder,
    expiration: &str,
) -> Result<SignedOrderV2> {
    // Salt must fit in IEEE 754 double precision (backend parses as float).
    // Mask to <= 2^53 - 1 to avoid precision loss.
    let salt: u64 = rand::thread_rng().gen::<u64>() & ((1u64 << 53) - 1);
    let token_id = config.token_id(order.team).to_string();
    let (maker_amount, taker_amount) = compute_amounts(order.side, order.price, order.size)
        .map_err(|e| {
            tracing::error!(
                error = %e,
                side = %order.side,
                team = ?order.team,
                price = %order.price,
                size = %order.size,
                "compute_amounts failed — refusing to build order",
            );
            e
        })?;

    let signer_addr = auth.address().to_string();
    // maker = who provides the funds:
    //   type 0 (EOA):         maker == signer (both are the EOA)
    //   type 1 (POLY_PROXY):  maker == funder/proxy wallet address
    //   type 2 (GNOSIS_SAFE): maker == funder/proxy wallet address
    let maker_addr = auth.funder_address().to_string();

    let v2_order = OrderV2 {
        salt: salt.to_string(),
        maker: maker_addr,
        signer: signer_addr,
        token_id,
        maker_amount,
        taker_amount,
        side: side_to_u8(order.side),
        signature_type: config.signature_type,
        timestamp: now_ms().to_string(),
        metadata: BYTES32_ZERO.to_string(),
        builder: builder_code(config),
        expiration: expiration.to_string(),
    };

    let exchange = config.exchange_address();
    tracing::info!(
        exchange,
        chain_id = config.chain_id,
        neg_risk = config.neg_risk,
        salt = %v2_order.salt,
        maker = %v2_order.maker,
        signer = %v2_order.signer,
        token_id = %v2_order.token_id,
        maker_amount = %v2_order.maker_amount,
        taker_amount = %v2_order.taker_amount,
        side = v2_order.side,
        signature_type = v2_order.signature_type,
        timestamp = %v2_order.timestamp,
        builder_set = v2_order.builder != BYTES32_ZERO,
        "V2 order EIP-712 signing"
    );

    SignedOrderV2::build(
        &config.polymarket_private_key,
        v2_order,
        exchange,
        config.chain_id,
    )
}

pub(crate) fn build_signed_order(config: &Config, auth: &ClobAuth, order: &FakOrder) -> Result<SignedOrderV2> {
    build_signed_order_inner(config, auth, order, "0")
}

/// Build a signed order with a GTD expiration (unix timestamp = now + expiry_secs).
fn build_signed_order_with_expiry(
    config: &Config,
    auth: &ClobAuth,
    order: &FakOrder,
    expiry_secs: u64,
) -> Result<SignedOrderV2> {
    let now_unix = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_secs();
    let expiration = (now_unix + expiry_secs).to_string();
    build_signed_order_inner(config, auth, order, &expiration)
}

async fn post_order(
    _config: &Config,
    auth: &ClobAuth,
    clob_order: &SignedOrderV2,
    order_type: &str,
    tag: &str,
) -> Result<PostOrderResponse> {
    post_order_with_post_only(_config, auth, clob_order, order_type, tag, false).await
}

/// Post a signed order with explicit control over the `postOnly` wire flag.
///
/// `post_only=true` is only meaningful for `order_type` of `"GTC"` or
/// `"GTD"`; the matcher rejects FAK + post_only as nonsense. We don't
/// validate at this layer — callers (strategy.rs, maker.rs) are expected to
/// only set `true` on resting-order purposes.
async fn post_order_with_post_only(
    _config: &Config,
    auth: &ClobAuth,
    clob_order: &SignedOrderV2,
    order_type: &str,
    tag: &str,
    post_only: bool,
) -> Result<PostOrderResponse> {
    let body = OrderSubmission::with_post_only(clob_order, auth.api_key.clone(), order_type, post_only);
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
        tracing::warn!(tag, status = %status, body = resp_body, post_only, "order HTTP error");
    }

    let result: PostOrderResponse = serde_json::from_str(&resp_body)
        .unwrap_or(PostOrderResponse {
            order_id: None,
            error_msg: Some(resp_body),
            status: None,
        });

    if let Some(ref oid) = result.order_id {
        tracing::info!(tag, order_id = oid, post_only, "order accepted");
    }
    if let Some(ref err) = result.error_msg {
        if result.order_id.is_none() {
            tracing::warn!(tag, error = err, post_only, "order rejected");
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

/// Post a pre-signed FAK order directly (zero signing latency).
pub async fn post_presigned_fak(
    config: &Config,
    auth: &ClobAuth,
    signed: &SignedOrderV2,
    tag: &str,
) -> Result<PostOrderResponse> {
    post_order(config, auth, signed, "FAK", tag).await
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
        items.push(OrderSubmission::new(&signed, auth.api_key.clone(), "FAK"));
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

/// Post multiple pre-signed FAK orders in a single HTTP call (POST /orders).
/// Skips the build+sign step entirely — orders were already signed by the order cache.
pub async fn post_presigned_fak_batch(
    _config: &Config,
    auth: &ClobAuth,
    orders: &[(SignedOrderV2, &str)],
) -> Result<Vec<BatchOrderResult>> {
    let mut items = Vec::with_capacity(orders.len());
    for (signed, tag) in orders {
        tracing::info!(tag, "batch pre-signed FAK order");
        items.push(OrderSubmission::new(signed, auth.api_key.clone(), "FAK"));
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
        tracing::warn!(status = %status, body = resp_body, "pre-signed batch FAK HTTP error");
        anyhow::bail!("pre-signed batch FAK HTTP {status}: {resp_body}");
    }

    let results: Vec<BatchOrderResult> = serde_json::from_str(&resp_body)
        .map_err(|e| {
            tracing::error!(error = %e, body = resp_body, "failed to parse pre-signed batch response — cannot determine fill status");
            anyhow::anyhow!("batch response parse failed: {e} body={resp_body}")
        })?;

    for (i, r) in results.iter().enumerate() {
        let tag = orders.get(i).map(|(_, t)| *t).unwrap_or("?");
        let err = r.error_msg.as_deref().unwrap_or("");
        if !err.is_empty() {
            tracing::warn!(tag, error = err, "pre-signed batch FAK rejected");
        } else if let Some(oid) = r.order_id.as_deref().filter(|s| !s.is_empty()) {
            tracing::info!(tag, order_id = oid, status = ?r.status, "pre-signed batch FAK accepted");
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
        items.push(OrderSubmission::new(&signed, auth.api_key.clone(), "GTC"));
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
    post_limit_order_with_post_only(config, auth, order, tag, false).await
}

/// Post a GTC with explicit `post_only` control. When `post_only=true` and
/// the matcher would cross the spread, the response carries `success:false`
/// + `errorMsg ~ "cross"` — the caller checks
/// [`is_post_only_cross_reject`] and decides whether to retry plain.
pub async fn post_limit_order_with_post_only(
    config: &Config,
    auth: &ClobAuth,
    order: &FakOrder,
    tag: &str,
    post_only: bool,
) -> Result<PostOrderResponse> {
    tracing::info!(
        tag, side = %order.side, team = %config.team_name(order.team),
        price = %order.price, size = %order.size, post_only,
        "posting GTC limit order",
    );
    let signed = build_signed_order(config, auth, order)?;
    post_order_with_post_only(config, auth, &signed, "GTC", tag, post_only).await
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
            matches!(lower.as_str(), "matched" | "cancelled" | "expired" | "unmatched")
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
