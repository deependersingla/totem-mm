//! SQLite persistence for trades, orders, and round-trip PnL.
//!
//! Uses a local `taker.db` file that survives restarts. All writes are
//! fire-and-forget (logged on error, never block the hot path).

use std::sync::Mutex;

use rust_decimal::Decimal;
use rusqlite::{params, Connection};
use serde::Serialize;

use crate::capture::OracleEvent;

const DB_FILE: &str = "taker.db";

/// Convert an f64 aggregate (from SQLite `SUM(CAST(... AS REAL))`) to Decimal
/// without binary-float drift, rounded to 6 dp (USDC base-unit precision).
fn f64_to_decimal(x: f64) -> Decimal {
    if !x.is_finite() {
        return Decimal::ZERO;
    }
    format!("{x}").parse::<Decimal>().unwrap_or(Decimal::ZERO).round_dp(6)
}

pub struct Db {
    conn: Mutex<Connection>,
}

/// A completed round-trip: FAK entry + GTC revert exit.
#[derive(Debug, Clone, Serialize)]
pub struct RoundTrip {
    pub id: i64,
    pub entry_ts: String,
    pub exit_ts: String,
    pub team: String,
    pub entry_side: String,  // BUY or SELL
    pub entry_price: String,
    pub exit_price: String,
    pub size: String,
    pub pnl: String,         // gross: exit_proceeds - entry_cost
    pub label: String,       // WICKET, RUN4, etc.
    pub entry_order_id: String,
    pub exit_order_id: String,
    /// Entry-leg fee, USDC. Always 0 for legacy rows.
    pub fee_in: String,
    /// Exit-leg fee, USDC. 0 for maker reverts when `fd.to=true`.
    pub fee_out: String,
    /// `pnl − fee_in − fee_out`.
    pub net_pnl: String,
}

/// A persisted trade record.
#[derive(Debug, Clone, Serialize)]
pub struct DbTrade {
    pub id: i64,
    pub ts: String,
    pub side: String,
    pub team: String,
    pub size: String,
    pub price: String,
    pub cost: String,
    pub order_type: String,
    pub label: String,
    pub order_id: String,
    pub slug: String,
    pub fee: String,
}

/// Borrow-style trade-row args used by [`Db::insert_revert_fill_atomic`].
/// Mirrors the field order of `Db::insert_trade`.
pub struct TradeArgs<'a> {
    pub ts: &'a str,
    pub side: &'a str,
    pub team: &'a str,
    pub size: &'a str,
    pub price: &'a str,
    pub cost: &'a str,
    pub order_type: &'a str,
    pub label: &'a str,
    pub order_id: &'a str,
    pub slug: &'a str,
    /// Platform fee for this fill, in USDC. `"0"` for makers (GTC reverts
    /// when `fd.to=true`) and for legacy rows.
    pub fee: &'a str,
}

/// Borrow-style round_trip args paired with [`TradeArgs`] in the atomic
/// revert-fill writer.
pub struct RoundTripArgs<'a> {
    pub entry_ts: &'a str,
    pub exit_ts: &'a str,
    pub team: &'a str,
    pub entry_side: &'a str,
    pub entry_price: &'a str,
    pub exit_price: &'a str,
    pub size: &'a str,
    pub pnl: &'a str,
    pub label: &'a str,
    pub entry_order_id: &'a str,
    pub exit_order_id: &'a str,
    pub slug: &'a str,
    /// Entry-leg fee in USDC.
    pub fee_in: &'a str,
    /// Exit-leg fee in USDC. `"0"` for maker reverts under `fd.to=true`.
    pub fee_out: &'a str,
    /// `pnl − fee_in − fee_out`, computed by the caller.
    pub net_pnl: &'a str,
}

/// One MATCHED/CONFIRMED fill against one of our orders. Persisted by
/// [`Db::insert_user_fill`] from the user-WS bridge (B6). Borrows fields so
/// callers can build a row from already-allocated strings without extra clones.
pub struct UserFill<'a> {
    pub ts_ist: &'a str,
    pub polymarket_trade_id: Option<&'a str>,
    pub order_id: &'a str,
    pub correlation_id: &'a str,
    pub asset_id: &'a str,
    pub team: &'a str,
    pub side: &'a str,
    pub size: &'a str,
    pub price: &'a str,
    pub status: &'a str,
    pub slug: &'a str,
    pub raw_json: &'a str,
}

