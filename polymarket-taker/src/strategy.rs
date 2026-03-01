use std::sync::Arc;
use std::time::Duration;

use rust_decimal::Decimal;
use tokio::sync::{mpsc, watch};

use rand::Rng;

use crate::clob_auth::ClobAuth;
use crate::config::Config;
use crate::orders::{self, BatchOrderResult};
use crate::position::Position;
use crate::state::{AppState, TradeRecord};
use crate::types::{CricketSignal, FakOrder, OrderBook, Side, Team};

fn random_tag(prefix: &str) -> String {
    let suffix: String = rand::thread_rng()
        .sample_iter(rand::distributions::Alphanumeric)
        .take(6)
        .map(char::from)
        .collect();
    format!("{}_{}", prefix, suffix)
}

pub async fn run(
    config: &Config,
    auth: &ClobAuth,
    mut signal_rx: mpsc::Receiver<CricketSignal>,
    book_rx: watch::Receiver<(OrderBook, OrderBook)>,
    position: Position,
    app: Arc<AppState>,
) {
    // Read current match state — innings 2 means India is already batting
    let mut state = app.match_state.read().unwrap().clone();

    tracing::info!(
        batting = %config.team_name(state.batting),
        bowling = %config.team_name(state.bowling()),
        innings = state.innings,
        dry_run = config.dry_run,
        "strategy engine started"
    );

    while let Some(signal) = signal_rx.recv().await {
        let config = app.config.read().unwrap().clone();

        match signal {
            CricketSignal::MatchOver => {
                tracing::info!("MO received — shutting down strategy");
                let pos = position.lock().unwrap();
                tracing::info!(position = %pos.summary(&config), "final position");
                app.push_event("strategy", "match over — strategy stopped");
                break;
            }

            CricketSignal::InningsOver => {
                state.switch_innings();
                *app.match_state.write().unwrap() = state.clone();
                let msg = format!("innings over — {} now batting (innings {})",
                    config.team_name(state.batting), state.innings);
                tracing::info!("{msg}");
                app.push_event("innings", &msg);
            }

            CricketSignal::Wicket(extra_runs) => {
                let batting = state.batting;
                let bowling = state.bowling();
                if extra_runs > 0 {
                    app.push_event("ball", &format!("{extra_runs} runs on wicket ball"));
                }

                let msg = format!("WICKET — sell {} buy {}", config.team_name(batting), config.team_name(bowling));
                tracing::info!("{msg}");
                app.push_event("wicket", &msg);

                let books = book_rx.borrow().clone();

                if !price_in_safe_range(&config, &books) {
                    let (min, max) = config.safe_price_range();
                    let msg = format!("price outside {min}-{max} safe range — skipping trade");
                    tracing::info!("{msg}");
                    app.push_event("skip", &msg);
                    continue;
                }

                let (batting_book, bowling_book) = team_books(&books, batting);
                let sell_order = if app.is_team_enabled(batting) {
                    let held = position.lock().unwrap().token_balance(batting);
                    build_sell_order(&config, batting, &batting_book, Some(held))
                } else {
                    tracing::debug!(team = %config.team_name(batting), "team disabled — sell skipped");
                    None
                };
                let buy_order = if app.is_team_enabled(bowling) {
                    build_buy_order(&config, bowling, &bowling_book)
                } else {
                    tracing::debug!(team = %config.team_name(bowling), "team disabled — buy skipped");
                    None
                };

                if sell_order.is_none() {
                    let msg = format!("no bid on {} — sell leg skipped", config.team_name(batting));
                    tracing::warn!("{msg}");
                    app.push_event("warn", &msg);
                }
                if buy_order.is_none() {
                    let msg = format!("no ask on {} — buy leg skipped", config.team_name(bowling));
                    tracing::warn!("{msg}");
                    app.push_event("warn", &msg);
                }
                if sell_order.is_none() && buy_order.is_none() {
                    app.push_event("skip", "no book liquidity on either side — wicket trade skipped");
                    continue;
                }

                let task_config = config.clone();
                let task_auth = auth.clone();
                let task_position = position.clone();
                let task_app = app.clone();
                let task_book_rx = book_rx.clone();

                tokio::spawn(async move {
                    execute_event_trade(
                        &task_config, &task_auth, &task_position, &task_app,
                        task_book_rx, sell_order, buy_order, "WICKET",
                    ).await;
                });
            }

            CricketSignal::Runs(r) => {
                tracing::debug!(runs = r, batting = %config.team_name(state.batting), "runs scored");
                app.push_event("ball", &format!("{r} runs"));
                if is_boundary(r) {
                    let books = book_rx.borrow().clone();
                    if price_in_safe_range(&config, &books) {
                        spawn_boundary_trade(config.clone(), auth.clone(), position.clone(), app.clone(), book_rx.clone(), books, state.batting, r, "RUN");
                    } else {
                        app.push_event("skip", &format!("RUN{r}: price outside safe range — boundary skipped"));
                    }
                }
            }
            CricketSignal::Wide(r) => {
                tracing::debug!(extra_runs = r, "wide");
                app.push_event("ball", &format!("Wd+{r}"));
                if is_boundary(r) {
                    let books = book_rx.borrow().clone();
                    if price_in_safe_range(&config, &books) {
                        spawn_boundary_trade(config.clone(), auth.clone(), position.clone(), app.clone(), book_rx.clone(), books, state.batting, r, "WD");
                    } else {
                        app.push_event("skip", &format!("WD{r}: price outside safe range — boundary skipped"));
                    }
                }
            }
            CricketSignal::NoBall(r) => {
                tracing::debug!(extra_runs = r, "no ball");
                app.push_event("ball", &format!("N+{r}"));
                if is_boundary(r) {
                    let books = book_rx.borrow().clone();
                    if price_in_safe_range(&config, &books) {
                        spawn_boundary_trade(config.clone(), auth.clone(), position.clone(), app.clone(), book_rx.clone(), books, state.batting, r, "NB");
                    } else {
                        app.push_event("skip", &format!("NB{r}: price outside safe range — boundary skipped"));
                    }
                }
            }
        }
    }

    tracing::info!("strategy engine stopped");
}

