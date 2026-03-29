//! Sweep — endgame position resolution engine.
//!
//! When a match result is clear (one team winning at >98¢), this module:
//! 1. FAK-sells all losing team tokens (sweep the bid side)
//! 2. Places a grid of resting GTC orders:
//!    - BUY winning team at prices just below best ask (0.995, 0.994, …)
//!    - SELL losing team at prices just above best bid (0.005, 0.006, …)
//! 3. Periodically refreshes the grid to track market movement
//!
//! Resting orders have NO matching delay (maker), unlike FAK/crossing orders
//! which get a 3-second sports market delay.

use std::sync::Arc;
use std::time::Duration;

use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use tokio_util::sync::CancellationToken;

use crate::clob_auth::ClobAuth;
use crate::config::Config;
use crate::orders;
use crate::state::{AppState, SweepConfig, SweepOrder, SweepPhase};
use crate::types::{FakOrder, OrderBook, Side, Team};

/// Build the grid of resting orders for the sweep.
///
/// Returns (buy_winning_orders, sell_losing_orders).
fn build_grid(
    config: &Config,
    sweep_cfg: &SweepConfig,
    books: &(OrderBook, OrderBook),
) -> (Vec<FakOrder>, Vec<FakOrder>) {
    let winning = sweep_cfg.winning_team;
    let losing = winning.opponent();
    let levels = sweep_cfg.grid_levels;

    let (win_book, lose_book) = match winning {
        Team::TeamA => (&books.0, &books.1),
        Team::TeamB => (&books.1, &books.0),
    };

    let tick: Decimal = config.tick_size.parse().unwrap_or(dec!(0.01));

    // ── Buy winning team: resting bids below the current best ask ──
    let mut buy_orders = Vec::new();
    if let Some(best_ask) = win_book.best_ask() {
        // Start 1 tick below best ask (ensures resting / maker status)
        let start_price = best_ask.price - tick;
        let per_level_usdc = sweep_cfg.budget_usdc / Decimal::from(levels);

        for i in 0..levels {
            let price = start_price - tick * Decimal::from(i);
            if price <= dec!(0.0) || price >= dec!(1.0) {
                break;
            }
            let size = (per_level_usdc / price).floor();
            if size < config.order_min_size {
                continue;
            }
            buy_orders.push(FakOrder {
                team: winning,
                side: Side::Buy,
                price,
                size,
            });
        }
    }

    // ── Sell losing team: resting asks above the current best bid ──
    let mut sell_orders = Vec::new();
    if let Some(best_bid) = lose_book.best_bid() {
        // Start 1 tick above best bid (ensures resting / maker status)
        let start_price = best_bid.price + tick;
        // We don't know exact token balance here — caller will cap sizes.
        // Split evenly across levels for now.
        for i in 0..levels {
            let price = start_price + tick * Decimal::from(i);
            if price <= dec!(0.0) || price >= dec!(1.0) {
                break;
            }
            sell_orders.push(FakOrder {
                team: losing,
                side: Side::Sell,
                price,
                size: Decimal::ZERO, // placeholder — filled by caller with actual balance
            });
        }
    }

    (buy_orders, sell_orders)
}

/// Distribute `total` tokens across `n` orders evenly (floor each, give remainder to first).
fn distribute_size(orders: &mut [FakOrder], total: Decimal, min_size: Decimal) {
    if orders.is_empty() || total < min_size {
        orders.iter_mut().for_each(|o| o.size = Decimal::ZERO);
        return;
    }
    let n = Decimal::from(orders.len());
    let per = (total / n).floor();
    let mut remaining = total;

    for (i, order) in orders.iter_mut().enumerate() {
        if i == 0 {
            // First level gets the remainder
            let first = total - per * (n - Decimal::ONE);
            order.size = first.max(Decimal::ZERO);
            remaining -= order.size;
        } else {
            let sz = per.min(remaining);
            order.size = sz.max(Decimal::ZERO);
            remaining -= order.size;
        }
    }

    // Remove orders below min size
    for order in orders.iter_mut() {
        if order.size < min_size {
            order.size = Decimal::ZERO;
        }
    }
}

