//! Market WebSocket health signal.
//!
//! Replaces the timestamp-based `book_is_stale` guard. Polymarket's market WS
//! is sequenced and reliable while the connection is up: we get an initial
//! `book` snapshot at subscribe time, then `price_change` deltas keep the
//! local book current. So the question the strategy actually needs to answer
//! before placing a trade is **"is my view of the book diverged from
//! Polymarket's?"** — which depends on connection state and whether we have
//! a base snapshot, NOT on how long it's been since the last delta.
//!
//! Quiet markets are fine. A connected WS that hasn't sent a delta in 30s
//! still represents a current book — nothing happened, nothing should change.
//!
//! Disconnects, on the other hand, can drop deltas in the gap. We require a
//! fresh snapshot after every (re)connect before trading is allowed again.

/// Live health of the market WebSocket subscription.
///
/// `Default` is the safe pre-startup state: not connected, no snapshot,
/// trading blocked.
#[derive(Debug, Clone, Copy, Default, serde::Serialize)]
pub struct WsHealth {
    /// True while the WS connection is up and authenticated. Set on successful
    /// `connect_async`, cleared on Close/error/read-failure.
    pub connected: bool,

    /// True after a `book` snapshot has been received since the *last*
    /// `on_connect`. Reset to false on every reconnect — deltas may have been
    /// missed during the gap, so the local book is suspect until resync.
    pub snapshot_received: bool,
}

impl WsHealth {
    /// Mark the WS as connected. Clears `snapshot_received` because the prior
    /// snapshot is now stale (we may have missed deltas during the disconnect).
    pub fn on_connect(&mut self) {
        self.connected = true;
        self.snapshot_received = false;
    }

    /// Mark the WS as disconnected. Also clears `snapshot_received`: any
    /// future `on_snapshot` must be paired with a fresh `on_connect`.
    pub fn on_disconnect(&mut self) {
        self.connected = false;
        self.snapshot_received = false;
    }

    /// Record arrival of a `book` snapshot event. Idempotent — repeated
    /// snapshots while connected don't change the gate, they just confirm
    /// resync is current.
    pub fn on_snapshot(&mut self) {
        self.snapshot_received = true;
    }
}

/// Returns `Some(reason)` when trading should be blocked, `None` when allowed.
///
/// Reason strings are stable and used as `dispatch_decision` ledger values:
/// - `"ws_not_connected"` — the WS task isn't holding an open connection.
/// - `"ws_no_snapshot"`   — connected, but the initial/resync `book` event
///   hasn't arrived yet, so we have no reliable base book.
///
/// Connection state is checked first because it's the more fundamental
/// problem: a disconnected WS may also have a stale prior snapshot, but
/// "not connected" is the actionable signal.
pub fn ws_blocks_trading_reason(health: &WsHealth) -> Option<&'static str> {
    if !health.connected {
        return Some("ws_not_connected");
    }
    if !health.snapshot_received {
        return Some("ws_no_snapshot");
    }
    None
}
