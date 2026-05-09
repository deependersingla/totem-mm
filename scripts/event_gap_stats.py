"""Per-match cricket_event gap statistics for the 17 complete matches.

For each qualifying match prints:
  - total events, innings 1 events, innings 2 events
  - first event's overs (does it start at 0.1 ?)
  - gap stats (overall and innings-1+innings-2 in-play only, excluding the
    innings break which is typically 15-20 min and would dominate the tail)
"""
from __future__ import annotations

import sqlite3
import statistics
from pathlib import Path

CAPTURES_DIR = Path("/Users/sobhagyaxd/DeepWork/totem-mm/captures")

# Slugs from find_complete_matches.py output (the 17 complete ones)
COMPLETE_SLUGS = [
    "cricipl-che-kol-2026-04-14",
    "cricipl-del-pun-2026-04-25",
    "cricipl-guj-kol-2026-04-17",
    "cricipl-guj-mum-2026-04-20",
    "cricipl-kol-luc-2026-04-09",
    "cricipl-kol-raj-2026-04-19",
    "cricipl-luc-raj-2026-04-22",
    "cricipl-mum-che-2026-04-23",
    "cricipl-mum-pun-2026-04-16",
    "cricipl-pun-luc-2026-04-19",
    "cricipl-pun-sun-2026-04-11",
    "cricipl-raj-sun-2026-04-25",
    "cricipl-roy-del-2026-04-18",
    "cricipl-roy-guj-2026-04-24",
    "cricipl-roy-luc-2026-04-15",
    "cricipl-sun-che-2026-04-18",
    "cricipl-sun-del-2026-04-21",
]


def overs_to_balls(overs_str: str) -> int:
    if overs_str is None:
        return 0
    whole, _, frac = str(overs_str).partition(".")
    try:
        return int(whole) * 6 + (int(frac) if frac else 0)
    except ValueError:
        return 0


def detect_split(rows: list[tuple]) -> int | None:
    """rows: (id, ts_ms, runs, wkts, overs_str)."""
    for i in range(1, len(rows)):
        gap = (rows[i][1] - rows[i - 1][1]) / 1000.0
        prev_balls = overs_to_balls(rows[i - 1][4])
        cur_balls = overs_to_balls(rows[i][4])
        if (
            gap >= 300
            and cur_balls < prev_balls
            and rows[i][2] < rows[i - 1][2]
            and rows[i][2] <= 10
        ):
            return i
    return None


def percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return float("nan")
    k = (len(sorted_values) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


def gap_block(label: str, gaps: list[float]) -> str:
    if not gaps:
        return f"  {label}: (no gaps)"
    s = sorted(gaps)
    lines = [
        f"  {label}: n={len(gaps)}",
        f"    min={s[0]:.1f}s  p10={percentile(s, 0.10):.1f}s  "
        f"p50={percentile(s, 0.50):.1f}s  mean={statistics.mean(s):.1f}s  "
        f"p90={percentile(s, 0.90):.1f}s  p99={percentile(s, 0.99):.1f}s  "
        f"max={s[-1]:.1f}s",
    ]
    top20 = s[:20]
    lines.append("    top-20 smallest: " + ", ".join(f"{x:.2f}" for x in top20))
    return "\n".join(lines)


def analyse(slug: str) -> None:
    candidates = list(CAPTURES_DIR.glob(f"match_capture_{slug}_*.db"))
    if not candidates:
        print(f"\n## {slug}\n  (no DB found)")
        return
    db = candidates[0]
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT id, local_ts_ms, COALESCE(runs, 0), COALESCE(wickets, 0), "
        "COALESCE(overs, '0.0') FROM cricket_events ORDER BY id"
    ).fetchall()
    conn.close()

    total = len(rows)
    split = detect_split(rows)
    inn1 = rows[:split] if split else rows
    inn2 = rows[split:] if split else []

    first_overs = inn1[0][4] if inn1 else "—"
    first_runs = inn1[0][2] if inn1 else 0
    first_wkts = inn1[0][3] if inn1 else 0
    starts_first_ball = first_overs in ("0.1", "0.0")

    overall_gaps = [
        (rows[i][1] - rows[i - 1][1]) / 1000.0 for i in range(1, len(rows))
    ]
    inn1_gaps = [
        (inn1[i][1] - inn1[i - 1][1]) / 1000.0 for i in range(1, len(inn1))
    ]
    inn2_gaps = [
        (inn2[i][1] - inn2[i - 1][1]) / 1000.0 for i in range(1, len(inn2))
    ]
    inplay_gaps = inn1_gaps + inn2_gaps  # excludes the innings break gap

    print(f"\n## {slug}")
    print(f"  events: total={total}  inn1={len(inn1)}  inn2={len(inn2)}")
    print(f"  first event: {first_runs}/{first_wkts} ({first_overs})  "
          f"-> starts at 0.1? {'YES' if starts_first_ball else 'NO'}")
    if split:
        ib_gap = (rows[split][1] - rows[split - 1][1]) / 1000.0
        print(f"  innings break gap: {ib_gap:.1f}s ({ib_gap / 60:.1f} min)")
    print(gap_block("overall (incl innings break)", overall_gaps))
    print(gap_block("in-play only (excl innings break)", inplay_gaps))


def main() -> None:
    for slug in COMPLETE_SLUGS:
        analyse(slug)


if __name__ == "__main__":
    main()
