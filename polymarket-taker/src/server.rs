use std::str::FromStr;
use std::sync::Arc;

use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::response::Json;
use axum::routing::{get, post};
use axum::Router;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use tokio::sync::{mpsc, watch};
use tower_http::cors::CorsLayer;

use crate::capture;
use crate::clob_auth::ClobAuth;
use crate::ctf;
use crate::market_ws;
use crate::sweep;

/// How often to sync on-chain token balances into the position tracker
/// while an innings is running.
const CHAIN_SYNC_INTERVAL_SECS: u64 = 30;
use crate::config::MakerConfig;
use crate::heartbeat;
use crate::maker;
use crate::order_cache;
use crate::orders;
use crate::state::{AppState, DispatchedSignal, MatchPhase};
use crate::strategy;
use crate::types::{CricketSignal, FillEvent, OrderBook, Team};
use crate::user_ws;
use crate::web;

type S = Arc<AppState>;


/// Start (or restart) the orderbook WebSocket feed so the UI shows live
/// bid/ask data even before a match is started.  Safe to call multiple times —
/// cancels any existing WS before spawning a new one.
pub fn start_book_ws(state: &Arc<AppState>) {
    let config = state.config.read().unwrap().clone();
    if !config.has_tokens() {
        return; // nothing to subscribe to yet
    }

    // Cancel previous WS if running
    if let Some(old) = state.ws_cancel.read().unwrap().as_ref() {
        old.cancel();
    }

    let (book_tx, book_rx) = watch::channel((OrderBook::default(), OrderBook::default()));
    *state.book_rx.write().unwrap() = Some(book_rx);
    *state.book_tx.write().unwrap() = Some(book_tx.clone());

    // WS health channel — strategy reads from `ws_health_rx` to gate trades
    // on connection state + snapshot freshness instead of book timestamps.
    let (health_tx, health_rx) = watch::channel(crate::ws_health::WsHealth::default());
    *state.ws_health_rx.write().unwrap() = Some(health_rx);
    *state.ws_health_tx.write().unwrap() = Some(health_tx.clone());

    let cancel = tokio_util::sync::CancellationToken::new();
    *state.ws_cancel.write().unwrap() = Some(cancel.clone());

    let team_a = config.team_a_name.clone();
    let team_b = config.team_b_name.clone();
    let price_history = state.price_history.clone();
    tokio::spawn(async move {
        tokio::select! {
            res = market_ws::run(&config, book_tx, health_tx, Some(price_history)) => {
                if let Err(e) = res {
                    tracing::error!(error = %e, "book ws failed");
                }
            }
            _ = cancel.cancelled() => {
                tracing::debug!("book ws stopped (setup changed)");
            }
        }
    });
    tracing::info!("orderbook WebSocket started for {} vs {}", team_a, team_b);
}

pub fn build_router(state: S) -> Router {
    Router::new()
        .route("/", get(serve_ui))
        .route("/api/status", get(get_status))
        .route("/api/config", get(get_config))
        .route("/api/events", get(get_events))
        .route("/api/trades", get(get_trades))
        .route("/api/inventory", get(get_inventory))
        .route("/api/setup", post(post_setup))
        .route("/api/wallet", post(post_wallet))
        .route("/api/limits", post(post_limits))
        .route("/api/start-innings", post(post_start_innings))
        .route("/api/stop-innings", post(post_stop_innings))
        .route("/api/signal", post(post_signal))
        .route("/api/tg-signal", post(post_tg_signal))
        .route("/api/taker/status", get(get_taker_status))
        .route("/api/taker/cancel-order", post(post_taker_cancel_order))
        .route("/api/toggle-team", post(post_toggle_team))
        .route("/api/match-over", post(post_match_over))
        .route("/api/cancel-all", post(post_cancel_all))
        .route("/api/reset", post(post_reset))
        .route("/api/fetch-market", post(post_fetch_market))
        .route("/api/book", get(get_book))
        .route("/api/price-history", get(get_price_history))
        .route("/api/ctf-balance", post(post_ctf_balance))
        .route("/api/ctf-split", post(post_ctf_split))
        .route("/api/ctf-merge", post(post_ctf_merge))
        .route("/api/ctf-redeem", post(post_ctf_redeem))
        .route("/api/mega-resolve", post(post_mega_resolve))
        .route("/api/wallets", get(get_wallets))
        .route("/api/move-tokens", post(post_move_tokens))
        .route("/api/move-usdc", post(post_move_usdc))
        .route("/api/maker/status", get(get_maker_status))
        .route("/api/maker/config", post(post_maker_config))
        .route("/api/latency", get(get_latency))
        .route("/api/positions", get(get_positions))
        .route("/api/balance", get(get_v2_balance))
        // ── Sweep (endgame) ───────────────────────────────────────────────
        .route("/sweep", get(serve_sweep_ui))
        .route("/api/sweep/status", get(get_sweep_status))
        .route("/api/sweep/start", post(post_sweep_start))
        .route("/api/sweep/stop", post(post_sweep_stop))
        .route("/api/sweep/balances", get(get_sweep_balances))
        .route("/api/sweep/builder", post(post_sweep_builder))
        .layer(CorsLayer::permissive())
        .with_state(state)
}

// ── UI ──────────────────────────────────────────────────────────────────────

async fn serve_ui() -> axum::response::Html<&'static str> {
    axum::response::Html(web::INDEX_HTML)
}

// ── Status ──────────────────────────────────────────────────────────────────

#[derive(Serialize)]
struct StatusResponse {
    phase: MatchPhase,
    batting: String,
    bowling: String,
    innings: u8,
    team_a_name: String,
    team_b_name: String,
    team_a_tokens: Decimal,
    team_b_tokens: Decimal,
    total_spent: Decimal,
    total_budget: Decimal,
    remaining: Decimal,
    trade_count: u64,
    dry_run: bool,
    wallet_set: bool,
    tokens_set: bool,
    book_a_bid: Option<Decimal>,
    book_a_ask: Option<Decimal>,
    book_b_bid: Option<Decimal>,
    book_b_ask: Option<Decimal>,
    live_orders: usize,
    pending_reverts: usize,
    trade_team_a: bool,
    trade_team_b: bool,
}

async fn get_status(State(state): State<S>) -> Json<StatusResponse> {
    let config = state.config.read().unwrap();
    let pos = state.position.lock().unwrap();
    let ms = state.match_state.read().unwrap();
    let phase = *state.phase.read().unwrap();

    let (ba_bid, ba_ask, bb_bid, bb_ask) = {
        let br = state.book_rx.read().unwrap();
        if let Some(rx) = br.as_ref() {
            let books = rx.borrow().clone();
            (
                books.0.best_bid().map(|l| l.price),
                books.0.best_ask().map(|l| l.price),
                books.1.best_bid().map(|l| l.price),
                books.1.best_ask().map(|l| l.price),
            )
        } else {
            (None, None, None, None)
        }
    };

    Json(StatusResponse {
        phase,
        batting: config.team_name(ms.batting).to_string(),
        bowling: config.team_name(ms.bowling()).to_string(),
        innings: ms.innings,
        team_a_name: config.team_a_name.clone(),
        team_b_name: config.team_b_name.clone(),
        team_a_tokens: pos.team_a_tokens,
        team_b_tokens: pos.team_b_tokens,
        total_spent: pos.total_spent,
        total_budget: pos.total_budget,
        remaining: pos.remaining_budget(),
        trade_count: pos.trade_count,
        dry_run: config.dry_run,
        wallet_set: config.has_wallet(),
        tokens_set: config.has_tokens(),
        book_a_bid: ba_bid,
        book_a_ask: ba_ask,
        book_b_bid: bb_bid,
        book_b_ask: bb_ask,
        live_orders: state.live_order_ids.lock().unwrap().len(),
        pending_reverts: state.pending_revert_count(),
        trade_team_a: *state.trade_team_a.read().unwrap(),
        trade_team_b: *state.trade_team_b.read().unwrap(),
    })
}

// ── Live order book (top N levels) ──────────────────────────────────────────

#[derive(Serialize)]
struct BookLevelDto {
    price: Decimal,
    size: Decimal,
}

#[derive(Serialize)]
struct BookResponse {
    team_a_name: String,
    team_b_name: String,
    team_a_bids: Vec<BookLevelDto>,
    team_a_asks: Vec<BookLevelDto>,
    team_b_bids: Vec<BookLevelDto>,
    team_b_asks: Vec<BookLevelDto>,
}

async fn get_book(State(state): State<S>) -> Json<BookResponse> {
    const N: usize = 7;
    let config = state.config.read().unwrap();
    let br = state.book_rx.read().unwrap();
    let (a_bids, a_asks, b_bids, b_asks) = if let Some(rx) = br.as_ref() {
        let books = rx.borrow().clone();
        let to_dto = |levels: &[crate::types::PriceLevel]| -> Vec<BookLevelDto> {
            levels.iter().take(N).map(|l| BookLevelDto { price: l.price, size: l.size }).collect()
        };
        (
            to_dto(&books.0.bids.levels),
            to_dto(&books.0.asks.levels),
            to_dto(&books.1.bids.levels),
            to_dto(&books.1.asks.levels),
        )
    } else {
        (vec![], vec![], vec![], vec![])
    };
    Json(BookResponse {
        team_a_name: config.team_a_name.clone(),
        team_b_name: config.team_b_name.clone(),
        team_a_bids: a_bids,
        team_a_asks: a_asks,
        team_b_bids: b_bids,
        team_b_asks: b_asks,
    })
}

#[derive(Deserialize)]
struct PriceHistoryQuery {
    interval: Option<String>,
}

async fn get_price_history(
    State(state): State<S>,
    Query(query): Query<PriceHistoryQuery>,
) -> Json<serde_json::Value> {
    let (token_a, token_b, name_a, name_b, clob) = {
        let config = state.config.read().unwrap();
        (
            config.team_a_token_id.clone(),
            config.team_b_token_id.clone(),
            config.team_a_name.clone(),
            config.team_b_name.clone(),
            config.clob_http.clone(),
        )
    };

    if token_a.is_empty() || token_b.is_empty() {
        return Json(serde_json::json!({"team_a_name": name_a, "team_b_name": name_b, "team_a": [], "team_b": []}));
    }

    let api_interval = query.interval.as_deref().unwrap_or("1h");

    let url_a = format!("{clob}/prices-history?interval={api_interval}&market={token_a}&fidelity=1");
    let url_b = format!("{clob}/prices-history?interval={api_interval}&market={token_b}&fidelity=1");

    let client = reqwest::Client::new();
    let (ra, rb) = tokio::join!(client.get(&url_a).send(), client.get(&url_b).send());

    let a = match ra {
        Ok(r) => r.json::<serde_json::Value>().await.unwrap_or_default(),
        Err(_) => serde_json::json!({}),
    };
    let b = match rb {
        Ok(r) => r.json::<serde_json::Value>().await.unwrap_or_default(),
        Err(_) => serde_json::json!({}),
    };

    Json(serde_json::json!({
        "team_a_name": name_a,
        "team_b_name": name_b,
        "team_a": a.get("history").cloned().unwrap_or(serde_json::json!([])),
        "team_b": b.get("history").cloned().unwrap_or(serde_json::json!([])),
    }))
}

