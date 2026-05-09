use std::collections::VecDeque;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, Instant};

use rust_decimal::Decimal;
use serde::Serialize;
use tokio::sync::{broadcast, mpsc, watch};
pub use tokio_util::sync::CancellationToken;

use chrono::FixedOffset;

use crate::capture::OracleEvent;

/// IST timestamp for display (UTC+5:30).
pub fn ist_now() -> String {
    let ist = FixedOffset::east_opt(5 * 3600 + 30 * 60).unwrap();
    chrono::Utc::now().with_timezone(&ist).format("%H:%M:%S").to_string()
}

/// `tracing_subscriber` timer that prints the same `HH:MM:SS IST` string used
/// by [`ist_now`]. Wired into both binaries so every `tracing::*` line is
/// timestamped IST without per-call instrumentation. (B1 in TODO.md.)
pub struct IstTimer;

impl tracing_subscriber::fmt::time::FormatTime for IstTimer {
    fn format_time(
        &self,
        w: &mut tracing_subscriber::fmt::format::Writer<'_>,
    ) -> std::fmt::Result {
        write!(w, "{} IST", ist_now())
    }
}
use crate::clob_auth::ClobAuth;
use crate::config::{Config, MakerConfig};
use crate::db::Db;
use crate::dls::DlsEngine;
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

// ── Per-signal 4-leg status group ────────────────────────────────────────────
//
// One `SignalGroup` per trade-triggering signal (Wicket / boundary 4 / 6 /
// AUGMENT). Holds the lifecycle state of all 4 orders (2 FAK takers, 2 GTC
// makers). Skipped signals (WAIT, BOOK_STALE, OutOfRange, NoLiquidity) get a
// row too so the UI can render the rejection reason. ~25 groups per match,
// capped at 50 in memory.

/// Which of the four legs this status entry refers to.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum LegRole {
    /// SELL FAK on the team that the signal disfavours (batting on Wicket,
    /// bowling on boundary).
    FakSell,
    /// BUY FAK on the team that the signal favours.
    FakBuy,
    /// GTC BUY revert paired with `FakSell` (closes the SELL leg's exposure).
    RevertBuy,
    /// GTC SELL revert paired with `FakBuy` (closes the BUY leg's exposure).
    RevertSell,
}

/// Lifecycle state of one leg.
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum LegState {
    /// The gate skipped this leg before posting (e.g. no held tokens for SELL,
    /// budget exhausted for BUY, or — for the revert legs — there is no FAK
    /// fill yet to close.
    NotPlanned { reason: String },
    /// Resolved into an order but not yet POSTed.
    Pending,
    /// Accepted by the CLOB; awaiting fill.
    Posted { order_id: String, price: Decimal, size: Decimal },
    /// Fully filled.
    Filled { order_id: String, size: Decimal, avg_price: Decimal, fee: Decimal },
    /// Partial fill on a FAK; remainder killed.
    Partial { order_id: String, filled: Decimal, requested: Decimal, avg_price: Decimal, fee: Decimal },
    /// Terminal without fill (FAK killed by matching engine).
    Killed { order_id: String },
    /// GTC revert cancelled externally (AUGMENT, match reset, terminal poll).
    Cancelled { order_id: String, reason: String },
    /// Revert was cancelled and replaced by a new revert under AUGMENT.
    Augmented { from: String, to: String, new_price: Decimal, new_size: Decimal },
    /// Order rejected at submit (post-only failure, sign error, balance error).
    Rejected { reason: String },
    /// Revert timed out (`revert_timeout_ms`) without filling as maker, and
    /// we flattened via a taker FAK at L+1 of the opposite side. The taker
    /// fee shows here; the original maker GTC was cancelled before this
    /// FAK posted. Terminal.
    TakerExit {
        order_id: String,
        size: Decimal,
        avg_price: Decimal,
        fee: Decimal,
    },
}

impl LegState {
    /// Convenience: extract any `fee` field from the variants that carry one.
    pub fn fee(&self) -> Decimal {
        match self {
            LegState::Filled { fee, .. }
            | LegState::Partial { fee, .. }
            | LegState::TakerExit { fee, .. } => *fee,
            _ => Decimal::ZERO,
        }
    }
}

