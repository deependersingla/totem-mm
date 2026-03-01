use std::sync::Arc;
use std::time::Duration;

use rust_decimal::Decimal;
use tokio::sync::{mpsc, watch};

use rand::Rng;

use crate::clob_auth::ClobAuth;
use crate::config::Config;
use crate::orders::{self, BatchOrderResult};
use crate::position::Position;
use crate::state::AppState;
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
                let sell_order = build_sell_order(&config, batting, &batting_book);
                let buy_order = build_buy_order(&config, bowling, &bowling_book);

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

                tokio::spawn(async move {
                    execute_wicket_trade(
                        &task_config, &task_auth, &task_position, &task_app,
                        batting, bowling, sell_order, buy_order,
                    ).await;
                });
            }

            CricketSignal::Runs(r) => {
                tracing::debug!(runs = r, batting = %config.team_name(state.batting), "runs scored");
                app.push_event("ball", &format!("{r} runs"));
            }
            CricketSignal::Wide(r) => {
                tracing::debug!(extra_runs = r, "wide");
                app.push_event("ball", &format!("Wd+{r}"));
            }
            CricketSignal::NoBall(r) => {
                tracing::debug!(extra_runs = r, "no ball");
                app.push_event("ball", &format!("N+{r}"));
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
}

async fn execute_wicket_trade(
    config: &Config,
    auth: &ClobAuth,
    position: &Position,
    app: &Arc<AppState>,
    batting: Team,
    bowling: Team,
    sell_order: Option<FakOrder>,
    buy_order: Option<FakOrder>,
) {
    let trade_start = tokio::time::Instant::now();

    // Capture order descriptions before they're moved into fire_fak
    let sell_desc = sell_order.as_ref()
        .map(|o| format!("SELL {} @ {} sz={}", config.team_name(o.team), o.price, o.size))
        .unwrap_or_else(|| "no order".into());
    let buy_desc = buy_order.as_ref()
        .map(|o| format!("BUY {} @ {} sz={}", config.team_name(o.team), o.price, o.size))
        .unwrap_or_else(|| "no order".into());

    // Use random per-trade tags — only in internal logs/events, never sent to Polymarket.
    let sell_tag = random_tag("sell");
    let buy_tag  = random_tag("buy");

    // Send both legs in a single batch HTTP call (POST /orders).
    let (sell_result, buy_result) = fire_fak_batch(
        config, auth, position, app,
        sell_order, &sell_tag,
        buy_order,  &buy_tag,
    ).await;

    let poll_interval = Duration::from_millis(config.fill_poll_interval_ms);
    let poll_timeout = Duration::from_millis(config.fill_poll_timeout_ms);
    let revert_delay = Duration::from_millis(config.revert_delay_ms);

    let (sell_fill, buy_fill) = tokio::join!(
        poll_fill_status(auth, app, sell_result, poll_interval, poll_timeout, config),
        poll_fill_status(auth, app, buy_result, poll_interval, poll_timeout, config),
    );

    if let Some(ref f) = sell_fill {
        let mut pos = position.lock().unwrap();
        let fill_order = FakOrder { team: f.order.team, side: f.order.side, price: f.avg_price, size: f.filled_size };
        pos.on_fill(&fill_order);
    }
    if let Some(ref f) = buy_fill {
        let mut pos = position.lock().unwrap();
        let fill_order = FakOrder { team: f.order.team, side: f.order.side, price: f.avg_price, size: f.filled_size };
        pos.on_fill(&fill_order);
    }
    app.snapshot_inventory();

    if sell_fill.is_none() && buy_fill.is_none() {
        let msg = format!("no fills — sell=[{}] buy=[{}]", sell_desc, buy_desc);
        tracing::info!("{msg}");
        app.push_event("warn", &msg);
        return;
    }

    let elapsed = trade_start.elapsed();
    if elapsed < revert_delay {
        tokio::time::sleep(revert_delay - elapsed).await;
    }

    tracing::info!(delay_ms = config.revert_delay_ms, "REVERT — placing limit orders at avg fill prices");
    app.push_event("revert", &format!("placing revert orders after {}ms", config.revert_delay_ms));

    if let Some(f) = sell_fill {
        let revert = FakOrder {
            team: batting,
            side: Side::Buy,
            price: f.avg_price,
            size: f.filled_size,
        };
        execute_limit(config, auth, &revert, position, "REVERT_BUY", app).await;
    }

    if let Some(f) = buy_fill {
        let revert = FakOrder {
            team: bowling,
            side: Side::Sell,
            price: f.avg_price,
            size: f.filled_size,
        };
        execute_limit(config, auth, &revert, position, "REVERT_SELL", app).await;
    }
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
        if !r.success.unwrap_or(false) {
            let err = r.error_msg.as_deref().unwrap_or("");
            if !err.is_empty() {
                app.push_event("error", &format!("{tag}: rejected — {err}"));
            }
            return None;
        }
        let oid = r.order_id.clone()?;
        let status = r.status.as_deref().unwrap_or("unknown");
        app.push_event("trade", &format!("{tag}: FAK {} {} @ {} sz={} ({}) [{}]",
            order.side, config.team_name(order.team), order.price, order.size, oid, status));
        Some(FakResult { order_id: Some(oid), intended_order: order, tag: tag.to_string() })
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
        Ok(resp) if resp.order_id.is_some() => {
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
    _config: &Config,
) -> Option<FillInfo> {
    let result = fak_result?;
    let order_id = result.order_id.as_deref()?;

    if order_id == "dry_run" {
        return Some(FillInfo {
            filled_size: result.intended_order.size,
            avg_price: result.intended_order.price,
            order: result.intended_order,
            tag: result.tag,
        });
    }

    let deadline = tokio::time::Instant::now() + poll_timeout;

    loop {
        tokio::time::sleep(poll_interval).await;

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
                    });
                }

                if open_order.is_terminal() {
                    app.push_event("fill", &format!("{}: no fill — status {}", result.tag, status));
                    return None;
                }
            }
            Err(e) => {
                tracing::warn!(tag = %result.tag, error = %e, "poll_fill error");
            }
        }

        if tokio::time::Instant::now() >= deadline {
            // One final attempt before giving up — fetch current order state.
            // If still ambiguous, return None (no confirmed fill) rather than
            // recording a phantom position. The on-chain balance sync will
            // reconcile any fill that was missed here.
            tracing::warn!(tag = %result.tag, order_id, "fill poll timed out — making final status check");
            match orders::get_order(auth, order_id).await {
                Ok(open_order) => {
                    let filled = open_order.filled_size();
                    if !filled.is_zero() {
                        let price = open_order.fill_price();
                        app.push_event("fill", &format!("{}: final check — filled {} @ {}",
                            result.tag, filled, price));
                        return Some(FillInfo {
                            filled_size: filled,
                            avg_price: if price.is_zero() { result.intended_order.price } else { price },
                            order: result.intended_order,
                            tag: result.tag,
                        });
                    }
                    tracing::warn!(tag = %result.tag, order_id, "fill poll timed out — no confirmed fill, skipping position update");
                    app.push_event("warn", &format!("{}: fill poll timed out, no confirmed fill — check on-chain balance", result.tag));
                    return None;
                }
                Err(e) => {
                    tracing::warn!(tag = %result.tag, order_id, error = %e, "fill poll timed out and final check failed");
                    app.push_event("warn", &format!("{}: fill poll timed out, final check failed: {e}", result.tag));
                    return None;
                }
            }
        }
    }
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

pub(crate) fn build_sell_order(config: &Config, team: Team, book: &OrderBook) -> Option<FakOrder> {
    let best_bid = book.best_bid()?;
    let size = compute_size(config, &best_bid.size, best_bid.price);
    if size.is_zero() {
        tracing::warn!(team = %config.team_name(team), "no bid liquidity to sell into");
        return None;
    }
    Some(FakOrder { team, side: Side::Sell, price: best_bid.price, size })
}

pub(crate) fn build_buy_order(config: &Config, team: Team, book: &OrderBook) -> Option<FakOrder> {
    let best_ask = book.best_ask()?;
    let size = compute_size(config, &best_ask.size, best_ask.price);
    if size.is_zero() {
        tracing::warn!(team = %config.team_name(team), "no ask liquidity to buy from");
        return None;
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
            tracing::info!(tag, order_id = oid, "GTC limit order placed");
            app.track_order(oid.clone());
            app.push_event("trade", &format!("{tag}: GTC {} {} @ {} ({})", order.side, config.team_name(order.team), order.price, oid));
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
