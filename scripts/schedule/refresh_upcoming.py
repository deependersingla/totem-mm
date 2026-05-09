#!/usr/bin/env python3
"""Refresh the upcoming-matches schedule.

Queries Polymarket gamma for cricket events, keeps only main IPL match slugs
(cricipl-xxx-yyy-YYYY-MM-DD — no suffixes) whose gameStartTime is in the next
N days, and writes scripts/schedule/upcoming_matches.json.

Also computes `launch_at` = toss_time - 15 min, where toss_time = start - 30 min.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))

DAYS_AHEAD = 10
SCHEDULE_PATH = Path(__file__).resolve().parent / "upcoming_matches.json"
SLUG_RE = re.compile(r"^cricipl-[a-z]+-[a-z]+-\d{4}-\d{2}-\d{2}$")


def curl_json(url: str):
    try:
        out = subprocess.run(
            ["curl", "-s", "-A", "Mozilla/5.0", "--max-time", "15", url],
            capture_output=True, text=True, timeout=20,
        )
    except subprocess.TimeoutExpired:
        print(f"  [timeout] {url}", file=sys.stderr)
        return None
    try:
        return json.loads(out.stdout) if out.stdout else None
    except Exception:
        return None


def parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    s = s.strip().replace(" ", "T")
    if s.endswith("+00"):
        s = s[:-3] + "+00:00"
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def fetch_all_cricket_events() -> list[dict]:
    collected: dict[str, dict] = {}
    # Upcoming matches are open (closed=false). We don't need to scan closed
    # events for scheduling — that scan times out at high offsets.
    for closed_flag in (False,):
        for offset in range(0, 1000, 100):
            url = (
                f"https://gamma-api.polymarket.com/events"
                f"?tag_slug=cricket&limit=100&offset={offset}"
                f"&closed={'true' if closed_flag else 'false'}"
                f"&order=endDate&ascending=false"
            )
            events = curl_json(url)
            if events is None:
                # timeout or parse error — give up this loop and use what we
                # already have rather than crashing
                print(f"  [skipping offset={offset} closed={closed_flag} after timeout]", file=sys.stderr)
                break
            if not isinstance(events, list) or not events:
                break
            for e in events:
                slug = e.get("slug", "") or ""
                if SLUG_RE.match(slug):
                    collected[slug] = e
            if len(events) < 100:
                break
    return list(collected.values())


def main() -> int:
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=DAYS_AHEAD)

    events = fetch_all_cricket_events()
    upcoming: list[dict] = []

    for ev in events:
        slug = ev["slug"]
        # The main market inside the event has the precise gameStartTime
        main_market = None
        for m in ev.get("markets", []) or []:
            if m.get("slug") == slug:
                main_market = m
                break
        if not main_market:
            continue
        gs_raw = main_market.get("gameStartTime") or main_market.get("startDate")
        gs = parse_iso(gs_raw or "")
        if gs is None:
            continue
        if gs < now - timedelta(hours=6) or gs > cutoff:
            continue

        start_ist = gs.astimezone(IST)
        toss_ist = start_ist - timedelta(minutes=30)
        launch_ist = toss_ist - timedelta(minutes=15)

        outcomes = main_market.get("outcomes") or "[]"
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except Exception:
                outcomes = []

        upcoming.append({
            "slug": slug,
            "title": ev.get("title") or main_market.get("question", ""),
            "outcomes": outcomes,
            "start_utc": gs.isoformat(),
            "start_ist": start_ist.strftime("%Y-%m-%d %H:%M"),
            "toss_ist": toss_ist.strftime("%Y-%m-%d %H:%M"),
            "launch_ist": launch_ist.strftime("%Y-%m-%d %H:%M"),
            "closed": bool(ev.get("closed")),
        })

    upcoming.sort(key=lambda r: r["start_utc"])

    SCHEDULE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SCHEDULE_PATH.open("w") as f:
        json.dump({
            "generated_at_ist": datetime.now(IST).isoformat(timespec="seconds"),
            "matches": upcoming,
        }, f, indent=2)

    print(f"wrote {SCHEDULE_PATH}")
    print(f"  {len(upcoming)} matches in the next {DAYS_AHEAD} days")
    for r in upcoming[:20]:
        print(f"  {r['start_ist']}  {r['slug']:34s}  toss={r['toss_ist'][-5:]}  launch={r['launch_ist'][-5:]}  "
              f"{'CLOSED' if r['closed'] else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
