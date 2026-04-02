//! HTTP server for the standalone sweep binary.
//!
//! Own routes, own state — no dependency on the taker's server.rs or AppState.

use std::sync::Arc;
use std::str::FromStr;

use axum::extract::State;
use axum::http::StatusCode;
use axum::response::Json;
use axum::routing::{get, post};
use axum::Router;
use rust_decimal::Decimal;
use serde::Deserialize;
use tokio::sync::watch;
use tower_http::cors::CorsLayer;

use crate::clob_auth::ClobAuth;
use crate::ctf;
use crate::heartbeat;
use crate::market_ws;
use crate::orders;
use crate::trading;
use crate::sweep_state::{SweepAppState, SweepPhase, SweepOrder};
use crate::types::{FakOrder, OrderBook, Side, Team};
use crate::web;

type S = Arc<SweepAppState>;

/// Start (or restart) orderbook WS feed.
pub fn start_book_ws(state: &Arc<SweepAppState>) {
    let cfg = state.shared_config();
    if !cfg.has_tokens() {
        return;
    }

    if let Some(old) = state.ws_cancel.read().unwrap().as_ref() {
        old.cancel();
    }

    let (book_tx, book_rx) = watch::channel((OrderBook::default(), OrderBook::default()));
    *state.book_rx.write().unwrap() = Some(book_rx);
    *state.book_tx.write().unwrap() = Some(book_tx.clone());

    let cancel = tokio_util::sync::CancellationToken::new();
    *state.ws_cancel.write().unwrap() = Some(cancel.clone());

    tokio::spawn(async move {
        tokio::select! {
            res = market_ws::run(&cfg, book_tx) => {
                if let Err(e) = res { tracing::error!(error = %e, "book ws failed"); }
            }
            _ = cancel.cancelled() => {}
        }
    });
    tracing::info!("orderbook WebSocket started");
}

pub fn build_router(state: S) -> Router {
    Router::new()
        .route("/", get(serve_ui))
        .route("/api/config", get(get_config))
        .route("/api/wallet", post(post_wallet))
        .route("/api/fetch-market", post(post_fetch_market))
        .route("/api/sweep/balances", get(get_balances))
        .route("/api/sweep/status", get(get_sweep_status))
        .route("/api/sweep/start", post(post_sweep_start))
        .route("/api/sweep/stop", post(post_sweep_stop))
        .route("/api/sweep/builder", post(post_builder))
        .route("/api/book", get(get_book))
        .route("/api/events", get(get_events))
        .route("/api/ctf-split", post(post_ctf_split))
        .route("/api/move-tokens", post(post_move_tokens))
        .route("/api/move-usdc", post(post_move_usdc))
        .route("/api/cancel-all", post(post_cancel_all))
        .route("/api/refresh-tick", post(post_refresh_tick))
        .route("/api/trade", post(post_trade))
        .layer(CorsLayer::permissive())
        .with_state(state)
}

// ── UI ──────────────────────────────────────────────────────────────────────

async fn serve_ui() -> axum::response::Html<&'static str> {
    axum::response::Html(web::SWEEP_HTML)
}

// ── Config ──────────────────────────────────────────────────────────────────

async fn get_config(State(state): State<S>) -> Json<serde_json::Value> {
    let config = state.config.read().unwrap();
    let eoa = config.eoa_address();
    let api_key_set = state.auth.read().unwrap().is_some();
    let builder_masked = if config.builder_api_key.is_empty() { String::new() }
        else {
            let k = &config.builder_api_key;
            let end = k.len().min(8);
            let tail_start = k.len().saturating_sub(4);
            format!("{}...{}", &k[..end], &k[tail_start..])
        };
    Json(serde_json::json!({
        "team_a_name": config.team_a_name,
        "team_b_name": config.team_b_name,
        "team_a_token_id": config.team_a_token_id,
        "team_b_token_id": config.team_b_token_id,
        "condition_id": config.condition_id,
        "dry_run": config.dry_run,
        "signature_type": config.signature_type,
        "neg_risk": config.neg_risk,
        "wallet_set": config.has_wallet(),
        "polymarket_address": config.polymarket_address,
        "eoa_address": eoa,
        "market_slug": config.market_slug,
        "api_key_set": api_key_set,
        "private_key_set": config.has_wallet(),
        "builder_key_set": !config.builder_api_key.is_empty(),
        "builder_api_key_masked": builder_masked,
    }))
}

// ── Wallet ──────────────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct WalletRequest {
    private_key: Option<String>,
    signature_type: Option<u8>,
}