/// Placement-time metadata for a freshly-submitted order. Written by
/// [`Db::record_order_placement`] from the strategy / maker placement sites
/// so a downstream `SELECT * FROM clob_orders WHERE correlation_id = ?` can
/// recover the FAK pair, the revert, and any AUGMENT replacements as one set.
///
/// `replaces_order_id` is `Some(oid)` for an AUGMENT'd revert that supersedes
/// a cancelled prior revert, otherwise `None`.
pub struct OrderPlacement<'a> {
    pub order_id: &'a str,
    pub slug: &'a str,
    pub asset_id: &'a str,
    pub side: &'a str,           // "BUY" / "SELL"
    pub price: &'a str,
    pub original_size: &'a str,
    pub status: &'a str,         // "live" placeholder; polling owns updates
    pub order_type: &'a str,     // "FAK" / "GTC" / "GTD"
    pub created_at: &'a str,     // IST timestamp from strategy/maker
    pub team: &'a str,
    pub correlation_id: &'a str, // "{event_seq}-{short_tag}" or "" for maker
    pub purpose: &'a str,        // FAK_BUY / FAK_SELL / REVERT_GTC / MAKER_QUOTE
    pub replaces_order_id: Option<&'a str>,
}

/// A CLOB order snapshot from polling.
#[derive(Debug, Clone, Serialize)]
pub struct ClobOrderRow {
    pub order_id: String,
    pub asset_id: String,
    pub side: String,
    pub price: String,
    pub original_size: String,
    pub size_matched: String,
    pub status: String,
    pub order_type: String,
    pub created_at: String,
    pub team: String,
}

impl Db {
    pub fn open() -> anyhow::Result<Self> {
        Self::open_at(std::path::Path::new(DB_FILE))
    }

    /// Open or create the SQLite database at `path`. Used by tests that need
    /// an isolated on-disk database; production callers should use [`Self::open`].
    pub fn open_at(path: &std::path::Path) -> anyhow::Result<Self> {
        let conn = Connection::open(path)?;
        conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")?;

        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                side TEXT NOT NULL,
                team TEXT NOT NULL,
                size TEXT NOT NULL,
                price TEXT NOT NULL,
                cost TEXT NOT NULL,
                order_type TEXT NOT NULL,
                label TEXT NOT NULL,
                order_id TEXT NOT NULL,
                slug TEXT NOT NULL DEFAULT '',
                fee TEXT NOT NULL DEFAULT '0',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS round_trips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_ts TEXT NOT NULL,
                exit_ts TEXT NOT NULL,
                team TEXT NOT NULL,
                entry_side TEXT NOT NULL,
                entry_price TEXT NOT NULL,
                exit_price TEXT NOT NULL,
                size TEXT NOT NULL,
                pnl TEXT NOT NULL,
                label TEXT NOT NULL,
                entry_order_id TEXT NOT NULL,
                exit_order_id TEXT NOT NULL,
                slug TEXT NOT NULL DEFAULT '',
                fee_in TEXT NOT NULL DEFAULT '0',
                fee_out TEXT NOT NULL DEFAULT '0',
                net_pnl TEXT NOT NULL DEFAULT '0',
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS clob_orders (
                order_id TEXT PRIMARY KEY,
                asset_id TEXT NOT NULL,
                side TEXT NOT NULL,
                price TEXT NOT NULL,
                original_size TEXT NOT NULL,
                size_matched TEXT NOT NULL DEFAULT '0',
                status TEXT NOT NULL,
                order_type TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                team TEXT NOT NULL DEFAULT '',
                slug TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                correlation_id TEXT NOT NULL DEFAULT '',
                purpose TEXT NOT NULL DEFAULT '',
                replaces_order_id TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_clob_orders_correlation_id ON clob_orders(correlation_id);
            CREATE INDEX IF NOT EXISTS idx_clob_orders_replaces_order_id ON clob_orders(replaces_order_id);

            CREATE TABLE IF NOT EXISTS oracle_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc TEXT NOT NULL DEFAULT (datetime('now')),
                ts_ist TEXT NOT NULL DEFAULT '',
                signal TEXT NOT NULL,
                source TEXT NOT NULL,
                innings INTEGER NOT NULL,
                batting TEXT NOT NULL,
                bowling TEXT NOT NULL,
                slug TEXT NOT NULL DEFAULT '',
                event_seq INTEGER NOT NULL DEFAULT 0,
                dispatch_decision TEXT NOT NULL DEFAULT 'PENDING'
            );

            CREATE INDEX IF NOT EXISTS idx_oracle_events_slug ON oracle_events(slug);
            CREATE INDEX IF NOT EXISTS idx_oracle_events_event_seq ON oracle_events(event_seq);

            CREATE TABLE IF NOT EXISTS user_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_ist TEXT NOT NULL,
                ts_utc TEXT NOT NULL DEFAULT (datetime('now')),
                polymarket_trade_id TEXT,
                order_id TEXT NOT NULL,
                correlation_id TEXT NOT NULL DEFAULT '',
                asset_id TEXT NOT NULL,
                team TEXT NOT NULL DEFAULT '',
                side TEXT NOT NULL,
                size TEXT NOT NULL,
                price TEXT NOT NULL,
                status TEXT NOT NULL,
                slug TEXT NOT NULL DEFAULT '',
                raw_json TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_user_fills_order_id ON user_fills(order_id);
            CREATE INDEX IF NOT EXISTS idx_user_fills_correlation_id ON user_fills(correlation_id);
            CREATE INDEX IF NOT EXISTS idx_user_fills_ts_ist ON user_fills(ts_ist);",
        )?;