/// Cancel all tracked sweep orders.
async fn cancel_sweep_orders(app: &Arc<AppState>, auth: &ClobAuth) {
    let ids: Vec<String> = {
        let orders = app.sweep_orders.lock().unwrap();
        orders.iter().map(|o| o.order_id.clone()).collect()
    };
    if ids.is_empty() {
        return;
    }
    tracing::info!(count = ids.len(), "[SWEEP] cancelling resting orders");
    if let Err(e) = orders::cancel_orders_batch(auth, &ids).await {
        tracing::warn!(error = %e, "[SWEEP] batch cancel failed");
    }
    app.sweep_orders.lock().unwrap().clear();
}

/// Place the resting order grid and track order IDs.
async fn place_grid(
    config: &Config,
    auth: &ClobAuth,
    app: &Arc<AppState>,
    buy_orders: &[FakOrder],
    sell_orders: &[FakOrder],
    dry_run: bool,
) {
    let mut tracked = Vec::new();

    for order in buy_orders.iter().chain(sell_orders.iter()) {
        if order.size.is_zero() {
            continue;
        }

        if dry_run {
            tracing::info!(
                side = %order.side, team = %config.team_name(order.team),
                price = %order.price, size = %order.size,
                "[SWEEP] [DRY] would place GTC"
            );
            app.push_event("sweep", &format!(
                "[DRY] GTC {} {} @ {} sz={}",
                order.side, config.team_name(order.team), order.price, order.size
            ));
            continue;
        }

        let tag = format!("sweep-{}-{}", order.side, order.price);
        match orders::post_limit_order(config, auth, order, &tag).await {
            Ok(resp) => {
                if let Some(oid) = resp.order_id {
                    tracing::info!(
                        order_id = %oid, side = %order.side,
                        price = %order.price, size = %order.size,
                        "[SWEEP] GTC placed"
                    );
                    app.push_event("sweep", &format!(
                        "GTC {} {} @ {} sz={} [{}]",
                        order.side, config.team_name(order.team), order.price, order.size, oid
                    ));
                    tracked.push(SweepOrder {
                        order_id: oid.clone(),
                        team: order.team,
                        side: order.side,
                        price: order.price,
                        size: order.size,
                    });
                    app.track_order(oid);
                }
            }
            Err(e) => {
                tracing::warn!(error = %e, "[SWEEP] GTC order failed");
                app.push_event("error", &format!("sweep GTC failed: {e}"));
            }
        }
    }

    app.sweep_orders.lock().unwrap().extend(tracked);
}

/// Execute the initial FAK sweep: dump all losing team tokens by hitting bids.
async fn initial_dump(
    config: &Config,
    auth: &ClobAuth,
    app: &Arc<AppState>,
    losing: Team,
    losing_balance: Decimal,
    dry_run: bool,
) {
    if losing_balance < config.order_min_size {
        app.push_event("sweep", &format!(
            "no {} tokens to dump (balance={})",
            config.team_name(losing), losing_balance
        ));
        return;
    }

    let order = FakOrder {
        team: losing,
        side: Side::Sell,
        // Aggressive price — sell at whatever bids exist (min price = 1 tick)
        price: dec!(0.01),
        size: losing_balance.floor(),
    };

    if dry_run {
        app.push_event("sweep", &format!(
            "[DRY] FAK SELL {} {} @ market (aggressive limit {})",
            order.size, config.team_name(losing), order.price
        ));
        return;
    }

    let tag = "sweep-dump";
    match orders::post_fak_order(config, auth, &order, tag).await {
        Ok(resp) => {
            let oid = resp.order_id.unwrap_or_default();
            app.push_event("sweep", &format!(
                "FAK SELL {} {} @ market [{}]",
                order.size, config.team_name(losing), oid
            ));
        }
        Err(e) => {
            app.push_event("error", &format!("sweep dump failed: {e}"));
        }
    }
}

