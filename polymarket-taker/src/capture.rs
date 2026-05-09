//! Background capture: store oracle events (signals) from Telegram/UI.
//!
//! The signal handler does a non-blocking `try_send()` and moves on —
//! the background task drains the channel and writes to SQLite.

use std::sync::Arc;

use tokio::sync::mpsc;
use tokio_util::sync::CancellationToken;

use crate::db::Db;

/// An oracle event (signal) to capture.
///
/// `event_seq` and `dispatch_decision` are populated from the request handler
/// once the per-signal id has been allocated and the dispatch outcome is known.
/// Until B4 wires the handler-side allocation, callers may leave `event_seq=0`
/// and `dispatch_decision="PENDING"` and rely on
/// [`crate::db::Db::update_oracle_event_decision`] to overwrite the decision
/// after strategy classifies the signal (NORMAL / WAIT / AUGMENT / …).
#[derive(Debug, Clone)]
pub struct OracleEvent {
    pub signal: String,             // "4", "6", "W", "IO", "MO"
    pub source: String,             // "telegram", "ui"
    pub innings: u8,
    pub batting: String,
    pub bowling: String,
    pub ts_ist: String,             // "HH:MM:SS" in IST, computed at capture time
    pub event_seq: u64,             // 0 until B4 plumbs the real value
    pub dispatch_decision: String,  // "PENDING" until decided
}

/// Spawn the background oracle event writer.
/// Returns the sender — callers do `try_send()` to avoid blocking the hot path.
pub fn spawn_oracle_writer(
    db: Arc<Db>,
    slug: String,
    cancel: CancellationToken,
) -> mpsc::Sender<OracleEvent> {
    let (tx, mut rx) = mpsc::channel::<OracleEvent>(256);

    tokio::spawn(async move {
        loop {
            tokio::select! {
                event = rx.recv() => {
                    match event {
                        Some(evt) => {
                            db.insert_oracle_event(&slug, &evt);
                        }
                        None => break,
                    }
                }
                _ = cancel.cancelled() => break,
            }
        }
        // Drain remaining events before exit
        while let Ok(evt) = rx.try_recv() {
            db.insert_oracle_event(&slug, &evt);
        }
        tracing::info!("[CAPTURE] oracle event writer stopped");
    });

    tx
}
