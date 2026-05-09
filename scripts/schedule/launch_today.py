#!/usr/bin/env python3
"""Open Terminal tabs for every match whose launch window hits now (±WINDOW_MIN).

Reads scripts/schedule/upcoming_matches.json. For each match whose `launch_ist`
is within ±WINDOW_MIN of the current IST time, resolves the match key via
cricket_match_lookup.py and opens one Terminal tab per script
(live_capture, dls_monitor, dp_monitor).

Writes a tracking file captures/.running/<slug>.json so stop_today.sh can
find and kill the running processes.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCHEDULE_PATH = PROJECT_ROOT / "scripts" / "schedule" / "upcoming_matches.json"
RUNNING_DIR = PROJECT_ROOT / "captures" / ".running"
VENV_ACTIVATE = ".venv/bin/activate"  # relative to PROJECT_ROOT — falls back to venv/

WINDOW_MIN = 5  # match fires if launch_ist is within ±5 minutes of now

SCRIPTS = [
    # (script_path, tab_title_suffix, requires_match_key)
    ("scripts/live_capture.py", "capture", False),  # works without --match
    ("scripts/dls_monitor.py",  "dls",     True),   # requires --match
    ("scripts/dp_monitor.py",   "dp",      True),   # requires --match
]

# How many times to retry the cricket-key lookup before giving up. The match
# may not be in the firebase rolling window if launched far ahead of toss.
KEY_LOOKUP_RETRIES = 3
KEY_LOOKUP_DELAY_S = 5.0


def venv_activate() -> str:
    # Prefer venv/ — it's the one with the full deps (aiosqlite, pandas, etc.)
    if (PROJECT_ROOT / "venv" / "bin" / "activate").exists():
        return "source venv/bin/activate"
    if (PROJECT_ROOT / ".venv" / "bin" / "activate").exists():
        return "source .venv/bin/activate"
    return ""  # no venv — run with system python


def open_terminal_tab(cmd_inside_project: str, title: str) -> None:
    """Opens a new Terminal tab and runs the given shell command.

    The command is run with cwd=PROJECT_ROOT and with the venv activated.
    """
    activate = venv_activate()
    full = f'cd "{PROJECT_ROOT}" && {activate + " && " if activate else ""}{cmd_inside_project}'
    # Escape for AppleScript string
    full_esc = full.replace('\\', '\\\\').replace('"', '\\"')
    title_esc = title.replace('"', '\\"')
    osa = f'''
    tell application "Terminal"
        activate
        do script "echo '=== {title_esc} ===' && {full_esc}"
        set custom title of front window to "{title_esc}"
    end tell
    '''
    subprocess.run(["osascript", "-e", osa], check=False)


def resolve_match_key(slug: str) -> str | None:
    """Call the existing cricket_match_lookup.py with --key-only.

    Returns the match key (e.g. "a-rz--cricket--XXXX") on success, None if the
    lookup fails. Retries a few times because the cricket firebase rolling
    window may not yet contain matches that are scheduled hours ahead.

    NB: cricket_match_lookup exits 0 even on failure and dumps an
    "Available matches: ..." block to stdout — that block contains valid match
    keys for OTHER matches, so we must reject any output that has more than one
    line or contains spaces.
    """
    import time
    act = venv_activate()
    cmd = (
        f'cd "{PROJECT_ROOT}" && {act + " && " if act else ""}'
        f'python scripts/cricket_match_lookup.py {slug} --key-only'
    )
    for attempt in range(KEY_LOOKUP_RETRIES):
        try:
            out = subprocess.run(
                ["bash", "-c", cmd], capture_output=True, text=True, timeout=30
            )
        except subprocess.TimeoutExpired:
            out = None
        if out is not None:
            lines = [ln.strip() for ln in (out.stdout or "").splitlines() if ln.strip()]
            # Valid: exactly one line, starts with a-rz--cricket--, no spaces inside
            if len(lines) == 1 and lines[0].startswith("a-rz--cricket--") and " " not in lines[0]:
                return lines[0]
        if attempt < KEY_LOOKUP_RETRIES - 1:
            time.sleep(KEY_LOOKUP_DELAY_S)
    return None


def iso_to_ist(s: str) -> datetime | None:
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M")
        return dt.replace(tzinfo=IST)
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--slug", help="Launch this specific slug right now, "
                    "ignoring the ±window check.")
    ap.add_argument("--now", action="store_true",
                    help="Launch the NEXT upcoming match right now, "
                    "ignoring the ±window check (next = soonest launch_ist today).")
    args = ap.parse_args()

    if not SCHEDULE_PATH.exists():
        print(f"ERR: schedule missing — run refresh_upcoming.py first ({SCHEDULE_PATH})",
              file=sys.stderr)
        return 2

    schedule = json.loads(SCHEDULE_PATH.read_text())
    now = datetime.now(IST)
    fire_low  = now - timedelta(minutes=WINDOW_MIN)
    fire_high = now + timedelta(minutes=WINDOW_MIN)

    fired: list[dict] = []

    if args.slug:
        m = next((x for x in schedule.get("matches", []) if x.get("slug") == args.slug), None)
        if m is None:
            print(f"ERR: slug {args.slug!r} not found in schedule", file=sys.stderr)
            return 2
        fired = [m]
    elif args.now:
        # Pick the soonest match whose launch_ist is either in the future today OR
        # within the last 6 hours (in case the match started recently and we missed cron).
        candidates = []
        for m in schedule.get("matches", []):
            ldt = iso_to_ist(m.get("launch_ist", ""))
            if ldt is None:
                continue
            delta = (ldt - now).total_seconds() / 60  # minutes
            if -360 <= delta <= 720:  # last 6h..next 12h
                candidates.append((abs(delta) if delta >= 0 else abs(delta)*2, m))
        if not candidates:
            print("no matches in the next 12h (or last 6h). "
                  "Run refresh_upcoming.py if the schedule is stale.")
            return 0
        candidates.sort(key=lambda x: x[0])
        fired = [candidates[0][1]]
    else:
        for m in schedule.get("matches", []):
            launch_dt = iso_to_ist(m.get("launch_ist", ""))
            if launch_dt is None:
                continue
            if not (fire_low <= launch_dt <= fire_high):
                continue
            fired.append(m)

    if not fired:
        print(f"[{now.strftime('%H:%M:%S')}] no matches in launch window "
              f"(±{WINDOW_MIN} min of now). Next up:")
        future = [m for m in schedule.get("matches", [])
                  if iso_to_ist(m.get("launch_ist","")) and iso_to_ist(m["launch_ist"]) > now]
        for m in future[:3]:
            print(f"  {m['launch_ist']}  {m['slug']}   (match starts {m['start_ist']})")
        print("(tip: use --now to launch the next match right now, "
              "or --slug <slug> for a specific match)")
        return 0

    RUNNING_DIR.mkdir(parents=True, exist_ok=True)

    for m in fired:
        slug = m["slug"]
        tracking_file = RUNNING_DIR / f"{slug}.json"

        # If we've launched before, figure out what's missing and re-attempt only those
        previously_launched_suffixes: set[str] = set()
        prior_skipped: list[str] = []
        if tracking_file.exists():
            try:
                prior = json.loads(tracking_file.read_text())
                previously_launched_suffixes = {
                    t.split(" :: ")[1] for t in prior.get("tab_titles", [])
                }
                prior_skipped = list(prior.get("scripts_skipped", []))
            except Exception:
                prior_skipped = []
            age_min = (now.timestamp() - tracking_file.stat().st_mtime) / 60
            if age_min < 60 and not prior_skipped:
                print(f"[skip] {slug} already fully launched {age_min:.1f} min ago.")
                continue
            if prior_skipped:
                print(f"[retry] {slug} — prior launch skipped {prior_skipped}; "
                      f"re-attempting just those.")

        print(f"[fire] {slug}  (match {m['start_ist']})")

        match_key = resolve_match_key(slug)
        match_arg = f" --match {match_key}" if match_key else ""

        if match_key is None:
            print(f"  ⚠  cricket match key NOT resolved (not in firebase rolling "
                  f"window yet). Will launch only key-optional scripts; key-required "
                  f"scripts (dls, dp) will be skipped — re-run later for them.")
        else:
            print(f"  ✓ match key: {match_key}")

        launched_titles: list[str] = list({  # preserve any tab_titles we still want
            t for t in
            (json.loads(tracking_file.read_text()).get("tab_titles", [])
             if tracking_file.exists() else [])
        })
        skipped: list[str] = []
        for rel, suffix, needs_key in SCRIPTS:
            # If this script was already launched in a prior run, don't re-open
            if suffix in previously_launched_suffixes:
                continue
            if needs_key and match_key is None:
                skipped.append(suffix)
                print(f"    × skipped: {suffix}  (needs --match)")
                continue
            title = f"{slug} :: {suffix}"
            cmd = f"python {rel} --slug {slug}{match_arg}"
            open_terminal_tab(cmd, title)
            launched_titles.append(title)
            print(f"    ↳ tab opened: {title}")

        if skipped:
            print(f"  ⓘ to launch the missing scripts later (after the cricket key "
                  f"becomes available), run:")
            print(f"      scripts/schedule/run_now.sh --skip-refresh {slug}")

        tracking_file.write_text(json.dumps({
            "slug": slug,
            "match_key": match_key or "",
            "launched_at_ist": now.isoformat(timespec="seconds"),
            "scripts_launched": [rel for rel, suf, _ in SCRIPTS
                                 if suf in [t.split(" :: ")[1] for t in launched_titles]],
            "scripts_skipped":  skipped,
            "tab_titles": launched_titles,
        }, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
