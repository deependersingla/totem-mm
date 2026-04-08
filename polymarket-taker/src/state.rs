use std::collections::VecDeque;
use std::sync::{Arc, Mutex, RwLock};
use std::time::Instant;

use rust_decimal::Decimal;
use serde::Serialize;
use tokio::sync::{broadcast, mpsc, watch};
pub use tokio_util::sync::CancellationToken;

use crate::capture::OracleEvent;
use crate::clob_auth::ClobAuth;
use crate::config::{Config, MakerConfig};
use crate::db::Db;
use crate::latency::LatencyTracker;
use crate::order_cache::OrderCache;
use crate::position::{self, Position};
use crate::types::{CricketSignal, FillEvent, MatchState, OrderBook, Side, Team};

/// Sweep (endgame) mode state.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SweepPhase {
    Idle,
    Active,
}

/// A resting order placed by the sweep engine.
#[derive(Debug, Clone, Serialize)]
pub struct SweepOrder {
    pub order_id: String,
    pub team: Team,
    pub side: Side,
    pub price: Decimal,
    pub size: Decimal,
}

/// Configuration for the sweep engine.
#[derive(Debug, Clone, Serialize)]
pub struct SweepConfig {
    pub winning_team: Team,
    pub budget_usdc: Decimal,
    pub dry_run: bool,
    /// Number of price levels for the resting order grid.
    pub grid_levels: usize,
    /// Refresh interval for the resting order grid (seconds).
    pub refresh_interval_secs: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum MatchPhase {
    Idle,
    InningsRunning,
    InningsPaused,
    MatchOver,
}

#[derive(Debug, Clone, Serialize)]
pub struct EventEntry {
    pub ts: String,
    pub kind: String,
    pub detail: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct InventorySnapshot {
    pub ts: String,
    pub team_a: Decimal,
    pub team_b: Decimal,
}

/// A single completed trade record for the trade log.
#[derive(Debug, Clone, Serialize)]
pub struct TradeRecord {
    pub ts: String,
    pub side: String,       // "BUY" or "SELL"
    pub team: String,       // team display name
    pub size: Decimal,      // tokens filled
    pub price: Decimal,     // fill price per token
    pub cost: Decimal,      // size * price (USDC)
    pub order_type: String, // "FAK" or "GTC"
    pub label: String,      // "WICKET", "RUN6", "REVERT_BUY", etc.
    pub order_id: String,
}

/// A GTC revert order that has been placed and is waiting to fill.
/// Tracked so we can cancel stale reverts on opposite events and implement break-even exit.
#[derive(Debug, Clone)]
pub struct PendingRevert {
    pub order_id: String,
    pub team: Team,
    pub side: Side,
    pub size: Decimal,
    pub entry_price: Decimal,
    pub revert_limit_price: Decimal,
    pub placed_at: Instant,
    pub label: String,
}

impl PendingRevert {
    /// For serialization in /api/status — Instant is not serializable.
    pub fn age_secs(&self) -> f64 {
        self.placed_at.elapsed().as_secs_f64()
    }
}

pub struct AppState {
    pub config: RwLock<Config>,
    pub auth: RwLock<Option<ClobAuth>>,
    pub position: Position,
    pub phase: RwLock<MatchPhase>,
    pub match_state: RwLock<MatchState>,
    pub signal_tx: RwLock<Option<broadcast::Sender<CricketSignal>>>,
    pub book_rx: RwLock<Option<watch::Receiver<(OrderBook, OrderBook)>>>,
    pub book_tx: RwLock<Option<watch::Sender<(OrderBook, OrderBook)>>>,
    pub events: Mutex<VecDeque<EventEntry>>,
    pub inventory_history: Mutex<Vec<InventorySnapshot>>,
    pub live_order_ids: Mutex<Vec<String>>,
    pub trade_log: Mutex<Vec<TradeRecord>>,
    pub ws_cancel: RwLock<Option<CancellationToken>>,
    /// Which teams are enabled for trading. Default: both true.
    pub trade_team_a: RwLock<bool>,
    pub trade_team_b: RwLock<bool>,
    pub latency: LatencyTracker,
    pub fill_tx: RwLock<Option<mpsc::Sender<FillEvent>>>,
    pub user_ws_cancel: RwLock<Option<CancellationToken>>,
    pub maker_config: RwLock<MakerConfig>,
    // ── Revert tracking ────────────────────────────────────────────────────
    pub pending_reverts: Mutex<Vec<PendingRevert>>,
    // ── Pre-signed order cache ────────────────────────────────────────────
    pub order_cache: Arc<OrderCache>,
    /// Cached tick size, updated by background task.
    pub cached_tick_size: RwLock<Option<Decimal>>,
    /// Buffer of fill events received from user WS, keyed by order_id.
    /// Strategy checks this before REST polling for faster fill detection.
    fill_event_buffer: Mutex<Vec<FillEvent>>,
    // ── Sweep (endgame) state ──────────────────────────────────────────────
    pub sweep_phase: RwLock<SweepPhase>,
    pub sweep_config: RwLock<Option<SweepConfig>>,
    pub sweep_orders: Mutex<Vec<SweepOrder>>,
    pub sweep_cancel: RwLock<Option<CancellationToken>>,
    // ── Database ──────────────────────────────────────────────────────────
    pub db: RwLock<Option<Arc<Db>>>,
    // ── Capture (background, non-blocking) ─────────────────────────────
    pub oracle_tx: RwLock<Option<mpsc::Sender<OracleEvent>>>,
}

const MAX_EVENTS: usize = 200;

impl AppState {
    pub fn new(config: Config) -> Arc<Self> {
        let budget = config.total_budget_usdc;
        let first_batting = config.first_batting;
        let maker_cfg = config.maker_config.clone();
        Arc::new(Self {
            config: RwLock::new(config),
            auth: RwLock::new(None),
            position: position::new_position(budget),
            phase: RwLock::new(MatchPhase::Idle),
            match_state: RwLock::new(MatchState::new(first_batting)),
            signal_tx: RwLock::new(None),
            book_rx: RwLock::new(None),
            book_tx: RwLock::new(None),
            events: Mutex::new(VecDeque::with_capacity(MAX_EVENTS)),
            inventory_history: Mutex::new(Vec::new()),
            live_order_ids: Mutex::new(Vec::new()),
            trade_log: Mutex::new(Vec::new()),
            ws_cancel: RwLock::new(None),
            trade_team_a: RwLock::new(true),
            trade_team_b: RwLock::new(true),
            latency: LatencyTracker::new(),
            fill_tx: RwLock::new(None),
            user_ws_cancel: RwLock::new(None),
            maker_config: RwLock::new(maker_cfg),
            pending_reverts: Mutex::new(Vec::new()),
            order_cache: Arc::new(OrderCache::new()),
            cached_tick_size: RwLock::new(None),
            fill_event_buffer: Mutex::new(Vec::new()),
            sweep_phase: RwLock::new(SweepPhase::Idle),
            sweep_config: RwLock::new(None),
            sweep_orders: Mutex::new(Vec::new()),
            sweep_cancel: RwLock::new(None),
            db: RwLock::new(None),
            oracle_tx: RwLock::new(None),
        })
    }