/// One row in `SignalGroup::legs`.
#[derive(Debug, Clone, Serialize)]
pub struct LegStatus {
    pub role: LegRole,
    pub state: LegState,
}

/// Outcome at the group level — drives the badge colour in the UI.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum GroupOutcome {
    /// Still in flight — at least one leg is non-terminal.
    Open,
    /// New signal opposed pending reverts → no FAK fired, reverts left alone.
    Wait,
    /// Book stale at gate; trade refused.
    BookStale,
    /// Best bid/ask outside the safe range.
    OutOfRange,
    /// No book liquidity on either side.
    NoLiquidity,
    /// AUGMENT path — reverts cancelled and reposted; no new FAK.
    Augmented,
    /// Generic skip reason; held by the gate path when none of the above match.
    Skipped,
    /// Both reverts terminal (filled or cancelled). PnL frozen.
    Closed,
}

/// One trade-triggering signal's full lifecycle.
#[derive(Debug, Clone, Serialize)]
pub struct SignalGroup {
    pub correlation_id: String,
    pub event_seq: u64,
    pub signal_tag: String,
    pub label: String,
    pub ts_ist: String,
    pub batting: String,
    pub bowling: String,
    pub legs: Vec<LegStatus>,
    /// Sum of every fee carried by `legs[*].state` (rolled up by `update_leg`).
    pub total_fee_paid: Decimal,
    /// `Some` only when both reverts have terminal-Filled (round-trip closed)
    /// or when the group ended without trading (Wait / Stale / Skipped).
    pub net_pnl: Option<Decimal>,
    pub outcome: GroupOutcome,
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
    /// V2 platform fee for this fill, in USDC. `Decimal::ZERO` for makers
    /// (GTC reverts under `fd.to=true`) and for placement-time stub rows.
    pub fee: Decimal,
}

