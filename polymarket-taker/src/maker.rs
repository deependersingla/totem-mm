//! Maker — 4-leg market making engine for cricket prediction markets.
//!
//! Maintains resting GTC/GTD orders on both sides of both tokens.
//! Uses Avellaneda-Stoikov inventory skew and event-driven cancellation.
//! DRY_RUN mode must be enabled initially — no live orders without explicit opt-in.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use tokio::sync::{broadcast, mpsc};
use tokio::time::interval;
use tokio_util::sync::CancellationToken;

use crate::orders;
use crate::state::AppState;
use crate::types::{CricketSignal, FakOrder, FillEvent, OrderBook, Side, Team};

// ── Types ────────────────────────────────────────────────────────────────────

/// Which of the 4 quote legs this order represents.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum QuoteLeg {
    TeamABuy,
    TeamASell,
    TeamBBuy,
    TeamBSell,
}

impl QuoteLeg {
    pub fn team(self) -> Team {
        match self {
            Self::TeamABuy | Self::TeamASell => Team::TeamA,
            Self::TeamBBuy | Self::TeamBSell => Team::TeamB,
        }
    }

    pub fn side(self) -> Side {
        match self {
            Self::TeamABuy | Self::TeamBBuy => Side::Buy,
            Self::TeamASell | Self::TeamBSell => Side::Sell,
        }
    }

    pub fn all() -> &'static [QuoteLeg] {
        &[Self::TeamABuy, Self::TeamASell, Self::TeamBBuy, Self::TeamBSell]
    }
}

impl std::fmt::Display for QuoteLeg {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::TeamABuy => write!(f, "A-BUY"),
            Self::TeamASell => write!(f, "A-SELL"),
            Self::TeamBBuy => write!(f, "B-BUY"),
            Self::TeamBSell => write!(f, "B-SELL"),
        }
    }
}

/// Inventory tier for risk management.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InventoryTier {
    Green,
    Yellow,
    Orange,
    Red,
}

impl std::fmt::Display for InventoryTier {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Green => write!(f, "GREEN"),
            Self::Yellow => write!(f, "YELLOW"),
            Self::Orange => write!(f, "ORANGE"),
            Self::Red => write!(f, "RED"),
        }
    }
}

/// Maker-specific inventory (separate from taker position tracker).
#[derive(Debug, Clone)]
pub struct MakerInventory {
    pub team_a_tokens: Decimal,
    pub team_b_tokens: Decimal,
    pub usdc_reserve: Decimal,
    pub initial_split: Decimal,
}

impl MakerInventory {
    pub fn new(initial_usdc: Decimal) -> Self {
        Self {
            team_a_tokens: Decimal::ZERO,
            team_b_tokens: Decimal::ZERO,
            usdc_reserve: initial_usdc,
            initial_split: initial_usdc,
        }
    }

    /// Net exposure: abs(team_a_tokens - team_b_tokens) in token terms.
    pub fn exposure(&self) -> Decimal {
        (self.team_a_tokens - self.team_b_tokens).abs()
    }
}

/// Runtime state for the maker engine.
pub struct MakerState {
    pub live_orders: HashMap<QuoteLeg, String>,
    pub inventory: MakerInventory,
    pub pending_cancel: Vec<QuoteLeg>,
}

impl MakerState {
    pub fn new(initial_usdc: Decimal) -> Self {
        Self {
            live_orders: HashMap::new(),
            inventory: MakerInventory::new(initial_usdc),
            pending_cancel: Vec::new(),
        }
    }
}

// ── Pure computation functions ───────────────────────────────────────────────

/// Compute fair value as the midpoint of best bid and best ask.
pub fn compute_fair_value(book: &OrderBook) -> Option<Decimal> {
    let bid = book.best_bid()?.price;
    let ask = book.best_ask()?.price;
    Some((bid + ask) / dec!(2))
}

/// Avellaneda-Stoikov reservation price: shifts the fair value by inventory skew.
///   reservation = fair - exposure * kappa
/// Where exposure is signed: positive if long, negative if short.
pub fn compute_reservation_price(
    fair: Decimal,
    exposure: Decimal,
    kappa: Decimal,
) -> Decimal {
    fair - exposure * kappa
}