    pub fn push_event(&self, kind: &str, detail: &str) {
        let entry = EventEntry {
            ts: chrono::Utc::now().format("%H:%M:%S").to_string(),
            kind: kind.to_string(),
            detail: detail.to_string(),
        };
        let mut events = self.events.lock().unwrap();
        if events.len() >= MAX_EVENTS {
            events.pop_front();
        }
        events.push_back(entry);
    }

    pub fn snapshot_inventory(&self) {
        let pos = self.position.lock().unwrap();
        self.inventory_history.lock().unwrap().push(InventorySnapshot {
            ts: chrono::Utc::now().format("%H:%M:%S").to_string(),
            team_a: pos.team_a_tokens,
            team_b: pos.team_b_tokens,
        });
    }

    pub fn track_order(&self, order_id: String) {
        self.live_order_ids.lock().unwrap().push(order_id);
    }

    pub fn clear_orders(&self) {
        self.live_order_ids.lock().unwrap().clear();
    }

    pub fn is_match_running(&self) -> bool {
        let phase = *self.phase.read().unwrap();
        phase == MatchPhase::InningsRunning
    }

    pub fn is_idle(&self) -> bool {
        let phase = *self.phase.read().unwrap();
        phase == MatchPhase::Idle || phase == MatchPhase::MatchOver
    }

    pub fn log_trade(&self, record: TradeRecord) {
        // Only persist real trades to SQLite (not dry_run)
        let is_dry = record.order_id.starts_with("dry_run");
        if !is_dry {
            if let Some(ref db) = *self.db.read().unwrap() {
                let slug = self.config.read().unwrap().market_slug.clone();
                db.insert_trade(
                    &record.ts, &record.side, &record.team,
                    &record.size.to_string(), &record.price.to_string(),
                    &record.cost.to_string(), &record.order_type, &record.label,
                    &record.order_id, &slug,
                );
            }
        }
        self.trade_log.lock().unwrap().push(record);
    }

