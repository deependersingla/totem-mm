use anyhow::Result;
use futures_util::{SinkExt, StreamExt};
use rust_decimal::Decimal;
use serde::Deserialize;
use std::str::FromStr;
use tokio::sync::watch;
use tokio_tungstenite::{connect_async, tungstenite::Message};

use std::sync::Arc;

use crate::config::Config;
use crate::price_history::PriceHistory;
use crate::types::{OrderBook, OrderBookSide, PriceLevel};
use crate::ws_health::WsHealth;

/// Market-level price update: {"market":"0x...","price_changes":[{asset_id,price,size,side}]}
/// This is a different format from per-asset events — no top-level event_type or asset_id.
#[derive(Debug, Deserialize)]
struct MarketUpdate {
    #[serde(default)]
    price_changes: Vec<PerAssetChange>,
}

#[derive(Debug, Deserialize)]
struct PerAssetChange {
    asset_id: String,
    price: String,
    size: String,
    side: String,
}

/// Per-asset event: Polymarket sends event_type (some versions send type) — handle both.
#[derive(Debug, Deserialize)]
struct WsEvent {
    #[serde(alias = "type")]
    event_type: Option<String>,

    /// Present on "book" snapshot events
    #[serde(default)]
    bids: Vec<serde_json::Value>,
    #[serde(default)]
    asks: Vec<serde_json::Value>,

    /// Present on "price_change" events — array of {price, size, side}
    #[serde(default)]
    changes: Vec<serde_json::Value>,

    asset_id: Option<String>,
    #[serde(default)]
    timestamp: Option<String>,
}

pub async fn run(
    config: &Config,
    book_tx: watch::Sender<(OrderBook, OrderBook)>,
    health_tx: watch::Sender<WsHealth>,
    price_history: Option<Arc<PriceHistory>>,
) -> Result<()> {
    let url = &config.clob_ws;
    let ping_interval = std::time::Duration::from_secs(config.ws_ping_interval_secs);
    let token_a = config.team_a_token_id.clone();
    let token_b = config.team_b_token_id.clone();

    loop {
        tracing::info!(url, "connecting to market websocket");

        match connect_async(url).await {
            Ok((ws_stream, _)) => {
                tracing::info!("market websocket connected");
                health_tx.send_modify(|h| h.on_connect());
                let (mut write, mut read) = ws_stream.split();

                // Plain object — Polymarket market channel subscription format
                let subscribe_msg = serde_json::json!({
                    "assets_ids": [&token_a, &token_b],
                    "type": "market"
                });
                write.send(Message::Text(subscribe_msg.to_string().into())).await?;
                tracing::info!("subscribed to market channel");

                let mut ping_timer = tokio::time::interval(ping_interval);
                let mut a_book = OrderBook::default();
                let mut b_book = OrderBook::default();
                let mut msg_count = 0u32;

                loop {
                    tokio::select! {
                        msg = read.next() => {
                            match msg {
                                Some(Ok(Message::Text(text))) => {
                                    msg_count += 1;
                                    // Log first few raw messages to help diagnose format issues
                                    if msg_count <= 5 {
                                        tracing::info!(n = msg_count, raw = %&text[..text.len().min(500)], "ws raw");
                                    }
                                    if let Err(e) = handle_message(
                                        &text,
                                        &token_a,
                                        &token_b,
                                        &mut a_book,
                                        &mut b_book,
                                        &book_tx,
                                        &health_tx,
                                        price_history.as_deref(),
                                    ) {
                                        tracing::warn!(error = %e, "market ws parse error");
                                    }
                                }
                                Some(Ok(Message::Close(_))) | None => {
                                    tracing::warn!("market websocket closed, reconnecting...");
                                    health_tx.send_modify(|h| h.on_disconnect());
                                    // Deltas may have been missed during the gap;
                                    // pre-disconnect touches no longer reflect live book.
                                    if let Some(ref ph) = price_history { ph.clear(); }
                                    break;
                                }
                                Some(Err(e)) => {
                                    tracing::error!(error = %e, "market websocket error");
                                    health_tx.send_modify(|h| h.on_disconnect());
                                    if let Some(ref ph) = price_history { ph.clear(); }
                                    break;
                                }
                                _ => {}
                            }
                        }
                        _ = ping_timer.tick() => {
                            if let Err(e) = write.send(Message::Text("PING".to_string().into())).await {
                                tracing::error!(error = %e, "failed to send PING");
                                health_tx.send_modify(|h| h.on_disconnect());
                                if let Some(ref ph) = price_history { ph.clear(); }
                                break;
                            }
                        }
                    }
                }
            }
            Err(e) => {
                tracing::error!(error = %e, "failed to connect to market websocket");
            }
        }

        tracing::info!("reconnecting market websocket in 2s...");
        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
    }
}