async fn get_config(State(state): State<S>) -> Json<serde_json::Value> {
    let config = state.config.read().unwrap();
    let auth_guard = state.auth.read().unwrap();
    let eoa_address = auth_guard.as_ref().map(|a| a.address().to_string());
    let api_key_id = auth_guard.as_ref().map(|a| a.api_key.clone()).filter(|k| !k.is_empty());
    drop(auth_guard);
    Json(serde_json::json!({
        "team_a_name": config.team_a_name,
        "team_b_name": config.team_b_name,
        "team_a_token_id": config.team_a_token_id,
        "team_b_token_id": config.team_b_token_id,
        "condition_id": config.condition_id,
        "first_batting": format!("{}", config.first_batting),
        "total_budget_usdc": config.total_budget_usdc.to_string(),
        "max_trade_usdc": config.max_trade_usdc.to_string(),
        "safe_percentage": config.safe_percentage,
        "revert_delay_ms": config.revert_delay_ms,
        "revert_timeout_ms": config.revert_timeout_ms,
        "fill_poll_interval_ms": config.fill_poll_interval_ms,
        "fill_poll_timeout_ms": config.fill_poll_timeout_ms,
        "signal_gap_secs": config.signal_gap_secs,
        "dry_run": config.dry_run,
        "signature_type": config.signature_type,
        "neg_risk": config.neg_risk,
        "wallet_set": config.has_wallet(),
        "polymarket_address": config.polymarket_address,
        "private_key_set": config.has_wallet(),
        "eoa_address": eoa_address,
        "api_key_set": !config.api_key.is_empty() || api_key_id.is_some(),
        "api_key_id": api_key_id,
        "market_slug": config.market_slug,
        "edge_wicket": config.edge_wicket,
        "edge_boundary_4": config.edge_boundary_4,
        "edge_boundary_6": config.edge_boundary_6,
    }))
}

// ── Wallets ──────────────────────────────────────────────────────────────────
//
// Returns just the addresses. Balances come from /api/balance (CLOB pUSD
// /balance-allowance) and positions from /api/positions (data-api). The V1
// USDC.e on-chain balance read used to live here — removed because pUSD is
// the V2 collateral and CLOB resolves it server-side.

async fn get_wallets(State(state): State<S>) -> Json<serde_json::Value> {
    let config = state.config.read().unwrap().clone();
    let eoa_address = state.auth.read().unwrap().as_ref().map(|a| a.address().to_string());
    let proxy_address = if config.polymarket_address.is_empty() { None } else { Some(config.polymarket_address.clone()) };

    Json(serde_json::json!({
        "eoa_address": eoa_address,
        "proxy_address": proxy_address,
        "sig_type": config.signature_type,
    }))
}

async fn get_events(State(state): State<S>) -> Json<Vec<crate::state::EventEntry>> {
    let events = state.events.lock().unwrap();
    Json(events.iter().cloned().collect())
}

async fn get_trades(State(state): State<S>) -> Json<serde_json::Value> {
    let (auth_opt, config) = {
        let auth = state.auth.read().unwrap().clone();
        let config = state.config.read().unwrap().clone();
        (auth, config)
    };

    let auth = match auth_opt {
        Some(a) => a,
        None => return Json(serde_json::json!({"trades": [], "summary": null, "error": "no auth"})),
    };

    let token_a = &config.team_a_token_id;
    let token_b = &config.team_b_token_id;

    if token_a.is_empty() && token_b.is_empty() {
        return Json(serde_json::json!({"trades": [], "summary": null}));
    }

    // Fetch orders for both tokens in parallel
    let (res_a, res_b) = tokio::join!(
        orders::get_user_orders(&auth, if token_a.is_empty() { None } else { Some(token_a.as_str()) }),
        orders::get_user_orders(&auth, if token_b.is_empty() { None } else { Some(token_b.as_str()) }),
    );

    let orders_a = res_a.unwrap_or_default();
    let orders_b = res_b.unwrap_or_default();

    // Helper to parse order into trade record
    fn parse_order(o: &serde_json::Value, team_name: &str) -> Option<serde_json::Value> {
        let size_matched = o.get("size_matched")
            .and_then(|v| v.as_str())
            .and_then(|s| Decimal::from_str(s).ok())
            .unwrap_or(Decimal::ZERO);
        // Skip orders with no fills
        if size_matched.is_zero() { return None; }

        let side = o.get("side").and_then(|v| v.as_str()).unwrap_or("BUY");
        let price = o.get("price")
            .and_then(|v| v.as_str())
            .and_then(|s| Decimal::from_str(s).ok())
            .unwrap_or(Decimal::ZERO);
        let original_size = o.get("original_size")
            .and_then(|v| v.as_str())
            .and_then(|s| Decimal::from_str(s).ok())
            .unwrap_or(Decimal::ZERO);
        let cost = size_matched * price;
        let status = o.get("status").and_then(|v| v.as_str()).unwrap_or("unknown");
        let order_type = o.get("type").and_then(|v| v.as_str()).unwrap_or("FOK");
        let order_id = o.get("id").and_then(|v| v.as_str()).unwrap_or("");
        let created = o.get("created_at").and_then(|v| v.as_str()).unwrap_or("");
        let ts = if created.len() > 19 {
            // Parse ISO timestamp to HH:MM:SS
            created.get(11..19).unwrap_or(created)
        } else { created };

        // Map CLOB side: "BUY"/"SELL" (CLOB uses "BUY"=0, "SELL"=1 but returns string)
        let display_side = if side == "BUY" || side == "0" { "BUY" } else { "SELL" };

        Some(serde_json::json!({
            "ts": ts,
            "side": display_side,
            "team": team_name,
            "size": size_matched.to_string(),
            "original_size": original_size.to_string(),
            "price": price.to_string(),
            "cost": cost.round_dp(2).to_string(),
            "order_type": order_type,
            "status": status,
            "order_id": order_id,
        }))
    }

    let mut trades: Vec<serde_json::Value> = Vec::new();
    for o in &orders_a { if let Some(t) = parse_order(o, &config.team_a_name) { trades.push(t); } }
    for o in &orders_b { if let Some(t) = parse_order(o, &config.team_b_name) { trades.push(t); } }

    // Sort by ts
    trades.sort_by(|a, b| {
        let ta = a.get("ts").and_then(|v| v.as_str()).unwrap_or("");
        let tb = b.get("ts").and_then(|v| v.as_str()).unwrap_or("");
        ta.cmp(tb)
    });

    // Compute per-team summaries
    let mut team_a_bought = Decimal::ZERO;
    let mut team_a_sold = Decimal::ZERO;
    let mut team_a_buy_cost = Decimal::ZERO;
    let mut team_a_sell_revenue = Decimal::ZERO;
    let mut team_b_bought = Decimal::ZERO;
    let mut team_b_sold = Decimal::ZERO;
    let mut team_b_buy_cost = Decimal::ZERO;
    let mut team_b_sell_revenue = Decimal::ZERO;

    for t in &trades {
        let is_a = t.get("team").and_then(|v| v.as_str()) == Some(&config.team_a_name);
        let side = t.get("side").and_then(|v| v.as_str()).unwrap_or("");
        let size = t.get("size").and_then(|v| v.as_str()).and_then(|s| Decimal::from_str(s).ok()).unwrap_or(Decimal::ZERO);
        let cost = t.get("cost").and_then(|v| v.as_str()).and_then(|s| Decimal::from_str(s).ok()).unwrap_or(Decimal::ZERO);
        match side {
            "BUY" => {
                if is_a { team_a_bought += size; team_a_buy_cost += cost; }
                else    { team_b_bought += size; team_b_buy_cost += cost; }
            }
            "SELL" => {
                if is_a { team_a_sold += size; team_a_sell_revenue += cost; }
                else    { team_b_sold += size; team_b_sell_revenue += cost; }
            }
            _ => {}
        }
    }

    let avg = |cost: Decimal, qty: Decimal| -> Decimal {
        if qty.is_zero() { Decimal::ZERO } else { (cost / qty).round_dp(4) }
    };

    let pnl_a = team_a_sell_revenue - team_a_buy_cost;
    let pnl_b = team_b_sell_revenue - team_b_buy_cost;

    Json(serde_json::json!({
        "trades": trades,
        "summary": {
            "team_a": {
                "name": config.team_a_name,
                "bought": team_a_bought,
                "sold": team_a_sold,
                "buy_cost": team_a_buy_cost,
                "sell_revenue": team_a_sell_revenue,
                "avg_buy": avg(team_a_buy_cost, team_a_bought),
                "avg_sell": avg(team_a_sell_revenue, team_a_sold),
                "net_tokens": team_a_bought - team_a_sold,
                "realized_pnl": pnl_a,
            },
            "team_b": {
                "name": config.team_b_name,
                "bought": team_b_bought,
                "sold": team_b_sold,
                "buy_cost": team_b_buy_cost,
                "sell_revenue": team_b_sell_revenue,
                "avg_buy": avg(team_b_buy_cost, team_b_bought),
                "avg_sell": avg(team_b_sell_revenue, team_b_sold),
                "net_tokens": team_b_bought - team_b_sold,
                "realized_pnl": pnl_b,
            },
            "total_pnl": pnl_a + pnl_b,
        }
    }))
}

async fn get_inventory(State(state): State<S>) -> Json<Vec<crate::state::InventorySnapshot>> {
    let history = state.inventory_history.lock().unwrap();
    Json(history.clone())
}

// ── Move tokens / USDC between EOA and proxy ────────────────────────────────

#[derive(Deserialize)]
struct MoveTokensRequest {
    direction: String,    // "to_proxy" or "to_eoa"
}

async fn post_move_tokens(
    State(state): State<S>,
    Json(body): Json<MoveTokensRequest>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let config = state.config.read().unwrap().clone();
    if !config.has_wallet() {
        return Err((StatusCode::BAD_REQUEST, "wallet not configured".into()));
    }
    if !config.has_tokens() {
        return Err((StatusCode::BAD_REQUEST, "token IDs not set (fetch a market first)".into()));
    }

    let label = if body.direction == "to_proxy" { "EOA → proxy" } else { "proxy → EOA" };
    state.push_event("move", &format!("moving all YES + NO tokens {label}…"));

    let result = if body.direction == "to_proxy" {
        ctf::move_tokens_to_proxy(&config).await
    } else {
        ctf::move_tokens_to_eoa(&config).await
    };

    match result {
        Ok((tx, dec_a, dec_b)) => {
            state.push_event("move", &format!("tokens moved {label}: {dec_a:.2} A + {dec_b:.2} B — tx={tx}"));
            Ok(Json(serde_json::json!({"ok": true, "tx": tx, "moved_a": dec_a.to_string(), "moved_b": dec_b.to_string()})))
        }
        Err(e) => {
            state.push_event("error", &format!("move tokens failed: {e}"));
            Err((StatusCode::INTERNAL_SERVER_ERROR, format!("move failed: {e}")))
        }
    }
}

#[derive(Deserialize)]
struct MoveUsdcRequest {
    amount_usdc: u64,   // whole dollars (e.g. 50 = $50 USDC)
    direction: String,  // "to_proxy" or "to_eoa"
}