async fn post_wallet(
    State(state): State<S>,
    Json(body): Json<WalletRequest>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    {
        let mut config = state.config.write().unwrap();
        if let Some(v) = body.private_key { config.polymarket_private_key = v; }
        if let Some(v) = body.signature_type { config.signature_type = v; }
        // Clear old API keys so derivation runs fresh
        config.api_key = String::new();
        config.api_secret = String::new();
        config.api_passphrase = String::new();
        // Auto-derive proxy address from private key + signature_type
        config.auto_derive_proxy();
    }

    let (eoa, proxy) = {
        let config = state.config.read().unwrap();
        (config.eoa_address(), config.polymarket_address.clone())
    };
    state.config.read().unwrap().persist();

    let shared = state.shared_config();
    if shared.has_wallet() {
        match ClobAuth::derive(&shared).await {
            Ok(auth) => {
                let api_key = auth.api_key.clone();
                {
                    let mut cfg = state.config.write().unwrap();
                    cfg.api_key = auth.api_key.clone();
                    cfg.api_secret = auth.api_secret.clone();
                    cfg.api_passphrase = auth.passphrase.clone();
                    cfg.persist();
                }
                *state.auth.write().unwrap() = Some(auth);
                state.push_event("wallet", &format!(
                    "wallet OK — EOA: {} | Proxy: {} | API key: {}",
                    eoa.as_deref().unwrap_or("?"), proxy, api_key
                ));
                return Ok(Json(serde_json::json!({
                    "ok": true,
                    "eoa_address": eoa,
                    "proxy_address": proxy,
                    "api_key": api_key,
                })));
            }
            Err(e) => {
                return Err((StatusCode::INTERNAL_SERVER_ERROR, format!("auth failed: {e}")));
            }
        }
    }

    Ok(Json(serde_json::json!({"ok": true})))
}

// ── Fetch Market ────────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct FetchMarketRequest {
    slug: String,
}

async fn post_fetch_market(
    State(state): State<S>,
    Json(body): Json<FetchMarketRequest>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let url = format!("https://gamma-api.polymarket.com/markets?slug={}", body.slug);
    let resp = reqwest::get(&url).await
        .map_err(|e| (StatusCode::BAD_GATEWAY, format!("fetch failed: {e}")))?;
    let markets: Vec<serde_json::Value> = resp.json().await
        .map_err(|e| (StatusCode::BAD_GATEWAY, format!("parse failed: {e}")))?;

    let market = markets.first()
        .ok_or_else(|| (StatusCode::NOT_FOUND, "no market found".into()))?;

    let condition_id = market["conditionId"].as_str().unwrap_or("").to_string();
    let neg_risk = market["negRisk"].as_bool().unwrap_or(false);
    let restricted = market["restricted"].as_bool().unwrap_or(false);
    let tick_size = market["orderPriceMinTickSize"]
        .as_f64().map(|v| format!("{v}")).unwrap_or_else(|| "0.01".into());
    let order_min_size = market["orderMinSize"].as_f64().unwrap_or(1.0);

    let outcomes: Vec<String> = serde_json::from_str(
        market["outcomes"].as_str().unwrap_or("[]")
    ).unwrap_or_default();
    let token_ids: Vec<String> = serde_json::from_str(
        market["clobTokenIds"].as_str().unwrap_or("[]")
    ).unwrap_or_default();

    let team_a_name = outcomes.first().cloned().unwrap_or_default();
    let team_b_name = outcomes.get(1).cloned().unwrap_or_default();
    let team_a_token = token_ids.first().cloned().unwrap_or_default();
    let team_b_token = token_ids.get(1).cloned().unwrap_or_default();

    {
        let mut config = state.config.write().unwrap();
        config.team_a_name = team_a_name.clone();
        config.team_b_name = team_b_name.clone();
        config.team_a_token_id = team_a_token.clone();
        config.team_b_token_id = team_b_token.clone();
        config.condition_id = condition_id.clone();
        config.neg_risk = neg_risk;
        config.tick_size = tick_size.clone();
        config.order_min_size = Decimal::from_str(&order_min_size.to_string()).unwrap_or(Decimal::ONE);
        config.market_slug = body.slug.clone();
        config.persist();
    }

    state.push_event("setup", &format!("fetched: {} vs {} (tick={})", team_a_name, team_b_name, tick_size));
    start_book_ws(&state);

    Ok(Json(serde_json::json!({
        "ok": true,
        "team_a_name": team_a_name, "team_b_name": team_b_name,
        "team_a_token_id": team_a_token, "team_b_token_id": team_b_token,
        "condition_id": condition_id, "neg_risk": neg_risk,
        "tick_size": tick_size, "order_min_size": order_min_size,
        "restricted": restricted,
    })))
}

