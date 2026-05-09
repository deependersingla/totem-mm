//! Tests for `db.rs` — focused on the `oracle_events` migration introduced
//! in B3 (TODO.md). The migration is idempotent and must:
//!   * rename legacy `ts` → `ts_utc`
//!   * add `ts_ist`, `event_seq`, `dispatch_decision`
//!   * backfill `ts_ist` from `ts_utc` (UTC + 5h30m → IST "HH:MM:SS")
//!   * leave already-migrated databases unchanged on a second run
//!   * not corrupt fresh databases

use rusqlite::Connection;

use crate::db::{migrate_clob_orders, migrate_oracle_events};

/// Old schema as it existed before B3. Used to seed a legacy DB for the
/// migration test.
const LEGACY_SCHEMA: &str = "CREATE TABLE oracle_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    signal TEXT NOT NULL,
    source TEXT NOT NULL,
    innings INTEGER NOT NULL,
    batting TEXT NOT NULL,
    bowling TEXT NOT NULL,
    slug TEXT NOT NULL DEFAULT ''
);";

fn columns(conn: &Connection) -> Vec<String> {
    conn.prepare("PRAGMA table_info(oracle_events)")
        .unwrap()
        .query_map([], |row| row.get::<_, String>(1))
        .unwrap()
        .filter_map(|r| r.ok())
        .collect()
}

#[test]
fn migration_renames_ts_and_adds_new_columns() {
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch(LEGACY_SCHEMA).unwrap();

    // Seed one legacy row at a known UTC time so the IST backfill is checkable.
    conn.execute(
        "INSERT INTO oracle_events (ts, signal, source, innings, batting, bowling, slug)
         VALUES ('2025-04-01 12:00:00', 'W', 'telegram', 1, 'A', 'B', 'test-slug')",
        [],
    ).unwrap();

    migrate_oracle_events(&conn).unwrap();

    let cols = columns(&conn);
    assert!(cols.contains(&"ts_utc".to_string()), "ts_utc missing: {cols:?}");
    assert!(!cols.contains(&"ts".to_string()), "legacy ts column should be gone: {cols:?}");
    assert!(cols.contains(&"ts_ist".to_string()));
    assert!(cols.contains(&"event_seq".to_string()));
    assert!(cols.contains(&"dispatch_decision".to_string()));

    let (ts_utc, ts_ist, event_seq, decision): (String, String, i64, String) = conn
        .query_row(
            "SELECT ts_utc, ts_ist, event_seq, dispatch_decision FROM oracle_events",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )
        .unwrap();
    assert_eq!(ts_utc, "2025-04-01 12:00:00");
    // 12:00:00 UTC + 5h30m = 17:30:00 IST
    assert_eq!(ts_ist, "17:30:00");
    assert_eq!(event_seq, 0);
    assert_eq!(decision, "PENDING");
}

#[test]
fn migration_is_idempotent() {
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch(LEGACY_SCHEMA).unwrap();
    conn.execute(
        "INSERT INTO oracle_events (ts, signal, source, innings, batting, bowling)
         VALUES ('2025-04-01 12:00:00', '6', 'ui', 2, 'A', 'B')",
        [],
    ).unwrap();

    migrate_oracle_events(&conn).unwrap();
    let after_first = columns(&conn);

    // Second run must succeed and leave the schema identical.
    migrate_oracle_events(&conn).unwrap();
    let after_second = columns(&conn);
    assert_eq!(after_first, after_second);

    let n: i64 = conn
        .query_row("SELECT COUNT(*) FROM oracle_events", [], |r| r.get(0))
        .unwrap();
    assert_eq!(n, 1, "row count must not change");
}

// ── clob_orders migration (B5) ──────────────────────────────────────────────

const LEGACY_CLOB_ORDERS_SCHEMA: &str = "CREATE TABLE clob_orders (
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
);";

fn clob_columns(conn: &Connection) -> Vec<String> {
    conn.prepare("PRAGMA table_info(clob_orders)")
        .unwrap()
        .query_map([], |row| row.get::<_, String>(1))
        .unwrap()
        .filter_map(|r| r.ok())
        .collect()
}

