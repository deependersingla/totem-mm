"""Show every consecutive-event pair with gap < 1 second across the 17 matches,
with a window of context rows so we can see what's actually happening.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

CAPTURES_DIR = Path("/Users/sobhagyaxd/DeepWork/totem-mm/captures")

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

THRESHOLD_MS = 1000  # show pairs with gap < 1.0s
CONTEXT = 1          # rows of context before and after each cluster


def fetch(slug: str) -> tuple[str, list[tuple]]:
    db = next(CAPTURES_DIR.glob(f"match_capture_{slug}_*.db"))
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT id, local_ts_ms, signal_type, COALESCE(runs, 0), "
        "COALESCE(wickets, 0), COALESCE(overs, '0.0'), score_str "
        "FROM cricket_events ORDER BY id"
    ).fetchall()
    conn.close()
    return db.name, rows


def main() -> None:
    grand_total = 0
    for slug in COMPLETE_SLUGS:
        db_name, rows = fetch(slug)
        flagged_indices = [
            i for i in range(1, len(rows))
            if (rows[i][1] - rows[i - 1][1]) < THRESHOLD_MS
        ]
        if not flagged_indices:
            continue

        # Cluster contiguous indices so consecutive pairs print together.
        clusters: list[list[int]] = []
        for i in flagged_indices:
            if clusters and i == clusters[-1][-1] + 1:
                clusters[-1].append(i)
            else:
                clusters.append([i])

        grand_total += len(flagged_indices)
        print(f"\n## {slug}  ({db_name})")
        print(f"   {len(flagged_indices)} sub-second pairs in "
              f"{len(clusters)} cluster(s)")

        for cluster in clusters:
            lo = max(0, cluster[0] - 1 - CONTEXT)
            hi = min(len(rows), cluster[-1] + 1 + CONTEXT)
            print()
            print(f"   --- cluster around id {rows[cluster[0]][0]} "
                  f"to {rows[cluster[-1]][0]} ---")
            print(f"   {'idx':>5}  {'id':>5}  {'ts_ms':>13}  "
                  f"{'gap_s':>6}  {'sig':>3}  {'overs':>6}  score")
            prev_ts = None
            for j in range(lo, hi):
                rid, ts, sig, runs, wkts, overs, score = rows[j]
                gap = "" if prev_ts is None else f"{(ts - prev_ts) / 1000:.3f}"
                marker = "  *" if j in cluster else "   "
                print(f"   {j:>5}  {rid:>5}  {ts:>13}  "
                      f"{gap:>6}  {str(sig):>3}  {overs:>6}  {score}{marker}")
                prev_ts = ts

    print(f"\n\nTotal sub-second pairs across all 17 matches: {grand_total}")


if __name__ == "__main__":
    main()