        migrate_oracle_events(&conn)?;
        migrate_clob_orders(&conn)?;
        migrate_trades(&conn)?;
        migrate_round_trips(&conn)?;

        Ok(Self { conn: Mutex::new(conn) })
    }

    // ── Trades ────────────────────────────────────────────────────────────

    pub fn insert_trade(
        &self, ts: &str, side: &str, team: &str, size: &str,
        price: &str, cost: &str, order_type: &str, label: &str,
        order_id: &str, slug: &str, fee: &str,
    ) {
        let conn = self.conn.lock().unwrap();
        if let Err(e) = conn.execute(
            "INSERT INTO trades (ts, side, team, size, price, cost, order_type, label, order_id, slug, fee)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
            params![ts, side, team, size, price, cost, order_type, label, order_id, slug, fee],
        ) {
            tracing::warn!(error = %e, "db: failed to insert trade");
        }
    }

    /// Sum `trades.fee` for the given slug (or all rows if slug is empty).
    /// Returns `Decimal::ZERO` on query failure.
    pub fn total_fee_paid(&self, slug: &str) -> Decimal {
        let conn = self.conn.lock().unwrap();
        let result: f64 = conn.query_row(
            "SELECT COALESCE(SUM(CAST(fee AS REAL)), 0) FROM trades WHERE slug = ?1 OR ?1 = ''",
            params![slug],
            |row| row.get(0),
        ).unwrap_or(0.0);
        f64_to_decimal(result)
    }

    /// Sum `round_trips.net_pnl` for the given slug. `Decimal::ZERO` if none.
    pub fn total_net_pnl(&self, slug: &str) -> Decimal {
        let conn = self.conn.lock().unwrap();
        let result: f64 = conn.query_row(
            "SELECT COALESCE(SUM(CAST(net_pnl AS REAL)), 0) FROM round_trips WHERE slug = ?1 OR ?1 = ''",
            params![slug],
            |row| row.get(0),
        ).unwrap_or(0.0);
        f64_to_decimal(result)
    }

    pub fn get_trades(&self, slug: &str, limit: usize) -> Vec<DbTrade> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT id, ts, side, team, size, price, cost, order_type, label, order_id, slug, fee
             FROM trades WHERE slug = ?1 OR ?1 = '' ORDER BY id DESC LIMIT ?2"
        ).unwrap();
        stmt.query_map(params![slug, limit as i64], |row| {
            Ok(DbTrade {
                id: row.get(0)?,
                ts: row.get(1)?,
                side: row.get(2)?,
                team: row.get(3)?,
                size: row.get(4)?,
                price: row.get(5)?,
                cost: row.get(6)?,
                order_type: row.get(7)?,
                label: row.get(8)?,
                order_id: row.get(9)?,
                slug: row.get(10)?,
                fee: row.get(11)?,
            })
        }).unwrap().filter_map(|r| r.ok()).collect()
    }

    // ── Round-trips ───────────────────────────────────────────────────────

    #[allow(clippy::too_many_arguments)]
    pub fn insert_round_trip(
        &self, entry_ts: &str, exit_ts: &str, team: &str, entry_side: &str,
        entry_price: &str, exit_price: &str, size: &str, pnl: &str,
        label: &str, entry_order_id: &str, exit_order_id: &str, slug: &str,
        fee_in: &str, fee_out: &str, net_pnl: &str,
    ) {
        let conn = self.conn.lock().unwrap();
        if let Err(e) = conn.execute(
            "INSERT INTO round_trips (entry_ts, exit_ts, team, entry_side, entry_price, exit_price, size, pnl, label, entry_order_id, exit_order_id, slug, fee_in, fee_out, net_pnl)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15)",
            params![entry_ts, exit_ts, team, entry_side, entry_price, exit_price, size, pnl, label, entry_order_id, exit_order_id, slug, fee_in, fee_out, net_pnl],
        ) {
            tracing::warn!(error = %e, "db: failed to insert round_trip");
        }
    }

    /// D7 (TODO.md): atomic write of the exit trade and the round-trip row
    /// when a revert fills. Either both rows land or neither does — a kill -9
    /// between the two no longer leaves the ledger out of sync.
    pub fn insert_revert_fill_atomic(
        &self,
        trade: &TradeArgs<'_>,
        rt: &RoundTripArgs<'_>,
    ) {
        let mut conn = self.conn.lock().unwrap();
        let tx = match conn.transaction() {
            Ok(t) => t,
            Err(e) => {
                tracing::warn!(error = %e, "db: failed to begin revert-fill tx");
                return;
            }
        };

        let trade_res = tx.execute(
            "INSERT INTO trades (ts, side, team, size, price, cost, order_type, label, order_id, slug, fee)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
            params![
                trade.ts, trade.side, trade.team, trade.size, trade.price,
                trade.cost, trade.order_type, trade.label, trade.order_id, trade.slug,
                trade.fee,
            ],
        );
        if let Err(e) = trade_res {
            tracing::warn!(error = %e, "db: trade insert failed inside revert-fill tx — rolling back");
            return; // tx auto-rolls back on drop
        }

        let rt_res = tx.execute(
            "INSERT INTO round_trips (entry_ts, exit_ts, team, entry_side, entry_price, exit_price, size, pnl, label, entry_order_id, exit_order_id, slug, fee_in, fee_out, net_pnl)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15)",
            params![
                rt.entry_ts, rt.exit_ts, rt.team, rt.entry_side, rt.entry_price,
                rt.exit_price, rt.size, rt.pnl, rt.label, rt.entry_order_id,
                rt.exit_order_id, rt.slug, rt.fee_in, rt.fee_out, rt.net_pnl,
            ],
        );
        if let Err(e) = rt_res {
            tracing::warn!(error = %e, "db: round_trip insert failed inside revert-fill tx — rolling back");
            return;
        }

        if let Err(e) = tx.commit() {
            tracing::warn!(error = %e, "db: revert-fill tx commit failed");
        }
    }

    pub fn get_round_trips(&self, slug: &str, limit: usize) -> Vec<RoundTrip> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT id, entry_ts, exit_ts, team, entry_side, entry_price, exit_price, size, pnl, label, entry_order_id, exit_order_id, fee_in, fee_out, net_pnl
             FROM round_trips WHERE slug = ?1 OR ?1 = '' ORDER BY id DESC LIMIT ?2"
        ).unwrap();
        stmt.query_map(params![slug, limit as i64], |row| {
            Ok(RoundTrip {
                id: row.get(0)?,
                entry_ts: row.get(1)?,
                exit_ts: row.get(2)?,
                team: row.get(3)?,
                entry_side: row.get(4)?,
                entry_price: row.get(5)?,
                exit_price: row.get(6)?,
                size: row.get(7)?,
                pnl: row.get(8)?,
                label: row.get(9)?,
                entry_order_id: row.get(10)?,
                exit_order_id: row.get(11)?,
                fee_in: row.get(12)?,
                fee_out: row.get(13)?,
                net_pnl: row.get(14)?,
            })
        }).unwrap().filter_map(|r| r.ok()).collect()
    }

    pub fn total_pnl(&self, slug: &str) -> Decimal {
        let conn = self.conn.lock().unwrap();
        let result: f64 = conn.query_row(
            "SELECT COALESCE(SUM(CAST(pnl AS REAL)), 0) FROM round_trips WHERE slug = ?1 OR ?1 = ''",
            params![slug],
            |row| row.get(0),
        ).unwrap_or(0.0);
        f64_to_decimal(result)
    }

    // ── CLOB orders ───────────────────────────────────────────────────────

    /// Record an order at placement time, before the polling loop discovers
    /// it. Writes the placement-known fields plus the B5 metadata
    /// (`correlation_id`, `purpose`, `replaces_order_id`). On conflict only
    /// the metadata fields are updated — the polling loop's [`Self::upsert_clob_order`]
    /// retains exclusive ownership of `status` and `size_matched`.
    pub fn record_order_placement(&self, p: &OrderPlacement<'_>) {
        let conn = self.conn.lock().unwrap();
        if let Err(e) = conn.execute(
            "INSERT INTO clob_orders
              (order_id, asset_id, side, price, original_size, size_matched,
               status, order_type, created_at, team, slug, updated_at,
               correlation_id, purpose, replaces_order_id)
             VALUES (?1, ?2, ?3, ?4, ?5, '0',
                     ?6, ?7, ?8, ?9, ?10, datetime('now'),
                     ?11, ?12, ?13)
             ON CONFLICT(order_id) DO UPDATE SET
               correlation_id = excluded.correlation_id,
               purpose = excluded.purpose,
               replaces_order_id = COALESCE(excluded.replaces_order_id, replaces_order_id),
               updated_at = datetime('now')",
            params![
                p.order_id, p.asset_id, p.side, p.price, p.original_size,
                p.status, p.order_type, p.created_at, p.team, p.slug,
                p.correlation_id, p.purpose, p.replaces_order_id,
            ],
        ) {
            tracing::warn!(error = %e, order_id = p.order_id, "db: failed to record order placement");
        }
    }

    /// Mark an existing clob_orders row as terminal — flips `status` and
    /// updates `size_matched`. Used at every point where the strategy KNOWS
    /// an order's final state (FAK fill, FAK kill, GTC fill, cancel, AUGMENT
    /// repost). Without this the polling sync would never update the row
    /// because Polymarket's `/data/orders` only returns live orders, leaving
    /// our local view permanently showing the order as `live`/`delayed`.
    pub fn mark_clob_order_terminal(&self, order_id: &str, status: &str, size_matched: &str) {
        let conn = self.conn.lock().unwrap();
        if let Err(e) = conn.execute(
            "UPDATE clob_orders SET status = ?1, size_matched = ?2, updated_at = datetime('now') WHERE order_id = ?3",
            params![status, size_matched, order_id],
        ) {
            tracing::warn!(error = %e, order_id, status, "db: failed to mark order terminal");
        }
    }

    pub fn upsert_clob_order(&self, o: &ClobOrderRow, slug: &str) {
        let conn = self.conn.lock().unwrap();
        if let Err(e) = conn.execute(
            "INSERT INTO clob_orders (order_id, asset_id, side, price, original_size, size_matched, status, order_type, created_at, team, slug, updated_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, datetime('now'))
             ON CONFLICT(order_id) DO UPDATE SET
               size_matched = excluded.size_matched,
               status = excluded.status,
               updated_at = datetime('now')",
            params![o.order_id, o.asset_id, o.side, o.price, o.original_size, o.size_matched, o.status, o.order_type, o.created_at, o.team, slug],
        ) {
            tracing::warn!(error = %e, "db: failed to upsert clob_order");
        }
    }

    pub fn get_open_orders(&self, slug: &str) -> Vec<ClobOrderRow> {
        let conn = self.conn.lock().unwrap();
        // Filter out stale rows: only show orders whose `updated_at` is
        // within the last 90 seconds. The clob_order_sync poll refreshes
        // updated_at every 5s for orders Polymarket still considers live;
        // mark_clob_order_terminal flips status (and updated_at) when we
        // know an order is terminal. Rows that fall through both — e.g.,
        // orders cancelled on Polymarket via a different session, or that
        // existed before mark_clob_order_terminal was wired — go stale and
        // would otherwise pollute the Open Orders view forever.
        let mut stmt = conn.prepare(
            "SELECT order_id, asset_id, side, price, original_size, size_matched, status, order_type, created_at, team
             FROM clob_orders
             WHERE (slug = ?1 OR ?1 = '')
               AND status IN ('live', 'delayed', 'open')
               AND updated_at >= datetime('now', '-90 seconds')
             ORDER BY created_at DESC"
        ).unwrap();
        stmt.query_map(params![slug], |row| {
            Ok(ClobOrderRow {
                order_id: row.get(0)?,
                asset_id: row.get(1)?,
                side: row.get(2)?,
                price: row.get(3)?,
                original_size: row.get(4)?,
                size_matched: row.get(5)?,
                status: row.get(6)?,
                order_type: row.get(7)?,
                created_at: row.get(8)?,
                team: row.get(9)?,
            })
        }).unwrap().filter_map(|r| r.ok()).collect()
    }

    pub fn get_closed_orders(&self, slug: &str, limit: usize) -> Vec<ClobOrderRow> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT order_id, asset_id, side, price, original_size, size_matched, status, order_type, created_at, team
             FROM clob_orders WHERE (slug = ?1 OR ?1 = '') AND status NOT IN ('live', 'delayed', 'open')
             ORDER BY updated_at DESC LIMIT ?2"
        ).unwrap();
        stmt.query_map(params![slug, limit as i64], |row| {
            Ok(ClobOrderRow {
                order_id: row.get(0)?,
                asset_id: row.get(1)?,
                side: row.get(2)?,
                price: row.get(3)?,
                original_size: row.get(4)?,
                size_matched: row.get(5)?,
                status: row.get(6)?,
                order_type: row.get(7)?,
                created_at: row.get(8)?,
                team: row.get(9)?,
            })
        }).unwrap().filter_map(|r| r.ok()).collect()
    }

    // ── Capture tables ───────────────────────────────────────────────────

    pub fn insert_oracle_event(&self, slug: &str, evt: &OracleEvent) {
        let conn = self.conn.lock().unwrap();
        if let Err(e) = conn.execute(
            "INSERT INTO oracle_events
             (signal, source, innings, batting, bowling, slug,
              ts_ist, event_seq, dispatch_decision)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
            params![
                evt.signal, evt.source, evt.innings, evt.batting, evt.bowling, slug,
                evt.ts_ist, evt.event_seq as i64, evt.dispatch_decision,
            ],
        ) {
            tracing::warn!(error = %e, "db: failed to insert oracle_event");
        }
    }

    /// Look up the `correlation_id` recorded for an order at placement time.
    /// Returns `None` if the row is missing or has the empty default — this
    /// is normal for orders we never placed ourselves (rare; appears only if
    /// the user-WS leaks fills from another session sharing the same wallet).
    pub fn lookup_correlation_id(&self, order_id: &str) -> Option<String> {
        let conn = self.conn.lock().unwrap();
        conn.query_row(
            "SELECT correlation_id FROM clob_orders WHERE order_id = ?1",
            params![order_id],
            |r| r.get::<_, String>(0),
        )
        .ok()
        .filter(|s| !s.is_empty())
    }

    /// Insert one MATCHED/CONFIRMED user fill into the per-event ledger
    /// (B6). Called from the WS bridge once per fill event — partial fills
    /// of one resting order generate one row each, preserving the granularity
    /// the aggregate `clob_orders.size_matched` would lose.
    pub fn insert_user_fill(&self, f: &UserFill<'_>) {
        let conn = self.conn.lock().unwrap();
        if let Err(e) = conn.execute(
            "INSERT INTO user_fills
              (ts_ist, polymarket_trade_id, order_id, correlation_id,
               asset_id, team, side, size, price, status, slug, raw_json)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)",
            params![
                f.ts_ist, f.polymarket_trade_id, f.order_id, f.correlation_id,
                f.asset_id, f.team, f.side, f.size, f.price, f.status, f.slug, f.raw_json,
            ],
        ) {
            tracing::warn!(error = %e, order_id = f.order_id, "db: failed to insert user_fill");
        }
    }

    /// Update the dispatch decision for an oracle event by `event_seq`.
    ///
    /// Called by the strategy after it classifies a forwarded signal as
    /// NORMAL / WAIT / AUGMENT / BOOK_STALE / ERROR. `event_seq` is unique
    /// within a single process lifetime (the in-memory counter resets on
    /// restart), which is sufficient because the strategy task that performs
    /// the update dies with the process.
    pub fn update_oracle_event_decision(&self, event_seq: u64, decision: &str) {
        let conn = self.conn.lock().unwrap();
        if let Err(e) = conn.execute(
            "UPDATE oracle_events SET dispatch_decision = ?1 WHERE event_seq = ?2",
            params![decision, event_seq as i64],
        ) {
            tracing::warn!(error = %e, event_seq, decision, "db: failed to update oracle_event decision");
        }
    }
}