// ── Balances ────────────────────────────────────────────────────────────────

async fn get_balances(State(state): State<S>) -> Json<serde_json::Value> {
    let config = state.config.read().unwrap().clone();
    let shared = config.to_shared_config();
    let eoa_address = config.eoa_address();
    let proxy_address = if config.polymarket_address.is_empty() { None } else { Some(config.polymarket_address.clone()) };

    let eoa_usdc = match &eoa_address {
        Some(addr) => ctf::usdc_balance(&shared.polygon_rpc, addr).await.ok(),
        None => None,
    };
    let proxy_usdc = match &proxy_address {
        Some(addr) => ctf::usdc_balance(&shared.polygon_rpc, addr).await.ok(),
        None => None,
    };
    // Query token balances for BOTH EOA and proxy (split lands in EOA, trading uses proxy)
    let (eoa_token_a, eoa_token_b, proxy_token_a, proxy_token_b) = if shared.has_tokens() {
        // Build an EOA-direct config (sig_type=0) to query EOA balances
        let mut eoa_cfg = shared.clone();
        eoa_cfg.signature_type = 0;
        eoa_cfg.polymarket_address = String::new();

        let ea = ctf::balance_of(&eoa_cfg, &shared.team_a_token_id).await.ok();
        let eb = ctf::balance_of(&eoa_cfg, &shared.team_b_token_id).await.ok();
        let pa = ctf::balance_of(&shared, &shared.team_a_token_id).await.ok();
        let pb = ctf::balance_of(&shared, &shared.team_b_token_id).await.ok();

        // Position tracker uses proxy balances (that's where CLOB trades from)
        if let (Some(a_val), Some(b_val)) = (pa, pb) {
            let mut pos = state.position.lock().unwrap();
            pos.team_a_tokens = a_val;
            pos.team_b_tokens = b_val;
        }
        (ea, eb, pa, pb)
    } else {
        (None, None, None, None)
    };

    // Fetch current tick size from CLOB (may change during match)
    let tick_size = if !shared.team_a_token_id.is_empty() {
        let url = format!("{}/tick-size?token_id={}", shared.clob_http, shared.team_a_token_id);
        match reqwest::get(&url).await {
            Ok(r) => r.json::<serde_json::Value>().await.ok()
                .and_then(|v| v.get("minimum_tick_size").and_then(|t| t.as_f64()))
                .map(|v| format!("{v}")),
            Err(_) => None,
        }
    } else {
        None
    };

    Json(serde_json::json!({
        "eoa_address": eoa_address,
        "proxy_address": proxy_address,
        "eoa_usdc": eoa_usdc.map(|v| v.to_string()),
        "proxy_usdc": proxy_usdc.map(|v| v.to_string()),
        "team_a_name": shared.team_a_name,
        "team_b_name": shared.team_b_name,
        "eoa_team_a_tokens": eoa_token_a.map(|v| v.to_string()),
        "eoa_team_b_tokens": eoa_token_b.map(|v| v.to_string()),
        "proxy_team_a_tokens": proxy_token_a.map(|v| v.to_string()),
        "proxy_team_b_tokens": proxy_token_b.map(|v| v.to_string()),
        "sig_type": shared.signature_type,
        "tick_size": tick_size,
    }))
}

// ── Book ────────────────────────────────────────────────────────────────────

#[derive(serde::Serialize)]
#[allow(dead_code)]
struct BookLevel { price: Decimal, size: Decimal }

async fn get_book(State(state): State<S>) -> Json<serde_json::Value> {
    let config = state.config.read().unwrap();
    let br = state.book_rx.read().unwrap();
    let (a_bids, a_asks, b_bids, b_asks) = if let Some(rx) = br.as_ref() {
        let books = rx.borrow().clone();
        let to_levels = |levels: &[crate::types::PriceLevel]| -> Vec<serde_json::Value> {
            levels.iter().take(5).map(|l| serde_json::json!({"price": l.price, "size": l.size})).collect()
        };
        (to_levels(&books.0.bids.levels), to_levels(&books.0.asks.levels),
         to_levels(&books.1.bids.levels), to_levels(&books.1.asks.levels))
    } else {
        (vec![], vec![], vec![], vec![])
    };
    Json(serde_json::json!({
        "team_a_name": config.team_a_name, "team_b_name": config.team_b_name,
        "team_a_bids": a_bids, "team_a_asks": a_asks,
        "team_b_bids": b_bids, "team_b_asks": b_asks,
    }))
}

