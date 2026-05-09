"""Detailed cricket-event timing analysis for the 17 complete matches.

Notation in this feed: '0.1' = 1st ball, '1.0' = 6th ball (over 1 complete),
'1.1' = 7th ball (1st of over 2). overs_to_balls(X.Y) = X*6 + Y.

Per match we compute:
  - delivery counts: legal (balls incremented) vs extras (balls same)
  - extras count and rate (per innings)
  - gap-to-this-event distributions, sliced by:
      * all events
      * legal deliveries only
      * extras only (WD / NB)
      * 4 / 6 / W events specifically (with prev legal delivery as reference too)
      * over-changeover gaps (last ball of over -> first ball of next over)

Each "gap" is local_ts_ms[i] - local_ts_ms[i-1] for consecutive ordered events.
The innings-break gap is excluded everywhere except where noted.
"""
from __future__ import annotations

import sqlite3
import statistics
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


def overs_to_balls(overs_str: str) -> int:
    if overs_str is None:
        return 0
    whole, _, frac = str(overs_str).partition(".")
    try:
        return int(whole) * 6 + (int(frac) if frac else 0)
    except ValueError:
        return 0


def detect_split(rows: list[tuple]) -> int | None:
    for i in range(1, len(rows)):
        gap = (rows[i][1] - rows[i - 1][1]) / 1000.0
        if (
            gap >= 300
            and overs_to_balls(rows[i][4]) < overs_to_balls(rows[i - 1][4])
            and rows[i][2] < rows[i - 1][2]
            and rows[i][2] <= 10
        ):
            return i
    return None


def percentile(s: list[float], p: float) -> float:
    if not s:
        return float("nan")
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def stats_line(label: str, gaps: list[float]) -> str:
    if not gaps:
        return f"  {label:<32}: (none)"
    s = sorted(gaps)
    return (
        f"  {label:<32}: n={len(s):>3}  "
        f"min={s[0]:>5.1f}  p10={percentile(s, 0.10):>5.1f}  "
        f"p50={percentile(s, 0.50):>5.1f}  mean={statistics.mean(s):>5.1f}  "
        f"p90={percentile(s, 0.90):>6.1f}  p99={percentile(s, 0.99):>6.1f}  "
        f"max={s[-1]:>6.1f}"
    )


def analyse_match(slug: str) -> dict:
    db = next(CAPTURES_DIR.glob(f"match_capture_{slug}_*.db"))
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT id, local_ts_ms, COALESCE(runs, 0), COALESCE(wickets, 0), "
        "COALESCE(overs, '0.0'), signal_type FROM cricket_events ORDER BY id"
    ).fetchall()
    conn.close()

    split = detect_split(rows)
    inn_chunks = [rows[:split], rows[split:]] if split else [rows]
    inn_labels = ["inn1", "inn2"] if split else ["inn1"]

    out = {"slug": slug, "innings": []}

    for label, chunk in zip(inn_labels, inn_chunks):
        legal_count = 0
        extras_count = 0
        # Gap collections
        gaps_all = []
        gaps_legal = []     # gap into a legal delivery
        gaps_extra = []     # gap into an extra (ball didn't increment)
        gaps_4 = []
        gaps_6 = []
        gaps_W = []
        gaps_over_change = []  # prev was X.0 (over ended), cur is X.1
        gaps_within_over = []  # legal deliveries that are NOT over-changeovers

        prev_balls = 0
        for i, (eid, ts, runs, wkts, overs, sig) in enumerate(chunk):
            cur_balls = overs_to_balls(overs)
            if i == 0:
                prev_balls = cur_balls
                continue
            prev_ts = chunk[i - 1][1]
            gap = (ts - prev_ts) / 1000.0
            gaps_all.append(gap)

            is_legal = cur_balls == prev_balls + 1
            is_extra = cur_balls == prev_balls

            if is_legal:
                legal_count += 1
                gaps_legal.append(gap)
                # Over changeover: prev event ended an over (prev_balls % 6 == 0
                # AND prev_balls > 0) and current is start of next (cur_balls % 6 == 1)
                if prev_balls > 0 and prev_balls % 6 == 0 and cur_balls % 6 == 1:
                    gaps_over_change.append(gap)
                else:
                    gaps_within_over.append(gap)
            elif is_extra:
                extras_count += 1
                gaps_extra.append(gap)
            # else: ball count jumped by >1 -> missed event, ignore for typing

            if sig == "4":
                gaps_4.append(gap)
            elif sig == "6":
                gaps_6.append(gap)
            elif sig == "W":
                gaps_W.append(gap)

            prev_balls = cur_balls

        out["innings"].append({
            "label": label,
            "events": len(chunk),
            "legal": legal_count,
            "extras": extras_count,
            "first_overs": chunk[0][4],
            "last_overs": chunk[-1][4],
            "gaps_all": gaps_all,
            "gaps_legal": gaps_legal,
            "gaps_extra": gaps_extra,
            "gaps_4": gaps_4,
            "gaps_6": gaps_6,
            "gaps_W": gaps_W,
            "gaps_over_change": gaps_over_change,
            "gaps_within_over": gaps_within_over,
        })

    return out


def print_match(m: dict) -> None:
    print(f"\n## {m['slug']}")
    for inn in m["innings"]:
        print(f"\n  [{inn['label']}]  events={inn['events']}  "
              f"legal={inn['legal']}  extras={inn['extras']}  "
              f"first={inn['first_overs']}  last={inn['last_overs']}")
        print(stats_line("all events", inn["gaps_all"]))
        print(stats_line("legal deliveries (incl over-ch)", inn["gaps_legal"]))
        print(stats_line("within-over (legal, no over-ch)", inn["gaps_within_over"]))
        print(stats_line("over changeover (X.0 -> X+1.1)", inn["gaps_over_change"]))
        print(stats_line("extras (WD/NB)", inn["gaps_extra"]))
        print(stats_line("4s", inn["gaps_4"]))
        print(stats_line("6s", inn["gaps_6"]))
        print(stats_line("Ws", inn["gaps_W"]))


def aggregate_block(matches: list[dict]) -> None:
    """Pool gaps across all matches (both innings) and print aggregate stats."""
    pools = {
        "all events": [],
        "legal deliveries": [],
        "within-over (legal)": [],
        "over changeover": [],
        "extras (WD/NB)": [],
        "4s": [],
        "6s": [],
        "Ws": [],
    }
    total_legal = 0
    total_extras = 0
    for m in matches:
        for inn in m["innings"]:
            total_legal += inn["legal"]
            total_extras += inn["extras"]
            pools["all events"].extend(inn["gaps_all"])
            pools["legal deliveries"].extend(inn["gaps_legal"])
            pools["within-over (legal)"].extend(inn["gaps_within_over"])
            pools["over changeover"].extend(inn["gaps_over_change"])
            pools["extras (WD/NB)"].extend(inn["gaps_extra"])
            pools["4s"].extend(inn["gaps_4"])
            pools["6s"].extend(inn["gaps_6"])
            pools["Ws"].extend(inn["gaps_W"])

    print("\n" + "=" * 110)
    print(f"AGGREGATE across {len(matches)} matches  "
          f"(total legal={total_legal}  extras={total_extras}  "
          f"extras rate={total_extras / max(total_legal + total_extras, 1):.1%})")
    print("=" * 110)
    for label, gaps in pools.items():
        print(stats_line(label, gaps))


def main() -> None:
    matches = [analyse_match(s) for s in COMPLETE_SLUGS]
    for m in matches:
        print_match(m)
    aggregate_block(matches)


if __name__ == "__main__":
    main()