#[test]
fn clob_orders_migration_adds_b5_columns() {
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch(LEGACY_CLOB_ORDERS_SCHEMA).unwrap();
    conn.execute(
        "INSERT INTO clob_orders
         (order_id, asset_id, side, price, original_size, status)
         VALUES ('legacy-1', 'tok-A', 'BUY', '0.5', '10', 'live')",
        [],
    ).unwrap();

    migrate_clob_orders(&conn).unwrap();

    let cols = clob_columns(&conn);
    assert!(cols.contains(&"correlation_id".to_string()), "missing: {cols:?}");
    assert!(cols.contains(&"purpose".to_string()));
    assert!(cols.contains(&"replaces_order_id".to_string()));

    // Legacy rows pick up the empty defaults.
    let (corr, purpose, replaces): (String, String, Option<String>) = conn
        .query_row(
            "SELECT correlation_id, purpose, replaces_order_id FROM clob_orders WHERE order_id = 'legacy-1'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .unwrap();
    assert_eq!(corr, "");
    assert_eq!(purpose, "");
    assert_eq!(replaces, None);
}

#[test]
fn clob_orders_migration_is_idempotent() {
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch(LEGACY_CLOB_ORDERS_SCHEMA).unwrap();
    migrate_clob_orders(&conn).unwrap();
    let after_first = clob_columns(&conn);
    migrate_clob_orders(&conn).unwrap();
    let after_second = clob_columns(&conn);
    assert_eq!(after_first, after_second);
}

#[test]
fn record_order_placement_roundtrips_metadata() {
    use crate::db::{Db, OrderPlacement};
    use std::path::PathBuf;

    // Throwaway file under temp dir; cleaned up at end of test.
    let mut path = std::env::temp_dir();
    path.push(format!(
        "totem_taker_test_{}_{}.db",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()
    ));
    let _ = std::fs::remove_file(&path);

    struct Cleanup(PathBuf);
    impl Drop for Cleanup {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.0);
            let _ = std::fs::remove_file(self.0.with_extension("db-wal"));
            let _ = std::fs::remove_file(self.0.with_extension("db-shm"));
        }
    }
    let _cleanup = Cleanup(path.clone());

    let db = Db::open_at(&path).unwrap();

    db.record_order_placement(&OrderPlacement {
        order_id: "ord-1",
        slug: "match-1",
        asset_id: "tok-A",
        side: "BUY",
        price: "0.5",
        original_size: "10",
        status: "live",
        order_type: "FAK",
        created_at: "12:34:56",
        team: "IND",
        correlation_id: "42-W",
        purpose: "FAK_BUY",
        replaces_order_id: None,
    });

    // AUGMENT: replaces ord-1 with ord-2 under same correlation_id
    db.record_order_placement(&OrderPlacement {
        order_id: "ord-2",
        slug: "match-1",
        asset_id: "tok-A",
        side: "SELL",
        price: "0.78",
        original_size: "10",
        status: "live",
        order_type: "GTC",
        created_at: "12:35:00",
        team: "IND",
        correlation_id: "42-W",
        purpose: "REVERT_GTC",
        replaces_order_id: Some("ord-1"),
    });

    // Verify acceptance criterion: SELECT WHERE correlation_id returns both.
    let conn = Connection::open(&path).unwrap();
    let n: i64 = conn.query_row(
        "SELECT COUNT(*) FROM clob_orders WHERE correlation_id = '42-W'", [], |r| r.get(0),
    ).unwrap();
    assert_eq!(n, 2);

    let replaces: Option<String> = conn.query_row(
        "SELECT replaces_order_id FROM clob_orders WHERE order_id = 'ord-2'", [], |r| r.get(0),
    ).unwrap();
    assert_eq!(replaces, Some("ord-1".to_string()));
}

