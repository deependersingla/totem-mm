//! User WebSocket — real-time fill/trade notifications from Polymarket CLOB.
//!
//! Replaces REST polling for fill detection. Connects to
//! `wss://ws-subscriptions-clob.polymarket.com/ws/user` with API credentials
//! in the subscription message.

use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use rust_decimal::Decimal;
use serde::Deserialize;
use serde_json::json;
use tokio::sync::mpsc;
use tokio::time::{interval, timeout};
use tokio_tungstenite::tungstenite::Message;
use tokio_util::sync::CancellationToken;

use crate::types::{FillEvent, Side};

const USER_WS_URL: &str = "wss://ws-subscriptions-clob.polymarket.com/ws/user";
const RECONNECT_DELAY: Duration = Duration::from_secs(2);
const PING_INTERVAL: Duration = Duration::from_secs(10);
const READ_TIMEOUT: Duration = Duration::from_secs(30);

/// Raw trade message from user WebSocket.
#[derive(Debug, Deserialize)]
#[allow(dead_code)]
struct RawTradeMessage {
    #[serde(default)]
    event_type: String,
    #[serde(default, rename = "type")]
    msg_type: String,
    #[serde(default)]
    taker_order_id: String,
    #[serde(default)]
    status: String,
    #[serde(default)]
    asset_id: String,
    #[serde(default)]
    price: String,
    #[serde(default)]
    size: String,
    #[serde(default)]
    side: String,
}

/// Raw order message from user WebSocket.
#[derive(Debug, Deserialize)]
#[allow(dead_code)]
struct RawOrderMessage {
    #[serde(default)]
    event_type: String,
    #[serde(default, rename = "type")]
    msg_type: String,
    #[serde(default)]
    id: String,
    #[serde(default)]
    size_matched: String,
    #[serde(default)]
    original_size: String,
    #[serde(default)]
    price: String,
    #[serde(default)]
    side: String,
    #[serde(default)]
    asset_id: String,
    #[serde(default)]
    status: String,
}

fn parse_side(s: &str) -> Side {
    match s.to_uppercase().as_str() {
        "SELL" => Side::Sell,
        _ => Side::Buy,
    }
}

fn parse_decimal(s: &str) -> Decimal {
    s.parse().unwrap_or(Decimal::ZERO)
}

/// Parse a raw JSON message from the user WebSocket into FillEvents.
pub fn parse_user_ws_message(text: &str) -> Vec<FillEvent> {
    let mut results = Vec::new();

    // Try as array of events first
    if let Ok(arr) = serde_json::from_str::<Vec<serde_json::Value>>(text) {
        for val in &arr {
            results.extend(parse_single_event(val));
        }
        if !results.is_empty() {
            return results;
        }
    }

    // Try as single event
    if let Ok(val) = serde_json::from_str::<serde_json::Value>(text) {
        results.extend(parse_single_event(&val));
    }

    results
}

fn parse_single_event(val: &serde_json::Value) -> Vec<FillEvent> {
    let mut results = Vec::new();
    let event_type = val.get("event_type").and_then(|v| v.as_str()).unwrap_or("");
    let msg_type = val.get("type").and_then(|v| v.as_str()).unwrap_or("");

    let is_trade = event_type == "trade" || msg_type == "trade";
    let is_order = event_type == "order" || msg_type == "order";

    if is_trade {
        if let Ok(trade) = serde_json::from_value::<RawTradeMessage>(val.clone()) {
            let status = trade.status.to_uppercase();
            // Only emit fills for MATCHED or CONFIRMED status
            if status == "MATCHED" || status == "CONFIRMED" {
                results.push(FillEvent {
                    order_id: trade.taker_order_id.clone(),
                    filled_size: parse_decimal(&trade.size),
                    avg_price: parse_decimal(&trade.price),
                    status: trade.status.clone(),
                    asset_id: trade.asset_id.clone(),
                    side: parse_side(&trade.side),
                });
            }
        }
    } else if is_order {
        if let Ok(order) = serde_json::from_value::<RawOrderMessage>(val.clone()) {
            let size_matched = parse_decimal(&order.size_matched);
            if size_matched > Decimal::ZERO {
                results.push(FillEvent {
                    order_id: order.id.clone(),
                    filled_size: size_matched,
                    avg_price: parse_decimal(&order.price),
                    status: order.status.clone(),
                    asset_id: order.asset_id.clone(),
                    side: parse_side(&order.side),
                });
            }
        }
    }

    results
}