// ── Events ──────────────────────────────────────────────────────────────────

async fn get_events(State(state): State<S>) -> Json<Vec<crate::sweep_state::EventEntry>> {
    let events = state.events.lock().unwrap();
    Json(events.iter().cloned().collect())
}

// ── Sweep Start/Stop ────────────────────────────────────────────────────────

const LEVELS: usize = 5;

#[derive(Deserialize)]
struct SweepStartRequest {
    winning_team: String,
    budget_usdc: String,
    dry_run: Option<bool>,
    /// If true, use fixed prices (0.995-0.999 / 0.001-0.005). Default false = book-relative.
    absolute: Option<bool>,
}

async fn post_sweep_start(
    State(state): State<S>,
    Json(body): Json<SweepStartRequest>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    if *state.phase.read().unwrap() == SweepPhase::Active {
        return Err((StatusCode::CONFLICT, "sweep already active — stop first".into()));
    }

    let shared = state.shared_config();
    if !shared.has_wallet() {
        return Err((StatusCode::BAD_REQUEST, "wallet not configured".into()));
    }
    if !shared.has_tokens() {
        return Err((StatusCode::BAD_REQUEST, "token IDs not set — fetch market first".into()));
    }

    // Ensure auth
    if state.auth.read().unwrap().is_none() {
        match ClobAuth::derive(&shared).await {
            Ok(auth) => { *state.auth.write().unwrap() = Some(auth); }
            Err(e) => return Err((StatusCode::INTERNAL_SERVER_ERROR, format!("auth failed: {e}"))),
        }
    }

    let winning = match body.winning_team.to_uppercase().as_str() {
        "A" => Team::TeamA,
        "B" => Team::TeamB,
        _ => return Err((StatusCode::BAD_REQUEST, "winning_team must be 'A' or 'B'".into())),
    };
    let losing = winning.opponent();

    let budget: Decimal = body.budget_usdc.parse()
        .map_err(|_| (StatusCode::BAD_REQUEST, "invalid budget_usdc".into()))?;
    let dry_run = body.dry_run.unwrap_or(true);
    let absolute = body.absolute.unwrap_or(false);

    // Safety: check losing team price
    {
        let br = state.book_rx.read().unwrap();
        if let Some(rx) = br.as_ref() {
            let books = rx.borrow().clone();
            let lose_book = match winning {
                Team::TeamA => &books.1,
                Team::TeamB => &books.0,
            };
            if let Some(ask) = lose_book.best_ask() {
                let lose_name = shared.team_name(losing);
                if ask.price >= Decimal::new(5, 2) {
                    return Err((StatusCode::BAD_REQUEST, format!(
                        "BLOCKED: {} at {:.1}¢ (>= 5¢) — too early",
                        lose_name, ask.price * Decimal::from(100)
                    )));
                }
                if ask.price >= Decimal::new(1, 2) {
                    state.push_event("warn", &format!(
                        "WARNING: {} still at {:.1}¢",
                        lose_name, ask.price * Decimal::from(100)
                    ));
                }
            }
        }
    }

    // Persist config
    {
        let mut cfg = state.config.write().unwrap();
        cfg.sweep_budget_usdc = budget;
        cfg.dry_run = dry_run;
        cfg.persist();
    }

    start_book_ws(&state);

    // Sync balances
    if shared.has_tokens() {
        if let Ok((a, b)) = ctf::sync_balances(&shared).await {
            let mut pos = state.position.lock().unwrap();
            pos.team_a_tokens = a;
            pos.team_b_tokens = b;
        }
    }

    // Refresh tick size
    let tick = refresh_tick_from_clob(&state, &shared).await;

    // ── Build 10 GTC orders (5 buy + 5 sell) ────────────────────────────
    let books = {
        let br = state.book_rx.read().unwrap();
        match br.as_ref() {
            Some(rx) => rx.borrow().clone(),
            None => return Err((StatusCode::BAD_REQUEST, "no orderbook data yet — wait for WS".into())),
        }
    };

    let (win_book, lose_book) = match winning {
        Team::TeamA => (&books.0, &books.1),
        Team::TeamB => (&books.1, &books.0),
    };

    let losing_balance = state.position.lock().unwrap().token_balance(losing);

    let max_price = Decimal::ONE - tick;  // 0.99 or 0.999 depending on tick
    let min_price = tick;                 // 0.01 or 0.001

    // Compute buy prices (winning team) — clamped to max_price
    let buy_prices: Vec<Decimal> = if absolute {
        (0..LEVELS).map(|i| (Decimal::ONE - tick * Decimal::from(LEVELS - i)).min(max_price)).collect()
    } else {
        let asks = &win_book.asks.levels;
        let start = if asks.len() >= 2 { asks[1].price } else if !asks.is_empty() { asks[0].price + tick } else {
            return Err((StatusCode::BAD_REQUEST, "no asks on winning team book".into()));
        };
        // Clamp all levels to max_price — if 2nd ask is 0.998 with tick 0.01,
        // levels would be 0.998, 1.008(!) — clamp overflow to max_price
        (0..LEVELS).map(|i| (start + tick * Decimal::from(i)).min(max_price)).collect()
    };

    // Compute sell prices (losing team) — clamped to min_price
    let sell_prices: Vec<Decimal> = if absolute {
        (0..LEVELS).map(|i| (tick * Decimal::from(LEVELS - i)).max(min_price)).collect()
    } else {
        let bids = &lose_book.bids.levels;
        let start = if bids.len() >= 2 { bids[1].price } else if !bids.is_empty() { bids[0].price - tick } else {
            Decimal::new(1, 2)
        };
        (0..LEVELS).map(|i| (start - tick * Decimal::from(i)).max(min_price)).collect()
    };

    // Deduplicate — if multiple levels clamped to the same price, keep only one
    let dedup = |prices: Vec<Decimal>| -> Vec<Decimal> {
        let mut seen = Vec::new();
        for p in prices { if !seen.contains(&p) { seen.push(p); } }
        seen
    };
    let buy_prices = dedup(buy_prices);
    let sell_prices = dedup(sell_prices);
    let buy_levels = buy_prices.len();
    let sell_levels = sell_prices.len();
    let budget_per_level = if buy_levels > 0 { budget / Decimal::from(buy_levels) } else { Decimal::ZERO };

    // Build order batch
    let mut batch: Vec<(FakOrder, String)> = Vec::with_capacity(LEVELS * 2);

    for (i, &price) in buy_prices.iter().enumerate() {
        if price <= Decimal::ZERO || price >= Decimal::ONE { continue; }
        let size = (budget_per_level / price).floor();
        if size < shared.order_min_size { continue; }
        batch.push((
            FakOrder { team: winning, side: Side::Buy, price, size },
            format!("sweep-buy-{}-{}", i + 1, price),
        ));
    }

    let sell_per_level = if losing_balance >= shared.order_min_size && sell_levels > 0 {
        (losing_balance / Decimal::from(sell_levels)).floor()
    } else {
        Decimal::ZERO
    };
    let mut sell_remaining = losing_balance;
    for (i, &price) in sell_prices.iter().enumerate() {
        if price <= Decimal::ZERO || price >= Decimal::ONE { continue; }
        let sz = if i == sell_levels - 1 { sell_remaining } else { sell_per_level.min(sell_remaining) };
        if sz < shared.order_min_size { continue; }
        sell_remaining -= sz;
        batch.push((
            FakOrder { team: losing, side: Side::Sell, price, size: sz },
            format!("sweep-sell-{}-{}", i + 1, price),
        ));
    }

    if batch.is_empty() {
        return Err((StatusCode::BAD_REQUEST, "no valid orders to place (check budget and token balance)".into()));
    }

    // Log what we're about to do
    let mode = if absolute { "ABSOLUTE" } else { "BOOK-RELATIVE" };
    let buy_desc: Vec<String> = buy_prices.iter().map(|p| format!("{p}")).collect();
    let sell_desc: Vec<String> = sell_prices.iter().map(|p| format!("{p}")).collect();
    state.push_event("sweep", &format!(
        "SWEEP {} mode: {} wins | BUY @ [{}] | SELL @ [{}] | tick={} | dry={}",
        mode, shared.team_name(winning),
        buy_desc.join(","), sell_desc.join(","),
        tick, dry_run
    ));

    if dry_run {
        for (order, tag) in &batch {
            state.push_event("sweep", &format!(
                "[DRY] GTC {} {} @ {} sz={} ({})",
                order.side, shared.team_name(order.team), order.price, order.size, tag
            ));
        }
        // Dry run: don't set Active — user can immediately re-run with live mode
        return Ok(Json(serde_json::json!({
            "ok": true, "mode": mode, "orders": batch.len(), "dry_run": true,
            "buy_prices": buy_desc, "sell_prices": sell_desc,
        })));
    }

    // ── Fire batch POST /orders (all 10 GTC in one HTTP call) ───────────
    let auth = state.auth.read().unwrap().clone().unwrap();
    let batch_refs: Vec<(FakOrder, &str)> = batch.iter().map(|(o, t)| (o.clone(), t.as_str())).collect();

    let t0 = tokio::time::Instant::now();
    let results = orders::post_gtc_orders_batch(&shared, &auth, &batch_refs).await
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, format!("batch failed: {e}")))?;
    let batch_ms = t0.elapsed().as_millis() as u64;
    state.latency.lock().unwrap().record(batch_ms);

    // Track accepted orders
    let mut tracked = Vec::new();
    for (i, r) in results.iter().enumerate() {
        if let Some((order, tag)) = batch.get(i) {
            let err = r.error_msg.as_deref().unwrap_or("");
            if let Some(oid) = r.order_id.as_deref().filter(|s| !s.is_empty()) {
                state.push_event("sweep", &format!(
                    "GTC {} {} @ {} sz={} [{}] ({})",
                    order.side, shared.team_name(order.team), order.price, order.size, oid, tag
                ));
                tracked.push(SweepOrder {
                    order_id: oid.to_string(), team: order.team, side: order.side,
                    price: order.price, size: order.size,
                });
            } else if !err.is_empty() {
                state.push_event("error", &format!("{}: {}", tag, err));
            }
        }
    }

    state.push_event("sweep", &format!(
        "batch done: {}/{} accepted ({}ms)", tracked.len(), batch.len(), batch_ms
    ));

    state.sweep_orders.lock().unwrap().extend(tracked);
    *state.winning_team.write().unwrap() = Some(winning);
    *state.phase.write().unwrap() = SweepPhase::Active;

    // Start heartbeat to keep GTC orders alive
    let hb_auth = auth.clone();
    let hb_cancel = tokio_util::sync::CancellationToken::new();
    *state.heartbeat_cancel.write().unwrap() = Some(hb_cancel.clone());
    tokio::spawn(async move {
        heartbeat::run_standalone(hb_auth, hb_cancel).await;
    });

    Ok(Json(serde_json::json!({
        "ok": true, "mode": mode, "orders": batch.len(),
        "accepted": state.sweep_orders.lock().unwrap().len(),
        "batch_ms": batch_ms,
        "buy_prices": buy_desc, "sell_prices": sell_desc,
    })))
}