#[test]
fn user_fills_insert_and_correlation_lookup() {
    use crate::db::{Db, OrderPlacement, UserFill};
    use std::path::PathBuf;

    let mut path = std::env::temp_dir();
    path.push(format!(
        "totem_taker_test_uf_{}_{}.db",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()
    ));
    let _ = std::fs::remove_file(&path);
    struct Cleanup(PathBuf);
    impl Drop for Cleanup {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.0);
            let _ = std::fs::remove_file(self.0.with_extension("db-wal"));
            let _ = std::fs::remove_file(self.0.with_extension("db-shm"));
        }
    }
    let _cleanup = Cleanup(path.clone());

    let db = Db::open_at(&path).unwrap();

    // First record an order so the lookup has a correlation_id to find.
    db.record_order_placement(&OrderPlacement {
        order_id: "ord-9",
        slug: "match-1",
        asset_id: "tok-A",
        side: "SELL",
        price: "0.78",
        original_size: "100",
        status: "live",
        order_type: "GTC",
        created_at: "12:00:00",
        team: "IND",
        correlation_id: "99-W",
        purpose: "REVERT_GTC",
        replaces_order_id: None,
    });
    assert_eq!(db.lookup_correlation_id("ord-9"), Some("99-W".to_string()));
    assert_eq!(db.lookup_correlation_id("ord-missing"), None);

    // Insert several partial fills against the same order. All four rows
    // should be queryable individually — that is the whole point of B6.
    for (i, sz) in ["5", "15", "20", "60"].iter().enumerate() {
        db.insert_user_fill(&UserFill {
            ts_ist: "12:00:01",
            polymarket_trade_id: Some(&format!("tr-{i}")),
            order_id: "ord-9",
            correlation_id: "99-W",
            asset_id: "tok-A",
            team: "IND",
            side: "SELL",
            size: sz,
            price: "0.78",
            status: "MATCHED",
            slug: "match-1",
            raw_json: r#"{"event_type":"trade"}"#,
        });
    }

    let conn = rusqlite::Connection::open(&path).unwrap();
    let n: i64 = conn
        .query_row("SELECT COUNT(*) FROM user_fills WHERE order_id = 'ord-9'", [], |r| r.get(0))
        .unwrap();
    assert_eq!(n, 4, "all four partial fills must be persisted");

    let total: f64 = conn
        .query_row(
            "SELECT COALESCE(SUM(CAST(size AS REAL)), 0) FROM user_fills WHERE correlation_id = '99-W'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert!((total - 100.0).abs() < 1e-9, "size sum reconstructable: {total}");
}

// ── trades.fee migration (L1) + round_trips fee/net_pnl (L2) ────────────────

const LEGACY_TRADES_SCHEMA: &str = "CREATE TABLE trades (
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
);";

const LEGACY_ROUND_TRIPS_SCHEMA: &str = "CREATE TABLE round_trips (
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
);";

fn cols(conn: &Connection, table: &str) -> Vec<String> {
    conn.prepare(&format!("PRAGMA table_info({table})"))
        .unwrap()
        .query_map([], |row| row.get::<_, String>(1))
        .unwrap()
        .filter_map(|r| r.ok())
        .collect()
}

#[test]
fn trades_migration_adds_fee_column() {
    use crate::db::migrate_trades;
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch(LEGACY_TRADES_SCHEMA).unwrap();
    conn.execute(
        "INSERT INTO trades (ts, side, team, size, price, cost, order_type, label, order_id)
         VALUES ('12:00:00', 'BUY', 'IND', '10', '0.6', '6', 'FAK', 'WICKET', 'ord-1')",
        [],
    ).unwrap();

    migrate_trades(&conn).unwrap();

    let c = cols(&conn, "trades");
    assert!(c.contains(&"fee".to_string()), "fee missing: {c:?}");

    let fee: String = conn.query_row(
        "SELECT fee FROM trades WHERE order_id = 'ord-1'", [], |r| r.get(0),
    ).unwrap();
    assert_eq!(fee, "0", "legacy rows backfill to '0'");
}

#[test]
fn trades_migration_idempotent() {
    use crate::db::migrate_trades;
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch(LEGACY_TRADES_SCHEMA).unwrap();
    migrate_trades(&conn).unwrap();
    let after_first = cols(&conn, "trades");
    migrate_trades(&conn).unwrap();
    let after_second = cols(&conn, "trades");
    assert_eq!(after_first, after_second);
}

