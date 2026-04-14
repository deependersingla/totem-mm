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
    pub pnl: String,         // exit_proceeds - entry_cost
    pub label: String,       // WICKET, RUN4, etc.
    pub entry_order_id: String,
    pub exit_order_id: String,
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
        let conn = Connection::open(DB_FILE)?;
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
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS oracle_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL DEFAULT (datetime('now')),
                signal TEXT NOT NULL,
                source TEXT NOT NULL,
                innings INTEGER NOT NULL,
                batting TEXT NOT NULL,
                bowling TEXT NOT NULL,
                slug TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_oracle_events_slug ON oracle_events(slug);",
        )?;

        Ok(Self { conn: Mutex::new(conn) })
    }

    // ── Trades ────────────────────────────────────────────────────────────

    pub fn insert_trade(
        &self, ts: &str, side: &str, team: &str, size: &str,
        price: &str, cost: &str, order_type: &str, label: &str,
        order_id: &str, slug: &str,
    ) {
        let conn = self.conn.lock().unwrap();
        if let Err(e) = conn.execute(
            "INSERT INTO trades (ts, side, team, size, price, cost, order_type, label, order_id, slug)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
            params![ts, side, team, size, price, cost, order_type, label, order_id, slug],
        ) {
            tracing::warn!(error = %e, "db: failed to insert trade");
        }
    }

    pub fn get_trades(&self, slug: &str, limit: usize) -> Vec<DbTrade> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT id, ts, side, team, size, price, cost, order_type, label, order_id, slug
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
            })
        }).unwrap().filter_map(|r| r.ok()).collect()
    }

    // ── Round-trips ───────────────────────────────────────────────────────

    pub fn insert_round_trip(
        &self, entry_ts: &str, exit_ts: &str, team: &str, entry_side: &str,
        entry_price: &str, exit_price: &str, size: &str, pnl: &str,
        label: &str, entry_order_id: &str, exit_order_id: &str, slug: &str,
    ) {
        let conn = self.conn.lock().unwrap();
        if let Err(e) = conn.execute(
            "INSERT INTO round_trips (entry_ts, exit_ts, team, entry_side, entry_price, exit_price, size, pnl, label, entry_order_id, exit_order_id, slug)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)",
            params![entry_ts, exit_ts, team, entry_side, entry_price, exit_price, size, pnl, label, entry_order_id, exit_order_id, slug],
        ) {
            tracing::warn!(error = %e, "db: failed to insert round_trip");
        }
    }

    pub fn get_round_trips(&self, slug: &str, limit: usize) -> Vec<RoundTrip> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT id, entry_ts, exit_ts, team, entry_side, entry_price, exit_price, size, pnl, label, entry_order_id, exit_order_id
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
            })
        }).unwrap().filter_map(|r| r.ok()).collect()
    }

    pub fn total_pnl(&self, slug: &str) -> Decimal {
        let conn = self.conn.lock().unwrap();
        let result: String = conn.query_row(
            "SELECT COALESCE(SUM(CAST(pnl AS REAL)), 0) FROM round_trips WHERE slug = ?1 OR ?1 = ''",
            params![slug],
            |row| row.get(0),
        ).unwrap_or_else(|_| "0".to_string());
        result.parse().unwrap_or(Decimal::ZERO)
    }

    // ── CLOB orders ───────────────────────────────────────────────────────

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
        let mut stmt = conn.prepare(
            "SELECT order_id, asset_id, side, price, original_size, size_matched, status, order_type, created_at, team
             FROM clob_orders WHERE (slug = ?1 OR ?1 = '') AND status IN ('live', 'delayed', 'open')
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
            "INSERT INTO oracle_events (signal, source, innings, batting, bowling, slug)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![evt.signal, evt.source, evt.innings, evt.batting, evt.bowling, slug],
        ) {
            tracing::warn!(error = %e, "db: failed to insert oracle_event");
        }
    }
}