/// Compute bid/ask quote prices from reservation price and half-spread,
/// rounded to the nearest tick.
pub fn compute_quote_prices(
    reservation: Decimal,
    half_spread: Decimal,
    tick: &str,
) -> (Decimal, Decimal) {
    let tick_size = Decimal::from_str_exact(tick).unwrap_or(dec!(0.01));
    let raw_bid = reservation - half_spread;
    let raw_ask = reservation + half_spread;

    let bid = round_to_tick(raw_bid, tick_size);
    let ask = round_to_tick(raw_ask, tick_size);

    // Ensure bid < ask (at least 1 tick apart)
    if bid >= ask {
        let mid = round_to_tick(reservation, tick_size);
        (mid - tick_size, mid + tick_size)
    } else {
        (bid, ask)
    }
}

fn round_to_tick(val: Decimal, tick: Decimal) -> Decimal {
    if tick.is_zero() {
        return val;
    }
    (val / tick).round() * tick
}

/// Determine which legs must be cancelled on a given signal.
/// `batting` is the team currently batting.
pub fn cancellation_legs(signal: &CricketSignal, batting: Team) -> Vec<QuoteLeg> {
    let bowling = batting.opponent();

    let batting_buy = match batting {
        Team::TeamA => QuoteLeg::TeamABuy,
        Team::TeamB => QuoteLeg::TeamBBuy,
    };
    let batting_sell = match batting {
        Team::TeamA => QuoteLeg::TeamASell,
        Team::TeamB => QuoteLeg::TeamBSell,
    };
    let bowling_buy = match bowling {
        Team::TeamA => QuoteLeg::TeamABuy,
        Team::TeamB => QuoteLeg::TeamBBuy,
    };
    let bowling_sell = match bowling {
        Team::TeamA => QuoteLeg::TeamASell,
        Team::TeamB => QuoteLeg::TeamBSell,
    };

    match signal {
        CricketSignal::Wicket(_) => {
            // Wicket: cancel batting-BUY + bowling-SELL
            vec![batting_buy, bowling_sell]
        }
        CricketSignal::Runs(r) if *r >= 4 => {
            // Boundary (4/6): cancel batting-SELL + bowling-BUY
            vec![batting_sell, bowling_buy]
        }
        CricketSignal::NoBall(r) if *r >= 4 => {
            vec![batting_sell, bowling_buy]
        }
        CricketSignal::Runs(_) | CricketSignal::Wide(_) | CricketSignal::NoBall(_) => {
            // Dot/1-3 runs: keep all
            vec![]
        }
        CricketSignal::InningsOver | CricketSignal::MatchOver => {
            // Cancel ALL 4 legs
            vec![batting_buy, batting_sell, bowling_buy, bowling_sell]
        }
    }
}

/// Classify inventory exposure into a tier.
pub fn inventory_tier(
    exposure: Decimal,
    initial_split: Decimal,
    t1_pct: f64,
    t2_pct: f64,
    t3_pct: f64,
) -> InventoryTier {
    if initial_split.is_zero() {
        return InventoryTier::Green;
    }
    let ratio = exposure / initial_split;
    let ratio_f64 = ratio.to_string().parse::<f64>().unwrap_or(0.0);

    if ratio_f64 < t1_pct {
        InventoryTier::Green
    } else if ratio_f64 < t2_pct {
        InventoryTier::Yellow
    } else if ratio_f64 < t3_pct {
        InventoryTier::Orange
    } else {
        InventoryTier::Red
    }
}

// ── Main loop ────────────────────────────────────────────────────────────────

pub async fn run(
    state: Arc<AppState>,
    mut signal_rx: broadcast::Receiver<CricketSignal>,
    mut fill_rx: mpsc::Receiver<FillEvent>,
    cancel: CancellationToken,
) {
    let maker_cfg = state.maker_config.read().unwrap().clone();

    if !maker_cfg.enabled {
        tracing::info!("[MAKER] maker is disabled, exiting");
        return;
    }

    tracing::info!(
        dry_run = maker_cfg.dry_run,
        half_spread = %maker_cfg.half_spread,
        quote_size = %maker_cfg.quote_size,
        refresh_secs = maker_cfg.refresh_interval_secs,
        "[MAKER] starting"
    );

    let initial_usdc = {
        let config = state.config.read().unwrap();
        config.total_budget_usdc / dec!(2) // reserve half for maker
    };
    let mut maker_state = MakerState::new(initial_usdc);

    let mut refresh_timer = interval(Duration::from_secs(maker_cfg.refresh_interval_secs));
    refresh_timer.tick().await; // consume first immediate tick

    loop {
        tokio::select! {
            Ok(signal) = signal_rx.recv() => {
                handle_signal(&state, &signal, &mut maker_state).await;
            }
            Some(fill) = fill_rx.recv() => {
                handle_fill(&state, &fill, &mut maker_state).await;
            }
            _ = refresh_timer.tick() => {
                refresh_quotes(&state, &mut maker_state).await;
            }
            _ = cancel.cancelled() => {
                tracing::info!("[MAKER] shutting down, cancelling all maker orders");
                cancel_all_maker_orders(&state, &mut maker_state).await;
                break;
            }
        }
    }

    tracing::info!("[MAKER] stopped");
}