async fn post_move_usdc(
    State(state): State<S>,
    Json(body): Json<MoveUsdcRequest>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let config = state.config.read().unwrap().clone();
    if !config.has_wallet() {
        return Err((StatusCode::BAD_REQUEST, "wallet not configured".into()));
    }
    if body.amount_usdc == 0 {
        return Err((StatusCode::BAD_REQUEST, "amount must be > 0".into()));
    }

    let label = if body.direction == "to_proxy" { "EOA → proxy" } else { "proxy → EOA" };
    state.push_event("move", &format!("moving ${} USDC {label}…", body.amount_usdc));

    let result = if body.direction == "to_proxy" {
        ctf::move_usdc_to_proxy(&config, body.amount_usdc).await
    } else {
        ctf::move_usdc_to_eoa(&config, body.amount_usdc).await
    };

    match result {
        Ok(tx) => {
            state.push_event("move", &format!("USDC moved {label}: tx={tx}"));
            Ok(Json(serde_json::json!({"ok": true, "tx": tx})))
        }
        Err(e) => {
            state.push_event("error", &format!("move USDC failed: {e}"));
            Err((StatusCode::INTERNAL_SERVER_ERROR, format!("move failed: {e}")))
        }
    }
}

// ── Setup (teams + tokens) ─────────────────────────────────────────────────

#[derive(Deserialize)]
struct SetupRequest {
    team_a_name: Option<String>,
    team_b_name: Option<String>,
    team_a_token_id: Option<String>,
    team_b_token_id: Option<String>,
    condition_id: Option<String>,
    first_batting: Option<String>,
    neg_risk: Option<bool>,
}

async fn post_setup(
    State(state): State<S>,
    Json(body): Json<SetupRequest>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    if state.is_match_running() {
        return Err((StatusCode::CONFLICT, "cannot change setup while match is running".into()));
    }

    let mut config = state.config.write().unwrap();

    if let Some(v) = body.team_a_name { config.team_a_name = v; }
    if let Some(v) = body.team_b_name { config.team_b_name = v; }
    if let Some(v) = body.team_a_token_id { config.team_a_token_id = v; }
    if let Some(v) = body.team_b_token_id { config.team_b_token_id = v; }
    if let Some(v) = body.condition_id { config.condition_id = v; }
    if let Some(v) = body.neg_risk { config.neg_risk = v; }
    if let Some(v) = &body.first_batting {
        config.first_batting = if v.to_uppercase() == "B" { Team::TeamB } else { Team::TeamA };
    }

    config.persist();
    drop(config);
    *state.match_state.write().unwrap() = crate::types::MatchState::new(
        state.config.read().unwrap().first_batting,
    );

    state.push_event("setup", "match setup updated + saved");

    // Start/restart orderbook WS so book data shows before innings
    start_book_ws(&state);

    Ok(Json(serde_json::json!({"ok": true})))
}

// ── Wallet ──────────────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct WalletRequest {
    private_key: Option<String>,
    address: Option<String>,
    signature_type: Option<u8>,
}

async fn post_wallet(
    State(state): State<S>,
    Json(body): Json<WalletRequest>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    if state.is_match_running() {
        return Err((StatusCode::CONFLICT, "cannot change wallet while match is running".into()));
    }

    {
        let mut config = state.config.write().unwrap();
        if let Some(v) = body.private_key { config.polymarket_private_key = v; }
        if let Some(v) = body.address { config.polymarket_address = v; }
        if let Some(v) = body.signature_type { config.signature_type = v; }
        // Clear pre-configured API keys so derivation always runs from private key
        config.api_key = String::new();
        config.api_secret = String::new();
        config.api_passphrase = String::new();
    }

    let config = state.config.read().unwrap().clone();
    config.persist();

    if config.has_wallet() {
        match ClobAuth::derive(&config).await {
            Ok(auth) => {
                let api_key = auth.api_key.clone();
                // Persist derived credentials so future startups skip L1 derivation
                {
                    let mut cfg = state.config.write().unwrap();
                    cfg.api_key = auth.api_key.clone();
                    cfg.api_secret = auth.api_secret.clone();
                    cfg.api_passphrase = auth.passphrase.clone();
                    cfg.persist();
                }
                *state.auth.write().unwrap() = Some(auth);
                state.push_event("wallet", &format!("wallet configured, API key derived: {api_key}"));
                return Ok(Json(serde_json::json!({"ok": true, "api_key": api_key})));
            }
            Err(e) => {
                state.push_event("wallet", &format!("auth derivation failed: {e}"));
                return Err((StatusCode::INTERNAL_SERVER_ERROR, format!("auth failed: {e}")));
            }
        }
    }

    Ok(Json(serde_json::json!({"ok": true})))
}

// ── Limits (can update during match) ────────────────────────────────────────

#[derive(Deserialize)]
struct LimitsRequest {
    total_budget_usdc: Option<String>,
    max_trade_usdc: Option<String>,
    safe_percentage: Option<u64>,
    revert_delay_ms: Option<u64>,
    fill_poll_interval_ms: Option<u64>,
    fill_poll_timeout_ms: Option<u64>,
    signal_gap_secs: Option<u64>,
    dry_run: Option<bool>,
    edge_wicket: Option<f64>,
    edge_boundary_4: Option<f64>,
    edge_boundary_6: Option<f64>,
    breakeven_timeout_ms: Option<u64>,
    revert_timeout_ms: Option<u64>,
}

async fn post_limits(
    State(state): State<S>,
    Json(body): Json<LimitsRequest>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let mut config = state.config.write().unwrap();

    // Parse new budget value first (while holding config lock), then apply to
    // position AFTER releasing config lock. Holding config.write() while
    // acquiring position.lock() creates ABBA deadlock with strategy tasks that
    // hold position.lock() and then read config.read().
    let new_budget = if let Some(v) = &body.total_budget_usdc {
        let b: Decimal = v.parse().map_err(|_| (StatusCode::BAD_REQUEST, "invalid budget".into()))?;
        config.total_budget_usdc = b;
        Some(b)
    } else {
        None
    };
    if let Some(v) = &body.max_trade_usdc {
        config.max_trade_usdc = v.parse().map_err(|_| (StatusCode::BAD_REQUEST, "invalid max_trade".into()))?;
    }
    if let Some(v) = body.safe_percentage { config.safe_percentage = v; }
    if let Some(v) = body.revert_delay_ms { config.revert_delay_ms = v; }
    if let Some(v) = body.fill_poll_interval_ms { config.fill_poll_interval_ms = v; }
    if let Some(v) = body.fill_poll_timeout_ms { config.fill_poll_timeout_ms = v; }
    if let Some(v) = body.signal_gap_secs { config.signal_gap_secs = v; }
    if let Some(v) = body.dry_run { config.dry_run = v; }
    if let Some(v) = body.edge_wicket { config.edge_wicket = v; }
    if let Some(v) = body.edge_boundary_4 { config.edge_boundary_4 = v; }
    if let Some(v) = body.edge_boundary_6 { config.edge_boundary_6 = v; }
    if let Some(v) = body.breakeven_timeout_ms { config.breakeven_timeout_ms = v; }
    if let Some(v) = body.revert_timeout_ms { config.revert_timeout_ms = v; }
    config.persist();
    drop(config); // release write lock before acquiring position mutex

    if let Some(b) = new_budget {
        state.position.lock().unwrap().total_budget = b;
    }

    state.push_event("limits", "trading limits updated + saved");
    Ok(Json(serde_json::json!({"ok": true})))
}

// ── Start innings ───────────────────────────────────────────────────────────