/// Result of firing a single FAK order
struct FakResult {
    order_id: Option<String>,
    intended_order: FakOrder,
    tag: String,
}

/// Result after polling for fill
struct FillInfo {
    filled_size: Decimal,
    avg_price: Decimal,
    order: FakOrder,
    tag: String,
    order_id: String,
}

/// Generic trade executor: fire sell+buy FAK pair, poll fills, record position, revert.
/// Used by both wicket trades (sell batting/buy bowling) and boundary trades (sell bowling/buy batting).
/// The revert is symmetric: buy back whatever was sold, sell back whatever was bought — determined
/// by `f.order.team` so no separate "batting/bowling" parameters are needed.
async fn execute_event_trade(
    config: &Config,
    auth: &ClobAuth,
    position: &Position,
    app: &Arc<AppState>,
    _book_rx: watch::Receiver<(OrderBook, OrderBook)>,
    sell_order: Option<FakOrder>,
    buy_order: Option<FakOrder>,
    label: &str, // "WICKET" or "BOUNDARY_4" etc. for log/event context
) {
    let trade_start = tokio::time::Instant::now();

    let sell_desc = sell_order.as_ref()
        .map(|o| format!("SELL {} @ {} sz={}", config.team_name(o.team), o.price, o.size))
        .unwrap_or_else(|| "no order".into());
    let buy_desc = buy_order.as_ref()
        .map(|o| format!("BUY {} @ {} sz={}", config.team_name(o.team), o.price, o.size))
        .unwrap_or_else(|| "no order".into());

    let sell_tag = random_tag("sell");
    let buy_tag  = random_tag("buy");

    let (sell_result, buy_result) = fire_fak_batch(
        config, auth, position, app,
        sell_order, &sell_tag,
        buy_order,  &buy_tag,
    ).await;

    let poll_interval = Duration::from_millis(config.fill_poll_interval_ms);
    let poll_timeout  = Duration::from_millis(config.fill_poll_timeout_ms);
    let revert_delay  = Duration::from_millis(config.revert_delay_ms);

    let (sell_fill, buy_fill) = tokio::join!(
        poll_fill_status(auth, app, sell_result, poll_interval, poll_timeout, config),
        poll_fill_status(auth, app, buy_result,  poll_interval, poll_timeout, config),
    );

    if let Some(ref f) = sell_fill {
        let cost = f.filled_size * f.avg_price;
        let mut pos = position.lock().unwrap();
        pos.on_fill(&FakOrder { team: f.order.team, side: f.order.side, price: f.avg_price, size: f.filled_size });
        let msg = format!("{label}: SELL {} {} @ {} = ${} filled",
            f.filled_size, config.team_name(f.order.team), f.avg_price, cost.round_dp(2));
        tracing::info!("{msg}");
        app.push_event("filled", &msg);
        app.log_trade(TradeRecord {
            ts: chrono::Utc::now().format("%H:%M:%S").to_string(),
            side: "SELL".into(),
            team: config.team_name(f.order.team).to_string(),
            size: f.filled_size,
            price: f.avg_price,
            cost,
            order_type: "FAK".into(),
            label: label.to_string(),
            order_id: f.order_id.clone(),
        });
    }
    if let Some(ref f) = buy_fill {
        let cost = f.filled_size * f.avg_price;
        let mut pos = position.lock().unwrap();
        pos.on_fill(&FakOrder { team: f.order.team, side: f.order.side, price: f.avg_price, size: f.filled_size });
        let msg = format!("{label}: BUY {} {} @ {} = ${} filled",
            f.filled_size, config.team_name(f.order.team), f.avg_price, cost.round_dp(2));
        tracing::info!("{msg}");
        app.push_event("filled", &msg);
        app.log_trade(TradeRecord {
            ts: chrono::Utc::now().format("%H:%M:%S").to_string(),
            side: "BUY".into(),
            team: config.team_name(f.order.team).to_string(),
            size: f.filled_size,
            price: f.avg_price,
            cost,
            order_type: "FAK".into(),
            label: label.to_string(),
            order_id: f.order_id.clone(),
        });
    }
    app.snapshot_inventory();

    if sell_fill.is_none() && buy_fill.is_none() {
        let msg = format!("{label}: no fills — sell=[{sell_desc}] buy=[{buy_desc}]");
        tracing::info!("{msg}");
        app.push_event("warn", &msg);
        return;
    }

    let elapsed = trade_start.elapsed();
    if elapsed < revert_delay {
        tokio::time::sleep(revert_delay - elapsed).await;
    }

    // Determine edge % based on signal type (label)
    let edge_pct = if label.starts_with("WICKET") || label.starts_with("W") {
        config.edge_wicket
    } else if label.contains('6') {
        config.edge_boundary_6
    } else {
        config.edge_boundary_4
    };
    let edge_mult = Decimal::from_f64_retain(edge_pct / 100.0).unwrap_or(Decimal::ZERO);

    tracing::info!(delay_ms = config.revert_delay_ms, edge_pct, "{label} REVERT");
    app.push_event("revert", &format!("{label}: revert after {}ms (edge {edge_pct}%)", config.revert_delay_ms));

    // Revert buy → sell back at original_price * (1 + edge)
    // e.g. bought at 75¢ with 1% edge → sell limit at 75.75¢ → rounds to 76¢
    // Fills at 76¢ or better. If market is at 80¢, fills at 80¢.
    if let Some(f) = buy_fill {
        let raw_limit = f.avg_price * (Decimal::ONE + edge_mult);
        let limit_price = raw_limit.round_dp(2);
        let size = f.filled_size.round_dp(2); // CLOB requires max 2 dp for token amounts
        tracing::info!(
            team = %config.team_name(f.order.team),
            original = %f.avg_price, edge_pct,
            limit_price = %limit_price, size = %size,
            "REVERT_SELL: GTC sell limit (original + {edge_pct}% edge)"
        );
        execute_limit(config, auth, &FakOrder {
            team: f.order.team,
            side: Side::Sell,
            price: limit_price,
            size,
        }, position, "REVERT_SELL", app).await;
    }
    // Revert sell → buy back at original_price * (1 - edge)
    // e.g. sold at 25¢ with 1% edge → buy limit at 24.75¢ → rounds to 25¢
    // Fills at 25¢ or better (lower). If market is at 22¢, fills at 22¢.
    if let Some(f) = sell_fill {
        let raw_limit = f.avg_price * (Decimal::ONE - edge_mult);
        let limit_price = raw_limit.round_dp(2);
        let size = f.filled_size.round_dp(2);
        tracing::info!(
            team = %config.team_name(f.order.team),
            original = %f.avg_price, edge_pct,
            limit_price = %limit_price, size = %size,
            "REVERT_BUY: GTC buy limit (original - {edge_pct}% edge)"
        );
        execute_limit(config, auth, &FakOrder {
            team: f.order.team,
            side: Side::Buy,
            price: limit_price,
            size,
        }, position, "REVERT_BUY", app).await;
    }
}

