#!/usr/bin/env python3
"""Cricket match lookup from Polymarket slug.

Resolves a Polymarket slug → team names → Firebase match key + metadata.

Usage:
    # Lookup match and show metadata:
    python cricket_match_lookup.py cricipl-raj-mum-2026-04-07

    # Just print the match key (for piping into event_book_capture.py):
    python cricket_match_lookup.py cricipl-raj-mum-2026-04-07 --key-only

    # Run event_book_capture directly:
    python cricket_match_lookup.py cricipl-raj-mum-2026-04-07 --run

Requires CRICKET_API_KEY in .env.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

import httpx
import requests
from dotenv import load_dotenv

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))
GAMMA_API = "https://gamma-api.polymarket.com"

# Polymarket slug fragments → Firebase short names
# Slugs use city/nickname abbreviations, Firebase uses official IPL codes
SLUG_TO_TEAM = {
    "che": ["CSK"],
    "csk": ["CSK"],
    "mum": ["MI"],
    "del": ["DC"],
    "kol": ["KKR"],
    "pun": ["PBKS"],
    "raj": ["RR"],
    "roy": ["RCB"],  # "Royal" in slug = RCB
    "rcb": ["RCB"],
    "ban": ["RCB"],
    "sun": ["SRH"],
    "srh": ["SRH"],
    "hyd": ["SRH"],
    "guj": ["GT"],
    "luc": ["LSG"],
    "lsg": ["LSG"],
}


def parse_teams_from_slug(slug: str) -> tuple[list[str], list[str]]:
    """Extract possible team codes from Polymarket slug.
    Slug format: cricipl-<team1>-<team2>-<date>
    Returns (team1_candidates, team2_candidates).
    """
    parts = slug.split("-")
    # Skip 'cricipl' prefix, last 3 parts are date (YYYY-MM-DD)
    team_parts = parts[1:-3] if len(parts) > 4 else parts[1:]

    if len(team_parts) < 2:
        return [], []

    t1 = SLUG_TO_TEAM.get(team_parts[0].lower(), [team_parts[0].upper()])
    t2 = SLUG_TO_TEAM.get(team_parts[1].lower(), [team_parts[1].upper()])
    return t1, t2


def get_cricket_base() -> str:
    base = os.getenv("CRICKET_API_KEY", "").rstrip("/")
    if not base:
        print("ERROR: CRICKET_API_KEY not set in .env")
        sys.exit(1)
    return base


def fetch_all_matches(base: str) -> dict:
    resp = httpx.get(f"{base}/events/cricket/match/recent.json", timeout=15)
    resp.raise_for_status()
    return resp.json() or {}


def find_match(matches: dict, team1_codes: list[str], team2_codes: list[str]) -> list[tuple[str, dict]]:
    """Find matches where short_name contains both team codes."""
    results = []
    for key, data in matches.items():
        short = data.get("short_name", "")
        name = data.get("name", "")
        combined = f"{short} {name}"
        for t1 in team1_codes:
            for t2 in team2_codes:
                if t1 in combined and t2 in combined:
                    results.append((key, data))
    return results


def format_timestamp(ts) -> str:
    if not ts:
        return "--"
    try:
        dt = datetime.fromtimestamp(float(ts), tz=IST)
        return dt.strftime("%Y-%m-%d %H:%M IST")
    except (ValueError, TypeError, OSError):
        return str(ts)


def print_match_details(key: str, data: dict):
    """Print detailed match metadata."""
    short = data.get("short_name", "?")
    name = data.get("name", "?")
    status = data.get("play_status", "?")
    fmt = data.get("format", "?")
    gender = data.get("gender", "?")

    # Venue
    venue = data.get("venue", {})
    venue_name = venue.get("name", "?") if isinstance(venue, dict) else "?"
    venue_city = venue.get("city", "") if isinstance(venue, dict) else ""

    # Times
    start_at = data.get("start_at")
    end_at = data.get("estimated_end_date") or data.get("completed_date_approximate")

    # Toss
    toss = data.get("toss", {})
    toss_winner = ""
    toss_decision = ""
    if isinstance(toss, dict):
        winner_key = toss.get("winner", "")
        toss_decision = toss.get("decision", "")
        # Resolve team name from winner key (a/b)
        teams = data.get("teams", {})
        if isinstance(teams, dict) and winner_key in teams:
            team_data = teams[winner_key]
            toss_winner = team_data.get("code", team_data.get("name", winner_key)) if isinstance(team_data, dict) else winner_key

    # First batting
    play = data.get("play", {})
    first_batting_key = play.get("first_batting", "") if isinstance(play, dict) else ""
    first_batting = ""
    if first_batting_key:
        teams = data.get("teams", {})
        if isinstance(teams, dict) and first_batting_key in teams:
            team_data = teams[first_batting_key]
            first_batting = team_data.get("code", team_data.get("name", first_batting_key)) if isinstance(team_data, dict) else first_batting_key

    # Result
    result_msg = ""
    if isinstance(play, dict):
        result = play.get("result", {})
        if isinstance(result, dict):
            result_msg = result.get("msg", "")

    # Weather
    weather = data.get("weather", "")

    # Teams
    teams_info = data.get("teams", {})
    team_a = ""
    team_b = ""
    if isinstance(teams_info, dict):
        a = teams_info.get("a", {})
        b = teams_info.get("b", {})
        team_a = f"{a.get('code', '?')} ({a.get('name', '?')})" if isinstance(a, dict) else str(a)
        team_b = f"{b.get('code', '?')} ({b.get('name', '?')})" if isinstance(b, dict) else str(b)

    # Tournament
    tournament = data.get("tournament", {})
    tournament_name = tournament.get("name", "?") if isinstance(tournament, dict) else "?"

    print(f"""
{'='*60}
  {name}
{'='*60}
  Short Name:     {short}
  Status:         {status}
  Format:         {fmt} | {gender}
  Tournament:     {tournament_name}

  Team A:         {team_a}
  Team B:         {team_b}

  Venue:          {venue_name}{f', {venue_city}' if venue_city else ''}
  Weather:        {weather or '--'}

  Start:          {format_timestamp(start_at)}
  End:            {format_timestamp(end_at)}

  Toss Winner:    {toss_winner or '--'}
  Toss Decision:  {toss_decision or '--'}
  First Batting:  {first_batting or '--'}

  Result:         {result_msg or '(in progress / not started)'}

  Match Key:      {key}
{'='*60}
""")


def fetch_polymarket_info(slug: str) -> dict | None:
    try:
        resp = requests.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=15)
        markets = resp.json()
        return markets[0] if markets else None
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description="Cricket Match Lookup from Polymarket slug")
    parser.add_argument("slug", help="Polymarket market slug (e.g. cricipl-raj-mum-2026-04-07)")
    parser.add_argument("--key-only", action="store_true", help="Print only the match key")
    parser.add_argument("--run", action="store_true",
                        help="Auto-run event_book_capture.py with resolved match key")
    args = parser.parse_args()

    slug = args.slug
    team1_codes, team2_codes = parse_teams_from_slug(slug)

    if not team1_codes or not team2_codes:
        print(f"Could not parse team names from slug: {slug}")
        sys.exit(1)

    base = get_cricket_base()
    matches = fetch_all_matches(base)

    found = find_match(matches, team1_codes, team2_codes)

    if not found:
        print(f"No match found for teams {team1_codes} vs {team2_codes}")
        print("Available matches:")
        for k, v in matches.items():
            s = v.get("play_status", "")
            if s in ("in_play", "pre_match", "scheduled"):
                print(f"  {v.get('short_name', '?'):15s} [{s}] {k}")
        sys.exit(1)

    # Prefer in_play > pre_match > result > scheduled
    priority = {"in_play": 0, "pre_match": 1, "result": 2, "scheduled": 3}
    found.sort(key=lambda x: priority.get(x[1].get("play_status", ""), 99))
    match_key, match_data = found[0]

    if args.key_only:
        print(match_key)
        return

    # Show Polymarket info
    poly = fetch_polymarket_info(slug)
    if poly:
        print(f"\n  Polymarket:     {poly.get('question', slug)}")

    print_match_details(match_key, match_data)

    if len(found) > 1:
        print(f"  (Found {len(found)} matches, showing best status match)\n")

    if args.run:
        import subprocess
        cmd = [
            sys.executable, "scripts/event_book_capture.py",
            "--slug", slug,
            "--match", match_key,
        ]
        print(f"Running: {' '.join(cmd)}\n")
        subprocess.execvp(sys.executable, cmd)


if __name__ == "__main__":
    main()
