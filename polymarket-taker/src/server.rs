use std::sync::Arc;

use axum::extract::State;
use axum::http::StatusCode;
use axum::response::Json;
use axum::routing::{get, post};
use axum::Router;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use tokio::sync::{mpsc, watch};
use tower_http::cors::CorsLayer;

use crate::clob_auth::ClobAuth;
use crate::ctf;
use crate::market_ws;
use crate::orders;
use crate::state::{AppState, MatchPhase};
use crate::strategy;
use crate::types::{CricketSignal, OrderBook, Team};
use crate::web;

type S = Arc<AppState>;

pub fn build_router(state: S) -> Router {
    Router::new()
        .route("/", get(serve_ui))
        .route("/api/status", get(get_status))
        .route("/api/config", get(get_config))
        .route("/api/events", get(get_events))
        .route("/api/inventory", get(get_inventory))
        .route("/api/setup", post(post_setup))
        .route("/api/wallet", post(post_wallet))
        .route("/api/limits", post(post_limits))
        .route("/api/start-innings", post(post_start_innings))
        .route("/api/stop-innings", post(post_stop_innings))
        .route("/api/signal", post(post_signal))
        .route("/api/match-over", post(post_match_over))
        .route("/api/cancel-all", post(post_cancel_all))
        .route("/api/reset", post(post_reset))
        .route("/api/ctf-split", post(post_ctf_split))
        .route("/api/ctf-merge", post(post_ctf_merge))
        .route("/api/ctf-redeem", post(post_ctf_redeem))
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
    })
}

async fn get_config(State(state): State<S>) -> Json<serde_json::Value> {
    let config = state.config.read().unwrap();
    Json(serde_json::json!({
        "team_a_name": config.team_a_name,
        "team_b_name": config.team_b_name,
        "team_a_token_id": config.team_a_token_id,
        "team_b_token_id": config.team_b_token_id,
        "condition_id": config.condition_id,
        "first_batting": format!("{}", config.first_batting),
        "total_budget_usdc": config.total_budget_usdc.to_string(),
        "max_trade_usdc": config.max_trade_usdc.to_string(),
        "revert_delay_ms": config.revert_delay_ms,
        "dry_run": config.dry_run,
        "signature_type": config.signature_type,
        "neg_risk": config.neg_risk,
        "wallet_set": config.has_wallet(),
        "polymarket_address": config.polymarket_address,
    }))
}

async fn get_events(State(state): State<S>) -> Json<Vec<crate::state::EventEntry>> {
    let events = state.events.lock().unwrap();
    Json(events.iter().cloned().collect())
}