// ── Migrations ────────────────────────────────────────────────────────────
//
// Idempotent — runs on every `Db::open()`. Brings legacy `oracle_events`
// rows up to the schema declared in `Db::open()`'s CREATE TABLE block:
//   * renames `ts` → `ts_utc` (column name only; default still `datetime('now')`)
//   * adds `ts_ist`, `event_seq`, `dispatch_decision`
//   * backfills `ts_ist` from `ts_utc` for existing rows (UTC + 5h30m → IST)
//   * ensures the `event_seq` index exists
//
// Depends on SQLite 3.25+ (`ALTER TABLE … RENAME COLUMN`); rusqlite 0.31's
// `bundled` feature ships 3.45+, so this is safe.
pub(crate) fn migrate_oracle_events(conn: &Connection) -> anyhow::Result<()> {
    let cols: Vec<String> = conn
        .prepare("PRAGMA table_info(oracle_events)")?
        .query_map([], |row| row.get::<_, String>(1))?
        .filter_map(|r| r.ok())
        .collect();
    let has = |name: &str| cols.iter().any(|c| c == name);

    if has("ts") && !has("ts_utc") {
        conn.execute_batch("ALTER TABLE oracle_events RENAME COLUMN ts TO ts_utc;")?;
    }
    if !has("ts_ist") {
        conn.execute_batch(
            "ALTER TABLE oracle_events ADD COLUMN ts_ist TEXT NOT NULL DEFAULT '';"
        )?;
        // Backfill: convert ts_utc (UTC text) → IST "HH:MM:SS".
        // strftime returns NULL for unparseable input; coalesce keeps the row valid.
        conn.execute_batch(
            "UPDATE oracle_events
             SET ts_ist = COALESCE(
                 strftime('%H:%M:%S', ts_utc, '+5 hours', '+30 minutes'),
                 ''
             )
             WHERE ts_ist = '';"
        )?;
    }
    if !has("event_seq") {
        conn.execute_batch(
            "ALTER TABLE oracle_events ADD COLUMN event_seq INTEGER NOT NULL DEFAULT 0;"
        )?;
    }
    if !has("dispatch_decision") {
        conn.execute_batch(
            "ALTER TABLE oracle_events ADD COLUMN dispatch_decision TEXT NOT NULL DEFAULT 'PENDING';"
        )?;
    }
    // Index on event_seq is in the CREATE TABLE block for fresh installs;
    // ensure it exists on migrated databases too.
    conn.execute_batch(
        "CREATE INDEX IF NOT EXISTS idx_oracle_events_event_seq ON oracle_events(event_seq);"
    )?;
    Ok(())
}