async fn post_start_innings(
    State(state): State<S>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    {
        let phase = *state.phase.read().unwrap();
        if phase == MatchPhase::InningsRunning {
            return Err((StatusCode::CONFLICT, "innings already running".into()));
        }
    }

    let config = state.config.read().unwrap().clone();
    if !config.has_wallet() {
        return Err((StatusCode::BAD_REQUEST, "wallet not configured".into()));
    }
    if !config.has_tokens() {
        return Err((StatusCode::BAD_REQUEST, "token IDs not set".into()));
    }

    let needs_auth = state.auth.read().unwrap().is_none();
    if needs_auth {
        match ClobAuth::derive(&config).await {
            Ok(auth) => { *state.auth.write().unwrap() = Some(auth); }
            Err(e) => return Err((StatusCode::INTERNAL_SERVER_ERROR, format!("auth failed: {e}"))),
        }
    }

    // Snapshot auth now, before spawning, to avoid a TOCTOU race where
    // a concurrent post_wallet/post_reset could clear auth between here
    // and the spawned task body, causing an unwrap() panic inside tokio.
    let auth_snapshot = match state.auth.read().unwrap().clone() {
        Some(a) => a,
        None => return Err((StatusCode::INTERNAL_SERVER_ERROR, "auth not initialized".into())),
    };

    let (signal_tx, signal_rx) = tokio::sync::broadcast::channel::<DispatchedSignal>(64);

    *state.signal_tx.write().unwrap() = Some(signal_tx);
    *state.phase.write().unwrap() = MatchPhase::InningsRunning;

    // Always (re-)start the orderbook WS.  It may have been cancelled by a
    // previous stop-innings, so we can't just check book_rx.is_some().
    start_book_ws(&state);

    let book_rx = state.book_rx.read().unwrap().clone()
        .expect("book_rx must be set after start_book_ws");
    let cancel = state.ws_cancel.read().unwrap().clone()
        .expect("ws_cancel must be set after start_book_ws");

    // Sync on-chain balances into position tracker before the innings starts.
    // This reconciles any fills or manual token movements (split/merge) that
    // happened since the last session.
    {
        let sync_config = config.clone();
        let sync_state = state.clone();
        tokio::spawn(async move {
            if sync_config.has_tokens() {
                match ctf::sync_balances(&sync_config).await {
                    Ok((a, b)) => {
                        let mut pos = sync_state.position.lock().unwrap();
                        pos.team_a_tokens = a;
                        pos.team_b_tokens = b;
                        drop(pos);
                        sync_state.snapshot_inventory();
                        sync_state.push_event("sync", &format!(
                            "on-chain balance synced (sig_type={}): {} = {}, {} = {}",
                            sync_config.signature_type,
                            sync_config.team_a_name, a,
                            sync_config.team_b_name, b,
                        ));
                        tracing::info!(team_a = %a, team_b = %b, "on-chain balances synced at innings start");
                    }
                    Err(e) => {
                        tracing::warn!(error = %e, "could not sync on-chain balances at innings start");
                        sync_state.push_event("warn", &format!("on-chain balance sync failed: {e}"));
                    }
                }
            }
        });
    }

    // Background task: periodically sync on-chain balances while innings is running.
    // This keeps inventory accurate even if local tracking drifts (e.g., timed-out fills).
    {
        let sync_config = config.clone();
        let sync_state = state.clone();
        let sync_cancel = cancel.clone();
        tokio::spawn(async move {
            let mut interval = tokio::time::interval(
                std::time::Duration::from_secs(CHAIN_SYNC_INTERVAL_SECS)
            );
            interval.tick().await; // skip immediate first tick (covered by the sync above)
            loop {
                tokio::select! {
                    _ = interval.tick() => {
                        if !sync_config.has_tokens() { continue; }
                        match ctf::sync_balances(&sync_config).await {
                            Ok((a, b)) => {
                                let mut pos = sync_state.position.lock().unwrap();
                                pos.team_a_tokens = a;
                                pos.team_b_tokens = b;
                                drop(pos);
                                sync_state.snapshot_inventory();
                                tracing::debug!(team_a = %a, team_b = %b, "periodic on-chain balance sync");
                            }
                            Err(e) => {
                                tracing::warn!(error = %e, "periodic on-chain balance sync failed");
                            }
                        }
                    }
                    _ = sync_cancel.cancelled() => {
                        tracing::debug!("chain sync task stopped");
                        break;
                    }
                }
            }
        });
    }

    // ── User WS for real-time fill detection ────────────────────────────────
    let (taker_fill_tx, taker_fill_rx) = mpsc::channel::<FillEvent>(128);
    {
        let ws_url = config.clob_ws.clone();
        let api_key = config.api_key.clone();
        let api_secret = config.api_secret.clone();
        let api_passphrase = config.api_passphrase.clone();
        let condition_id = config.condition_id.clone();
        let token_ids = vec![config.team_a_token_id.clone(), config.team_b_token_id.clone()];
        let user_ws_cancel = cancel.clone();
        let fill_tx_for_ws = taker_fill_tx.clone();
        let (bridge_tx, mut bridge_rx) = mpsc::channel::<FillEvent>(128);

        // Bridge task: forward WS fills to both the mpsc channel (for strategy)
        // and the AppState buffer (for revert monitors and other consumers).
        // Also persists every fill into the `user_fills` ledger (B6) — this
        // is the only place per-MATCHED-event granularity is captured; the
        // strategy's downstream `clob_orders.size_matched` only carries the
        // running aggregate.
        let bridge_app = state.clone();
        tokio::spawn(async move {
            while let Some(event) = bridge_rx.recv().await {
                // B6: persist before fanning out so a panic downstream does
                // not lose the fill record.
                if let Some(ref db) = *bridge_app.db.read().unwrap() {
                    let cfg = bridge_app.config.read().unwrap();
                    let team = if event.asset_id == cfg.team_a_token_id {
                        cfg.team_a_name.as_str()
                    } else if event.asset_id == cfg.team_b_token_id {
                        cfg.team_b_name.as_str()
                    } else {
                        ""
                    };
                    let correlation_id = db
                        .lookup_correlation_id(&event.order_id)
                        .unwrap_or_default();
                    let side_str = format!("{}", event.side);
                    let size_str = event.filled_size.to_string();
                    let price_str = event.avg_price.to_string();
                    let ts = crate::state::ist_now();
                    db.insert_user_fill(&crate::db::UserFill {
                        ts_ist: &ts,
                        polymarket_trade_id: event.polymarket_trade_id.as_deref(),
                        order_id: &event.order_id,
                        correlation_id: &correlation_id,
                        asset_id: &event.asset_id,
                        team,
                        side: &side_str,
                        size: &size_str,
                        price: &price_str,
                        status: &event.status,
                        slug: &cfg.market_slug,
                        raw_json: &event.raw_json,
                    });
                }
                bridge_app.buffer_fill_event(event.clone());
                let _ = fill_tx_for_ws.send(event).await;
            }
        });

        tokio::spawn(async move {
            user_ws::run(
                ws_url,
                api_key,
                api_secret,
                api_passphrase,
                condition_id,
                token_ids,
                bridge_tx,
                user_ws_cancel,
            ).await;
        });
        tracing::info!("[TAKER] user WS spawned for real-time fill detection");
    }

    // ── Order cache: pre-sign orders in background ────────────────────────
    {
        let cache_app = state.clone();
        let cache = state.order_cache.clone();
        let cache_book_rx = state.book_rx.read().unwrap().clone()
            .expect("book_rx must be set");
        let cache_cancel = cancel.clone();
        tokio::spawn(async move {
            order_cache::run(cache_app, cache, cache_book_rx, cache_cancel).await;
        });
        tracing::info!("[TAKER] order cache pre-signer spawned");
    }

    // ── Background tick_size refresher ────────────────────────────────────
    {
        let tick_app = state.clone();
        let tick_cancel = cancel.clone();
        tokio::spawn(async move {
            strategy::tick_size_refresher(tick_app, tick_cancel).await;
        });
        tracing::info!("[TAKER] background tick_size refresher spawned");
    }

    // ── CLOB order sync (polls /data/orders every 5s) ────────────────────
    {
        let sync_app = state.clone();
        let sync_cancel = cancel.clone();
        tokio::spawn(async move {
            strategy::clob_order_sync(sync_app, sync_cancel).await;
        });
        tracing::info!("[TAKER] CLOB order sync spawned");
    }

    // ── Background oracle event capture ────────────────────────────────
    if let Some(ref db) = *state.db.read().unwrap() {
        let slug = config.market_slug.clone();
        let oracle_tx = capture::spawn_oracle_writer(db.clone(), slug, cancel.clone());
        *state.oracle_tx.write().unwrap() = Some(oracle_tx);
        tracing::info!("[CAPTURE] oracle event writer spawned");
    }

    // Wait for book to populate then start strategy.
    let st = state.clone();
    tokio::spawn(async move {
        // Poll until at least one token has a best bid/ask (max 5s)
        let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(5);
        loop {
            {
                let books = book_rx.borrow();
                if books.0.best_bid().is_some() || books.1.best_bid().is_some() {
                    tracing::info!("orderbook ready — starting strategy");
                    break;
                }
            }
            if tokio::time::Instant::now() >= deadline {
                tracing::warn!("orderbook not ready after 5s — starting strategy anyway");
                break;
            }
            tokio::time::sleep(std::time::Duration::from_millis(100)).await;
        }

        let config = st.config.read().unwrap().clone();
        strategy::run(&config, &auth_snapshot, signal_rx, book_rx, st.position.clone(), st.clone(), taker_fill_rx).await;
    });

    // If maker is enabled, spawn maker::run with its own signal subscriber and fill channel.
    {
        let maker_cfg = state.maker_config.read().unwrap().clone();
        if maker_cfg.enabled {
            let maker_signal_rx = state.signal_tx.read().unwrap().as_ref()
                .expect("signal_tx must be set").subscribe();
            let (maker_fill_tx, maker_fill_rx) = mpsc::channel::<FillEvent>(64);
            // Store the maker fill_tx so user_ws or poll can forward fills
            *state.fill_tx.write().unwrap() = Some(maker_fill_tx);

            let maker_state = state.clone();
            let maker_cancel = cancel.clone();
            tokio::spawn(async move {
                maker::run(maker_state, maker_signal_rx, maker_fill_rx, maker_cancel).await;
            });

            // Also spawn heartbeat to keep GTC/GTD orders alive
            let hb_state = state.clone();
            let hb_cancel = cancel.clone();
            tokio::spawn(async move {
                heartbeat::run(hb_state, hb_cancel).await;
            });

            state.push_event("maker", "maker engine spawned");
            tracing::info!("[MAKER] spawned maker + heartbeat tasks");
        }
    }

    let ms = state.match_state.read().unwrap();
    state.push_event("innings", &format!(
        "innings {} started — {} batting",
        ms.innings, config.team_name(ms.batting)
    ));

    Ok(Json(serde_json::json!({"ok": true})))
}

// ── Stop innings (pause — does IO internally) ──────────────────────────────

async fn post_stop_innings(
    State(state): State<S>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    if !state.is_match_running() {
        return Err((StatusCode::CONFLICT, "no innings running".into()));
    }

    // Capture current innings number before strategy processes the signal.
    let current_innings = {
        state.match_state.read().unwrap().innings
    };

    // Signal strategy to stop. The strategy's InningsOver handler calls
    // switch_innings on app.match_state — do NOT call it here too or the
    // innings counter and batting team will be toggled twice (double-switch bug).
    let tx = state.signal_tx.read().unwrap().clone();
    if let Some(tx) = tx {
        let dispatched = state.make_dispatch(CricketSignal::InningsOver);
        let _ = tx.send(dispatched);
    }
    *state.signal_tx.write().unwrap() = None;

    if let Some(cancel) = state.ws_cancel.read().unwrap().clone() {
        cancel.cancel();
    }
    *state.ws_cancel.write().unwrap() = None;

    if current_innings >= 2 {
        *state.phase.write().unwrap() = MatchPhase::MatchOver;
        state.push_event("match", "innings 2 over — match complete, see you again");
    } else {
        *state.phase.write().unwrap() = MatchPhase::InningsPaused;
        let config = state.config.read().unwrap();
        let ms = state.match_state.read().unwrap();
        // By now strategy may have already switched, so read the updated state
        let next_batting = config.team_name(ms.batting.opponent());
        state.push_event("innings", &format!(
            "innings {current_innings} over — {next_batting} batting next"
        ));
    };

    Ok(Json(serde_json::json!({"ok": true})))
}

// ── Signal ──────────────────────────────────────────────────────────────────

#[derive(Deserialize)]
struct SignalRequest {
    signal: String,
}

async fn post_signal(
    State(state): State<S>,
    Json(body): Json<SignalRequest>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    if !state.is_match_running() {
        return Err((StatusCode::CONFLICT, "no innings running — start innings first".into()));
    }

    // Parse first; on failure, capture as PARSE_ERROR so the ledger has a
    // record of every receive. The `event_seq` allocated here only feeds the
    // ledger row — no downstream broadcast happens.
    let parsed = match CricketSignal::parse(&body.signal) {
        Some(p) => p,
        None => {
            let event_seq = state.next_event_seq();
            state.capture_signal(&body.signal, "ui", event_seq, "PARSE_ERROR");
            return Err((StatusCode::BAD_REQUEST, format!("unknown signal: {}", body.signal)));
        }
    };

    if parsed == CricketSignal::MatchOver {
        return Err((StatusCode::BAD_REQUEST, "use /api/match-over endpoint for MO".into()));
    }
    if parsed == CricketSignal::InningsOver {
        return Err((StatusCode::BAD_REQUEST, "use /api/stop-innings endpoint for IO".into()));
    }

    let tx = state.signal_tx.read().unwrap().clone();
    let Some(tx) = tx else {
        return Err((StatusCode::CONFLICT, "signal channel not ready".into()));
    };

    // Allocate event_seq once; share it between the ledger row and the
    // broadcast envelope so downstream order rows can be joined back.
    let dispatched = state.make_dispatch(parsed.clone());
    let event_seq = dispatched.event_seq;
    let correlation_id = dispatched.correlation_id.clone();

    // A1: enforce the dispatch gap. Inside the window we still record the
    // signal (handler is the single source of truth for the ledger) but do
    // not broadcast — strategy only sees `FORWARDED` rows.
    if let Some(gap_secs) = state.check_and_update_dispatch_gap() {
        state.capture_signal(&body.signal, "ui", event_seq, "GAP_REJECTED");
        tracing::info!(
            source = "ui",
            signal = %parsed,
            event_seq,
            correlation_id = %correlation_id,
            gap_secs,
            "signal dropped — within dispatch gap",
        );
        state.push_event("signal", &format!("{parsed} (event{event_seq}, dropped: gap)"));
        return Ok(Json(serde_json::json!({
            "ok": true,
            "dropped": "gap",
            "signal": body.signal,
            "event_seq": event_seq,
            "gap_secs": gap_secs,
        })));
    }

    state.capture_signal(&body.signal, "ui", event_seq, "FORWARDED");

    tx.send(dispatched)
        .map_err(|_| (StatusCode::INTERNAL_SERVER_ERROR, "signal channel closed".into()))?;

    tracing::info!(
        source = "ui",
        signal = %parsed,
        event_seq,
        correlation_id = %correlation_id,
        "signal forwarded",
    );
    state.push_event("signal", &format!("{parsed} (event{event_seq})"));
    Ok(Json(serde_json::json!({"ok": true, "signal": body.signal, "event_seq": event_seq})))
}

