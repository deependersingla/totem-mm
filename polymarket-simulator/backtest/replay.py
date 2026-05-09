"""SQLite capture → time-ordered event stream.

`stream_events(db_path)` yields BookEvent and TradeEvent in strict ts_ms
order. CricketEvent is opt-in via `include_cricket=True` (default off — cricket
signals in our captures are 25-30s late vs the market and are not used in
backtests by default).

Capture schema (captures/match_capture_*.db):
  match_meta(key, value)        — slug, condition_id, token_ids (JSON), outcome_names
  book_snapshots(local_ts_ms, asset_id, bid1_p..bid5_p, bid1_s..bid5_s, ...)
  trades(clob_ts_ms, local_ts_ms, asset_id, side, price, size, taker_wallet,
         transaction_hash, fee_rate_bps)
  cricket_events(local_ts_ms, signal_type, runs, wickets, overs, score_str, innings)

Time-base note: trades carry both a CLOB-server timestamp (clob_ts_ms,
authoritative) and a capture-host timestamp (local_ts_ms). For most trades
they are within a second; for trades retrieved by REST catch-up after a
WebSocket gap, local_ts_ms can lag clob_ts_ms by minutes to days. We use
clob_ts_ms as the event time AND drop trades whose lag exceeds
config.MAX_TRADE_CAPTURE_LAG_MS, because their associated book state was
not captured live. See the bug-1 writeup for details.
"""

from __future__ import annotations

import heapq
import json
import sqlite3
from pathlib import Path
from typing import Iterator, Optional

from .book import BookSnapshot, PriceLevel
from .config import MAX_TRADE_CAPTURE_LAG_MS
from .enums import CricketSignal, MarketCategory, Side
from .events import BookEvent, CricketEvent, Event, TradeEvent
from .market import Market


# ── Market loading ───────────────────────────────────────────────────


def load_market(db_path: str | Path, category: MarketCategory = MarketCategory.SPORTS) -> Market:
    conn = sqlite3.connect(str(db_path))
    try:
        kv = dict(conn.execute("SELECT key, value FROM match_meta").fetchall())
    finally:
        conn.close()

    token_ids = tuple(json.loads(kv.get("token_ids", "[]")))
    outcome_names = tuple(json.loads(kv.get("outcome_names", "[]")))
    if len(token_ids) != 2 or len(outcome_names) != 2:
        raise ValueError(f"capture {db_path} is not a binary market")

    return Market(
        slug=kv.get("slug", ""),
        condition_id=kv.get("condition_id", ""),
        token_ids=token_ids,
        outcome_names=outcome_names,
        category=category,
    )


# ── Row → event builders ─────────────────────────────────────────────


def _book_row_to_event(row: sqlite3.Row) -> BookEvent:
    bids: list[PriceLevel] = []
    asks: list[PriceLevel] = []
    for i in range(1, 6):
        bp, bs = row[f"bid{i}_p"], row[f"bid{i}_s"]
        if bp is not None and bs is not None and bs > 0:
            bids.append(PriceLevel(price=float(bp), size=float(bs)))
        ap, asz = row[f"ask{i}_p"], row[f"ask{i}_s"]
        if ap is not None and asz is not None and asz > 0:
            asks.append(PriceLevel(price=float(ap), size=float(asz)))
    bids.sort(key=lambda lv: -lv.price)
    asks.sort(key=lambda lv: lv.price)
    snap = BookSnapshot(
        token_id=row["asset_id"], ts_ms=int(row["local_ts_ms"]),
        bids=tuple(bids), asks=tuple(asks),
    )
    return BookEvent(snapshot=snap)


def _trade_row_to_event(row: sqlite3.Row) -> TradeEvent:
    raw_bps = row["fee_rate_bps"]
    captured_bps: int | None = None
    if raw_bps not in (None, "", "None"):
        try:
            captured_bps = int(raw_bps)
        except (TypeError, ValueError):
            captured_bps = None
    return TradeEvent(
        token_id=row["asset_id"],
        ts_ms=int(row["clob_ts_ms"]),
        side=Side(row["side"]),
        price=float(row["price"]),
        size_shares=float(row["size"]),
        taker_wallet=row["taker_wallet"] or "",
        tx_hash=row["transaction_hash"] or "",
        captured_fee_rate_bps=captured_bps,
    )


def _cricket_row_to_event(row: sqlite3.Row) -> CricketEvent:
    return CricketEvent(
        ts_ms=int(row["local_ts_ms"]),
        signal=CricketSignal.parse(row["signal_type"] or "?"),
        runs=row["runs"], wickets=row["wickets"],
        overs=row["overs"] or "", score_str=row["score_str"] or "",
        innings=row["innings"],
    )


# ── Event stream ─────────────────────────────────────────────────────