fn is_boundary(runs: u8) -> bool {
    runs > 3
}

/// Spawn a boundary trade: sell bowling team, buy batting team, then revert.
/// Called for Runs(4/6), Wide(4/6), NoBall(4/6). NOT called for Wicket(4/6) — those stay wicket.
fn spawn_boundary_trade(
    config: Config,
    auth: ClobAuth,
    position: Position,
    app: Arc<AppState>,
    book_rx: watch::Receiver<(OrderBook, OrderBook)>,
    books: (OrderBook, OrderBook),
    batting: Team,
    runs: u8,
    kind: &'static str,
) {
    let bowling = batting.opponent();
    let (batting_book, bowling_book) = team_books(&books, batting);

    // Boundary: bowler got hit → sell bowling (price drops), buy batting (price rises)
    let sell_order = if app.is_team_enabled(bowling) {
        let held = position.lock().unwrap().token_balance(bowling);
        build_sell_order(&config, bowling, &bowling_book, Some(held))
    } else {
        None
    };
    let buy_order = if app.is_team_enabled(batting) {
        build_buy_order(&config, batting, &batting_book)
    } else {
        None
    };

    if sell_order.is_none() && buy_order.is_none() {
        app.push_event("skip", &format!("{kind}{runs}: no liquidity for boundary trade"));
        return;
    }

    let msg = format!("{kind}{runs} BOUNDARY — sell {} buy {}",
        config.team_name(bowling), config.team_name(batting));
    tracing::info!("{msg}");
    app.push_event("boundary", &msg);

    let label = format!("{kind}{runs}");
    tokio::spawn(async move {
        execute_event_trade(&config, &auth, &position, &app, book_rx, sell_order, buy_order, &label).await;
    });
}