// ── Telegram signal webhook ──────────────────────────────────────────────────
// Fastest possible signal path: Telegram bot → HTTP POST → parse → broadcast
// Accepts both JSON body {"signal": "W"} and raw text body "W"
// Can also accept Telegram webhook format {"message":{"text":"W"}}

#[derive(Deserialize)]
struct TelegramUpdate {
    message: Option<TelegramMessage>,
}

#[derive(Deserialize)]
struct TelegramMessage {
    text: Option<String>,
}

async fn post_tg_signal(
    State(state): State<S>,
    body: axum::body::Bytes,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let recv_time = std::time::Instant::now();

    if !state.is_match_running() {
        return Err((StatusCode::CONFLICT, "no innings running".into()));
    }

    // Extract signal text from various formats
    let raw_text = extract_signal_text(&body)
        .ok_or_else(|| (StatusCode::BAD_REQUEST, "could not extract signal from body".into()))?;

    // Parse first; on failure, capture as PARSE_ERROR for ledger completeness.
    let parsed = match CricketSignal::parse(&raw_text) {
        Some(p) => p,
        None => {
            let event_seq = state.next_event_seq();
            state.capture_signal(&raw_text, "telegram", event_seq, "PARSE_ERROR");
            return Err((StatusCode::BAD_REQUEST, format!("unknown signal: {raw_text}")));
        }
    };

    // IO and MO should go through the same logic as dedicated endpoints
    // to ensure proper state transitions (phase changes, etc.)
    if parsed == CricketSignal::InningsOver {
        return Err((StatusCode::BAD_REQUEST, "use /api/stop-innings for IO (or send via regular /api/signal)".into()));
    }
    if parsed == CricketSignal::MatchOver {
        return Err((StatusCode::BAD_REQUEST, "use /api/match-over for MO (or send via regular /api/signal)".into()));
    }

    let tx = state.signal_tx.read().unwrap().clone();
    let Some(tx) = tx else {
        return Err((StatusCode::CONFLICT, "signal channel not ready".into()));
    };

    let dispatched = state.make_dispatch(parsed.clone());
    let event_seq = dispatched.event_seq;
    let correlation_id = dispatched.correlation_id.clone();

    // A1: dispatch gap — capture even when dropped so the ledger sees every receive.
    if let Some(gap_secs) = state.check_and_update_dispatch_gap() {
        state.capture_signal(&raw_text, "telegram", event_seq, "GAP_REJECTED");
        let latency_us = recv_time.elapsed().as_micros();
        tracing::info!(
            source = "telegram",
            signal = %parsed,
            event_seq,
            correlation_id = %correlation_id,
            gap_secs,
            latency_us,
            "tg-signal dropped — within dispatch gap",
        );
        state.push_event(
            "tg-signal",
            &format!("{parsed} (event{event_seq}, dropped: gap, {latency_us}us)"),
        );
        return Ok(Json(serde_json::json!({
            "ok": true,
            "dropped": "gap",
            "signal": format!("{parsed}"),
            "event_seq": event_seq,
            "gap_secs": gap_secs,
            "latency_us": latency_us,
        })));
    }

    state.capture_signal(&raw_text, "telegram", event_seq, "FORWARDED");

    tx.send(dispatched)
        .map_err(|_| (StatusCode::INTERNAL_SERVER_ERROR, "signal channel closed".into()))?;

    let latency_us = recv_time.elapsed().as_micros();
    // B2 (TODO.md): the telegram receipt/decode/forward line lives here in
    // the handler, not in the strategy — strategy logs cover dispatch
    // (NORMAL/WAIT/AUGMENT) and order lifecycle.
    tracing::info!(
        source = "telegram",
        signal = %parsed,
        event_seq,
        correlation_id = %correlation_id,
        latency_us,
        "tg-signal forwarded",
    );
    state.push_event("tg-signal", &format!("{parsed} (event{event_seq}, {latency_us}us)"));
    Ok(Json(serde_json::json!({
        "ok": true,
        "signal": format!("{parsed}"),
        "event_seq": event_seq,
        "latency_us": latency_us,
    })))
}

fn extract_signal_text(body: &[u8]) -> Option<String> {
    let text = std::str::from_utf8(body).ok()?;
    let trimmed = text.trim();

    // 1) Try Telegram webhook format: {"message":{"text":"W"}}
    if let Ok(update) = serde_json::from_str::<TelegramUpdate>(trimmed) {
        if let Some(msg) = update.message {
            if let Some(t) = msg.text {
                return Some(t.trim().to_string());
            }
        }
    }

    // 2) Try simple JSON: {"signal":"W"}
    if let Ok(req) = serde_json::from_str::<SignalRequest>(trimmed) {
        return Some(req.signal.trim().to_string());
    }

    // 3) Raw text body: "W"
    if !trimmed.is_empty() && trimmed.len() < 10 {
        return Some(trimmed.to_string());
    }

    None
}

// ── Toggle team trading ──────────────────────────────────────────────────────

#[derive(Deserialize)]
struct ToggleTeamRequest {
    team: String,       // "A" or "B"
    enabled: bool,
}

async fn post_toggle_team(
    State(state): State<S>,
    Json(body): Json<ToggleTeamRequest>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let config = state.config.read().unwrap();
    match body.team.to_uppercase().as_str() {
        "A" => {
            *state.trade_team_a.write().unwrap() = body.enabled;
            let label = if body.enabled { "enabled" } else { "disabled" };
            state.push_event("config", &format!("{} trading {}", config.team_a_name, label));
        }
        "B" => {
            *state.trade_team_b.write().unwrap() = body.enabled;
            let label = if body.enabled { "enabled" } else { "disabled" };
            state.push_event("config", &format!("{} trading {}", config.team_b_name, label));
        }
        _ => return Err((StatusCode::BAD_REQUEST, "team must be 'A' or 'B'".into())),
    }
    Ok(Json(serde_json::json!({"ok": true})))
}

// ── Match Over ──────────────────────────────────────────────────────────────

async fn post_match_over(
    State(state): State<S>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let tx = state.signal_tx.read().unwrap().clone();
    if let Some(tx) = tx {
        let dispatched = state.make_dispatch(CricketSignal::MatchOver);
        let _ = tx.send(dispatched);
    }

    if let Some(cancel) = state.ws_cancel.read().unwrap().clone() {
        cancel.cancel();
    }

    *state.phase.write().unwrap() = MatchPhase::MatchOver;
    *state.signal_tx.write().unwrap() = None;

    state.push_event("match", "MATCH OVER");

    let pos = state.position.lock().unwrap();
    let config = state.config.read().unwrap();
    let summary = pos.summary(&config);

    Ok(Json(serde_json::json!({"ok": true, "position": summary})))
}

// ── Cancel All Orders ───────────────────────────────────────────────────────

async fn post_cancel_all(
    State(state): State<S>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let auth = state.auth.read().unwrap().clone()
        .ok_or_else(|| (StatusCode::BAD_REQUEST, "no auth — configure wallet first".into()))?;

    // Use bulk cancel-all endpoint to cancel ALL open orders (including orphans
    // from previous sessions that we don't track in live_order_ids).
    match orders::cancel_all_open_orders(&auth).await {
        Ok(()) => {
            state.clear_orders();
            state.push_event("cancel", "cancel-all: all open CLOB orders cancelled");
            Ok(Json(serde_json::json!({"ok": true})))
        }
        Err(e) => {
            tracing::warn!(error = %e, "cancel-all failed");
            state.push_event("error", &format!("cancel-all failed: {e}"));
            Err((StatusCode::INTERNAL_SERVER_ERROR, format!("cancel-all failed: {e}")))
        }
    }
}

// ── Reset ───────────────────────────────────────────────────────────────────

async fn post_reset(
    State(state): State<S>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    if state.is_match_running() {
        return Err((StatusCode::CONFLICT, "stop match first".into()));
    }

    if let Some(cancel) = state.ws_cancel.read().unwrap().as_ref() {
        cancel.cancel();
    }

    state.reset_for_new_match();
    state.push_event("reset", "state reset for new match");
    Ok(Json(serde_json::json!({"ok": true})))
}

// ── Fetch Market from Gamma API ──────────────────────────────────────────────

#[derive(Deserialize)]
struct FetchMarketRequest {
    slug: String,
}

