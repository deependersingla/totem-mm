//! Pre-signed order cache — maintains ready-to-fire FAK orders for instant execution.
//!
//! A background task watches the orderbook and continuously pre-signs FAK orders
//! for both teams × {BUY, SELL}. On signal, the strategy grabs the cached signed
//! order and POSTs it immediately — zero signing latency on the hot path.

use std::sync::Arc;

use rust_decimal::Decimal;
use tokio::sync::{watch, RwLock};

use crate::clob_auth::ClobAuth;
use crate::config::Config;
use crate::orders::{self, ClobOrder};
use crate::state::AppState;
use crate::strategy::{build_buy_order, build_sell_order};
use crate::types::{FakOrder, OrderBook, Side, Team};

/// A cached pre-signed order ready to POST.
#[derive(Debug, Clone)]
pub struct CachedOrder {
    pub fak: FakOrder,
    pub signed: ClobOrder,
    /// Book price at the time of signing — stale if book moved.
    pub book_price: Decimal,
}

/// Thread-safe cache of pre-signed orders keyed by (Team, Side).
/// Each slot holds the latest pre-signed order for that direction.
pub struct OrderCache {
    // 4 slots: TeamA×Buy, TeamA×Sell, TeamB×Buy, TeamB×Sell
    slots: [RwLock<Option<CachedOrder>>; 4],
}

impl OrderCache {
    pub fn new() -> Self {
        Self {
            slots: [
                RwLock::new(None),
                RwLock::new(None),
                RwLock::new(None),
                RwLock::new(None),
            ],
        }
    }

    fn index(team: Team, side: Side) -> usize {
        match (team, side) {
            (Team::TeamA, Side::Buy) => 0,
            (Team::TeamA, Side::Sell) => 1,
            (Team::TeamB, Side::Buy) => 2,
            (Team::TeamB, Side::Sell) => 3,
        }
    }

    pub async fn get(&self, team: Team, side: Side) -> Option<CachedOrder> {
        self.slots[Self::index(team, side)].read().await.clone()
    }

    async fn set(&self, team: Team, side: Side, order: Option<CachedOrder>) {
        *self.slots[Self::index(team, side)].write().await = order;
    }

    /// Take the cached order out (returns it and clears the slot).
    /// Used by the hot path to ensure each pre-signed order is only used once.
    pub async fn take(&self, team: Team, side: Side) -> Option<CachedOrder> {
        self.slots[Self::index(team, side)].write().await.take()
    }
}

/// Background task: watches book updates and pre-signs FAK orders.
///
/// Runs until the cancellation token fires. On every book change, re-signs
/// orders for all 4 directions (TeamA×Buy, TeamA×Sell, TeamB×Buy, TeamB×Sell).
pub async fn run(
    app: Arc<AppState>,
    cache: Arc<OrderCache>,
    mut book_rx: watch::Receiver<(OrderBook, OrderBook)>,
    cancel: tokio_util::sync::CancellationToken,
) {
    tracing::info!("[ORDER-CACHE] background pre-signer started");

    loop {
        tokio::select! {
            _ = cancel.cancelled() => {
                tracing::info!("[ORDER-CACHE] cancelled, stopping");
                return;
            }
            result = book_rx.changed() => {
                if result.is_err() {
                    tracing::info!("[ORDER-CACHE] book channel closed, stopping");
                    return;
                }

                let books = book_rx.borrow_and_update().clone();
                let config = app.config.read().unwrap().clone();
                let auth = match app.auth.read().unwrap().clone() {
                    Some(a) => a,
                    None => continue,
                };

                // Pre-sign all 4 directions
                for &team in &[Team::TeamA, Team::TeamB] {
                    let (team_book, _opp_book) = match team {
                        Team::TeamA => (&books.0, &books.1),
                        Team::TeamB => (&books.1, &books.0),
                    };

                    // Pre-sign SELL (hitting best bid)
                    let held = app.position.lock().unwrap().token_balance(team);
                    let sell_cached = build_and_sign_sell(&config, &auth, team, team_book, held);
                    cache.set(team, Side::Sell, sell_cached).await;

                    // Pre-sign BUY (lifting best ask)
                    let buy_cached = build_and_sign_buy(&config, &auth, team, team_book);
                    cache.set(team, Side::Buy, buy_cached).await;
                }
            }
        }
    }
}

fn build_and_sign_sell(
    config: &Config,
    auth: &ClobAuth,
    team: Team,
    book: &OrderBook,
    held: Decimal,
) -> Option<CachedOrder> {
    let fak = build_sell_order(config, team, book, Some(held))?;
    let signed = orders::build_signed_order(config, auth, &fak).ok()?;
    Some(CachedOrder {
        book_price: fak.price,
        fak,
        signed,
    })
}

fn build_and_sign_buy(
    config: &Config,
    auth: &ClobAuth,
    team: Team,
    book: &OrderBook,
) -> Option<CachedOrder> {
    let fak = build_buy_order(config, team, book)?;
    let signed = orders::build_signed_order(config, auth, &fak).ok()?;
    Some(CachedOrder {
        book_price: fak.price,
        fak,
        signed,
    })
}