/// Migration for `clob_orders` (B5):
///   * adds `correlation_id`, `purpose`, `replaces_order_id`
///   * ensures the supporting indexes exist on databases that pre-date them
///
/// Idempotent — safe to call on every `Db::open()`. Existing rows get
/// `correlation_id=''` and `purpose=''`; the live placement path
/// ([`Db::record_order_placement`]) populates these fields on every new
/// order written from strategy or maker.
pub(crate) fn migrate_clob_orders(conn: &Connection) -> anyhow::Result<()> {
    let cols: Vec<String> = conn
        .prepare("PRAGMA table_info(clob_orders)")?
        .query_map([], |row| row.get::<_, String>(1))?
        .filter_map(|r| r.ok())
        .collect();
    let has = |name: &str| cols.iter().any(|c| c == name);

    if !has("correlation_id") {
        conn.execute_batch(
            "ALTER TABLE clob_orders ADD COLUMN correlation_id TEXT NOT NULL DEFAULT '';"
        )?;
    }
    if !has("purpose") {
        conn.execute_batch(
            "ALTER TABLE clob_orders ADD COLUMN purpose TEXT NOT NULL DEFAULT '';"
        )?;
    }
    if !has("replaces_order_id") {
        // NULLable on purpose — only AUGMENT chains populate it.
        conn.execute_batch(
            "ALTER TABLE clob_orders ADD COLUMN replaces_order_id TEXT;"
        )?;
    }
    conn.execute_batch(
        "CREATE INDEX IF NOT EXISTS idx_clob_orders_correlation_id ON clob_orders(correlation_id);
         CREATE INDEX IF NOT EXISTS idx_clob_orders_replaces_order_id ON clob_orders(replaces_order_id);"
    )?;
    Ok(())
}

