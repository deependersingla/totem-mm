/// Tests for `PriceHistory` — small ring buffer of recent best-bid/ask snapshots
/// used by `decide_entry_direction` to detect pre-signal moves.
///
/// Tests exercise explicit `Instant`s rather than wall-clock so they're
/// deterministic and don't depend on tokio time mocking.

use std::time::{Duration, Instant};

use rust_decimal::Decimal;
use rust_decimal_macros::dec;

use crate::price_history::{PriceHistory, TouchSnapshot};
use crate::types::{OrderBook, OrderBookSide, PriceLevel, Team};

// ── helpers ──────────────────────────────────────────────────────────────────

fn book_with(bid: Option<Decimal>, ask: Option<Decimal>) -> OrderBook {
    OrderBook {
        bids: OrderBookSide {
            levels: bid.map(|p| vec![PriceLevel { price: p, size: dec!(100) }]).unwrap_or_default(),
        },
        asks: OrderBookSide {
            levels: ask.map(|p| vec![PriceLevel { price: p, size: dec!(100) }]).unwrap_or_default(),
        },
        timestamp_ms: 0,
    }
}

fn books(bid_a: Decimal, ask_a: Decimal, bid_b: Decimal, ask_b: Decimal) -> (OrderBook, OrderBook) {
    (book_with(Some(bid_a), Some(ask_a)), book_with(Some(bid_b), Some(ask_b)))
}

// ── record / len / clear ─────────────────────────────────────────────────────

#[test]
fn record_pushes_to_buffer() {
    let h = PriceHistory::new(Duration::from_secs(3));
    let t0 = Instant::now();
    h.record_at(t0, &books(dec!(0.34), dec!(0.35), dec!(0.65), dec!(0.66)));
    assert_eq!(h.len(), 1);
}

#[test]
fn record_multiple_keeps_chronological_order() {
    let h = PriceHistory::new(Duration::from_secs(3));
    let t0 = Instant::now();
    h.record_at(t0, &books(dec!(0.34), dec!(0.35), dec!(0.65), dec!(0.66)));
    h.record_at(t0 + Duration::from_millis(100), &books(dec!(0.33), dec!(0.34), dec!(0.66), dec!(0.67)));
    h.record_at(t0 + Duration::from_millis(200), &books(dec!(0.32), dec!(0.33), dec!(0.67), dec!(0.68)));
    assert_eq!(h.len(), 3);
}

#[test]
fn clear_empties_buffer() {
    let h = PriceHistory::new(Duration::from_secs(3));
    let t0 = Instant::now();
    h.record_at(t0, &books(dec!(0.34), dec!(0.35), dec!(0.65), dec!(0.66)));
    h.record_at(t0 + Duration::from_millis(50), &books(dec!(0.33), dec!(0.34), dec!(0.66), dec!(0.67)));
    assert_eq!(h.len(), 2);
    h.clear();
    assert_eq!(h.len(), 0);
}

// ── eviction ─────────────────────────────────────────────────────────────────

#[test]
fn record_evicts_entries_older_than_double_lookback() {
    // Eviction policy: drop entries older than 2× lookback. Keeps a single
    // backup window so the closest-to-target search always has a candidate
    // even if `lookback` falls between two consecutive samples.
    let h = PriceHistory::new(Duration::from_secs(3));
    let t0 = Instant::now();
    // Older than 2×3s = 6s
    h.record_at(t0, &books(dec!(0.34), dec!(0.35), dec!(0.65), dec!(0.66)));
    // Within window
    h.record_at(t0 + Duration::from_secs(2), &books(dec!(0.33), dec!(0.34), dec!(0.66), dec!(0.67)));
    // Still within 6s of newest
    h.record_at(t0 + Duration::from_secs(5), &books(dec!(0.32), dec!(0.33), dec!(0.67), dec!(0.68)));
    // Triggers eviction of t0 (now 7s old)
    h.record_at(t0 + Duration::from_secs(7), &books(dec!(0.31), dec!(0.32), dec!(0.68), dec!(0.69)));
    assert_eq!(h.len(), 3, "oldest snapshot (t0) should have been evicted");
}

#[test]
fn record_does_not_evict_entries_within_double_lookback() {
    let h = PriceHistory::new(Duration::from_secs(3));
    let t0 = Instant::now();
    h.record_at(t0, &books(dec!(0.34), dec!(0.35), dec!(0.65), dec!(0.66)));
    h.record_at(t0 + Duration::from_millis(500), &books(dec!(0.33), dec!(0.34), dec!(0.66), dec!(0.67)));
    h.record_at(t0 + Duration::from_secs(5), &books(dec!(0.32), dec!(0.33), dec!(0.67), dec!(0.68)));
    // Newest is 5s, oldest is 0s → 5s diff, under 2×3=6s threshold
    assert_eq!(h.len(), 3);
}

