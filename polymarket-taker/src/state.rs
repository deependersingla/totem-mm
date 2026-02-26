use std::collections::VecDeque;
use std::sync::{Arc, Mutex, RwLock};

use rust_decimal::Decimal;
use serde::Serialize;
use tokio::sync::{mpsc, watch};
use tokio_util::sync::CancellationToken;

use crate::clob_auth::ClobAuth;
use crate::config::Config;
use crate::position::{self, Position};
use crate::types::{CricketSignal, MatchState, OrderBook};

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

pub struct AppState {
    pub config: RwLock<Config>,
    pub auth: RwLock<Option<ClobAuth>>,
    pub position: Position,
    pub phase: RwLock<MatchPhase>,
    pub match_state: RwLock<MatchState>,
    pub signal_tx: RwLock<Option<mpsc::Sender<CricketSignal>>>,
    pub book_rx: RwLock<Option<watch::Receiver<(OrderBook, OrderBook)>>>,
    pub book_tx: RwLock<Option<watch::Sender<(OrderBook, OrderBook)>>>,
    pub events: Mutex<VecDeque<EventEntry>>,
    pub inventory_history: Mutex<Vec<InventorySnapshot>>,
    pub live_order_ids: Mutex<Vec<String>>,
    pub ws_cancel: RwLock<Option<CancellationToken>>,
}

const MAX_EVENTS: usize = 200;

impl AppState {
    pub fn new(config: Config) -> Arc<Self> {
        let budget = config.total_budget_usdc;
        let first_batting = config.first_batting;
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
            ws_cancel: RwLock::new(None),
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
        self.events.lock().unwrap().clear();
        self.inventory_history.lock().unwrap().clear();
    }
}
