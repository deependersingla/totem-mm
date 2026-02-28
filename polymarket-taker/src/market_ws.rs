use anyhow::Result;
use futures_util::{SinkExt, StreamExt};
use rust_decimal::Decimal;
use serde::Deserialize;
use std::str::FromStr;
use tokio::sync::watch;
use tokio_tungstenite::{connect_async, tungstenite::Message};

use crate::config::Config;
use crate::types::{OrderBook, OrderBookSide, PriceLevel};

#[derive(Debug, Deserialize)]
struct WsEvent {
    #[serde(rename = "type")]
    event_type: Option<String>,
    #[serde(default)]
    bids: Vec<Vec<serde_json::Value>>,
    #[serde(default)]
    asks: Vec<Vec<serde_json::Value>>,
    #[serde(default)]
    asset_id: Option<String>,
    #[serde(default)]
    timestamp: Option<String>,
}

/// Streams L2 orderbook for both team tokens.
/// Sends (team_a_book, team_b_book) on every update.
pub async fn run(
    config: &Config,
    book_tx: watch::Sender<(OrderBook, OrderBook)>,
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
                let (mut write, mut read) = ws_stream.split();

                let subscribe_msg = serde_json::json!({
                    "assets_ids": [&token_a, &token_b],
                    "type": "market"
                });
                write.send(Message::Text(subscribe_msg.to_string().into())).await?;
                tracing::info!("subscribed to market channel");

                let mut ping_timer = tokio::time::interval(ping_interval);
                let mut a_book = OrderBook::default();
                let mut b_book = OrderBook::default();

                loop {
                    tokio::select! {
                        msg = read.next() => {
                            match msg {
                                Some(Ok(Message::Text(text))) => {
                                    if let Err(e) = handle_message(
                                        &text,
                                        &token_a,
                                        &token_b,
                                        &mut a_book,
                                        &mut b_book,
                                        &book_tx,
                                    ) {
                                        tracing::warn!(error = %e, "market ws parse error");
                                    }
                                }
                                Some(Ok(Message::Close(_))) | None => {
                                    tracing::warn!("market websocket closed, reconnecting...");
                                    break;
                                }
                                Some(Err(e)) => {
                                    tracing::error!(error = %e, "market websocket error");
                                    break;
                                }
                                _ => {}
                            }
                        }
                        _ = ping_timer.tick() => {
                            if let Err(e) = write.send(Message::Text("PING".to_string().into())).await {
                                tracing::error!(error = %e, "failed to send PING");
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
) -> Result<()> {
    if text == "PONG" {
        return Ok(());
    }

    let events: Vec<WsEvent> = match serde_json::from_str(text) {
        Ok(v) => v,
        Err(_) => {
            if let Ok(single) = serde_json::from_str::<WsEvent>(text) {
                vec![single]
            } else {
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
                book_changed = true;
            }
            Some("price_change") => {
                if !event.bids.is_empty() || !event.asks.is_empty() {
                    apply_deltas(&mut book.bids, &event.bids);
                    apply_deltas(&mut book.asks, &event.asks);
                    sort_bids(&mut book.bids);
                    sort_asks(&mut book.asks);
                    book_changed = true;
                }
            }
            _ => {}
        }
    }

    if book_changed {
        let _ = book_tx.send((a_book.clone(), b_book.clone()));
    }
    Ok(())
}

/// Sorts bids highest-first so `best()` / `best_bid()` returns the top of book.
fn sort_bids(side: &mut OrderBookSide) {
    side.levels.sort_by(|a, b| b.price.cmp(&a.price));
}

/// Sorts asks lowest-first so `best()` / `best_ask()` returns the top of book.
fn sort_asks(side: &mut OrderBookSide) {
    side.levels.sort_by(|a, b| a.price.cmp(&b.price));
}

fn parse_levels(raw: &[Vec<serde_json::Value>]) -> OrderBookSide {
    let levels = raw
        .iter()
        .filter_map(|pair| {
            let price = decimal_from_value(pair.first()?)?;
            let size = decimal_from_value(pair.get(1)?)?;
            Some(PriceLevel { price, size })
        })
        .collect();
    OrderBookSide { levels }
}

fn apply_deltas(side: &mut OrderBookSide, deltas: &[Vec<serde_json::Value>]) {
    for delta in deltas {
        let Some(price) = delta.first().and_then(decimal_from_value) else {
            continue;
        };
        let Some(size) = delta.get(1).and_then(decimal_from_value) else {
            continue;
        };

        if size.is_zero() {
            side.levels.retain(|l| l.price != price);
        } else if let Some(level) = side.levels.iter_mut().find(|l| l.price == price) {
            level.size = size;
        } else {
            side.levels.push(PriceLevel { price, size });
        }
    }
}

fn decimal_from_value(v: &serde_json::Value) -> Option<Decimal> {
    match v {
        serde_json::Value::String(s) => Decimal::from_str(s).ok(),
        serde_json::Value::Number(n) => Decimal::from_str(&n.to_string()).ok(),
        _ => None,
    }
}