async fn post_fetch_market(
    State(state): State<S>,
    Json(body): Json<FetchMarketRequest>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    if state.is_match_running() {
        return Err((StatusCode::CONFLICT, "cannot change setup while match is running".into()));
    }

    let url = format!("https://gamma-api.polymarket.com/markets?slug={}", body.slug);
    let resp = reqwest::get(&url).await
        .map_err(|e| (StatusCode::BAD_GATEWAY, format!("fetch failed: {e}")))?;
    let markets: Vec<serde_json::Value> = resp.json().await
        .map_err(|e| (StatusCode::BAD_GATEWAY, format!("parse failed: {e}")))?;

    let market = markets.first()
        .ok_or_else(|| (StatusCode::NOT_FOUND, "no market found for this slug".into()))?;

    let condition_id = market["conditionId"].as_str().unwrap_or("").to_string();
    let neg_risk = market["negRisk"].as_bool().unwrap_or(false);
    let restricted = market["restricted"].as_bool().unwrap_or(false);
    let tick_size = market["orderPriceMinTickSize"]
        .as_f64()
        .map(|v| format!("{v}"))
        .unwrap_or_else(|| "0.01".to_string());
    let order_min_size = market["orderMinSize"].as_f64().unwrap_or(1.0);
    let order_min_size_dec = Decimal::from_str(&order_min_size.to_string()).unwrap_or(Decimal::ONE);
    let seconds_delay = market["secondsDelay"].as_u64().unwrap_or(3);

    if restricted {
        tracing::warn!("market is restricted — API trading may be blocked or limited");
        state.push_event("warn", "Market is restricted; API orders may be rejected");
    }

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

    // V2 per-market parameters from /clob-markets/{condition_id}: fee rate +
    // exponent (V2 fee formula), taker-order-delay flag, min-order-age.
    let clob_http = state.config.read().unwrap().clob_http.clone();
    let v2_info = match crate::clob_market::fetch(
        &reqwest::Client::new(),
        &clob_http,
        &condition_id,
    ).await {
        Ok(info) => Some(info),
        Err(e) => {
            tracing::warn!(error = %e, "clob-markets V2 fetch failed — fee rate will be 0");
            state.push_event("warn", &format!("V2 market info fetch failed: {e}"));
            None
        }
    };
    let (fee_rate, fee_exponent, takers_only_fees) = v2_info
        .as_ref()
        .map(|i| (i.fee_rate(), i.fee_exponent(), i.takers_only()))
        .unwrap_or((0.0, 0.0, true));
    if let Some(info) = &v2_info {
        if info.taker_order_delay_enabled {
            tracing::error!("market has taker-order-delay (itode=true) — strategy edge is gone");
            state.push_event("warn", "Market has taker-order-delay enabled — DO NOT trade with low-latency strategy");
        }
        if !info.takers_only() && info.fee_rate() > 0.0 {
            tracing::error!(
                "market has non-zero maker fee (fd.to=false) — strategy assumes maker reverts are fee-free; refusing to trade",
            );
            state.push_event(
                "error",
                "Market charges maker fee (fd.to=false) — refusing to start; revert PnL math would be wrong",
            );
        }
        if info.min_order_age_secs > 0 {
            tracing::warn!(min_order_age_secs = info.min_order_age_secs, "market enforces min order age");
            state.push_event("warn", &format!("Market enforces min_order_age={}s — fresh orders may be rejected", info.min_order_age_secs));
        }
        if info.rfq_enabled {
            tracing::info!("market is RFQ-enabled");
        }
        if let Some(gst) = &info.game_start {
            tracing::info!(game_start = %gst, "V2 market game start time");
        }
    }

    {
        let mut config = state.config.write().unwrap();
        config.team_a_name = team_a_name.clone();
        config.team_b_name = team_b_name.clone();
        config.team_a_token_id = team_a_token.clone();
        config.team_b_token_id = team_b_token.clone();
        config.condition_id = condition_id.clone();
        config.neg_risk = neg_risk;
        config.tick_size = tick_size.clone();
        config.order_min_size = order_min_size_dec;
        config.fee_rate = fee_rate;
        config.fee_exponent = fee_exponent;
        config.takers_only_fees = takers_only_fees;
        config.market_slug = body.slug.clone();
        config.persist();
    }

    state.push_event("setup", &format!("fetched market: {} vs {} (tick={}, min_size={}, fee_rate={:.4}, exp={:.2}, takers_only={}, delay={}s)", team_a_name, team_b_name, tick_size, order_min_size, fee_rate, fee_exponent, takers_only_fees, seconds_delay));

    // Start/restart orderbook WS so book data shows before innings
    start_book_ws(&state);

    Ok(Json(serde_json::json!({
        "ok": true,
        "team_a_name": team_a_name,
        "team_b_name": team_b_name,
        "team_a_token_id": team_a_token,
        "team_b_token_id": team_b_token,
        "condition_id": condition_id,
        "neg_risk": neg_risk,
        "tick_size": tick_size,
        "order_min_size": order_min_size,
        "fee_rate": fee_rate,
        "fee_exponent": fee_exponent,
        "takers_only_fees": takers_only_fees,
        "seconds_delay": seconds_delay,
        "restricted": restricted,
    })))
}

// ── CTF Balance (fetch on-chain token balances) ─────────────────────────────

async fn post_ctf_balance(
    State(state): State<S>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let config = state.config.read().unwrap().clone();
    if !config.has_wallet() {
        return Err((StatusCode::BAD_REQUEST, "wallet not configured".into()));
    }
    if !config.has_tokens() {
        return Err((StatusCode::BAD_REQUEST, "token IDs not set".into()));
    }

    state.push_event("ctf", "fetching on-chain token balances…");

    let bal_a = ctf::balance_of(&config, &config.team_a_token_id).await
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, format!("balance_of A failed: {e}")))?;
    let bal_b = ctf::balance_of(&config, &config.team_b_token_id).await
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, format!("balance_of B failed: {e}")))?;

    {
        let mut pos = state.position.lock().unwrap();
        pos.team_a_tokens = bal_a;
        pos.team_b_tokens = bal_b;
    }
    state.snapshot_inventory();

    state.push_event("ctf", &format!(
        "on-chain balances (sig_type={}): {} = {}, {} = {}",
        config.signature_type, config.team_a_name, bal_a, config.team_b_name, bal_b
    ));

    Ok(Json(serde_json::json!({
        "ok": true,
        "team_a": bal_a,
        "team_b": bal_b,
    })))
}

// ── CTF Split (USDC → YES + NO tokens on-chain) ────────────────────────────

#[derive(Deserialize)]
struct CtfSplitRequest {
    amount_usdc: u64,
}

async fn post_ctf_split(
    State(state): State<S>,
    Json(body): Json<CtfSplitRequest>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let config = state.config.read().unwrap().clone();
    if !config.has_wallet() {
        return Err((StatusCode::BAD_REQUEST, "wallet not configured".into()));
    }
    if config.condition_id.is_empty() {
        return Err((StatusCode::BAD_REQUEST, "condition_id not set — fill it in Setup".into()));
    }
    if body.amount_usdc == 0 {
        return Err((StatusCode::BAD_REQUEST, "amount must be > 0".into()));
    }

    state.push_event("ctf", &format!("splitting {} USDC → YES + NO tokens…", body.amount_usdc));

    match ctf::split(&config, &config.condition_id, body.amount_usdc).await {
        Ok(tx_hash) => {
            let mut pos = state.position.lock().unwrap();
            let added = rust_decimal::Decimal::from(body.amount_usdc);
            pos.team_a_tokens += added;
            pos.team_b_tokens += added;
            drop(pos);
            state.snapshot_inventory();

            state.push_event("ctf", &format!("split OK — tx: {tx_hash}"));
            Ok(Json(serde_json::json!({"ok": true, "tx": tx_hash})))
        }
        Err(e) => {
            state.push_event("ctf", &format!("split FAILED: {e}"));
            Err((StatusCode::INTERNAL_SERVER_ERROR, format!("split failed: {e}")))
        }
    }
}

// ── CTF Merge (YES + NO tokens → USDC on-chain) ────────────────────────────

#[derive(Deserialize)]
struct CtfMergeRequest {
    amount_tokens: u64,
}

async fn post_ctf_merge(
    State(state): State<S>,
    Json(body): Json<CtfMergeRequest>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let config = state.config.read().unwrap().clone();
    if !config.has_wallet() {
        return Err((StatusCode::BAD_REQUEST, "wallet not configured".into()));
    }
    if config.condition_id.is_empty() {
        return Err((StatusCode::BAD_REQUEST, "condition_id not set".into()));
    }
    if body.amount_tokens == 0 {
        return Err((StatusCode::BAD_REQUEST, "amount must be > 0".into()));
    }

    state.push_event("ctf", &format!("merging {} YES + NO tokens → USDC…", body.amount_tokens));

    match ctf::merge(&config, &config.condition_id, body.amount_tokens).await {
        Ok(tx_hash) => {
            let mut pos = state.position.lock().unwrap();
            let removed = rust_decimal::Decimal::from(body.amount_tokens);
            pos.team_a_tokens = (pos.team_a_tokens - removed).max(rust_decimal::Decimal::ZERO);
            pos.team_b_tokens = (pos.team_b_tokens - removed).max(rust_decimal::Decimal::ZERO);
            drop(pos);
            state.snapshot_inventory();

            state.push_event("ctf", &format!("merge OK — tx: {tx_hash}"));
            Ok(Json(serde_json::json!({"ok": true, "tx": tx_hash})))
        }
        Err(e) => {
            state.push_event("ctf", &format!("merge FAILED: {e}"));
            Err((StatusCode::INTERNAL_SERVER_ERROR, format!("merge failed: {e}")))
        }
    }
}

// ── CTF Redeem (winning tokens → USDC after resolution) ─────────────────────

async fn post_ctf_redeem(
    State(state): State<S>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let config = state.config.read().unwrap().clone();
    if !config.has_wallet() {
        return Err((StatusCode::BAD_REQUEST, "wallet not configured".into()));
    }
    if config.condition_id.is_empty() {
        return Err((StatusCode::BAD_REQUEST, "condition_id not set".into()));
    }

    state.push_event("ctf", "redeeming winning tokens for USDC…");

    match ctf::redeem(&config, &config.condition_id).await {
        Ok(tx_hash) => {
            state.push_event("ctf", &format!("redeem OK — tx: {tx_hash}"));
            Ok(Json(serde_json::json!({"ok": true, "tx": tx_hash})))
        }
        Err(e) => {
            state.push_event("ctf", &format!("redeem FAILED: {e}"));
            Err((StatusCode::INTERNAL_SERVER_ERROR, format!("redeem failed: {e}")))
        }
    }
}

// ── Mega Resolve (redeem ALL resolved positions) ─────────────────────────────

async fn post_mega_resolve(
    State(state): State<S>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let config = state.config.read().unwrap().clone();
    if !config.has_wallet() {
        return Err((StatusCode::BAD_REQUEST, "wallet not configured".into()));
    }

    let proxy_addr = if config.signature_type > 0 && !config.polymarket_address.is_empty() {
        &config.polymarket_address
    } else {
        return Err((StatusCode::BAD_REQUEST, "proxy wallet not configured".into()));
    };

    state.push_event("ctf", "mega resolve: fetching all positions…");

    // Fetch all positions from Gamma API for this proxy wallet
    let url = format!(
        "https://data-api.polymarket.com/positions?user={proxy_addr}&limit=200&sizeThreshold=0.001"
    );
    let positions: Vec<serde_json::Value> = reqwest::get(&url)
        .await
        .map_err(|e| (StatusCode::BAD_GATEWAY, format!("fetch positions failed: {e}")))?
        .json()
        .await
        .map_err(|e| (StatusCode::BAD_GATEWAY, format!("parse positions failed: {e}")))?;

    // Collect unique condition IDs from positions
    let mut condition_ids: Vec<String> = positions
        .iter()
        .filter_map(|p| p["conditionId"].as_str().map(|s| s.to_string()))
        .collect();
    condition_ids.sort();
    condition_ids.dedup();

    if condition_ids.is_empty() {
        state.push_event("ctf", "mega resolve: no positions found");
        return Ok(Json(serde_json::json!({"ok": true, "redeemed": 0, "total": 0})));
    }

    state.push_event("ctf", &format!(
        "mega resolve: found {} unique conditions from {} positions — trying to redeem each…",
        condition_ids.len(), positions.len()
    ));

    let mut redeemed = 0u32;
    let mut failed = 0u32;
    let mut skipped = 0u32;

    for cid in &condition_ids {
        match ctf::redeem(&config, cid).await {
            Ok(tx_hash) => {
                redeemed += 1;
                state.push_event("ctf", &format!("redeemed {cid} — tx: {tx_hash}"));
                tracing::info!(condition_id = cid, tx = %tx_hash, "mega resolve: redeemed");
            }
            Err(e) => {
                let err_str = format!("{e}");
                // Common failures: "not resolved yet", "no balance", "already redeemed"
                // These are expected for active/already-redeemed positions
                if err_str.contains("revert") || err_str.contains("execution reverted") {
                    skipped += 1;
                    tracing::debug!(condition_id = cid, error = %e, "mega resolve: skipped (likely not resolved or no balance)");
                } else {
                    failed += 1;
                    state.push_event("warn", &format!("mega resolve failed for {cid}: {e}"));
                    tracing::warn!(condition_id = cid, error = %e, "mega resolve: failed");
                }
            }
        }
    }

    let msg = format!(
        "mega resolve done: {redeemed} redeemed, {skipped} skipped (not resolved/no balance), {failed} failed"
    );
    state.push_event("ctf", &msg);

    Ok(Json(serde_json::json!({
        "ok": true,
        "redeemed": redeemed,
        "skipped": skipped,
        "failed": failed,
        "total_conditions": condition_ids.len(),
    })))
}