/// Send sell + buy FAK orders together via POST /orders (batch endpoint).
/// Handles dry_run, budget checks, and maps responses back to FakResult pairs.
async fn fire_fak_batch(
    config: &Config,
    auth: &ClobAuth,
    position: &Position,
    app: &Arc<AppState>,
    sell_order: Option<FakOrder>,
    sell_tag: &str,
    buy_order: Option<FakOrder>,
    buy_tag: &str,
) -> (Option<FakResult>, Option<FakResult>) {
    // Budget check for buy order
    let buy_order = if let Some(ref buy) = buy_order {
        let notional = buy.price * buy.size;
        let can_spend = position.lock().unwrap().can_spend(notional);
        if !can_spend {
            tracing::warn!(tag = buy_tag, notional = %notional, "budget exceeded — buy skipped");
            app.push_event("warn", &format!("{buy_tag}: budget exceeded, skipping"));
            None
        } else {
            buy_order
        }
    } else {
        buy_order
    };

    // Dry run: simulate both legs independently
    if config.dry_run {
        let dry = |order: Option<FakOrder>, tag: &str| -> Option<FakResult> {
            let o = order?;
            let notional = o.price * o.size;
            tracing::info!(tag, side = %o.side, team = %config.team_name(o.team),
                price = %o.price, size = %o.size, notional = %notional,
                "[DRY RUN] would place FAK order");
            app.push_event("trade", &format!("[DRY] {tag}: {} {} @ {} sz={}",
                o.side, config.team_name(o.team), o.price, o.size));
            Some(FakResult { order_id: Some("dry_run".to_string()), intended_order: o, tag: tag.to_string() })
        };
        return (dry(sell_order, sell_tag), dry(buy_order, buy_tag));
    }

    // Build batch — track which index corresponds to sell vs buy
    let mut batch: Vec<(FakOrder, &str)> = Vec::new();
    let mut sell_idx: Option<usize> = None;
    let mut buy_idx:  Option<usize> = None;

    if let Some(ref o) = sell_order { sell_idx = Some(batch.len()); batch.push((o.clone(), sell_tag)); }
    if let Some(ref o) = buy_order  { buy_idx  = Some(batch.len()); batch.push((o.clone(), buy_tag));  }

    if batch.is_empty() {
        return (None, None);
    }

    let batch_results = match orders::post_fak_orders_batch(config, auth, &batch).await {
        Ok(r) => r,
        Err(e) => {
            tracing::error!(error = %e, "batch FAK order failed");
            app.push_event("error", &format!("batch: {e}"));
            return (None, None);
        }
    };

    let extract = |idx: Option<usize>, order: Option<FakOrder>, tag: &str, results: &[BatchOrderResult]| -> Option<FakResult> {
        let i = idx?;
        let order = order?;
        let r = results.get(i)?;
        let err = r.error_msg.as_deref().unwrap_or("");
        if !r.success.unwrap_or(false) || !err.is_empty() {
            if !err.is_empty() {
                app.push_event("error", &format!("{tag}: rejected — {err}"));
            }
            return None;
        }
        let oid = r.order_id.as_deref().filter(|s| !s.is_empty())?;
        let status = r.status.as_deref().unwrap_or("unknown");
        app.push_event("trade", &format!("{tag}: FAK {} {} @ {} sz={} ({}) [{}]",
            order.side, config.team_name(order.team), order.price, order.size, oid, status));
        Some(FakResult { order_id: Some(oid.to_string()), intended_order: order, tag: tag.to_string() })
    };

    let sell_result = extract(sell_idx, sell_order, sell_tag, &batch_results);
    let buy_result  = extract(buy_idx,  buy_order,  buy_tag,  &batch_results);
    (sell_result, buy_result)
}