    pub fn is_team_enabled(&self, team: Team) -> bool {
        match team {
            Team::TeamA => *self.trade_team_a.read().unwrap(),
            Team::TeamB => *self.trade_team_b.read().unwrap(),
        }
    }

    // ── Fill event buffer (from user WS) ────────────────────────────────────

    /// Buffer a fill event from the user WebSocket.
    /// For the same order_id, keeps only the latest (largest) fill to prevent
    /// double-counting partial fills on GTC orders.
    /// Capped at 500 entries to prevent unbounded growth.
    pub fn buffer_fill_event(&self, event: FillEvent) {
        let mut buf = self.fill_event_buffer.lock().unwrap();
        // Deduplicate: if same order_id exists, keep the one with larger filled_size
        if let Some(existing) = buf.iter_mut().find(|e| e.order_id == event.order_id) {
            if event.filled_size >= existing.filled_size {
                *existing = event;
            }
            return;
        }
        if buf.len() >= 500 {
            buf.drain(..250);
        }
        buf.push(event);
    }

    /// Take a fill event for a specific order_id from the buffer.
    /// Returns the first matching event and removes it.
    pub fn take_fill_event(&self, order_id: &str) -> Option<FillEvent> {
        let mut buf = self.fill_event_buffer.lock().unwrap();
        if let Some(idx) = buf.iter().position(|e| e.order_id == order_id) {
            Some(buf.swap_remove(idx))
        } else {
            None
        }
    }

    // ── Revert tracking helpers ─────────────────────────────────────────────

    /// Record a newly placed revert GTC order.
    pub fn push_revert(&self, revert: PendingRevert) {
        self.pending_reverts.lock().unwrap().push(revert);
    }

    /// Remove a revert by order_id (e.g. when it fills or is cancelled).
    /// Returns the removed entry if found.
    pub fn remove_revert(&self, order_id: &str) -> Option<PendingRevert> {
        let mut reverts = self.pending_reverts.lock().unwrap();
        if let Some(idx) = reverts.iter().position(|r| r.order_id == order_id) {
            Some(reverts.swap_remove(idx))
        } else {
            None
        }
    }

    /// Remove and return all pending reverts for a given team.
    /// Used when an opposite event arrives and we need to cancel stale reverts.
    pub fn take_reverts_for_team(&self, team: Team) -> Vec<PendingRevert> {
        let mut reverts = self.pending_reverts.lock().unwrap();
        let mut taken = Vec::new();
        let mut i = 0;
        while i < reverts.len() {
            if reverts[i].team == team {
                taken.push(reverts.swap_remove(i));
                // Don't increment i — swap_remove moved last element here
            } else {
                i += 1;
            }
        }
        taken
    }

    /// Count of pending reverts (for /api/status).
    pub fn pending_revert_count(&self) -> usize {
        self.pending_reverts.lock().unwrap().len()
    }

    /// Fire-and-forget capture of an oracle event. Never blocks.
    pub fn capture_signal(&self, signal: &str, source: &str) {
        if let Some(tx) = self.oracle_tx.read().unwrap().as_ref() {
            let ms = self.match_state.read().unwrap();
            let config = self.config.read().unwrap();
            let _ = tx.try_send(OracleEvent {
                signal: signal.to_string(),
                source: source.to_string(),
                innings: ms.innings,
                batting: config.team_name(ms.batting).to_string(),
                bowling: config.team_name(ms.bowling()).to_string(),
            });
        }
    }

    pub fn reset_for_new_match(&self) {
        let config = self.config.read().unwrap();
        *self.phase.write().unwrap() = MatchPhase::Idle;
        *self.match_state.write().unwrap() = MatchState::new(config.first_batting);
        let mut pos = self.position.lock().unwrap();
        pos.team_a_tokens = Decimal::ZERO;
        pos.team_b_tokens = Decimal::ZERO;
        pos.total_spent = Decimal::ZERO;
        pos.trade_count = 0;
        pos.total_budget = config.total_budget_usdc;
        self.clear_orders();
        self.pending_reverts.lock().unwrap().clear();
        self.events.lock().unwrap().clear();
        self.inventory_history.lock().unwrap().clear();
        self.trade_log.lock().unwrap().clear();
    }
}
