//! Rolling buffer of recent best-bid/ask snapshots for the two market tokens.
//!
//! Used by `decide_entry_direction` to detect pre-signal price moves —
//! "did the market already react before our oracle delivered the signal?"
//! If yes, the strategy can flip from directional (momentum) to reverse
//! (mean-reversion) entry. See `STRATEGY_CURRENT.md` and the design discussion
//! in the conversation that introduced this module.
//!
//! Why touches and not mids: in tight 1-tick books the difference between
//! touch and mid is half a cent — well under any sensible move threshold —
//! and the touch is the actual price we'd execute at, so it's the truer
//! signal. See also the `OrderBook::best_bid` / `best_ask` accessors.

use std::collections::VecDeque;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use rust_decimal::Decimal;

use crate::types::{OrderBook, Team};

/// Minimal per-token state the strategy needs to compare past vs present.
/// `bid` / `ask` per side are `Option` because either side of a thin book
/// can be empty momentarily.
#[derive(Debug, Clone, Copy)]
pub struct TouchSnapshot {
    pub ts: Instant,
    pub bid_a: Option<Decimal>,
    pub ask_a: Option<Decimal>,
    pub bid_b: Option<Decimal>,
    pub ask_b: Option<Decimal>,
}

impl TouchSnapshot {
    pub fn from_books(ts: Instant, books: &(OrderBook, OrderBook)) -> Self {
        let (a, b) = books;
        Self {
            ts,
            bid_a: a.best_bid().map(|l| l.price),
            ask_a: a.best_ask().map(|l| l.price),
            bid_b: b.best_bid().map(|l| l.price),
            ask_b: b.best_ask().map(|l| l.price),
        }
    }

    /// Best bid for `team` if its side has at least one level.
    pub fn bid(&self, team: Team) -> Option<Decimal> {
        match team {
            Team::TeamA => self.bid_a,
            Team::TeamB => self.bid_b,
        }
    }

    /// Best ask for `team` if its side has at least one level.
    pub fn ask(&self, team: Team) -> Option<Decimal> {
        match team {
            Team::TeamA => self.ask_a,
            Team::TeamB => self.ask_b,
        }
    }
}

/// Bounded ring buffer of `TouchSnapshot`s. Keeps roughly `2 × lookback` of
/// history — enough to answer "what was the touch `lookback` ago?" with a
/// nearby sample even when book updates land asynchronously.
///
/// Thread-safe via `Mutex`. The hot path (record, lookup) walks at most a
/// few hundred entries per call (~30-100 book updates/sec × 6s); contention
/// is negligible compared to the network I/O on the strategy path.
pub struct PriceHistory {
    buf: Mutex<VecDeque<TouchSnapshot>>,
    lookback: Duration,
}

impl PriceHistory {
    pub fn new(lookback: Duration) -> Self {
        Self {
            buf: Mutex::new(VecDeque::new()),
            lookback,
        }
    }

    pub fn lookback(&self) -> Duration {
        self.lookback
    }

    /// Append a snapshot at `ts`. Evicts entries older than `2 × lookback`
    /// from the front so the buffer stays bounded.
    pub fn record_at(&self, ts: Instant, books: &(OrderBook, OrderBook)) {
        let snap = TouchSnapshot::from_books(ts, books);
        let mut buf = self.buf.lock().unwrap();
        buf.push_back(snap);
        let cutoff = ts.checked_sub(self.lookback * 2);
        if let Some(cutoff) = cutoff {
            while let Some(front) = buf.front() {
                if front.ts < cutoff {
                    buf.pop_front();
                } else {
                    break;
                }
            }
        }
    }

    /// Convenience wrapper using `Instant::now()` — production callers.
    pub fn record(&self, books: &(OrderBook, OrderBook)) {
        self.record_at(Instant::now(), books);
    }

    /// Return the snapshot whose `ts` is closest to `target`. None when the
    /// buffer is empty. Used by tests; production should prefer
    /// `touches_lookback_ago` which also enforces the min-history guarantee.
    pub fn closest_to(&self, target: Instant) -> Option<TouchSnapshot> {
        let buf = self.buf.lock().unwrap();
        buf.iter()
            .min_by_key(|s| if s.ts >= target { s.ts - target } else { target - s.ts })
            .copied()
    }

    /// Snapshot from approximately `lookback` ago, or None when the buffer
    /// doesn't yet span the lookback window.
    ///
    /// "Spans the window" means the *oldest* entry is at least as old as
    /// `now - lookback`. Returning a too-recent stand-in would silently
    /// misreport a calm pre-signal book and could cause the strategy to
    /// over-trigger reverse on noise. Cold start callers should fall back
    /// to directional.
    pub fn touches_lookback_ago(&self, now: Instant) -> Option<TouchSnapshot> {
        let target = now.checked_sub(self.lookback)?;
        let buf = self.buf.lock().unwrap();
        let oldest = buf.front()?;
        if oldest.ts > target {
            return None;
        }
        buf.iter()
            .min_by_key(|s| if s.ts >= target { s.ts - target } else { target - s.ts })
            .copied()
    }

    /// Drop all buffered snapshots. Called on WS disconnect — deltas may
    /// have been missed during the gap, so prior touches no longer reflect
    /// the live book.
    pub fn clear(&self) {
        self.buf.lock().unwrap().clear();
    }

    pub fn len(&self) -> usize {
        self.buf.lock().unwrap().len()
    }
}