/// Main sweep loop: place grid, refresh periodically.
pub async fn run(
    app: Arc<AppState>,
    cancel: CancellationToken,
) {
    let sweep_cfg = match app.sweep_config.read().unwrap().clone() {
        Some(cfg) => cfg,
        None => {
            tracing::error!("[SWEEP] no sweep config set");
            return;
        }
    };

    let config = app.config.read().unwrap().clone();
    let auth = match app.auth.read().unwrap().clone() {
        Some(a) => a,
        None => {
            tracing::error!("[SWEEP] no auth configured");
            app.push_event("error", "sweep: no auth configured");
            return;
        }
    };

    let winning = sweep_cfg.winning_team;
    let losing = winning.opponent();

    tracing::info!(
        winning = %config.team_name(winning),
        losing = %config.team_name(losing),
        budget = %sweep_cfg.budget_usdc,
        levels = sweep_cfg.grid_levels,
        dry_run = sweep_cfg.dry_run,
        "[SWEEP] starting"
    );

    app.push_event("sweep", &format!(
        "SWEEP started: {} wins, budget=${}, {} levels, dry_run={}",
        config.team_name(winning), sweep_cfg.budget_usdc,
        sweep_cfg.grid_levels, sweep_cfg.dry_run
    ));

    // Step 1: Dump losing team tokens via FAK
    let losing_balance = app.position.lock().unwrap().token_balance(losing);
    initial_dump(&config, &auth, &app, losing, losing_balance, sweep_cfg.dry_run).await;

    // Step 2: Place initial resting grid
    refresh_grid(&config, &auth, &app, &sweep_cfg).await;

    // Step 3: Refresh loop
    let mut interval = tokio::time::interval(Duration::from_secs(sweep_cfg.refresh_interval_secs));
    interval.tick().await; // consume first immediate tick

    loop {
        tokio::select! {
            _ = interval.tick() => {
                // Re-read config in case it changed
                let config = app.config.read().unwrap().clone();
                let sweep_cfg = match app.sweep_config.read().unwrap().clone() {
                    Some(cfg) => cfg,
                    None => break,
                };
                refresh_grid(&config, &auth, &app, &sweep_cfg).await;
            }
            _ = cancel.cancelled() => {
                tracing::info!("[SWEEP] stopping — cancelling all resting orders");
                cancel_sweep_orders(&app, &auth).await;
                break;
            }
        }
    }

    *app.sweep_phase.write().unwrap() = SweepPhase::Idle;
    *app.sweep_config.write().unwrap() = None;
    tracing::info!("[SWEEP] stopped");
    app.push_event("sweep", "SWEEP stopped");
}

/// Cancel old grid, compute new grid from current book, place it.
async fn refresh_grid(
    config: &Config,
    auth: &ClobAuth,
    app: &Arc<AppState>,
    sweep_cfg: &SweepConfig,
) {
    // Cancel existing resting orders
    cancel_sweep_orders(app, auth).await;

    // Get current book
    let books = {
        let br = app.book_rx.read().unwrap();
        match br.as_ref() {
            Some(rx) => rx.borrow().clone(),
            None => {
                tracing::debug!("[SWEEP] no book_rx, skipping refresh");
                return;
            }
        }
    };

    let losing = sweep_cfg.winning_team.opponent();

    // Build grid
    let (buy_orders, mut sell_orders) = build_grid(config, sweep_cfg, &books);

    // Distribute losing token balance across sell orders
    let losing_balance = app.position.lock().unwrap().token_balance(losing);
    distribute_size(&mut sell_orders, losing_balance, config.order_min_size);

    let buy_count = buy_orders.iter().filter(|o| o.size >= config.order_min_size).count();
    let sell_count = sell_orders.iter().filter(|o| !o.size.is_zero()).count();
    tracing::info!(buy_levels = buy_count, sell_levels = sell_count, "[SWEEP] refreshing grid");

    // Place new grid
    place_grid(config, auth, app, &buy_orders, &sell_orders, sweep_cfg.dry_run).await;
}
