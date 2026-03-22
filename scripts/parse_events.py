#!/usr/bin/env python3
"""Parse match events (wickets, boundaries) from score snapshots and map to Polymarket sides.

Usage:
    python parse_events.py <scores_jsonl> <team1> <team2> [-o output.json]

    team1/team2: the two team abbreviations as they appear in the score strings.
    The first team listed is treated as "home" for side assignment:
      - team1 wicket falls -> side=team2 (buy team2)
      - team1 boundary    -> side=team1 (buy team1)

Example:
    python parse_events.py captures/20260322_115713_scores_1491738.jsonl SA NZ
"""

import argparse
import json
import re
from pathlib import Path


def overs_to_balls(ov_str):
    """Convert '7.4' to 46 balls."""
    parts = ov_str.split('.')
    complete = int(parts[0])
    partial = int(parts[1]) if len(parts) > 1 else 0
    return complete * 6 + partial


def parse_score(score_str, teams):
    """Parse 'SA 44/4 (7 ov)' or 'NZ 5/0 (1.2 ov) | SA 164/5 (20.0 ov)' into current batting team info.

    For multi-innings format, extract the FIRST part (currently batting team).
    Returns (team, runs, wickets, overs_str) or None.
    """
    # Build regex dynamically from team names
    team_pattern = "|".join(re.escape(t) for t in teams)
    pattern = rf'({team_pattern})\s+(\d+)/(\d+)\s+\(([\d.]+)\s*ov'

    # For multi-innings, split on | and take the first (current batting)
    parts = score_str.split("|")
    batting_part = parts[0].strip()

    m = re.search(pattern, batting_part)
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)


def detect_events(entries, teams):
    """Compare consecutive entries to find wickets and boundaries."""
    events = []
    prev = None
    team1, team2 = teams

    for entry in entries:
        parsed = parse_score(entry["score"], teams)
        if not parsed:
            continue
        team, runs, wickets, overs = parsed
        balls = overs_to_balls(overs)

        if prev and prev["team"] == team:
            # Skip backward/duplicate entries (scraper noise)
            if balls < prev["balls"]:
                continue
            if balls == prev["balls"] and runs <= prev["runs"] and wickets <= prev["wickets"]:
                continue

            run_diff = runs - prev["runs"]
            wkt_diff = wickets - prev["wickets"]

            # Wicket detected
            if wkt_diff > 0:
                batting_team = team
                # Batting team loses wicket -> other team benefits
                side = team2 if batting_team == team1 else team1

                for _ in range(wkt_diff):
                    events.append({
                        "ist": entry["ist"],
                        "innings": f"{team} batting",
                        "over": overs,
                        "event": "WICKET",
                        "score_before": f"{prev['runs']}/{prev['wickets']}",
                        "score_after": f"{runs}/{wickets}",
                        "side": side,
                        "market_movement": ""
                    })

            # Boundary detected (4 or 6 run jump on same wicket count, within 1 ball)
            if wkt_diff == 0 and balls - prev["balls"] <= 2:
                if run_diff == 4:
                    event_type = "FOUR"
                elif run_diff == 6:
                    event_type = "SIX"
                else:
                    prev = {"team": team, "runs": runs, "wickets": wickets, "balls": balls}
                    continue

                # Batting team scores boundary -> batting team benefits
                side = team1 if team == team1 else team2

                events.append({
                    "ist": entry["ist"],
                    "innings": f"{team} batting",
                    "over": overs,
                    "event": event_type,
                    "score_before": f"{prev['runs']}/{prev['wickets']}",
                    "score_after": f"{runs}/{wickets}",
                    "side": side,
                    "market_movement": ""
                })

        prev = {"team": team, "runs": runs, "wickets": wickets, "balls": balls}

    return events


def get_data_dir(slug):
    """Get data/<slug>/ directory, creating it if needed."""
    data_dir = Path(__file__).parent.parent / "data" / slug
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def main():
    parser = argparse.ArgumentParser(description="Parse cricket match events from score JSONL")
    parser.add_argument("scores_jsonl", help="Path to scores JSONL file")
    parser.add_argument("team1", help="First team abbreviation (e.g. SA)")
    parser.add_argument("team2", help="Second team abbreviation (e.g. NZ)")
    parser.add_argument("--slug", help="Market slug for output directory (default: auto from scores filename)")
    parser.add_argument("-o", "--output", help="Output events JSON path (overrides --slug)")
    args = parser.parse_args()

    input_path = Path(args.scores_jsonl)
    teams = [args.team1, args.team2]

    # Auto-detect slug from input filename if not provided
    # e.g. "20260322_115713_scores_1491738.jsonl" in captures/ -> need explicit slug
    # e.g. "20260322_115551_crint-nzl-zaf-2026-03-22.jsonl" -> slug = crint-nzl-zaf-2026-03-22
    slug = args.slug
    if not slug:
        # Try to extract slug from input filename (capture files often have slug in name)
        stem = input_path.stem
        parts = stem.split("_", 2)  # "20260322_115551_crint-nzl-zaf-2026-03-22"
        if len(parts) >= 3 and parts[2].startswith("crint"):
            slug = parts[2]

    if args.output:
        output_path = Path(args.output)
    elif slug:
        data_dir = get_data_dir(slug)
        output_path = data_dir / f"{input_path.stem}_events.json"
    else:
        # Fallback: write next to input file
        output_path = input_path.parent / f"{input_path.stem}_events.json"
        print(f"WARNING: No --slug provided, writing to {output_path}")

    entries = []
    with open(input_path) as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))

    events = detect_events(entries, teams)

    with open(output_path, "w") as f:
        json.dump(events, f, indent=2)

    print(f"Found {len(events)} events -> {output_path}")
    for e in events:
        print(f"  {e['ist'][:19]}  {e['over']:>5} ov  {e['event']:6}  "
              f"{e['score_before']} -> {e['score_after']}  [{e['innings']}]  side={e['side']}")


if __name__ == "__main__":
    main()