async fn fire_fak(
    config: &Config,
    auth: &ClobAuth,
    position: &Position,
    app: &Arc<AppState>,
    order: Option<FakOrder>,
    tag: &str,
) -> Option<FakResult> {
    let order = order?;
    let notional = order.price * order.size;

    {
        let pos = position.lock().unwrap();
        if order.side == Side::Buy && !pos.can_spend(notional) {
            tracing::warn!(tag, notional = %notional, remaining = %pos.remaining_budget(), "budget exceeded — skipping");
            app.push_event("warn", &format!("{tag}: budget exceeded, skipping"));
            return None;
        }
    }

    if config.dry_run {
        tracing::info!(tag, side = %order.side, team = %config.team_name(order.team),
            price = %order.price, size = %order.size, notional = %notional,
            "[DRY RUN] would place FAK order");
        app.push_event("trade", &format!("[DRY] {tag}: {} {} @ {} sz={}", order.side, config.team_name(order.team), order.price, order.size));
        return Some(FakResult {
            order_id: Some("dry_run".to_string()),
            intended_order: order,
            tag: tag.to_string(),
        });
    }

    match orders::post_fak_order(config, auth, &order, tag).await {
        Ok(resp) if resp.order_id.as_deref().map_or(false, |s| !s.is_empty()) => {
            let oid = resp.order_id.unwrap();
            let status = resp.status.as_deref().unwrap_or("unknown");
            app.push_event("trade", &format!("{tag}: FAK {} {} @ {} sz={} ({}) [{}]",
                order.side, config.team_name(order.team), order.price, order.size, oid, status));
            Some(FakResult {
                order_id: Some(oid),
                intended_order: order,
                tag: tag.to_string(),
            })
        }
        Ok(resp) => {
            let msg = resp.error_msg.unwrap_or_default();
            app.push_event("error", &format!("{tag}: rejected — {msg}"));
            None
        }
        Err(e) => {
            tracing::error!(tag, error = %e, "FAK order failed");
            app.push_event("error", &format!("{tag}: {e}"));
            None
        }
    }
}