// ── touches_lookback_ago ─────────────────────────────────────────────────────

#[test]
fn touches_lookback_ago_returns_none_when_empty() {
    let h = PriceHistory::new(Duration::from_secs(3));
    let now = Instant::now();
    assert!(h.touches_lookback_ago(now).is_none());
}

#[test]
fn touches_lookback_ago_returns_none_when_buffer_does_not_span_lookback() {
    // We only have 1s of history; lookback is 3s → can't answer.
    let h = PriceHistory::new(Duration::from_secs(3));
    let t0 = Instant::now();
    h.record_at(t0, &books(dec!(0.34), dec!(0.35), dec!(0.65), dec!(0.66)));
    h.record_at(t0 + Duration::from_millis(500), &books(dec!(0.33), dec!(0.34), dec!(0.66), dec!(0.67)));
    h.record_at(t0 + Duration::from_secs(1), &books(dec!(0.32), dec!(0.33), dec!(0.67), dec!(0.68)));
    let now = t0 + Duration::from_secs(1);
    assert!(h.touches_lookback_ago(now).is_none(),
        "must refuse to return a stand-in when buffer is shallower than lookback");
}

#[test]
fn touches_lookback_ago_returns_closest_snapshot_to_target() {
    let h = PriceHistory::new(Duration::from_secs(3));
    let t0 = Instant::now();
    h.record_at(t0, &books(dec!(0.34), dec!(0.35), dec!(0.65), dec!(0.66))); // marker A
    h.record_at(t0 + Duration::from_millis(2_900), &books(dec!(0.33), dec!(0.34), dec!(0.66), dec!(0.67))); // close to target (3s ago from t0+5.9)
    h.record_at(t0 + Duration::from_millis(3_500), &books(dec!(0.32), dec!(0.33), dec!(0.67), dec!(0.68)));
    h.record_at(t0 + Duration::from_secs(5), &books(dec!(0.31), dec!(0.32), dec!(0.68), dec!(0.69))); // marker D

    // now = t0 + 5.9s → target = t0 + 2.9s → closest is the second entry
    let now = t0 + Duration::from_millis(5_900);
    let snap = h.touches_lookback_ago(now).expect("should return a snapshot");
    assert_eq!(snap.bid_a, Some(dec!(0.33)));
    assert_eq!(snap.ask_a, Some(dec!(0.34)));
    assert_eq!(snap.bid_b, Some(dec!(0.66)));
    assert_eq!(snap.ask_b, Some(dec!(0.67)));
}

// ── TouchSnapshot::from_books — empty sides ──────────────────────────────────

#[test]
fn record_handles_book_with_no_bids() {
    let h = PriceHistory::new(Duration::from_secs(3));
    let t0 = Instant::now();
    let bk = (book_with(None, Some(dec!(0.35))), book_with(Some(dec!(0.65)), Some(dec!(0.66))));
    h.record_at(t0, &bk);
    let snap = h.closest_to(t0).unwrap();
    assert_eq!(snap.bid_a, None);
    assert_eq!(snap.ask_a, Some(dec!(0.35)));
    assert_eq!(snap.bid_b, Some(dec!(0.65)));
}

#[test]
fn record_handles_book_with_no_asks() {
    let h = PriceHistory::new(Duration::from_secs(3));
    let t0 = Instant::now();
    let bk = (book_with(Some(dec!(0.34)), None), book_with(Some(dec!(0.65)), Some(dec!(0.66))));
    h.record_at(t0, &bk);
    let snap = h.closest_to(t0).unwrap();
    assert_eq!(snap.bid_a, Some(dec!(0.34)));
    assert_eq!(snap.ask_a, None);
}

// ── TouchSnapshot::bid / ask accessors ───────────────────────────────────────

#[test]
fn touch_snapshot_bid_ask_dispatch_on_team() {
    let snap = TouchSnapshot {
        ts: Instant::now(),
        bid_a: Some(dec!(0.34)),
        ask_a: Some(dec!(0.35)),
        bid_b: Some(dec!(0.65)),
        ask_b: Some(dec!(0.66)),
    };
    assert_eq!(snap.bid(Team::TeamA), Some(dec!(0.34)));
    assert_eq!(snap.ask(Team::TeamA), Some(dec!(0.35)));
    assert_eq!(snap.bid(Team::TeamB), Some(dec!(0.65)));
    assert_eq!(snap.ask(Team::TeamB), Some(dec!(0.66)));
}