def stream_events(
    db_path: str | Path,
    *,
    start_ts_ms: int | None = None,
    end_ts_ms: int | None = None,
    include_cricket: bool = False,
    cricket_lead_ms: int = 0,
    cricket_lead_ms_by_signal: Optional[dict[str, int]] = None,
    max_trade_lag_ms: int = MAX_TRADE_CAPTURE_LAG_MS,
) -> Iterator[Event]:
    """Yield events in strict ts_ms order.

    `include_cricket=False` (default): only BookEvent + TradeEvent are emitted.
    `cricket_lead_ms`: subtract this from each cricket event's ts_ms to
    simulate a faster signal feed. e.g. our captured cricket is ~25s late
    vs a fast TV/CricBuzz feed, so set this to 25000 to model "I would
    have seen this 25s earlier in production."
    `cricket_lead_ms_by_signal`: optional per-signal-type override map,
    e.g. {"4": 30000, "6": 30000, "W": 40000}. Falls back to cricket_lead_ms
    for any signal type not in the map.
    `max_trade_lag_ms`: drop trades where (local_ts_ms − clob_ts_ms) exceeds
    this — those are catch-up / pre-capture trades with no live book state.
    Set to None to disable filtering.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        ranges_local = _range_clause("local_ts_ms", start_ts_ms, end_ts_ms)
        ranges_clob = _range_clause("clob_ts_ms", start_ts_ms, end_ts_ms)

        book_iter = iter(conn.execute(
            f"SELECT * FROM book_snapshots {ranges_local} ORDER BY local_ts_ms ASC",
        ))

        trade_where = _trade_where_clause(
            start_ts_ms=start_ts_ms, end_ts_ms=end_ts_ms,
            max_lag_ms=max_trade_lag_ms,
        )
        trade_iter = iter(conn.execute(
            f"SELECT * FROM trades {trade_where} ORDER BY clob_ts_ms ASC",
        ))

        if include_cricket:
            crk_iter: Optional[Iterator] = iter(conn.execute(
                f"SELECT * FROM cricket_events {ranges_local} ORDER BY local_ts_ms ASC",
            ))
        else:
            crk_iter = None

        def _cricket_offset(row) -> int:
            if cricket_lead_ms_by_signal:
                st = row["signal_type"] or ""
                return cricket_lead_ms_by_signal.get(st, cricket_lead_ms)
            return cricket_lead_ms

        def _cricket_builder(row, offset: int):
            evt = _cricket_row_to_event(row)
            if offset > 0:
                evt.ts_ms -= offset
            return evt

        heap: list = []
        seq_counter = iter(range(1 << 31))

        def _push_next(it, kind_pri: int, ts_field: str, builder, ts_offset: int = 0):
            if it is None:
                return
            row = next(it, None)
            if row is None:
                return
            if kind_pri == 2:  # cricket — per-row offset
                ts_offset = _cricket_offset(row)
            ts = int(row[ts_field]) - ts_offset
            heapq.heappush(heap, (ts, kind_pri, next(seq_counter), builder, row, it, ts_field, ts_offset))

        _push_next(book_iter, 0, "local_ts_ms", _book_row_to_event)
        _push_next(trade_iter, 1, "clob_ts_ms", _trade_row_to_event)
        _push_next(crk_iter, 2, "local_ts_ms", _cricket_builder, ts_offset=cricket_lead_ms)

        while heap:
            ts, _kind, _seq, builder, row, it, ts_field, ts_offset = heapq.heappop(heap)
            if _kind == 2:
                yield builder(row, ts_offset)
            else:
                yield builder(row)
            _push_next(it, _kind, ts_field, builder, ts_offset=ts_offset)
    finally:
        conn.close()


def _range_clause(field: str, start: int | None, end: int | None) -> str:
    parts = []
    if start is not None:
        parts.append(f"{field} >= {int(start)}")
    if end is not None:
        parts.append(f"{field} <= {int(end)}")
    return f"WHERE {' AND '.join(parts)}" if parts else ""


def _trade_where_clause(
    *, start_ts_ms: int | None, end_ts_ms: int | None, max_lag_ms: int | None,
) -> str:
    parts = ["clob_ts_ms IS NOT NULL"]
    if start_ts_ms is not None:
        parts.append(f"clob_ts_ms >= {int(start_ts_ms)}")
    if end_ts_ms is not None:
        parts.append(f"clob_ts_ms <= {int(end_ts_ms)}")
    if max_lag_ms is not None:
        # Only keep trades observed within max_lag of when they happened.
        # `local_ts_ms` may be NULL on a few rows; treat NULL as "good enough"
        # so we don't accidentally drop everything.
        parts.append(
            f"(local_ts_ms IS NULL OR (local_ts_ms - clob_ts_ms) <= {int(max_lag_ms)})"
        )
    return f"WHERE {' AND '.join(parts)}" if parts else ""


# ── Inspection helpers ───────────────────────────────────────────────


def trade_lag_stats(db_path: str | Path) -> dict:
    """Diagnostic — return lag distribution for trades in this capture.

    Useful before running a backtest to see how clean the capture is.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT clob_ts_ms, local_ts_ms FROM trades "
            "WHERE clob_ts_ms IS NOT NULL AND local_ts_ms IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return {"count": 0}
    lags = [(local - clob) for clob, local in rows]
    n = len(lags)
    return {
        "count": n,
        "live_pct": 100.0 * sum(1 for l in lags if l <= 1_000) / n,
        "catchup_count": sum(1 for l in lags if l > 60_000),
        "catchup_pct": 100.0 * sum(1 for l in lags if l > 60_000) / n,
        "max_lag_s": max(lags) / 1000.0,
    }