/// Run the user WebSocket connection loop.
///
/// Connects to the Polymarket user channel, authenticates with API credentials,
/// and forwards fill events to the provided channel. Reconnects automatically
/// on disconnect.
pub async fn run(
    ws_base_url: String,
    api_key: String,
    api_secret: String,
    api_passphrase: String,
    condition_id: String,
    token_ids: Vec<String>,
    fill_tx: mpsc::Sender<FillEvent>,
    cancel: CancellationToken,
) {
    loop {
        if cancel.is_cancelled() {
            tracing::info!("[USER-WS] cancelled, stopping");
            return;
        }

        let url = ws_base_url
            .replace("/ws/market", "/ws/user")
            .replace("ws/market", "ws/user");
        let url = if url.contains("/ws/user") {
            url
        } else {
            USER_WS_URL.to_string()
        };

        tracing::info!("[USER-WS] connecting to {}", url);

        let connect_result = tokio_tungstenite::connect_async(&url).await;
        let (mut ws, _) = match connect_result {
            Ok(pair) => pair,
            Err(e) => {
                tracing::warn!("[USER-WS] connection failed: {e}, retrying in {:?}", RECONNECT_DELAY);
                tokio::select! {
                    _ = tokio::time::sleep(RECONNECT_DELAY) => continue,
                    _ = cancel.cancelled() => return,
                }
            }
        };

        tracing::info!("[USER-WS] connected");

        // Send authenticated subscription
        let sub_msg = json!({
            "auth": {
                "apiKey": api_key,
                "secret": api_secret,
                "passphrase": api_passphrase,
            },
            "markets": [condition_id],
            "assets_ids": token_ids,
            "type": "user"
        });

        if let Err(e) = ws.send(Message::Text(sub_msg.to_string().into())).await {
            tracing::warn!("[USER-WS] failed to send subscription: {e}");
            continue;
        }
        tracing::info!("[USER-WS] subscription sent");

        let mut ping_timer = interval(PING_INTERVAL);
        ping_timer.tick().await; // consume first immediate tick
        let mut msg_count: u64 = 0;

        loop {
            tokio::select! {
                _ = cancel.cancelled() => {
                    tracing::info!("[USER-WS] cancelled, closing");
                    let _ = ws.close(None).await;
                    return;
                }
                _ = ping_timer.tick() => {
                    if ws.send(Message::Text("PING".to_string().into())).await.is_err() {
                        tracing::warn!("[USER-WS] ping failed, reconnecting");
                        break;
                    }
                }
                msg = timeout(READ_TIMEOUT, ws.next()) => {
                    match msg {
                        Ok(Some(Ok(Message::Text(text)))) => {
                            if text == "PONG" || text == "pong" {
                                continue;
                            }
                            msg_count += 1;
                            if msg_count <= 3 {
                                tracing::debug!("[USER-WS] raw msg #{msg_count}: {text}");
                            }

                            let events = parse_user_ws_message(&text);
                            for event in events {
                                tracing::info!(
                                    order_id = %event.order_id,
                                    status = %event.status,
                                    filled_size = %event.filled_size,
                                    price = %event.avg_price,
                                    side = %event.side,
                                    "[USER-WS] fill event"
                                );
                                if fill_tx.send(event).await.is_err() {
                                    tracing::warn!("[USER-WS] fill channel closed");
                                    return;
                                }
                            }
                        }
                        Ok(Some(Ok(Message::Close(_)))) => {
                            tracing::info!("[USER-WS] server closed connection");
                            break;
                        }
                        Ok(Some(Ok(_))) => {} // binary/ping/pong frames
                        Ok(Some(Err(e))) => {
                            tracing::warn!("[USER-WS] read error: {e}");
                            break;
                        }
                        Ok(None) => {
                            tracing::info!("[USER-WS] stream ended");
                            break;
                        }
                        Err(_) => {
                            tracing::warn!("[USER-WS] read timeout ({:?}), reconnecting", READ_TIMEOUT);
                            break;
                        }
                    }
                }
            }
        }

        tracing::info!("[USER-WS] reconnecting in {:?}", RECONNECT_DELAY);
        tokio::select! {
            _ = tokio::time::sleep(RECONNECT_DELAY) => {}
            _ = cancel.cancelled() => return,
        }
    }
}