fn handle_message(
    text: &str,
    token_a: &str,
    token_b: &str,
    a_book: &mut OrderBook,
    b_book: &mut OrderBook,
    book_tx: &watch::Sender<(OrderBook, OrderBook)>,
    health_tx: &watch::Sender<WsHealth>,
    price_history: Option<&PriceHistory>,
) -> Result<()> {
    if text == "PONG" {
        return Ok(());
    }

    // Polymarket sends either an array of events or a single event
    let events: Vec<WsEvent> = match serde_json::from_str(text) {
        Ok(v) => v,
        Err(_) => {
            if let Ok(single) = serde_json::from_str::<WsEvent>(text) {
                vec![single]
            } else {
                tracing::debug!(raw = %text, "ws message unparseable — skipping");
                return Ok(());
            }
        }
    };

    let mut book_changed = false;

    for event in events {
        let asset_id = match &event.asset_id {
            Some(id) => id.as_str(),
            None => continue,
        };

        let is_a = asset_id == token_a;
        let is_b = asset_id == token_b;
        if !is_a && !is_b {
            tracing::debug!(asset_id, "ws event for unknown asset — skipping");
            continue;
        }

        let book = if is_a { &mut *a_book } else { &mut *b_book };

        match event.event_type.as_deref() {
            Some("book") => {
                book.bids = parse_levels(&event.bids);
                book.asks = parse_levels(&event.asks);
                sort_bids(&mut book.bids);
                sort_asks(&mut book.asks);
                if let Some(ts) = &event.timestamp {
                    book.timestamp_ms = ts.parse().unwrap_or(0);
                }
                let label = if is_a { "team_a" } else { "team_b" };
                tracing::info!(
                    team = label,
                    bid = ?book.best_bid().map(|l| l.price),
                    ask = ?book.best_ask().map(|l| l.price),
                    "book snapshot"
                );
                health_tx.send_modify(|h| h.on_snapshot());
                book_changed = true;
            }
            Some("price_change") => {
                if !event.changes.is_empty() {
                    // Polymarket price_change format: [{price, size, side:"BUY"|"SELL"}]
                    for change in &event.changes {
                        if let Some(obj) = change.as_object() {
                            let price_str = obj.get("price").and_then(|v| v.as_str()).unwrap_or("");
                            let size_str  = obj.get("size").and_then(|v| v.as_str()).unwrap_or("0");
                            let side_str  = obj.get("side").and_then(|v| v.as_str()).unwrap_or("");
                            if let (Ok(price), Ok(size)) = (
                                Decimal::from_str(price_str),
                                Decimal::from_str(size_str),
                            ) {
                                let side = if side_str == "BUY" { &mut book.bids } else { &mut book.asks };
                                apply_level(side, price, size);
                            }
                        }
                    }
                } else {
                    // Fallback: separate bids/asks arrays in price_change
                    apply_deltas(&mut book.bids, &event.bids);
                    apply_deltas(&mut book.asks, &event.asks);
                }
                sort_bids(&mut book.bids);
                sort_asks(&mut book.asks);
                book_changed = true;
            }
            other => {
                if other.is_some() {
                    tracing::debug!(event_type = ?other, "unhandled ws event type");
                }
            }
        }
    }

    // Handle market-level price_changes format:
    // {"market":"0x...","price_changes":[{"asset_id":"...","price":"...","size":"...","side":"BUY|SELL"}]}
    // These have no top-level event_type or asset_id, so the per-event loop above skips them.
    if let Ok(market) = serde_json::from_str::<MarketUpdate>(text) {
        if !market.price_changes.is_empty() {
            for change in &market.price_changes {
                let is_a = change.asset_id == token_a;
                let is_b = change.asset_id == token_b;
                if !is_a && !is_b {
                    continue;
                }
                let book = if is_a { &mut *a_book } else { &mut *b_book };
                if let (Ok(price), Ok(size)) = (
                    Decimal::from_str(&change.price),
                    Decimal::from_str(&change.size),
                ) {
                    let side = if change.side == "BUY" { &mut book.bids } else { &mut book.asks };
                    apply_level(side, price, size);
                }
                book_changed = true;
            }
            if book_changed {
                sort_bids(&mut a_book.bids);
                sort_asks(&mut a_book.asks);
                sort_bids(&mut b_book.bids);
                sort_asks(&mut b_book.asks);
            }
        }
    }

    if book_changed {
        let snapshot = (a_book.clone(), b_book.clone());
        if let Some(ph) = price_history {
            ph.record(&snapshot);
        }
        let _ = book_tx.send(snapshot);
    }
    Ok(())
}

fn sort_bids(side: &mut OrderBookSide) {
    side.levels.sort_by(|a, b| b.price.cmp(&a.price));
}

fn sort_asks(side: &mut OrderBookSide) {
    side.levels.sort_by(|a, b| a.price.cmp(&b.price));
}

/// Parse a level list — handles both object {price,size} and array [price,size] formats
fn parse_levels(raw: &[serde_json::Value]) -> OrderBookSide {
    let levels = raw.iter().filter_map(level_from_value).collect();
    OrderBookSide { levels }
}

fn level_from_value(v: &serde_json::Value) -> Option<PriceLevel> {
    match v {
        serde_json::Value::Object(obj) => {
            let price = decimal_from_str(obj.get("price")?.as_str()?)?;
            let size  = decimal_from_str(obj.get("size")?.as_str()?)?;
            Some(PriceLevel { price, size })
        }
        serde_json::Value::Array(arr) => {
            let price = decimal_from_value(arr.first()?)?;
            let size  = decimal_from_value(arr.get(1)?)?;
            Some(PriceLevel { price, size })
        }
        _ => None,
    }
}

fn apply_level(side: &mut OrderBookSide, price: Decimal, size: Decimal) {
    if size.is_zero() {
        side.levels.retain(|l| l.price != price);
    } else if let Some(level) = side.levels.iter_mut().find(|l| l.price == price) {
        level.size = size;
    } else {
        side.levels.push(PriceLevel { price, size });
    }
}

fn apply_deltas(side: &mut OrderBookSide, deltas: &[serde_json::Value]) {
    for delta in deltas {
        let Some(pl) = level_from_value(delta) else { continue };
        apply_level(side, pl.price, pl.size);
    }
}

fn decimal_from_value(v: &serde_json::Value) -> Option<Decimal> {
    match v {
        serde_json::Value::String(s) => Decimal::from_str(s).ok(),
        serde_json::Value::Number(n) => Decimal::from_str(&n.to_string()).ok(),
        _ => None,
    }
}

fn decimal_from_str(s: &str) -> Option<Decimal> {
    Decimal::from_str(s).ok()
}
