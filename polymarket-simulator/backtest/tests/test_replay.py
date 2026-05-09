"""Replay stream filtering + cricket-default-off tests."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from backtest.events import BookEvent, CricketEvent, TradeEvent
from backtest.replay import load_market, stream_events, trade_lag_stats


def _make_capture(path: Path) -> Path:
    """Build a synthetic capture DB with controlled timestamps."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript("""
            CREATE TABLE match_meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE book_snapshots (
                id INTEGER PRIMARY KEY, local_ts_ms INTEGER NOT NULL,
                asset_id TEXT NOT NULL,
                bid1_p REAL, bid1_s REAL, bid2_p REAL, bid2_s REAL,
                bid3_p REAL, bid3_s REAL, bid4_p REAL, bid4_s REAL,
                bid5_p REAL, bid5_s REAL,
                ask1_p REAL, ask1_s REAL, ask2_p REAL, ask2_s REAL,
                ask3_p REAL, ask3_s REAL, ask4_p REAL, ask4_s REAL,
                ask5_p REAL, ask5_s REAL
            );
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY, clob_ts_ms INTEGER, local_ts_ms INTEGER,
                asset_id TEXT, side TEXT, price REAL, size REAL,
                taker_wallet TEXT, transaction_hash TEXT, fee_rate_bps INTEGER
            );
            CREATE TABLE cricket_events (
                id INTEGER PRIMARY KEY, local_ts_ms INTEGER NOT NULL,
                signal_type TEXT, runs INTEGER, wickets INTEGER,
                overs TEXT, score_str TEXT, innings INTEGER
            );
        """)
        conn.executemany(
            "INSERT INTO match_meta(key, value) VALUES (?, ?)",
            [
                ("slug", "test"), ("condition_id", "c"),
                ("token_ids", '["A","B"]'), ("outcome_names", '["Yes","No"]'),
            ],
        )
        # 2 book snapshots
        conn.execute(
            "INSERT INTO book_snapshots(local_ts_ms, asset_id, bid1_p, bid1_s, ask1_p, ask1_s) "
            "VALUES (1000, 'A', 0.50, 100, 0.51, 80)"
        )
        conn.execute(
            "INSERT INTO book_snapshots(local_ts_ms, asset_id, bid1_p, bid1_s, ask1_p, ask1_s) "
            "VALUES (2000, 'A', 0.50, 90,  0.51, 70)"
        )
        # 1 live trade (lag 100ms)  +  1 catch-up trade (lag 5 minutes)
        conn.executemany(
            "INSERT INTO trades(clob_ts_ms, local_ts_ms, asset_id, side, price, size, "
            "taker_wallet, transaction_hash, fee_rate_bps) VALUES (?, ?, 'A', ?, ?, ?, '', ?, 300)",
            [
                (1500, 1600, "BUY",  0.51, 10, "h_live"),     # 100ms lag → keep
                (1700, 301700, "SELL", 0.50, 20, "h_catchup"), # 5min lag → drop
            ],
        )
        # 1 cricket event
        conn.execute(
            "INSERT INTO cricket_events(local_ts_ms, signal_type, runs, score_str, innings) "
            "VALUES (1800, 'W', 0, '5/1', 1)"
        )
        conn.commit()
    finally:
        conn.close()
    return path


def test_load_market_basic(tmp_path):
    db = _make_capture(tmp_path / "cap.db")
    m = load_market(db)
    assert m.slug == "test"
    assert m.token_ids == ("A", "B")


def test_stream_excludes_cricket_by_default(tmp_path):
    db = _make_capture(tmp_path / "cap.db")
    events = list(stream_events(db))
    types = [type(e).__name__ for e in events]
    assert "CricketEvent" not in types


def test_stream_includes_cricket_when_opted_in(tmp_path):
    db = _make_capture(tmp_path / "cap.db")
    events = list(stream_events(db, include_cricket=True))
    types = [type(e).__name__ for e in events]
    assert "CricketEvent" in types


def test_catchup_trades_are_dropped(tmp_path):
    db = _make_capture(tmp_path / "cap.db")
    events = list(stream_events(db))
    trades = [e for e in events if isinstance(e, TradeEvent)]
    assert len(trades) == 1
    assert trades[0].tx_hash == "h_live"


def test_catchup_filter_disabled_keeps_everything(tmp_path):
    db = _make_capture(tmp_path / "cap.db")
    events = list(stream_events(db, max_trade_lag_ms=None))
    trades = [e for e in events if isinstance(e, TradeEvent)]
    assert len(trades) == 2


def test_trade_uses_clob_ts(tmp_path):
    db = _make_capture(tmp_path / "cap.db")
    trades = [e for e in stream_events(db) if isinstance(e, TradeEvent)]
    assert trades[0].ts_ms == 1500   # clob_ts, not local_ts (1600)


def test_strict_time_order_book_then_trade(tmp_path):
    db = _make_capture(tmp_path / "cap.db")
    events = list(stream_events(db))
    timestamps = [e.snapshot.ts_ms if isinstance(e, BookEvent) else e.ts_ms for e in events]
    assert timestamps == sorted(timestamps)


def test_trade_lag_stats(tmp_path):
    db = _make_capture(tmp_path / "cap.db")
    stats = trade_lag_stats(db)
    assert stats["count"] == 2
    assert stats["catchup_count"] == 1
    assert stats["live_pct"] == 50.0