// ── Maker endpoints ─────────────────────────────────────────────────────────

#[derive(Serialize)]
struct MakerStatusResponse {
    #[serde(flatten)]
    config: MakerConfig,
}

async fn get_maker_status(State(state): State<S>) -> Json<MakerStatusResponse> {
    let config = state.maker_config.read().unwrap().clone();
    Json(MakerStatusResponse { config })
}

async fn post_maker_config(
    State(state): State<S>,
    Json(update): Json<serde_json::Value>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let mut cfg = state.maker_config.write().unwrap();

    if let Some(v) = update.get("enabled").and_then(|v| v.as_bool()) {
        cfg.enabled = v;
    }
    if let Some(v) = update.get("dry_run").and_then(|v| v.as_bool()) {
        cfg.dry_run = v;
    }
    if let Some(v) = update.get("half_spread").and_then(|v| v.as_str()) {
        if let Ok(d) = v.parse::<Decimal>() {
            cfg.half_spread = d;
        }
    }
    if let Some(v) = update.get("quote_size").and_then(|v| v.as_str()) {
        if let Ok(d) = v.parse::<Decimal>() {
            cfg.quote_size = d;
        }
    }
    if let Some(v) = update.get("use_gtd").and_then(|v| v.as_bool()) {
        cfg.use_gtd = v;
    }
    if let Some(v) = update.get("gtd_expiry_secs").and_then(|v| v.as_u64()) {
        cfg.gtd_expiry_secs = v;
    }
    if let Some(v) = update.get("refresh_interval_secs").and_then(|v| v.as_u64()) {
        cfg.refresh_interval_secs = v;
    }
    if let Some(v) = update.get("skew_kappa").and_then(|v| v.as_str()) {
        if let Ok(d) = v.parse::<Decimal>() {
            cfg.skew_kappa = d;
        }
    }
    if let Some(v) = update.get("max_exposure").and_then(|v| v.as_str()) {
        if let Ok(d) = v.parse::<Decimal>() {
            cfg.max_exposure = d;
        }
    }
    if let Some(v) = update.get("t1_pct").and_then(|v| v.as_f64()) {
        cfg.t1_pct = v;
    }
    if let Some(v) = update.get("t2_pct").and_then(|v| v.as_f64()) {
        cfg.t2_pct = v;
    }
    if let Some(v) = update.get("t3_pct").and_then(|v| v.as_f64()) {
        cfg.t3_pct = v;
    }

    // Also persist to the main config's saved settings
    let mut main_config = state.config.write().unwrap();
    main_config.maker_config = cfg.clone();
    main_config.persist();

    Ok(Json(serde_json::json!({"ok": true})))
}

// ── Latency endpoint ─────────────────────────────────────────────────────────

async fn get_latency(State(state): State<S>) -> Json<crate::latency::LatencySnapshot> {
    Json(state.latency.snapshot())
}

// ── V2 Positions (data-api passthrough) & Balance (CLOB /balance-allowance) ──

/// GET /api/positions — proxy to data-api.polymarket.com/positions for the
/// configured proxy wallet. Public endpoint, no auth needed. Returns the raw
/// data-api shape so the frontend renders the enriched columns directly.
async fn get_positions(State(state): State<S>) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let proxy = {
        let cfg = state.config.read().unwrap();
        cfg.polymarket_address.clone()
    };
    if proxy.is_empty() {
        return Err((StatusCode::BAD_REQUEST, "polymarket_address not configured".into()));
    }
    let url = format!(
        "https://data-api.polymarket.com/positions?user={proxy}&sortBy=CURRENT&sortDirection=DESC&sizeThreshold=1&limit=200"
    );
    let resp = reqwest::get(&url).await
        .map_err(|e| (StatusCode::BAD_GATEWAY, format!("data-api fetch failed: {e}")))?;
    let status = resp.status();
    let body = resp.text().await
        .map_err(|e| (StatusCode::BAD_GATEWAY, format!("data-api read body: {e}")))?;
    if !status.is_success() {
        return Err((StatusCode::BAD_GATEWAY, format!("data-api {status}: {body}")));
    }
    let json: serde_json::Value = serde_json::from_str(&body)
        .map_err(|e| (StatusCode::BAD_GATEWAY, format!("data-api JSON parse: {e}")))?;
    Ok(Json(json))
}

/// GET /api/balance — calls CLOB `/balance-allowance` with L2 HMAC. Returns
/// pUSD balance + V2 exchange allowance for the active market's collateral.
/// No on-chain pUSD address required — CLOB resolves it server-side.
async fn get_v2_balance(State(state): State<S>) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    // Clone the auth out of the lock so the guard isn't held across await.
    let auth = {
        let g = state.auth.read().unwrap();
        g.as_ref()
            .cloned()
            .ok_or_else(|| (StatusCode::PRECONDITION_FAILED, "ClobAuth not derived yet (set wallet first)".into()))?
    };
    let sig_type = state.config.read().unwrap().signature_type;
    // CRITICAL: Polymarket's L2 HMAC is signed over the bare path WITHOUT
    // query string (matches py-clob-client-v2 client.py::get_balance_allowance,
    // which calls _l2_headers("GET", "/balance-allowance") and then attaches
    // params via the HTTP client's params= kwarg). Including query params in
    // the signed path produces a 401 "Invalid api key".
    let path = "/balance-allowance";
    let headers = auth.l2_headers("GET", path, None)
        .map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, format!("l2_headers: {e}")))?;
    let url = format!("{}{}", auth.clob_http_url(), path);
    let resp = auth.http_client()
        .get(&url)
        .headers(headers)
        .query(&[("asset_type", "COLLATERAL"), ("signature_type", &sig_type.to_string())])
        .send().await
        .map_err(|e| (StatusCode::BAD_GATEWAY, format!("clob fetch failed: {e}")))?;
    let status = resp.status();
    let body = resp.text().await
        .map_err(|e| (StatusCode::BAD_GATEWAY, format!("clob read body: {e}")))?;
    if !status.is_success() {
        return Err((StatusCode::BAD_GATEWAY, format!("/balance-allowance {status}: {body}")));
    }
    let json: serde_json::Value = serde_json::from_str(&body).unwrap_or(serde_json::json!({"raw": body}));
    Ok(Json(json))
}

// ── Taker UI & status ─────────────────────────────────────────────────────

#[derive(Serialize)]
struct TakerTradeEntry {
    ts: String,
    side: String,
    team: String,
    size: Decimal,
    price: Decimal,
    cost: Decimal,
    /// V2 platform fee for this fill, USDC. `0` for makers (`fd.to=true`).
    fee: Decimal,
    order_type: String,
    label: String,
    order_id: String,
    /// Running gross PnL (sum of net cash flow up to and including this trade).
    pnl: Decimal,
}

#[derive(Serialize)]
struct PendingRevertEntry {
    order_id: String,
    team: String,
    side: String,
    size: Decimal,
    entry_price: Decimal,
    revert_limit: Decimal,
    age_secs: f64,
    label: String,
}

#[derive(Serialize)]
struct TakerStatusResponse {
    phase: MatchPhase,
    batting: String,
    bowling: String,
    innings: u8,
    team_a_name: String,
    team_b_name: String,
    team_a_tokens: Decimal,
    team_b_tokens: Decimal,
    total_spent: Decimal,
    total_budget: Decimal,
    remaining: Decimal,
    trade_count: u64,
    dry_run: bool,
    // Book prices
    book_a_bid: Option<Decimal>,
    book_a_ask: Option<Decimal>,
    book_b_bid: Option<Decimal>,
    book_b_ask: Option<Decimal>,
    // PnL
    unrealized_pnl: Decimal,
    round_trip_pnl: Decimal,
    /// L6: cumulative platform fee paid this match, USDC.
    total_fee_paid: Decimal,
    /// `round_trip_pnl − total_fee_paid` (net of fees).
    net_pnl: Decimal,
    // Trades and reverts
    trades: Vec<TakerTradeEntry>,
    pending_reverts: Vec<PendingRevertEntry>,
    // CLOB orders (from DB)
    open_orders: Vec<crate::db::ClobOrderRow>,
    closed_orders: Vec<crate::db::ClobOrderRow>,
    // Round-trips
    round_trips: Vec<crate::db::RoundTrip>,
    /// S: per-signal 4-leg lifecycle. Newest-first.
    signal_groups: Vec<crate::state::SignalGroup>,
    // Latency
    latency: crate::latency::LatencySnapshot,
    // Settings summary
    market_slug: String,
    max_trade_usdc: Decimal,
    edge_wicket: f64,
    edge_boundary_4: f64,
    edge_boundary_6: f64,
    revert_delay_ms: u64,
    /// Wall-clock window before a maker revert is forced into a taker FAK
    /// exit. `0` disables. Default `15000`.
    revert_timeout_ms: u64,
    fill_poll_timeout_ms: u64,
    signal_gap_secs: u64,
    // Recent events
    events: Vec<crate::state::EventEntry>,
}