async fn handle_signal(
    state: &Arc<AppState>,
    signal: &CricketSignal,
    maker_state: &mut MakerState,
) {
    let batting = state.match_state.read().unwrap().batting;
    let legs = cancellation_legs(signal, batting);

    if legs.is_empty() {
        tracing::debug!("[MAKER] signal {signal} — no legs to cancel");
        return;
    }

    tracing::info!(
        signal = %signal,
        legs = ?legs.iter().map(|l| l.to_string()).collect::<Vec<_>>(),
        "[MAKER] cancelling legs"
    );

    // Cancel the affected legs
    for leg in &legs {
        if let Some(order_id) = maker_state.live_orders.remove(leg) {
            cancel_maker_order(state, &order_id).await;
        }
    }

    // For InningsOver / MatchOver, don't re-quote
    if matches!(signal, CricketSignal::InningsOver | CricketSignal::MatchOver) {
        return;
    }

    // Re-quote after cancellation
    refresh_quotes(state, maker_state).await;
}

async fn handle_fill(
    _state: &Arc<AppState>,
    fill: &FillEvent,
    maker_state: &mut MakerState,
) {
    tracing::info!(
        order_id = %fill.order_id,
        size = %fill.filled_size,
        price = %fill.avg_price,
        side = %fill.side,
        "[MAKER] fill received"
    );

    // Update maker inventory based on fill
    // Determine which team by matching against live_orders
    let mut filled_leg = None;
    for (leg, oid) in &maker_state.live_orders {
        if *oid == fill.order_id {
            filled_leg = Some(*leg);
            break;
        }
    }

    if let Some(leg) = filled_leg {
        match (leg.team(), leg.side()) {
            (Team::TeamA, Side::Buy) => {
                maker_state.inventory.team_a_tokens += fill.filled_size;
                maker_state.inventory.usdc_reserve -= fill.filled_size * fill.avg_price;
            }
            (Team::TeamA, Side::Sell) => {
                maker_state.inventory.team_a_tokens -= fill.filled_size;
                maker_state.inventory.usdc_reserve += fill.filled_size * fill.avg_price;
            }
            (Team::TeamB, Side::Buy) => {
                maker_state.inventory.team_b_tokens += fill.filled_size;
                maker_state.inventory.usdc_reserve -= fill.filled_size * fill.avg_price;
            }
            (Team::TeamB, Side::Sell) => {
                maker_state.inventory.team_b_tokens -= fill.filled_size;
                maker_state.inventory.usdc_reserve += fill.filled_size * fill.avg_price;
            }
        }

        // Remove the filled order from live_orders
        if fill.status == "MATCHED" {
            maker_state.live_orders.remove(&leg);
        }
    }
}