/// Fetch current tick size from CLOB and update config.
async fn refresh_tick_from_clob(state: &Arc<SweepAppState>, shared: &crate::config::Config) -> Decimal {
    let current = state.config.read().unwrap().tick_size.clone();
    if shared.team_a_token_id.is_empty() {
        return current.parse().unwrap_or(rust_decimal_macros::dec!(0.01));
    }
    let url = format!("{}/tick-size?token_id={}", shared.clob_http, shared.team_a_token_id);
    if let Ok(resp) = reqwest::get(&url).await {
        if let Ok(val) = resp.json::<serde_json::Value>().await {
            if let Some(tick) = val.get("minimum_tick_size").and_then(|t| t.as_f64()) {
                let tick_str = format!("{tick}");
                if tick_str != current {
                    tracing::info!(old = %current, new = %tick_str, "[SWEEP] tick size updated");
                    state.push_event("config", &format!("tick: {} → {}", current, tick_str));
                }
                state.config.write().unwrap().tick_size = tick_str.clone();
                return tick_str.parse().unwrap_or(rust_decimal_macros::dec!(0.01));
            }
        }
    }
    current.parse().unwrap_or(rust_decimal_macros::dec!(0.01))
}

async fn post_sweep_stop(
    State(state): State<S>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    if *state.phase.read().unwrap() != SweepPhase::Active {
        return Err((StatusCode::CONFLICT, "sweep not active".into()));
    }
    if let Some(cancel) = state.sweep_cancel.read().unwrap().clone() {
        cancel.cancel();
    }
    *state.sweep_cancel.write().unwrap() = None;
    // Stop heartbeat
    if let Some(hb) = state.heartbeat_cancel.read().unwrap().clone() {
        hb.cancel();
    }
    *state.heartbeat_cancel.write().unwrap() = None;
    Ok(Json(serde_json::json!({"ok": true})))
}