/// Envelope around a [`CricketSignal`] sent through the broadcast channel.
///
/// Allocated by the HTTP handler at receive time so the strategy and maker
/// see the same `event_seq` and `correlation_id` for one ball, and so the
/// `oracle_events` ledger row can be linked to every downstream order.
///
/// `correlation_id = "{event_seq}-{short_tag}"`, e.g. `42-W`, `43-R6`. Use
/// [`AppState::make_dispatch`] rather than constructing this directly so the
/// allocation stays in one place.
#[derive(Debug, Clone)]
pub struct DispatchedSignal {
    pub signal: CricketSignal,
    pub event_seq: u64,
    pub correlation_id: String,
    pub ts_ist: String,
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
    /// Monotonic event sequence id assigned when the originating signal was received.
    /// Enables grouping FAK + revert + augment orders under one "event" in the ledger.
    pub event_seq: u64,
    /// Short tag of the originating signal (e.g., "W", "R4", "Wd6"). Matches
    /// `CricketSignal::short_tag()` output.
    pub signal_tag: String,
    /// Stable id used to join all rows for one ball: `"{event_seq}-{signal_tag}"`.
    /// Set on the initial revert from the dispatch envelope; on AUGMENT the new
    /// revert order INHERITS this value from the cancelled `stale_rev` so a
    /// `SELECT * FROM clob_orders WHERE correlation_id = ?` returns the FAK
    /// pair, the initial revert, and every augment in one row set.
    pub correlation_id: String,
    /// V2 platform fee paid on the entry FAK fill, USDC. Used by the round-trip
    /// writer (`log_revert_fill_with_round_trip`) to compute `net_pnl` without
    /// re-reading from the ledger.
    pub entry_fee: Decimal,
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
    /// Live DLS (Duckworth-Lewis-Stern) engine for T20 par-score tracking.
    /// Updated on every cricket signal in the strategy loop.
    pub dls: RwLock<DlsEngine>,
    pub signal_tx: RwLock<Option<broadcast::Sender<DispatchedSignal>>>,
    pub book_rx: RwLock<Option<watch::Receiver<(OrderBook, OrderBook)>>>,
    pub book_tx: RwLock<Option<watch::Sender<(OrderBook, OrderBook)>>>,
    /// Market-WS health signal — replaces the old `book.timestamp_ms` based
    /// staleness check. Strategy reads this to decide whether the local book
    /// is reliable enough to trade against. See `crate::ws_health`.
    pub ws_health_rx: RwLock<Option<watch::Receiver<crate::ws_health::WsHealth>>>,
    pub ws_health_tx: RwLock<Option<watch::Sender<crate::ws_health::WsHealth>>>,
    /// Rolling buffer of best-bid/ask snapshots over the last ~2× `move_lookback`.
    /// Updated by `market_ws` on every book event; consulted by the strategy
    /// to decide directional vs reverse entry. See `crate::price_history`.
    pub price_history: Arc<crate::price_history::PriceHistory>,
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
    // ── Per-signal 4-leg group ledger ─────────────────────────────────────
    /// Newest-first deque of `SignalGroup`s, capped at `MAX_SIGNAL_GROUPS`.
    pub signal_groups: Mutex<VecDeque<SignalGroup>>,
    // ── Sweep (endgame) state ──────────────────────────────────────────────
    pub sweep_phase: RwLock<SweepPhase>,
    pub sweep_config: RwLock<Option<SweepConfig>>,
    pub sweep_orders: Mutex<Vec<SweepOrder>>,
    pub sweep_cancel: RwLock<Option<CancellationToken>>,
    // ── Database ──────────────────────────────────────────────────────────
    pub db: RwLock<Option<Arc<Db>>>,
    // ── Capture (background, non-blocking) ─────────────────────────────
    pub oracle_tx: RwLock<Option<mpsc::Sender<OracleEvent>>>,
    // ── Event sequencing ───────────────────────────────────────────────
    /// Monotonic counter incremented at the top of the signal loop. Every trade-triggering
    /// signal gets a fresh id, which is threaded through FAK + revert orders into the
    /// PendingRevert ledger so related orders can be grouped by `event_seq`.
    event_seq: AtomicU64,
    // ── Dispatch gap (A1) ──────────────────────────────────────────────
    /// Instant of the most recent signal forwarded to the strategy broadcast.
    /// Read+written atomically inside `check_and_update_dispatch_gap` —
    /// `Mutex` rather than `RwLock` because every check also writes when it
    /// passes the gap. Reset on `reset_for_new_match`.
    last_dispatch_at: Mutex<Option<Instant>>,
}

const MAX_EVENTS: usize = 200;
const MAX_SIGNAL_GROUPS: usize = 50;

impl AppState {
    pub fn new(config: Config) -> Arc<Self> {
        let budget = config.total_budget_usdc;
        let first_batting = config.first_batting;
        let maker_cfg = config.maker_config.clone();
        let lookback = Duration::from_millis(config.move_lookback_ms);
        Arc::new(Self {
            config: RwLock::new(config),
            auth: RwLock::new(None),
            position: position::new_position(budget),
            phase: RwLock::new(MatchPhase::Idle),
            match_state: RwLock::new(MatchState::new(first_batting)),
            dls: RwLock::new(DlsEngine::new_t20()),
            signal_tx: RwLock::new(None),
            book_rx: RwLock::new(None),
            book_tx: RwLock::new(None),
            ws_health_rx: RwLock::new(None),
            ws_health_tx: RwLock::new(None),
            price_history: Arc::new(crate::price_history::PriceHistory::new(lookback)),
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
            signal_groups: Mutex::new(VecDeque::with_capacity(MAX_SIGNAL_GROUPS)),
            order_cache: Arc::new(OrderCache::new()),
            cached_tick_size: RwLock::new(None),
            fill_event_buffer: Mutex::new(Vec::new()),
            sweep_phase: RwLock::new(SweepPhase::Idle),
            sweep_config: RwLock::new(None),
            sweep_orders: Mutex::new(Vec::new()),
            sweep_cancel: RwLock::new(None),
            db: RwLock::new(None),
            oracle_tx: RwLock::new(None),
            event_seq: AtomicU64::new(0),
            last_dispatch_at: Mutex::new(None),
        })
    }