/// Migration for `trades` (L1): adds `fee TEXT NOT NULL DEFAULT '0'`. Idempotent.
/// Legacy rows backfill to `'0'`.
pub fn migrate_trades(conn: &Connection) -> anyhow::Result<()> {
    let cols: Vec<String> = conn
        .prepare("PRAGMA table_info(trades)")?
        .query_map([], |row| row.get::<_, String>(1))?
        .filter_map(|r| r.ok())
        .collect();
    if !cols.iter().any(|c| c == "fee") {
        conn.execute_batch(
            "ALTER TABLE trades ADD COLUMN fee TEXT NOT NULL DEFAULT '0';",
        )?;
    }
    Ok(())
}

/// Migration for `round_trips` (L2): adds `fee_in`, `fee_out`, `net_pnl`.
/// Backfills `net_pnl = pnl` on legacy rows so old data still aggregates
/// correctly. Idempotent.
pub fn migrate_round_trips(conn: &Connection) -> anyhow::Result<()> {
    let cols: Vec<String> = conn
        .prepare("PRAGMA table_info(round_trips)")?
        .query_map([], |row| row.get::<_, String>(1))?
        .filter_map(|r| r.ok())
        .collect();
    let has = |name: &str| cols.iter().any(|c| c == name);

    if !has("fee_in") {
        conn.execute_batch(
            "ALTER TABLE round_trips ADD COLUMN fee_in TEXT NOT NULL DEFAULT '0';",
        )?;
    }
    if !has("fee_out") {
        conn.execute_batch(
            "ALTER TABLE round_trips ADD COLUMN fee_out TEXT NOT NULL DEFAULT '0';",
        )?;
    }
    if !has("net_pnl") {
        conn.execute_batch(
            "ALTER TABLE round_trips ADD COLUMN net_pnl TEXT NOT NULL DEFAULT '0';",
        )?;
        // Backfill legacy rows: net_pnl = pnl when no fees were ever recorded.
        conn.execute_batch(
            "UPDATE round_trips SET net_pnl = pnl WHERE net_pnl = '0' OR net_pnl = '';",
        )?;
    }
    Ok(())
}