// ── Sweep Status ────────────────────────────────────────────────────────────

async fn get_sweep_status(State(state): State<S>) -> Json<serde_json::Value> {
    let phase = *state.phase.read().unwrap();
    let orders = state.sweep_orders.lock().unwrap().clone();
    let config = state.config.read().unwrap();
    let winning = *state.winning_team.read().unwrap();
    let latency = state.latency.lock().unwrap().clone();

    Json(serde_json::json!({
        "phase": phase,
        "resting_orders": orders.len(),
        "orders": orders,
        "dry_run": config.dry_run,
        "budget": config.sweep_budget_usdc.to_string(),
        "winning_team": winning,
        "builder_key_set": !config.builder_api_key.is_empty(),
        "latency": latency,
    }))
}

// ── Builder Keys ────────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct BuilderKeysRequest {
    builder_api_key: Option<String>,
    builder_api_secret: Option<String>,
    builder_api_passphrase: Option<String>,
}

async fn post_builder(
    State(state): State<S>,
    Json(body): Json<BuilderKeysRequest>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let mut config = state.config.write().unwrap();
    // Only update non-empty values — never overwrite with blank
    if let Some(v) = body.builder_api_key {
        if v.trim().is_empty() { return Err((StatusCode::BAD_REQUEST, "builder API key cannot be empty".into())); }
        config.builder_api_key = v;
    }
    if let Some(v) = body.builder_api_secret {
        if v.trim().is_empty() { return Err((StatusCode::BAD_REQUEST, "builder secret cannot be empty".into())); }
        config.builder_api_secret = v;
    }
    if let Some(v) = body.builder_api_passphrase {
        if v.trim().is_empty() { return Err((StatusCode::BAD_REQUEST, "builder passphrase cannot be empty".into())); }
        config.builder_api_passphrase = v;
    }
    config.persist();
    state.push_event("config", "builder keys saved");
    Ok(Json(serde_json::json!({"ok": true})))
}

