/// Tests for WS health-based trade gating.
///
/// Replaces the old `book_is_stale` timestamp logic. The strategy refuses to
/// trade only when the market WS is genuinely down/silent — not when the
/// market is quiet (no recent updates is fine if the connection is up and
/// we have a base snapshot to apply deltas to).

use crate::ws_health::{WsHealth, ws_blocks_trading_reason};

// ── default state ────────────────────────────────────────────────────────────

#[test]
fn default_health_blocks_trading() {
    // Before the WS task even starts: not connected, no snapshot.
    let h = WsHealth::default();
    assert!(!h.connected);
    assert!(!h.snapshot_received);
    assert_eq!(ws_blocks_trading_reason(&h), Some("ws_not_connected"));
}

// ── connected + snapshot → trading allowed ───────────────────────────────────

#[test]
fn connected_with_snapshot_allows_trading() {
    let h = WsHealth { connected: true, snapshot_received: true };
    assert_eq!(ws_blocks_trading_reason(&h), None);
}

#[test]
fn connected_with_snapshot_allows_trading_no_matter_how_long_quiet() {
    // Critical: this is the new semantics. A quiet market is OK.
    // No timestamp comparison anywhere in the gate.
    let h = WsHealth { connected: true, snapshot_received: true };
    assert!(ws_blocks_trading_reason(&h).is_none());
}

// ── disconnected → blocked ───────────────────────────────────────────────────

#[test]
fn disconnected_blocks_trading_even_with_prior_snapshot() {
    // We had a snapshot, but the WS dropped. Deltas may have been missed.
    // Local book is suspect until we reconnect and re-snapshot.
    let h = WsHealth { connected: false, snapshot_received: true };
    assert_eq!(ws_blocks_trading_reason(&h), Some("ws_not_connected"));
}

#[test]
fn disconnected_and_no_snapshot_reports_disconnect_first() {
    // Order matters: connection check is the most fundamental.
    let h = WsHealth { connected: false, snapshot_received: false };
    assert_eq!(ws_blocks_trading_reason(&h), Some("ws_not_connected"));
}

// ── connected but no snapshot yet ────────────────────────────────────────────

#[test]
fn connected_without_snapshot_blocks_trading() {
    // Just-connected, subscribe ack received, but the initial book snapshot
    // hasn't arrived yet. Trading would be against an empty/stale local book.
    let h = WsHealth { connected: true, snapshot_received: false };
    assert_eq!(ws_blocks_trading_reason(&h), Some("ws_no_snapshot"));
}

// ── reconnect resets snapshot_received ───────────────────────────────────────

#[test]
fn reconnect_clears_snapshot_received() {
    // After a disconnect, we're connected again but until the resync snapshot
    // lands we should NOT trade — deltas from before the gap may be missing.
    let mut h = WsHealth { connected: true, snapshot_received: true };
    // simulate disconnect → reconnect cycle
    h.on_disconnect();
    assert!(!h.connected);
    assert!(!h.snapshot_received);
    h.on_connect();
    assert!(h.connected);
    assert!(!h.snapshot_received, "reconnect must require fresh snapshot");
    assert_eq!(ws_blocks_trading_reason(&h), Some("ws_no_snapshot"));
}

#[test]
fn first_snapshot_after_connect_unblocks_trading() {
    let mut h = WsHealth::default();
    h.on_connect();
    assert_eq!(ws_blocks_trading_reason(&h), Some("ws_no_snapshot"));
    h.on_snapshot();
    assert!(h.snapshot_received);
    assert_eq!(ws_blocks_trading_reason(&h), None);
}

// ── on_snapshot without prior connect is a no-op-but-safe ────────────────────

#[test]
fn snapshot_event_while_disconnected_does_not_unblock() {
    // Defensive: if a stale snapshot arrives after disconnect (unlikely race),
    // it should not flip us back to "trading allowed".
    let mut h = WsHealth::default();
    h.on_snapshot();
    // Even if snapshot_received bit is set, connected=false still blocks.
    assert_eq!(ws_blocks_trading_reason(&h), Some("ws_not_connected"));
}