async fn refresh_quotes(
    state: &Arc<AppState>,
    maker_state: &mut MakerState,
) {
    let maker_cfg = state.maker_config.read().unwrap().clone();
    let config = state.config.read().unwrap().clone();

    // Get orderbook snapshot
    let (book_a, book_b) = {
        let br = state.book_rx.read().unwrap();
        match br.as_ref() {
            Some(rx) => rx.borrow().clone(),
            None => {
                tracing::debug!("[MAKER] no book_rx, skipping refresh");
                return;
            }
        }
    };

    // Compute fair values
    let fair_a = match compute_fair_value(&book_a) {
        Some(f) => f,
        None => {
            tracing::debug!("[MAKER] no fair value for team A, skipping");
            return;
        }
    };

    // Complementary pricing: fair_b = 1 - fair_a
    let fair_b = Decimal::ONE - fair_a;

    // Compute inventory exposure (positive = long A, negative = long B)
    let exposure = maker_state.inventory.team_a_tokens - maker_state.inventory.team_b_tokens;

    // Compute reservation prices
    let reservation_a = compute_reservation_price(fair_a, exposure, maker_cfg.skew_kappa);
    let reservation_b = compute_reservation_price(fair_b, -exposure, maker_cfg.skew_kappa);

    // Compute quote prices
    let (bid_a, ask_a) = compute_quote_prices(reservation_a, maker_cfg.half_spread, &config.tick_size);
    let (bid_b, ask_b) = compute_quote_prices(reservation_b, maker_cfg.half_spread, &config.tick_size);

    // Check inventory tier
    let tier = inventory_tier(
        maker_state.inventory.exposure(),
        maker_state.inventory.initial_split,
        maker_cfg.t1_pct,
        maker_cfg.t2_pct,
        maker_cfg.t3_pct,
    );

    // Adjust size based on tier
    let size = match tier {
        InventoryTier::Green => maker_cfg.quote_size,
        InventoryTier::Yellow => maker_cfg.quote_size * dec!(0.75),
        InventoryTier::Orange => maker_cfg.quote_size * dec!(0.5),
        InventoryTier::Red => {
            tracing::warn!("[MAKER] RED tier — not quoting");
            return;
        }
    };

    tracing::info!(
        fair_a = %fair_a,
        fair_b = %fair_b,
        bid_a = %bid_a,
        ask_a = %ask_a,
        bid_b = %bid_b,
        ask_b = %ask_b,
        exposure = %exposure,
        tier = %tier,
        size = %size,
        dry_run = maker_cfg.dry_run,
        "[MAKER] refresh quotes"
    );

    // DRY_RUN: log but don't submit
    if maker_cfg.dry_run {
        tracing::info!(
            "[MAKER] DRY_RUN — would place: A-BUY@{bid_a} A-SELL@{ask_a} B-BUY@{bid_b} B-SELL@{ask_b} sz={size}"
        );
        return;
    }

    // Snapshot auth to avoid holding RwLockReadGuard across awaits
    let auth = match state.auth.read().unwrap().clone() {
        Some(a) => a,
        None => {
            tracing::warn!("[MAKER] no auth, cannot place orders");
            return;
        }
    };

    // Cancel old orders first
    let old_ids: Vec<String> = maker_state.live_orders.values().cloned().collect();
    if !old_ids.is_empty() {
        if let Err(e) = orders::cancel_orders_batch(&auth, &old_ids).await {
            tracing::warn!(error = %e, "[MAKER] failed to cancel old orders");
        }
        maker_state.live_orders.clear();
    }

    // Place new orders
    let legs = [
        (QuoteLeg::TeamABuy, Team::TeamA, Side::Buy, bid_a),
        (QuoteLeg::TeamASell, Team::TeamA, Side::Sell, ask_a),
        (QuoteLeg::TeamBBuy, Team::TeamB, Side::Buy, bid_b),
        (QuoteLeg::TeamBSell, Team::TeamB, Side::Sell, ask_b),
    ];

    // Clamp prices to valid range
    let min_price = dec!(0.01);
    let max_price = dec!(0.99);

    for (leg, team, side, price) in &legs {
        let clamped = (*price).max(min_price).min(max_price);
        let order = FakOrder {
            team: *team,
            side: *side,
            price: clamped,
            size,
        };

        let tag = format!("maker-{leg}");
        let result = if maker_cfg.use_gtd {
            orders::post_gtd_order(&config, &auth, &order, maker_cfg.gtd_expiry_secs, &tag).await
        } else {
            orders::post_limit_order(&config, &auth, &order, &tag).await
        };

        match result {
            Ok(resp) => {
                if let Some(oid) = resp.order_id {
                    maker_state.live_orders.insert(*leg, oid);
                }
            }
            Err(e) => {
                tracing::warn!(leg = %leg, error = %e, "[MAKER] failed to place order");
            }
        }
    }
}

async fn cancel_maker_order(state: &Arc<AppState>, order_id: &str) {
    let config = state.config.read().unwrap().clone();
    let auth = state.auth.read().unwrap().clone();
    if let Some(auth) = auth.as_ref() {
        if let Err(e) = orders::cancel_order(&config, auth, order_id).await {
            tracing::warn!(order_id, error = %e, "[MAKER] cancel failed");
        }
    }
}

async fn cancel_all_maker_orders(state: &Arc<AppState>, maker_state: &mut MakerState) {
    let ids: Vec<String> = maker_state.live_orders.values().cloned().collect();
    if ids.is_empty() {
        return;
    }

    let maker_cfg = state.maker_config.read().unwrap().clone();
    if maker_cfg.dry_run {
        tracing::info!("[MAKER] DRY_RUN — would cancel {} orders", ids.len());
        maker_state.live_orders.clear();
        return;
    }

    let auth = state.auth.read().unwrap().clone();
    if let Some(auth) = auth.as_ref() {
        if let Err(e) = orders::cancel_orders_batch(auth, &ids).await {
            tracing::warn!(error = %e, "[MAKER] batch cancel on shutdown failed");
        }
    }
    maker_state.live_orders.clear();
}
