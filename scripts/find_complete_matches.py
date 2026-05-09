"""Scan match_capture_*.db files and identify matches with full ball-by-ball
coverage: innings 1 → innings 2 end-state (10 wkts / chase complete / overs done).

Notes on the data:
- The `innings` column in cricket_events is unreliable (parser bug); both innings
  are usually labeled innings=1. We split innings ourselves by detecting the
  score reset to 0/0 (0.0) accompanied by a long time gap (innings break ~15-20m).
- Match-end criteria for innings 2:
    (a) wickets >= 10                       (all out)
    (b) runs >  innings1_final_runs         (chase complete, even mid-over)
    (c) overs reached innings1_final_overs  (matches scheduled overs; handles
        rain-curtailed games like the 11-over RR/MUM mention)
"""
from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

CAPTURES_DIR = Path("/Users/sobhagyaxd/DeepWork/totem-mm/captures")
IST = timezone(timedelta(hours=5, minutes=30))


def overs_to_balls(overs_str: str) -> int:
    """Cricket overs notation: '12.4' = 12 overs + 4 balls = 76 balls."""
    if overs_str is None:
        return 0
    try:
        whole, _, frac = str(overs_str).partition(".")
        return int(whole) * 6 + (int(frac) if frac else 0)
    except (ValueError, AttributeError):
        return 0


def ist(ts_ms: int | None) -> str:
    if ts_ms is None:
        return "-"
    return datetime.fromtimestamp(ts_ms / 1000, IST).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class InningsSummary:
    start_id: int
    end_id: int
    start_ts: int
    end_ts: int
    final_runs: int
    final_wkts: int
    final_overs: str
    final_balls: int
    event_count: int


@dataclass
class MatchReport:
    db: Path
    slug: str | None
    start_time_meta: str | None
    book_first_ts: int | None
    book_last_ts: int | None
    book_count: int
    trades_count: int
    events_count: int
    inn1: InningsSummary | None
    inn2: InningsSummary | None
    end_reason: str
    is_complete: bool
    notes: list[str]


def detect_innings_break(rows: list[tuple]) -> int | None:
    """Return the index in `rows` where innings 2 starts.

    Heuristic: score (runs, overs) regresses sharply AND a >= 5 minute gap
    sits between this row and the previous one. Innings breaks are typically
    15-20 minutes; even a strategic timeout doesn't take 5+ min between balls.

    rows: list of (id, ts_ms, runs, wkts, overs_str, score_str)
    """
    for i in range(1, len(rows)):
        prev_id, prev_ts, prev_runs, prev_wkts, prev_overs, _ = rows[i - 1]
        cur_id, cur_ts, cur_runs, cur_wkts, cur_overs, _ = rows[i]
        gap_sec = (cur_ts - prev_ts) / 1000.0
        prev_balls = overs_to_balls(prev_overs)
        cur_balls = overs_to_balls(cur_overs)
        # innings 2 starts at 0.0 or 0.1, runs reset to a small number, big gap
        if (
            gap_sec >= 300
            and cur_balls < prev_balls
            and cur_runs < prev_runs
            and cur_runs <= 10
        ):
            return i
    return None


def summarise_innings(rows: list[tuple]) -> InningsSummary:
    first = rows[0]
    last = rows[-1]
    return InningsSummary(
        start_id=first[0],
        end_id=last[0],
        start_ts=first[1],
        end_ts=last[1],
        final_runs=last[2],
        final_wkts=last[3],
        final_overs=last[4],
        final_balls=overs_to_balls(last[4]),
        event_count=len(rows),
    )


def classify_end(inn1: InningsSummary, inn2: InningsSummary) -> tuple[str, bool]:
    if inn2.final_wkts >= 10:
        return "innings2_all_out", True
    if inn2.final_runs > inn1.final_runs:
        return "innings2_chase_complete", True
    if inn2.final_balls >= inn1.final_balls and inn1.final_balls >= 6:
        # innings 2 reached innings 1's full overs (handles rain-curtailed)
        return "innings2_overs_complete", True
    return "innings2_truncated", False