// ── CTF Split ───────────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct CtfSplitRequest { amount_usdc: u64 }

async fn post_ctf_split(
    State(state): State<S>,
    Json(body): Json<CtfSplitRequest>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let shared = state.shared_config();
    if !shared.has_wallet() {
        return Err((StatusCode::BAD_REQUEST, "wallet not configured".into()));
    }
    if shared.condition_id.is_empty() {
        return Err((StatusCode::BAD_REQUEST, "condition_id not set".into()));
    }

    state.push_event("ctf", &format!("splitting {} USDC...", body.amount_usdc));
    match ctf::split(&shared, &shared.condition_id, body.amount_usdc).await {
        Ok(tx) => {
            state.push_event("ctf", &format!("split done: tx={tx}"));
            Ok(Json(serde_json::json!({"ok": true, "tx_hash": tx})))
        }
        Err(e) => {
            state.push_event("error", &format!("split failed: {e}"));
            Err((StatusCode::INTERNAL_SERVER_ERROR, format!("{e}")))
        }
    }
}

// ── Move Tokens / USDC ──────────────────────────────────────────────────────

#[derive(Deserialize)]
struct MoveTokensRequest { direction: String }

async fn post_move_tokens(
    State(state): State<S>,
    Json(body): Json<MoveTokensRequest>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let shared = state.shared_config();
    if !shared.has_wallet() || !shared.has_tokens() {
        return Err((StatusCode::BAD_REQUEST, "wallet or tokens not configured".into()));
    }

    let result = if body.direction == "to_proxy" {
        ctf::move_tokens_to_proxy(&shared).await
    } else {
        ctf::move_tokens_to_eoa(&shared).await
    };

    match result {
        Ok((tx, a, b)) => {
            state.push_event("move", &format!("tokens moved: {a:.2} A + {b:.2} B tx={tx}"));
            Ok(Json(serde_json::json!({"ok": true, "tx": tx})))
        }
        Err(e) => Err((StatusCode::INTERNAL_SERVER_ERROR, format!("{e}"))),
    }
}

#[derive(Deserialize)]
struct MoveUsdcRequest { amount_usdc: u64, direction: String }

async fn post_move_usdc(
    State(state): State<S>,
    Json(body): Json<MoveUsdcRequest>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let shared = state.shared_config();
    if !shared.has_wallet() {
        return Err((StatusCode::BAD_REQUEST, "wallet not configured".into()));
    }

    let result = if body.direction == "to_proxy" {
        ctf::move_usdc_to_proxy(&shared, body.amount_usdc).await
    } else {
        ctf::move_usdc_to_eoa(&shared, body.amount_usdc).await
    };

    match result {
        Ok(tx) => {
            state.push_event("move", &format!("USDC moved: tx={tx}"));
            Ok(Json(serde_json::json!({"ok": true, "tx": tx})))
        }
        Err(e) => Err((StatusCode::INTERNAL_SERVER_ERROR, format!("{e}"))),
    }
}

// ── Cancel All ──────────────────────────────────────────────────────────────