async fn poll_fill_status(
    auth: &ClobAuth,
    app: &Arc<AppState>,
    fak_result: Option<FakResult>,
    poll_interval: Duration,
    poll_timeout: Duration,
    config: &Config,
) -> Option<FillInfo> {
    let result = fak_result?;
    let order_id = result.order_id.as_deref().filter(|s| !s.is_empty())?;

    let oid = order_id.to_string();

    if order_id == "dry_run" {
        return Some(FillInfo {
            filled_size: result.intended_order.size,
            avg_price: result.intended_order.price,
            order: result.intended_order,
            tag: result.tag,
            order_id: oid,
        });
    }

    // Sports markets have a 3-second matching delay ("delayed" status).
    // Wait before polling to avoid wasting requests on null responses.
    const MATCH_DELAY: Duration = Duration::from_millis(3500);
    tracing::info!(tag = %result.tag, order_id, "waiting 3.5s for sports market matching delay");
    tokio::time::sleep(MATCH_DELAY).await;

    // After the matching delay, the order should be processed quickly.
    // Poll for the result with a generous timeout for data indexer lag.
    let deadline = tokio::time::Instant::now() + poll_timeout;

    loop {
        match orders::get_order(auth, order_id).await {
            Ok(open_order) => {
                let filled = open_order.filled_size();
                let price = open_order.fill_price();
                let status = open_order.status.as_deref().unwrap_or("unknown");

                tracing::debug!(
                    tag = %result.tag, order_id, status,
                    filled = %filled, price = %price,
                    "poll fill status"
                );

                if !filled.is_zero() {
                    app.push_event("fill", &format!("{}: filled {} @ {} [{}]",
                        result.tag, filled, price, status));
                    return Some(FillInfo {
                        filled_size: filled,
                        avg_price: if price.is_zero() { result.intended_order.price } else { price },
                        order: result.intended_order,
                        tag: result.tag,
                        order_id: oid,
                    });
                }

                if open_order.is_terminal() {
                    tracing::warn!(
                        tag = %result.tag, order_id, status,
                        "FAK order got NO fill — killed by matching engine"
                    );
                    app.push_event("warn", &format!("{}: NO FILL — status {} (order killed)", result.tag, status));
                    return None;
                }
            }
            Err(e) => {
                tracing::debug!(tag = %result.tag, error = %e, "poll_fill: order not indexed yet");
            }
        }

        if tokio::time::Instant::now() >= deadline {
            break;
        }

        tokio::time::sleep(poll_interval).await;
    }

    // Order endpoint didn't return a result — try /data/trades as fallback.
    // The trades API may index faster than the order API.
    tracing::info!(tag = %result.tag, order_id, "order poll timed out — checking /data/trades fallback");
    let token_id = config.token_id(result.intended_order.team);
    match orders::get_user_trades(auth, Some(token_id)).await {
        Ok(trades) => {
            // Find a trade matching our order_id
            for t in &trades {
                let trade_oid = t.get("order_id")
                    .or_else(|| t.get("orderID"))
                    .and_then(|v| v.as_str())
                    .unwrap_or("");
                if trade_oid == order_id {
                    let size_str = t.get("size").and_then(|v| v.as_str()).unwrap_or("0");
                    let price_str = t.get("price").and_then(|v| v.as_str()).unwrap_or("0");
                    let filled: Decimal = size_str.parse().unwrap_or(Decimal::ZERO);
                    let price: Decimal = price_str.parse().unwrap_or(Decimal::ZERO);
                    if !filled.is_zero() {
                        tracing::info!(
                            tag = %result.tag, order_id,
                            filled = %filled, price = %price,
                            "found fill via /data/trades fallback"
                        );
                        app.push_event("fill", &format!("{}: filled {} @ {} (via trades API)",
                            result.tag, filled, price));
                        return Some(FillInfo {
                            filled_size: filled,
                            avg_price: if price.is_zero() { result.intended_order.price } else { price },
                            order: result.intended_order,
                            tag: result.tag,
                            order_id: oid,
                        });
                    }
                }
            }
            tracing::warn!(tag = %result.tag, order_id, "no matching trade found in /data/trades");
        }
        Err(e) => {
            tracing::warn!(tag = %result.tag, error = %e, "/data/trades fallback failed");
        }
    }

    tracing::warn!(tag = %result.tag, order_id, "fill poll timed out — no confirmed fill");
    app.push_event("warn", &format!("{}: fill poll timed out, no confirmed fill — check on-chain balance", result.tag));
    None
}

pub(crate) fn price_in_safe_range(config: &Config, books: &(OrderBook, OrderBook)) -> bool {
    let (min, max) = config.safe_price_range();
    let check = |book: &OrderBook| -> bool {
        if let Some(bid) = book.best_bid() {
            if bid.price < min || bid.price > max { return false; }
        }
        if let Some(ask) = book.best_ask() {
            if ask.price < min || ask.price > max { return false; }
        }
        true
    };
    check(&books.0) && check(&books.1)
}