def analyse_db(db_path: Path) -> MatchReport:
    notes: list[str] = []
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    meta = dict(cur.execute("SELECT key, value FROM match_meta").fetchall())
    slug = meta.get("slug")
    start_time = meta.get("start_time")

    book_first, book_last, book_count = cur.execute(
        "SELECT MIN(local_ts_ms), MAX(local_ts_ms), COUNT(*) FROM book_snapshots"
    ).fetchone()
    (trades_count,) = cur.execute("SELECT COUNT(*) FROM trades").fetchone()
    (events_count,) = cur.execute("SELECT COUNT(*) FROM cricket_events").fetchone()

    rows = cur.execute(
        """
        SELECT id, local_ts_ms, COALESCE(runs, 0), COALESCE(wickets, 0),
               COALESCE(overs, '0.0'), score_str
        FROM cricket_events
        ORDER BY id
        """
    ).fetchall()
    conn.close()

    if not rows:
        return MatchReport(
            db_path, slug, start_time, book_first, book_last, book_count,
            trades_count, events_count, None, None,
            "no_cricket_events", False, ["cricket_events table empty"],
        )

    split = detect_innings_break(rows)
    if split is None:
        # Either single innings only, or never finished. Treat all as innings 1.
        inn1 = summarise_innings(rows)
        notes.append("no innings 2 detected (no score reset + gap)")
        return MatchReport(
            db_path, slug, start_time, book_first, book_last, book_count,
            trades_count, events_count, inn1, None,
            "no_innings_2", False, notes,
        )

    inn1 = summarise_innings(rows[:split])
    inn2 = summarise_innings(rows[split:])
    end_reason, is_complete = classify_end(inn1, inn2)

    # Sanity flags
    if inn1.final_balls < 60:
        notes.append(f"innings1 short: only {inn1.final_balls} balls")
    if inn2.event_count < 50:
        notes.append(f"innings2 sparse: only {inn2.event_count} events")
    # Did book snapshots cover at least 60s past innings2 last event?
    if book_last is not None and book_last < inn2.end_ts - 60_000:
        notes.append("book_snapshots end before innings 2 final ball")
    if book_first is not None and book_first > inn1.start_ts + 60_000:
        notes.append("book_snapshots start after innings 1 first ball")

    return MatchReport(
        db_path, slug, start_time, book_first, book_last, book_count,
        trades_count, events_count, inn1, inn2, end_reason, is_complete, notes,
    )


def fmt_inn(inn: InningsSummary | None) -> str:
    if inn is None:
        return "—"
    return (
        f"{inn.final_runs}/{inn.final_wkts} ({inn.final_overs}) "
        f"[{inn.event_count} ev, {ist(inn.start_ts)} → {ist(inn.end_ts)}]"
    )


def main() -> int:
    dbs = sorted(CAPTURES_DIR.glob("match_capture_cricipl-*.db"))
    if not dbs:
        print(f"no match_capture_*.db files in {CAPTURES_DIR}", file=sys.stderr)
        return 1

    reports = [analyse_db(db) for db in dbs]
    complete = [r for r in reports if r.is_complete]
    incomplete = [r for r in reports if not r.is_complete]

    print(f"Scanned {len(reports)} match_capture DBs in {CAPTURES_DIR}")
    print(f"  COMPLETE (full ball-by-ball, valid end state): {len(complete)}")
    print(f"  INCOMPLETE: {len(incomplete)}")
    print()

    print("=" * 110)
    print("COMPLETE MATCHES")
    print("=" * 110)
    for r in complete:
        print(f"\n{r.slug}  ({r.db.name})")
        print(f"  meta start  : {r.start_time_meta}")
        print(f"  book span   : {ist(r.book_first_ts)} → {ist(r.book_last_ts)} "
              f"({r.book_count:,} snapshots, {r.trades_count:,} trades, {r.events_count} events)")
        print(f"  innings 1   : {fmt_inn(r.inn1)}")
        print(f"  innings 2   : {fmt_inn(r.inn2)}")
        print(f"  end reason  : {r.end_reason}")
        if r.notes:
            for n in r.notes:
                print(f"  note        : {n}")

    print()
    print("=" * 110)
    print("INCOMPLETE MATCHES")
    print("=" * 110)
    for r in incomplete:
        print(f"\n{r.slug}  ({r.db.name})")
        print(f"  meta start  : {r.start_time_meta}")
        print(f"  book span   : {ist(r.book_first_ts)} → {ist(r.book_last_ts)} "
              f"({r.book_count:,} snapshots, {r.trades_count:,} trades, {r.events_count} events)")
        print(f"  innings 1   : {fmt_inn(r.inn1)}")
        print(f"  innings 2   : {fmt_inn(r.inn2)}")
        print(f"  end reason  : {r.end_reason}")
        for n in r.notes:
            print(f"  note        : {n}")

    print()
    print("=" * 110)
    print(f"FINAL LIST OF COMPLETE MATCHES ({len(complete)}):")
    print("=" * 110)
    for r in complete:
        print(f"  {r.slug:<40}  inn1={r.inn1.final_runs}/{r.inn1.final_wkts}({r.inn1.final_overs})  "
              f"inn2={r.inn2.final_runs}/{r.inn2.final_wkts}({r.inn2.final_overs})  "
              f"[{r.end_reason}]")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