async fn post_cancel_all(
    State(state): State<S>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let auth = state.auth.read().unwrap().clone()
        .ok_or_else(|| (StatusCode::BAD_REQUEST, "no auth".into()))?;

    match orders::cancel_all_open_orders(&auth).await {
        Ok(()) => {
            state.sweep_orders.lock().unwrap().clear();
            *state.phase.write().unwrap() = SweepPhase::Idle;
            // Stop heartbeat if running
            if let Some(hb) = state.heartbeat_cancel.read().unwrap().clone() {
                hb.cancel();
            }
            *state.heartbeat_cancel.write().unwrap() = None;
            state.push_event("cancel", "all orders cancelled — ready to sweep again");
            Ok(Json(serde_json::json!({"ok": true})))
        }
        Err(e) => Err((StatusCode::INTERNAL_SERVER_ERROR, format!("{e}"))),
    }
}

// ── Refresh Tick Size ───────────────────────────────────────────────────────

async fn post_refresh_tick(
    State(state): State<S>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let token_id = state.config.read().unwrap().team_a_token_id.clone();
    if token_id.is_empty() {
        return Err((StatusCode::BAD_REQUEST, "no market set".into()));
    }
    let clob = state.config.read().unwrap().clob_http.clone();
    let url = format!("{clob}/tick-size?token_id={token_id}");
    let resp = reqwest::get(&url).await
        .map_err(|e| (StatusCode::BAD_GATEWAY, format!("{e}")))?;
    let val: serde_json::Value = resp.json().await
        .map_err(|e| (StatusCode::BAD_GATEWAY, format!("{e}")))?;
    let tick = val.get("minimum_tick_size")
        .and_then(|t| t.as_f64())
        .ok_or_else(|| (StatusCode::BAD_GATEWAY, "no tick size in response".into()))?;
    let tick_str = format!("{tick}");
    let old = {
        let mut cfg = state.config.write().unwrap();
        let old = cfg.tick_size.clone();
        cfg.tick_size = tick_str.clone();
        old
    };
    if old != tick_str {
        state.push_event("config", &format!("tick size changed: {old} -> {tick_str}"));
    }
    Ok(Json(serde_json::json!({"ok": true, "tick_size": tick_str, "changed": old != tick_str})))
}

// ── Manual Trade ────────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct TradeRequest {
    team: String,     // "A" or "B"
    side: String,     // "BUY" or "SELL"
    price: String,    // e.g. "0.55"
    size: String,     // e.g. "100" (tokens)
}

async fn post_trade(
    State(state): State<S>,
    Json(body): Json<TradeRequest>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let shared = state.shared_config();
    if !shared.has_wallet() {
        return Err((StatusCode::BAD_REQUEST, "wallet not configured".into()));
    }

    let auth = state.auth.read().unwrap().clone()
        .ok_or_else(|| (StatusCode::BAD_REQUEST, "no auth — save wallet first".into()))?;

    let team = match body.team.to_uppercase().as_str() {
        "A" => Team::TeamA,
        "B" => Team::TeamB,
        _ => return Err((StatusCode::BAD_REQUEST, "team must be 'A' or 'B'".into())),
    };

    let price: Decimal = body.price.parse()
        .map_err(|_| (StatusCode::BAD_REQUEST, "invalid price".into()))?;
    let size: Decimal = body.size.parse()
        .map_err(|_| (StatusCode::BAD_REQUEST, "invalid size".into()))?;

    if price <= Decimal::ZERO || price >= Decimal::ONE {
        return Err((StatusCode::BAD_REQUEST, "price must be between 0 and 1".into()));
    }
    if size < shared.order_min_size {
        return Err((StatusCode::BAD_REQUEST, format!("size must be >= {}", shared.order_min_size)));
    }

    let team_name = shared.team_name(team).to_string();
    let side_str = body.side.to_uppercase();

    let result = match side_str.as_str() {
        "BUY" => trading::limit_buy(&shared, &auth, team, price, size).await,
        "SELL" => trading::limit_sell(&shared, &auth, team, price, size).await,
        _ => return Err((StatusCode::BAD_REQUEST, "side must be 'BUY' or 'SELL'".into())),
    };

    match result {
        Ok((oid, ms)) => {
            state.push_event("trade", &format!(
                "GTC {} {} {} @ {} [{}] ({}ms)",
                side_str, size, team_name, price, oid, ms
            ));
            state.latency.lock().unwrap().record(ms);
            Ok(Json(serde_json::json!({
                "ok": true, "order_id": oid, "latency_ms": ms,
            })))
        }
        Err(e) => {
            state.push_event("error", &format!("trade failed: {e}"));
            Err((StatusCode::INTERNAL_SERVER_ERROR, format!("{e}")))
        }
    }
}