fn team_books(books: &(OrderBook, OrderBook), team: Team) -> (OrderBook, OrderBook) {
    match team {
        Team::TeamA => (books.0.clone(), books.1.clone()),
        Team::TeamB => (books.1.clone(), books.0.clone()),
    }
}

pub(crate) fn build_sell_order(config: &Config, team: Team, book: &OrderBook, held_tokens: Option<Decimal>) -> Option<FakOrder> {
    let best_bid = book.best_bid()?;
    let mut size = compute_size(config, &best_bid.size, best_bid.price);

    // Cap sell size to tokens actually held (avoid "not enough balance" errors)
    if let Some(held) = held_tokens {
        let held_floor = held.floor();
        if held_floor < config.order_min_size {
            tracing::debug!(team = %config.team_name(team), held = %held_floor, min = %config.order_min_size, "held tokens below market min — sell skipped");
            return None;
        }
        if size > held_floor {
            tracing::debug!(team = %config.team_name(team), original = %size, capped = %held_floor, "sell size capped to held tokens");
            size = held_floor;
        }
    }

    if size.is_zero() {
        tracing::warn!(team = %config.team_name(team), "no bid liquidity to sell into");
        return None;
    }
    if size < config.order_min_size {
        tracing::debug!(team = %config.team_name(team), size = %size, min = %config.order_min_size, "sell size below market min — skipping");
        return None;
    }
    Some(FakOrder { team, side: Side::Sell, price: best_bid.price, size })
}

pub(crate) fn build_buy_order(config: &Config, team: Team, book: &OrderBook) -> Option<FakOrder> {
    let best_ask = book.best_ask()?;
    let mut size = compute_size(config, &best_ask.size, best_ask.price);
    if size.is_zero() {
        tracing::warn!(team = %config.team_name(team), "no ask liquidity to buy from");
        return None;
    }
    if size < config.order_min_size {
        size = config.order_min_size;
        tracing::debug!(team = %config.team_name(team), size = %size, "buy size clamped to market min");
    }
    Some(FakOrder { team, side: Side::Buy, price: best_ask.price, size })
}

pub(crate) fn compute_size(config: &Config, available: &Decimal, price: Decimal) -> Decimal {
    if price.is_zero() { return Decimal::ZERO; }
    let max_tokens = config.max_trade_usdc / price;
    // Floor to whole tokens — Polymarket expects integer share sizes
    max_tokens.min(*available).floor()
}

async fn execute_limit(
    config: &Config, auth: &ClobAuth, order: &FakOrder,
    _position: &Position, tag: &str, app: &Arc<AppState>,
) {
    if config.dry_run {
        let notional = order.price * order.size;
        tracing::info!(tag, side = %order.side, team = %config.team_name(order.team),
            price = %order.price, size = %order.size, notional = %notional,
            "[DRY RUN] would place GTC limit order");
        app.push_event("trade", &format!("[DRY] {tag}: GTC {} {} @ {} sz={}", order.side, config.team_name(order.team), order.price, order.size));
        return;
    }

    match orders::post_limit_order(config, auth, order, tag).await {
        Ok(resp) if resp.order_id.is_some() => {
            let oid = resp.order_id.unwrap();
            let cost = order.price * order.size;
            tracing::info!(tag, order_id = oid, "GTC limit order placed");
            app.track_order(oid.clone());
            app.push_event("trade", &format!("{tag}: GTC {} {} @ {} sz={} = ${} ({})",
                order.side, config.team_name(order.team), order.price, order.size, cost.round_dp(2), oid));
            app.log_trade(TradeRecord {
                ts: chrono::Utc::now().format("%H:%M:%S").to_string(),
                side: format!("{}", order.side),
                team: config.team_name(order.team).to_string(),
                size: order.size,
                price: order.price,
                cost,
                order_type: "GTC".into(),
                label: tag.to_string(),
                order_id: oid,
            });
        }
        Ok(resp) => {
            let msg = resp.error_msg.unwrap_or_default();
            app.push_event("error", &format!("{tag}: GTC rejected — {msg}"));
        }
        Err(e) => {
            tracing::error!(tag, error = %e, "GTC limit order failed");
            app.push_event("error", &format!("{tag}: {e}"));
        }
    }
}