    /// Allocate the next event sequence id. Called once per signal received
    /// by the HTTP handlers (see [`AppState::make_dispatch`]).
    ///
    /// Post-B4 the counter advances on every signal — including non-trade
    /// ones (dot balls, 1-3 runs, IO, MO) — because the handler captures
    /// every receive into the ledger. The strategy still only spawns orders
    /// for trade-triggering signals; the embedded `event_seq` of a
    /// non-triggering signal sits on its ledger row and nothing else.
    pub fn next_event_seq(&self) -> u64 {
        self.event_seq.fetch_add(1, Ordering::Relaxed) + 1
    }

    /// Current value of the event sequence counter (for /api/status).
    pub fn current_event_seq(&self) -> u64 {
        self.event_seq.load(Ordering::Relaxed)
    }

    /// Resolve the live tick size for the current market.
    ///
    /// D8 (TODO.md): the previous inline `unwrap_or_else(... unwrap_or(dec!(0.01)))`
    /// silently fell back to `0.01` whenever both the cached value AND the
    /// configured string failed to parse — masking misconfiguration. This
    /// helper logs a `warn!` (with the offending input) every time the
    /// last-resort `0.01` is used, and a `debug!` for an in-band cache miss.
    pub fn resolve_tick_size(&self, config_tick_size: &str) -> Decimal {
        if let Some(t) = *self.cached_tick_size.read().unwrap() {
            return t;
        }
        match config_tick_size.parse::<Decimal>() {
            Ok(t) => {
                tracing::debug!(tick = %t, "tick_size cache miss; parsed from config");
                t
            }
            Err(e) => {
                tracing::warn!(
                    config_value = %config_tick_size,
                    error = %e,
                    "tick_size cache empty AND config unparseable; falling back to 0.01",
                );
                rust_decimal_macros::dec!(0.01)
            }
        }
    }

    /// A1: atomic check-and-set on the dispatch gap.
    ///
    /// If the configured `signal_gap_secs` is non-zero AND a previous signal
    /// was forwarded less than that many seconds ago, returns `Some(gap)` —
    /// the caller should record the signal as `GAP_REJECTED` and **not**
    /// broadcast it. Otherwise updates the timestamp to "now" and returns
    /// `None` (caller proceeds to broadcast as `FORWARDED`).
    ///
    /// The check and the update happen under the same mutex so concurrent
    /// handlers cannot both pass the gap on the same window edge.
    pub fn check_and_update_dispatch_gap(&self) -> Option<u64> {
        let gap_secs = self.config.read().unwrap().signal_gap_secs;
        if gap_secs == 0 {
            return None;
        }
        let mut last = self.last_dispatch_at.lock().unwrap();
        let now = Instant::now();
        if let Some(prev) = *last {
            if now.duration_since(prev) < Duration::from_secs(gap_secs) {
                return Some(gap_secs);
            }
        }
        *last = Some(now);
        None
    }

    /// Build a [`DispatchedSignal`] envelope for a freshly-received signal.
    /// Allocates the next `event_seq`, derives the `correlation_id`, and
    /// captures the IST receive time. Call this once per signal in the HTTP
    /// handler — both the ledger insert and the broadcast send must use the
    /// same envelope so downstream rows agree on `event_seq`.
    pub fn make_dispatch(&self, signal: CricketSignal) -> DispatchedSignal {
        let event_seq = self.next_event_seq();
        let correlation_id = format!("{event_seq}-{}", signal.short_tag());
        DispatchedSignal {
            signal,
            event_seq,
            correlation_id,
            ts_ist: ist_now(),
        }
    }

