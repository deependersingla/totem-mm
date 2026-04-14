//! Sweep binary's own application state — fully independent from the taker's AppState.
//!
//! Own wallet, own position tracking, own order list, own book WS.

use std::collections::VecDeque;
use std::sync::{Arc, Mutex, RwLock};

use rust_decimal::Decimal;
use serde::Serialize;
use tokio::sync::watch;
use tokio_util::sync::CancellationToken;

use crate::clob_auth::ClobAuth;
use crate::sweep_config::SweepAppConfig;
use crate::types::{OrderBook, Side, Team};

/// Simple latency tracker for sweep operations.
#[derive(Debug, Clone, Serialize)]
pub struct SweepLatency {
    pub last_order_ms: Option<u64>,
    pub avg_order_ms: Option<u64>,
    pub min_order_ms: Option<u64>,
    pub max_order_ms: Option<u64>,
    pub sample_count: u64,
}

impl Default for SweepLatency {
    fn default() -> Self {
        Self { last_order_ms: None, avg_order_ms: None, min_order_ms: None, max_order_ms: None, sample_count: 0 }
    }
}

impl SweepLatency {
    pub fn record(&mut self, ms: u64) {
        self.last_order_ms = Some(ms);
        self.sample_count += 1;
        self.min_order_ms = Some(self.min_order_ms.map_or(ms, |v| v.min(ms)));
        self.max_order_ms = Some(self.max_order_ms.map_or(ms, |v| v.max(ms)));
        // Running average
        let prev_avg = self.avg_order_ms.unwrap_or(0) as f64;
        let new_avg = prev_avg + (ms as f64 - prev_avg) / self.sample_count as f64;
        self.avg_order_ms = Some(new_avg as u64);
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SweepPhase {
    Idle,
    Active,
}

#[derive(Debug, Clone, Serialize)]
pub struct SweepOrder {
    pub order_id: String,
    pub team: Team,
    pub side: Side,
    pub price: Decimal,
    pub size: Decimal,
}

#[derive(Debug, Clone, Serialize)]
pub struct EventEntry {
    pub ts: String,
    pub kind: String,
    pub detail: String,
}

/// Lightweight position tracker for sweep.
#[derive(Debug, Clone, Serialize)]
pub struct SweepPosition {
    pub team_a_tokens: Decimal,
    pub team_b_tokens: Decimal,
    pub usdc_spent: Decimal,
}

impl SweepPosition {
    pub fn token_balance(&self, team: Team) -> Decimal {
        match team {
            Team::TeamA => self.team_a_tokens,
            Team::TeamB => self.team_b_tokens,
        }
    }
}

const MAX_EVENTS: usize = 200;

pub struct SweepAppState {
    pub config: RwLock<SweepAppConfig>,
    pub auth: RwLock<Option<ClobAuth>>,
    pub position: Mutex<SweepPosition>,
    pub phase: RwLock<SweepPhase>,
    pub events: Mutex<VecDeque<EventEntry>>,
    pub sweep_orders: Mutex<Vec<SweepOrder>>,
    pub sweep_cancel: RwLock<Option<CancellationToken>>,
    pub book_rx: RwLock<Option<watch::Receiver<(OrderBook, OrderBook)>>>,
    pub book_tx: RwLock<Option<watch::Sender<(OrderBook, OrderBook)>>>,
    pub ws_cancel: RwLock<Option<CancellationToken>>,
    /// Winning team selected by user (set when sweep starts).
    pub winning_team: RwLock<Option<Team>>,
    /// Order placement latency tracking.
    pub latency: Mutex<SweepLatency>,
    /// Heartbeat cancel token (separate from sweep cancel).
    pub heartbeat_cancel: RwLock<Option<CancellationToken>>,
}

impl SweepAppState {
    pub fn new(config: SweepAppConfig) -> Arc<Self> {
        Arc::new(Self {
            config: RwLock::new(config),
            auth: RwLock::new(None),
            position: Mutex::new(SweepPosition {
                team_a_tokens: Decimal::ZERO,
                team_b_tokens: Decimal::ZERO,
                usdc_spent: Decimal::ZERO,
            }),
            phase: RwLock::new(SweepPhase::Idle),
            events: Mutex::new(VecDeque::with_capacity(MAX_EVENTS)),
            sweep_orders: Mutex::new(Vec::new()),
            sweep_cancel: RwLock::new(None),
            book_rx: RwLock::new(None),
            book_tx: RwLock::new(None),
            ws_cancel: RwLock::new(None),
            winning_team: RwLock::new(None),
            latency: Mutex::new(SweepLatency::default()),
            heartbeat_cancel: RwLock::new(None),
        })
    }

    pub fn push_event(&self, kind: &str, detail: &str) {
        let entry = EventEntry {
            ts: crate::state::ist_now(),
            kind: kind.to_string(),
            detail: detail.to_string(),
        };
        let mut events = self.events.lock().unwrap();
        if events.len() >= MAX_EVENTS {
            events.pop_front();
        }
        events.push_back(entry);
    }

    pub fn shared_config(&self) -> crate::config::Config {
        self.config.read().unwrap().to_shared_config()
    }
}