async fn get_inventory(State(state): State<S>) -> Json<Vec<crate::state::InventorySnapshot>> {
    let history = state.inventory_history.lock().unwrap();
    Json(history.clone())
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

    drop(config);
    *state.match_state.write().unwrap() = crate::types::MatchState::new(
        state.config.read().unwrap().first_batting,
    );

    state.push_event("setup", "match setup updated");
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
    }

    let config = state.config.read().unwrap().clone();
    if config.has_wallet() {
        match ClobAuth::derive(&config).await {
            Ok(auth) => {
                *state.auth.write().unwrap() = Some(auth);
                state.push_event("wallet", "wallet configured and CLOB auth derived");
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
    revert_delay_ms: Option<u64>,
    dry_run: Option<bool>,
}

async fn post_limits(
    State(state): State<S>,
    Json(body): Json<LimitsRequest>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let mut config = state.config.write().unwrap();

    if let Some(v) = &body.total_budget_usdc {
        config.total_budget_usdc = v.parse().map_err(|_| (StatusCode::BAD_REQUEST, "invalid budget".into()))?;
        state.position.lock().unwrap().total_budget = config.total_budget_usdc;
    }
    if let Some(v) = &body.max_trade_usdc {
        config.max_trade_usdc = v.parse().map_err(|_| (StatusCode::BAD_REQUEST, "invalid max_trade".into()))?;
    }
    if let Some(v) = body.revert_delay_ms { config.revert_delay_ms = v; }
    if let Some(v) = body.dry_run { config.dry_run = v; }

    state.push_event("limits", "trading limits updated");
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

    let (signal_tx, signal_rx) = mpsc::channel::<CricketSignal>(64);
    let (book_tx, book_rx) = watch::channel((OrderBook::default(), OrderBook::default()));

    *state.signal_tx.write().unwrap() = Some(signal_tx);
    *state.book_rx.write().unwrap() = Some(book_rx.clone());
    *state.book_tx.write().unwrap() = Some(book_tx.clone());
    *state.phase.write().unwrap() = MatchPhase::InningsRunning;

    let cancel = tokio_util::sync::CancellationToken::new();
    *state.ws_cancel.write().unwrap() = Some(cancel.clone());

    // spawn market websocket
    let ws_config = config.clone();
    let ws_cancel = cancel.clone();
    tokio::spawn(async move {
        tokio::select! {
            res = market_ws::run(&ws_config, book_tx) => {
                if let Err(e) = res {
                    tracing::error!(error = %e, "market ws failed");
                }
            }
            _ = ws_cancel.cancelled() => {
                tracing::info!("market ws stopped by cancellation");
            }
        }
    });

    // wait for book to populate, then start strategy (no initial buy from CLOB)
    let st = state.clone();
    tokio::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_secs(3)).await;

        let config = st.config.read().unwrap().clone();
        let auth = st.auth.read().unwrap().clone().unwrap();

        strategy::run(&config, &auth, signal_rx, book_rx, st.position.clone(), st.clone()).await;

        *st.phase.write().unwrap() = MatchPhase::InningsPaused;
    });

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

    let tx = state.signal_tx.read().unwrap().clone();
    if let Some(tx) = tx {
        let _ = tx.send(CricketSignal::InningsOver).await;
    }

    if let Some(cancel) = state.ws_cancel.read().unwrap().clone() {
        cancel.cancel();
    }

    *state.phase.write().unwrap() = MatchPhase::InningsPaused;
    state.match_state.write().unwrap().switch_innings();

    let (batting_name, innings) = {
        let ms = state.match_state.read().unwrap();
        let config = state.config.read().unwrap();
        (config.team_name(ms.batting).to_string(), ms.innings)
    };
    state.push_event("innings", &format!(
        "innings paused — next: {batting_name} batting (innings {innings})"
    ));

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

    let parsed = CricketSignal::parse(&body.signal)
        .ok_or_else(|| (StatusCode::BAD_REQUEST, format!("unknown signal: {}", body.signal)))?;

    if parsed == CricketSignal::MatchOver {
        return Err((StatusCode::BAD_REQUEST, "use /api/match-over endpoint for MO".into()));
    }
    if parsed == CricketSignal::InningsOver {
        return Err((StatusCode::BAD_REQUEST, "use /api/stop-innings endpoint for IO".into()));
    }

    let tx = state.signal_tx.read().unwrap().clone();
    if let Some(tx) = tx {
        tx.send(parsed.clone())
            .await
            .map_err(|_| (StatusCode::INTERNAL_SERVER_ERROR, "signal channel closed".into()))?;
    } else {
        return Err((StatusCode::CONFLICT, "signal channel not ready".into()));
    }

    state.push_event("signal", &format!("{parsed}"));
    Ok(Json(serde_json::json!({"ok": true, "signal": body.signal})))
}

// ── Match Over ──────────────────────────────────────────────────────────────

async fn post_match_over(
    State(state): State<S>,
) -> Result<Json<serde_json::Value>, (StatusCode, String)> {
    let tx = state.signal_tx.read().unwrap().clone();
    if let Some(tx) = tx {
        let _ = tx.send(CricketSignal::MatchOver).await;
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

    let config = state.config.read().unwrap().clone();
    let order_ids: Vec<String> = state.live_order_ids.lock().unwrap().clone();

    let mut cancelled = 0u32;
    for oid in &order_ids {
        match orders::cancel_order(&config, &auth, oid).await {
            Ok(_) => cancelled += 1,
            Err(e) => tracing::warn!(order_id = oid, error = %e, "cancel failed"),
        }
    }
    state.clear_orders();

    state.push_event("cancel", &format!("cancelled {cancelled}/{} orders", order_ids.len()));
    Ok(Json(serde_json::json!({"ok": true, "cancelled": cancelled, "total": order_ids.len()})))
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