#[test]
fn round_trips_migration_adds_fee_and_net_pnl_columns() {
    use crate::db::migrate_round_trips;
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch(LEGACY_ROUND_TRIPS_SCHEMA).unwrap();
    conn.execute(
        "INSERT INTO round_trips (entry_ts, exit_ts, team, entry_side, entry_price, exit_price, size, pnl, label, entry_order_id, exit_order_id)
         VALUES ('12:00:00', '12:00:30', 'IND', 'BUY', '0.6', '0.62', '10', '0.20', 'WICKET', 'ord-1', 'ord-2')",
        [],
    ).unwrap();

    migrate_round_trips(&conn).unwrap();

    let c = cols(&conn, "round_trips");
    assert!(c.contains(&"fee_in".to_string()), "fee_in missing: {c:?}");
    assert!(c.contains(&"fee_out".to_string()));
    assert!(c.contains(&"net_pnl".to_string()));

    let (fi, fo, np, pnl): (String, String, String, String) = conn.query_row(
        "SELECT fee_in, fee_out, net_pnl, pnl FROM round_trips WHERE entry_order_id = 'ord-1'",
        [], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
    ).unwrap();
    assert_eq!(fi, "0");
    assert_eq!(fo, "0");
    // net_pnl on legacy rows backfills to == pnl
    assert_eq!(np, pnl);
}

#[test]
fn round_trips_migration_idempotent() {
    use crate::db::migrate_round_trips;
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch(LEGACY_ROUND_TRIPS_SCHEMA).unwrap();
    migrate_round_trips(&conn).unwrap();
    let after_first = cols(&conn, "round_trips");
    migrate_round_trips(&conn).unwrap();
    let after_second = cols(&conn, "round_trips");
    assert_eq!(after_first, after_second);
}

#[test]
fn insert_trade_persists_fee_column() {
    use crate::db::Db;
    use std::path::PathBuf;
    let mut path = std::env::temp_dir();
    path.push(format!(
        "totem_trades_fee_{}_{}.db",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()
    ));
    let _ = std::fs::remove_file(&path);
    struct Cleanup(PathBuf);
    impl Drop for Cleanup {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.0);
            let _ = std::fs::remove_file(self.0.with_extension("db-wal"));
            let _ = std::fs::remove_file(self.0.with_extension("db-shm"));
        }
    }
    let _c = Cleanup(path.clone());

    let db = Db::open_at(&path).unwrap();
    db.insert_trade(
        "12:00:00", "BUY", "IND", "10", "0.6", "6",
        "FAK", "WICKET", "ord-1", "match-1", "0.0432",
    );

    let conn = Connection::open(&path).unwrap();
    let fee: String = conn.query_row(
        "SELECT fee FROM trades WHERE order_id = 'ord-1'", [], |r| r.get(0),
    ).unwrap();
    assert_eq!(fee, "0.0432");

    let total = db.total_fee_paid("match-1");
    assert!(total > rust_decimal::Decimal::ZERO);
}

#[test]
fn total_fee_paid_aggregates_across_trades() {
    use crate::db::Db;
    use std::path::PathBuf;
    let mut path = std::env::temp_dir();
    path.push(format!(
        "totem_total_fee_{}_{}.db",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()
    ));
    let _ = std::fs::remove_file(&path);
    struct Cleanup(PathBuf);
    impl Drop for Cleanup {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.0);
            let _ = std::fs::remove_file(self.0.with_extension("db-wal"));
            let _ = std::fs::remove_file(self.0.with_extension("db-shm"));
        }
    }
    let _c = Cleanup(path.clone());
    let db = Db::open_at(&path).unwrap();

    db.insert_trade("12:00:00", "BUY", "IND", "10", "0.6", "6", "FAK", "WICKET", "o1", "m", "0.04");
    db.insert_trade("12:00:01", "SELL", "ENG", "10", "0.4", "4", "FAK", "WICKET", "o2", "m", "0.03");
    db.insert_trade("12:00:30", "BUY", "ENG", "10", "0.42", "4.2", "GTC", "REVERT", "o3", "m", "0");

    let total = db.total_fee_paid("m");
    assert_eq!(total, rust_decimal_macros::dec!(0.07));
}

#[test]
fn migration_noop_on_already_new_schema() {
    // Simulates a fresh install: the table is created with the new schema
    // by `Db::open()`; running the migration helper afterward must be safe.
    let conn = Connection::open_in_memory().unwrap();
    conn.execute_batch(
        "CREATE TABLE oracle_events (
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
        );"
    ).unwrap();

    migrate_oracle_events(&conn).unwrap();

    let cols = columns(&conn);
    assert!(cols.contains(&"ts_utc".to_string()));
    assert!(cols.contains(&"ts_ist".to_string()));
    assert!(cols.contains(&"event_seq".to_string()));
    assert!(cols.contains(&"dispatch_decision".to_string()));
}