async fn get_taker_status(State(state): State<S>) -> Json<TakerStatusResponse> {
    let config = state.config.read().unwrap();
    let pos = state.position.lock().unwrap();
    let ms = state.match_state.read().unwrap();
    let phase = *state.phase.read().unwrap();

    let (ba_bid, ba_ask, bb_bid, bb_ask) = {
        let br = state.book_rx.read().unwrap();
        if let Some(rx) = br.as_ref() {
            let books = rx.borrow().clone();
            (
                books.0.best_bid().map(|l| l.price),
                books.0.best_ask().map(|l| l.price),
                books.1.best_bid().map(|l| l.price),
                books.1.best_ask().map(|l| l.price),
            )
        } else {
            (None, None, None, None)
        }
    };

    // Unrealized PnL: mark-to-market of held tokens
    let team_a_mid = match (ba_bid, ba_ask) {
        (Some(b), Some(a)) => (b + a) / Decimal::TWO,
        (Some(b), None) => b,
        (None, Some(a)) => a,
        _ => Decimal::ZERO,
    };
    let team_b_mid = match (bb_bid, bb_ask) {
        (Some(b), Some(a)) => (b + a) / Decimal::TWO,
        (Some(b), None) => b,
        (None, Some(a)) => a,
        _ => Decimal::ZERO,
    };
    let token_value = pos.team_a_tokens * team_a_mid + pos.team_b_tokens * team_b_mid;
    let unrealized_pnl = token_value - pos.total_spent;

    // Round-trip PnL from DB (the real PnL — completed entry+exit pairs).
    // L6: also sum per-trade fees and compute net PnL.
    let slug = config.market_slug.clone();
    let (round_trip_pnl, round_trips, open_orders, closed_orders, total_fee_paid) = {
        let db = state.db.read().unwrap();
        if let Some(ref db) = *db {
            (
                db.total_pnl(&slug),
                db.get_round_trips(&slug, 100),
                db.get_open_orders(&slug),
                db.get_closed_orders(&slug, 100),
                db.total_fee_paid(&slug),
            )
        } else {
            (Decimal::ZERO, Vec::new(), Vec::new(), Vec::new(), Decimal::ZERO)
        }
    };
    let net_pnl = round_trip_pnl - total_fee_paid;
    // S: snapshot of the signal-group ledger.
    let signal_groups = state.signal_groups_snapshot(50);

    // Build trade entries with running PnL
    let trades = state.trade_log.lock().unwrap().clone();
    let mut running_pnl = Decimal::ZERO;
    let trade_entries: Vec<TakerTradeEntry> = trades.iter().map(|t| {
        match t.side.as_str() {
            "SELL" => running_pnl += t.cost,
            "BUY" => running_pnl -= t.cost,
            _ => {}
        }
        TakerTradeEntry {
            ts: t.ts.clone(),
            side: t.side.clone(),
            team: t.team.clone(),
            size: t.size,
            price: t.price,
            cost: t.cost,
            fee: t.fee,
            order_type: t.order_type.clone(),
            label: t.label.clone(),
            order_id: t.order_id.clone(),
            pnl: running_pnl,
        }
    }).collect();

    // Pending reverts
    let pending_reverts: Vec<PendingRevertEntry> = state.pending_reverts.lock().unwrap()
        .iter().map(|r| PendingRevertEntry {
            order_id: r.order_id.clone(),
            team: config.team_name(r.team).to_string(),
            side: format!("{}", r.side),
            size: r.size,
            entry_price: r.entry_price,
            revert_limit: r.revert_limit_price,
            age_secs: r.age_secs(),
            label: r.label.clone(),
        }).collect();

    // Recent events (last 50)
    let events: Vec<crate::state::EventEntry> = {
        let ev = state.events.lock().unwrap();
        ev.iter().rev().take(50).cloned().collect()
    };

    Json(TakerStatusResponse {
        phase,
        batting: config.team_name(ms.batting).to_string(),
        bowling: config.team_name(ms.bowling()).to_string(),
        innings: ms.innings,
        team_a_name: config.team_a_name.clone(),
        team_b_name: config.team_b_name.clone(),
        team_a_tokens: pos.team_a_tokens,
        team_b_tokens: pos.team_b_tokens,
        total_spent: pos.total_spent,
        total_budget: pos.total_budget,
        remaining: pos.remaining_budget(),
        trade_count: pos.trade_count,
        dry_run: config.dry_run,
        book_a_bid: ba_bid,
        book_a_ask: ba_ask,
        book_b_bid: bb_bid,
        book_b_ask: bb_ask,
        unrealized_pnl,
        round_trip_pnl,
        total_fee_paid,
        net_pnl,
        trades: trade_entries,
        pending_reverts,
        open_orders,
        closed_orders,
        round_trips,
        signal_groups,
        latency: state.latency.snapshot(),
        market_slug: slug,
        max_trade_usdc: config.max_trade_usdc,
        edge_wicket: config.edge_wicket,
        edge_boundary_4: config.edge_boundary_4,
        edge_boundary_6: config.edge_boundary_6,
        revert_delay_ms: config.revert_delay_ms,
        revert_timeout_ms: config.revert_timeout_ms,
        fill_poll_timeout_ms: config.fill_poll_timeout_ms,
        signal_gap_secs: config.signal_gap_secs,
        events,
    })
}

// ── Taker cancel order ───────────────────────────────────────────────────

#[derive(Deserialize)]
struct CancelOrderRequest {
    order_id: String,
}

async fn post_taker_cancel_order(
    State(state): State<S>,
    Json(body): Json<CancelOrderRequest>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let config = state.config.read().unwrap().clone();
    let auth = state.auth.read().unwrap().clone()
        .ok_or_else(|| (StatusCode::INTERNAL_SERVER_ERROR, "no auth".into()))?;

    match orders::cancel_order(&config, &auth, &body.order_id).await {
        Ok(()) => {
            // Also remove from pending_reverts if it's there
            state.remove_revert(&body.order_id);
            state.push_event("cancel", &format!("order {} cancelled", &body.order_id[..8.min(body.order_id.len())]));
            Ok(Json(serde_json::json!({"ok": true, "cancelled": body.order_id})))
        }
        Err(e) => {
            Err((StatusCode::BAD_REQUEST, format!("cancel failed: {e}")))
        }
    }
}

// ── Sweep (endgame) endpoints ──────────────────────────────────────────────

async fn serve_sweep_ui() -> axum::response::Html<&'static str> {
    axum::response::Html(crate::web::SWEEP_HTML)
}

async fn get_sweep_status(State(state): State<S>) -> Json<serde_json::Value> {
    let phase = *state.sweep_phase.read().unwrap();
    let sweep_cfg = state.sweep_config.read().unwrap().clone();
    let orders = state.sweep_orders.lock().unwrap().clone();
    let config = state.config.read().unwrap();

    Json(serde_json::json!({
        "phase": phase,
        "config": sweep_cfg,
        "resting_orders": orders.len(),
        "orders": orders,
        "builder_code_set": !config.builder_code.is_empty(),
    }))
}

#[derive(Deserialize)]
struct SweepStartRequest {
    winning_team: String,   // "A" or "B"
    budget_usdc: String,    // e.g. "100"
    dry_run: Option<bool>,
    grid_levels: Option<usize>,
    refresh_interval_secs: Option<u64>,
}

async fn post_sweep_start(
    State(state): State<S>,
    Json(body): Json<SweepStartRequest>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    // Guard: not already running
    {
        let phase = *state.sweep_phase.read().unwrap();
        if phase == crate::state::SweepPhase::Active {
            return Err((StatusCode::CONFLICT, "sweep already active".into()));
        }
    }

    let config = state.config.read().unwrap().clone();
    if !config.has_wallet() {
        return Err((StatusCode::BAD_REQUEST, "wallet not configured".into()));
    }
    if !config.has_tokens() {
        return Err((StatusCode::BAD_REQUEST, "token IDs not set — fetch market first".into()));
    }

    // Ensure auth
    if state.auth.read().unwrap().is_none() {
        match ClobAuth::derive(&config).await {
            Ok(auth) => { *state.auth.write().unwrap() = Some(auth); }
            Err(e) => return Err((StatusCode::INTERNAL_SERVER_ERROR, format!("auth failed: {e}"))),
        }
    }

    let winning = match body.winning_team.to_uppercase().as_str() {
        "A" => Team::TeamA,
        "B" => Team::TeamB,
        _ => return Err((StatusCode::BAD_REQUEST, "winning_team must be 'A' or 'B'".into())),
    };

    let budget: Decimal = body.budget_usdc.parse()
        .map_err(|_| (StatusCode::BAD_REQUEST, "invalid budget_usdc".into()))?;

    // Safety check: verify the losing team's price is < 5¢
    {
        let br = state.book_rx.read().unwrap();
        if let Some(rx) = br.as_ref() {
            let books = rx.borrow().clone();
            let lose_book = match winning {
                Team::TeamA => &books.1,
                Team::TeamB => &books.0,
            };
            if let Some(best_ask) = lose_book.best_ask() {
                let losing_name = config.team_name(winning.opponent());
                if best_ask.price >= Decimal::new(5, 2) {
                    return Err((StatusCode::BAD_REQUEST, format!(
                        "BLOCKED: {} price is {:.2}¢ (>= 5¢) — too early to call winner",
                        losing_name, best_ask.price * Decimal::from(100)
                    )));
                }
                if best_ask.price >= Decimal::new(1, 2) {
                    state.push_event("warn", &format!(
                        "WARNING: {} still at {:.2}¢ — are you sure?",
                        losing_name, best_ask.price * Decimal::from(100)
                    ));
                }
            }
        }
    }

    let sweep_cfg = crate::state::SweepConfig {
        winning_team: winning,
        budget_usdc: budget,
        dry_run: body.dry_run.unwrap_or(true),
        grid_levels: body.grid_levels.unwrap_or(4),
        refresh_interval_secs: body.refresh_interval_secs.unwrap_or(30),
    };

    *state.sweep_config.write().unwrap() = Some(sweep_cfg);
    *state.sweep_phase.write().unwrap() = crate::state::SweepPhase::Active;

    // Start orderbook WS if not running
    start_book_ws(&state);

    // Sync balances
    if config.has_tokens() {
        let sync_config = config.clone();
        let sync_state = state.clone();
        tokio::spawn(async move {
            if let Ok((a, b)) = crate::ctf::sync_balances(&sync_config).await {
                let mut pos = sync_state.position.lock().unwrap();
                pos.team_a_tokens = a;
                pos.team_b_tokens = b;
            }
        });
    }

    // Spawn sweep loop
    let cancel = tokio_util::sync::CancellationToken::new();
    *state.sweep_cancel.write().unwrap() = Some(cancel.clone());

    let sweep_state = state.clone();
    tokio::spawn(async move {
        // Wait a moment for balance sync
        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
        sweep::run(sweep_state, cancel).await;
    });

    Ok(Json(serde_json::json!({"ok": true})))
}

async fn post_sweep_stop(
    State(state): State<S>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let phase = *state.sweep_phase.read().unwrap();
    if phase != crate::state::SweepPhase::Active {
        return Err((StatusCode::CONFLICT, "sweep not active".into()));
    }

    if let Some(cancel) = state.sweep_cancel.read().unwrap().clone() {
        cancel.cancel();
    }
    *state.sweep_cancel.write().unwrap() = None;

    Ok(Json(serde_json::json!({"ok": true})))
}

async fn get_sweep_balances(State(state): State<S>) -> Json<serde_json::Value> {
    let config = state.config.read().unwrap().clone();
    let eoa_address = state.auth.read().unwrap().as_ref().map(|a| a.address().to_string());
    let proxy_address = if config.polymarket_address.is_empty() { None } else { Some(config.polymarket_address.clone()) };

    // Fetch balances sequentially (each is a quick RPC call, avoids Send issues)
    let eoa_usdc = match &eoa_address {
        Some(addr) => ctf::usdc_balance(&config.polygon_rpc, addr).await.ok(),
        None => None,
    };
    let proxy_usdc = match &proxy_address {
        Some(addr) => ctf::usdc_balance(&config.polygon_rpc, addr).await.ok(),
        None => None,
    };
    let (token_a, token_b) = if config.has_tokens() {
        let a = ctf::balance_of(&config, &config.team_a_token_id).await.ok();
        let b = ctf::balance_of(&config, &config.team_b_token_id).await.ok();
        (a, b)
    } else {
        (None, None)
    };

    Json(serde_json::json!({
        "eoa_address": eoa_address,
        "proxy_address": proxy_address,
        "eoa_usdc": eoa_usdc.map(|v| v.to_string()),
        "proxy_usdc": proxy_usdc.map(|v| v.to_string()),
        "team_a_name": config.team_a_name,
        "team_b_name": config.team_b_name,
        "team_a_tokens": token_a.map(|v| v.to_string()),
        "team_b_tokens": token_b.map(|v| v.to_string()),
        "sig_type": config.signature_type,
    }))
}

#[derive(Deserialize)]
struct BuilderCodeRequest {
    builder_code: Option<String>,
}

async fn post_sweep_builder(
    State(state): State<S>,
    Json(body): Json<BuilderCodeRequest>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let mut config = state.config.write().unwrap();
    if let Some(v) = body.builder_code { config.builder_code = v; }
    config.persist();
    state.push_event("sweep", "builder code updated");
    Ok(Json(serde_json::json!({"ok": true})))
}