    pub fn push_event(&self, kind: &str, detail: &str) {
        let entry = EventEntry {
            ts: ist_now(),
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
            ts: ist_now(),
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
                    &record.order_id, &slug, &record.fee.to_string(),
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

    // ── Signal-group helpers ──────────────────────────────────────────────

    /// Push a freshly-opened group at the front of the deque. Evicts the
    /// oldest entry when the cap (`MAX_SIGNAL_GROUPS = 50`) is hit.
    pub fn open_signal_group(&self, group: SignalGroup) {
        let mut q = self.signal_groups.lock().unwrap();
        q.push_front(group);
        while q.len() > MAX_SIGNAL_GROUPS {
            q.pop_back();
        }
    }

    /// Find a group by `correlation_id`, find the leg with `role`, and apply
    /// `f` to mutate it. Re-aggregates `total_fee_paid` afterwards. Silent
    /// no-op if the group doesn't exist (legitimate during AUGMENT chains
    /// where the original group may have aged out).
    pub fn update_leg<F>(&self, correlation_id: &str, role: LegRole, f: F)
    where F: FnOnce(&mut LegStatus) {
        let mut q = self.signal_groups.lock().unwrap();
        if let Some(group) = q.iter_mut().find(|g| g.correlation_id == correlation_id) {
            if let Some(leg) = group.legs.iter_mut().find(|l| l.role == role) {
                f(leg);
            }
            // Recompute the aggregate from scratch — cheap (≤ 4 legs).
            group.total_fee_paid = group.legs.iter().map(|l| l.state.fee()).sum();
        }
    }

    /// Set the group's outcome (Wait / BookStale / Closed / …). Idempotent.
    pub fn set_group_outcome(&self, correlation_id: &str, outcome: GroupOutcome) {
        let mut q = self.signal_groups.lock().unwrap();
        if let Some(group) = q.iter_mut().find(|g| g.correlation_id == correlation_id) {
            group.outcome = outcome;
        }
    }

    /// Set `net_pnl` once the round-trip is closed.
    pub fn set_group_net_pnl(&self, correlation_id: &str, net_pnl: Decimal) {
        let mut q = self.signal_groups.lock().unwrap();
        if let Some(group) = q.iter_mut().find(|g| g.correlation_id == correlation_id) {
            group.net_pnl = Some(net_pnl);
        }
    }

    /// Newest-first snapshot for `/api/signal_groups`.
    pub fn signal_groups_snapshot(&self, limit: usize) -> Vec<SignalGroup> {
        let q = self.signal_groups.lock().unwrap();
        q.iter().take(limit).cloned().collect()
    }

    /// Open a stub group for a signal that was rejected at the gate (WAIT,
    /// BOOK_STALE, out-of-range, no-liquidity, AUGMENT-only). All four legs
    /// land in `NotPlanned` and `outcome` is set immediately.
    #[allow(clippy::too_many_arguments)]
    pub fn open_skipped_signal_group(
        &self,
        correlation_id: String,
        event_seq: u64,
        signal_tag: &str,
        label: &str,
        batting: &str,
        bowling: &str,
        outcome: GroupOutcome,
        reason: &str,
    ) {
        self.open_signal_group(SignalGroup {
            correlation_id,
            event_seq,
            signal_tag: signal_tag.to_string(),
            label: label.to_string(),
            ts_ist: ist_now(),
            batting: batting.to_string(),
            bowling: bowling.to_string(),
            legs: vec![
                LegStatus { role: LegRole::FakSell,    state: LegState::NotPlanned { reason: reason.to_string() } },
                LegStatus { role: LegRole::FakBuy,     state: LegState::NotPlanned { reason: reason.to_string() } },
                LegStatus { role: LegRole::RevertBuy,  state: LegState::NotPlanned { reason: "no FAK fired".to_string() } },
                LegStatus { role: LegRole::RevertSell, state: LegState::NotPlanned { reason: "no FAK fired".to_string() } },
            ],
            total_fee_paid: Decimal::ZERO,
            net_pnl: None,
            outcome,
        });
    }

    /// If every leg in the group is in a terminal state, set `outcome` to
    /// `Closed` (unless it is already a non-Open terminal — Wait, BookStale,
    /// etc. — in which case we leave the more specific outcome alone).
    ///
    /// Revert-leg `NotPlanned` is **only** terminal if the paired FAK didn't
    /// fill (i.e. there is nothing to revert). If the paired FAK filled, a
    /// `NotPlanned` revert leg means the revert post is still pending and
    /// the group must remain Open.
    pub fn mark_group_closed_if_terminal(&self, correlation_id: &str) {
        let mut q = self.signal_groups.lock().unwrap();
        let Some(group) = q.iter_mut().find(|g| g.correlation_id == correlation_id) else { return };
        if group.outcome != GroupOutcome::Open { return; }

        // Helper: is this state a "settled" FAK state where no further
        // revert action is expected?
        fn fak_filled(state: &LegState) -> bool {
            matches!(state, LegState::Filled { .. } | LegState::Partial { .. })
        }
        fn paired_fak_role(role: LegRole) -> Option<LegRole> {
            match role {
                LegRole::RevertBuy => Some(LegRole::FakSell),
                LegRole::RevertSell => Some(LegRole::FakBuy),
                _ => None,
            }
        }

        let all_terminal = group.legs.iter().all(|l| match &l.state {
            // Definitively terminal across all roles.
            LegState::Filled { .. } | LegState::Partial { .. } | LegState::Killed { .. }
            | LegState::Cancelled { .. } | LegState::Augmented { .. }
            | LegState::Rejected { .. } | LegState::TakerExit { .. } => true,
            // NotPlanned: terminal IFF this is a revert leg whose paired
            // FAK didn't fill (so no revert was ever expected). For FAK
            // legs themselves, NotPlanned means the gate skipped them
            // (e.g., budget exhausted) — also terminal.
            LegState::NotPlanned { .. } => match paired_fak_role(l.role) {
                Some(fak_role) => {
                    // Revert leg: terminal only if paired FAK did NOT fill.
                    let paired = group.legs.iter().find(|p| p.role == fak_role);
                    paired.map(|p| !fak_filled(&p.state)).unwrap_or(true)
                }
                None => true, // FAK leg itself in NotPlanned → terminal
            },
            // Pending or Posted: in flight, not terminal.
            LegState::Pending | LegState::Posted { .. } => false,
        });
        if all_terminal {
            group.outcome = GroupOutcome::Closed;
        }
    }

    /// Fire-and-forget capture of an oracle event. Never blocks.
    ///
    /// `event_seq` must come from the same [`DispatchedSignal`] that goes onto
    /// the broadcast channel so the ledger row joins to downstream order rows.
    /// `dispatch_decision` is the handler's view at capture time —
    /// `"PARSE_ERROR"`, `"FORWARDED"`, `"GAP_REJECTED"` — and may be later
    /// overwritten by [`crate::db::Db::update_oracle_event_decision`] once
    /// the strategy classifies a forwarded signal as NORMAL/WAIT/AUGMENT.
    /// `ts_ist` is captured at receive-time and is the trader-readable
    /// timestamp; SQLite still records `ts_utc` automatically via the column
    /// default.
    pub fn capture_signal(&self, signal: &str, source: &str, event_seq: u64, dispatch_decision: &str) {
        if let Some(tx) = self.oracle_tx.read().unwrap().as_ref() {
            let ms = self.match_state.read().unwrap();
            let config = self.config.read().unwrap();
            let _ = tx.try_send(OracleEvent {
                signal: signal.to_string(),
                source: source.to_string(),
                innings: ms.innings,
                batting: config.team_name(ms.batting).to_string(),
                bowling: config.team_name(ms.bowling()).to_string(),
                ts_ist: ist_now(),
                event_seq,
                dispatch_decision: dispatch_decision.to_string(),
            });
        }
    }

    pub fn reset_for_new_match(&self) {
        let config = self.config.read().unwrap();
        *self.phase.write().unwrap() = MatchPhase::Idle;
        *self.match_state.write().unwrap() = MatchState::new(config.first_batting);
        *self.dls.write().unwrap() = DlsEngine::new_t20();
        let mut pos = self.position.lock().unwrap();
        pos.team_a_tokens = Decimal::ZERO;
        pos.team_b_tokens = Decimal::ZERO;
        pos.total_spent = Decimal::ZERO;
        pos.trade_count = 0;
        pos.total_budget = config.total_budget_usdc;
        self.clear_orders();
        self.pending_reverts.lock().unwrap().clear();
        self.signal_groups.lock().unwrap().clear();
        self.events.lock().unwrap().clear();
        self.inventory_history.lock().unwrap().clear();
        self.trade_log.lock().unwrap().clear();
        // Drop the previous match's dispatch timestamp so the first signal of
        // the new match is never considered "inside the gap".
        *self.last_dispatch_at.lock().unwrap() = None;
    }
}
