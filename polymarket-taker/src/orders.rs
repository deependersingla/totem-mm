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

fn side_to_u8(side: Side) -> u8 {
    match side {
        Side::Buy => 0,
        Side::Sell => 1,
    }
}

pub(crate) fn to_base_units(amount: Decimal) -> u128 {
    let scaled = amount * Decimal::from(USDC_DECIMALS);
    scaled.to_string().parse::<f64>().unwrap_or(0.0).floor() as u128
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
        let v: u128 = val.parse().unwrap_or(0);
        let mut buf = [0u8; 32];
        buf[16..].copy_from_slice(&v.to_be_bytes());
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

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct PostOrderRequest {
    order: PostOrderBody,
    owner: String,
    order_type: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct PostOrderBody {
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
            token_id: o.token_id.clone(),
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
    #[serde(rename = "errorMsg")]
    pub error_msg: Option<String>,
    pub status: Option<String>,
}

fn build_signed_order(config: &Config, auth: &ClobAuth, order: &FakOrder) -> Result<ClobOrder> {
    let salt: u128 = rand::thread_rng().gen();
    let token_id = config.token_id(order.team).to_string();
    let (maker_amount, taker_amount) = compute_amounts(order.side, order.price, order.size);

    let signer_addr = auth.address().to_string();

    let mut clob_order = ClobOrder {
        salt: salt.to_string(),
        maker: config.polymarket_address.clone(),
        signer: signer_addr,
        taker: "0x0000000000000000000000000000000000000000".to_string(),
        token_id,
        maker_amount,
        taker_amount,
        side: side_to_u8(order.side),
        expiration: "0".to_string(),
        nonce: "0".to_string(),
        fee_rate_bps: "0".to_string(),
        signature_type: config.signature_type,
        signature: String::new(),
    };

    let struct_hash = order_struct_hash(&clob_order);
    let signature = auth.sign_order(&struct_hash, config.exchange_address(), config.chain_id)?;
    clob_order.signature = signature;

    Ok(clob_order)
}

async fn post_order(
    _config: &Config,
    auth: &ClobAuth,
    clob_order: &ClobOrder,
    order_type: &str,
    tag: &str,
) -> Result<PostOrderResponse> {
    let body = PostOrderRequest {
        order: PostOrderBody::from(clob_order),
        owner: auth.api_key.clone(),
        order_type: order_type.to_string(),
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
        matches!(
            self.status.as_deref(),
            Some("matched") | Some("cancelled") | Some("expired")
        )
    }
}

pub async fn get_order(
    auth: &ClobAuth,
    order_id: &str,
) -> Result<OpenOrder> {
    let path = format!("/order/{order_id}");
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
