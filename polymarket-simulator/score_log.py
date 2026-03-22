#!/usr/bin/env python3
"""Live cricket score logger using ESPN Cricinfo free API.

Polls every 1 second, logs score changes to JSONL.

Usage:
    python score_log.py 1491738
    python score_log.py <espn_match_id> --duration 18000
"""

import argparse
import asyncio
import json
import os
import time
from datetime import datetime, timezone, timedelta

import httpx

IST = timezone(timedelta(hours=5, minutes=30))

# ESPN Cricinfo summary endpoint (free, no auth)
ESPN_URL = "https://site.web.api.espn.com/apis/site/v2/sports/cricket/{league_id}/summary?event={match_id}"

# Common league IDs: 8676 = international, 1410 = IPL, 1359 = BBL, etc.
# The API auto-resolves if you use the generic endpoint
ESPN_GENERIC_URL = "https://site.api.espn.com/apis/site/v2/sports/cricket/{league_id}/summary?event={match_id}"

LEAGUE_IDS = [8676, 1410, 1359, 1457, 1001, 1283]  # intl, IPL, BBL, PSL, etc.

C_RESET = "\033[0m"
C_GREEN = "\033[92m"
C_RED = "\033[91m"
C_YELLOW = "\033[93m"
C_CYAN = "\033[96m"
C_BOLD = "\033[1m"
C_DIM = "\033[90m"


def now_ist():
    t = time.time()
    ns = time.time_ns() % 1_000_000_000
    dt = datetime.fromtimestamp(t, tz=IST)
    return dt.strftime("%Y-%m-%d %H:%M:%S") + f".{ns:09d} IST"


def ist_short():
    ns = time.time_ns() % 1_000_000
    dt = datetime.now(IST)
    return dt.strftime("%H:%M:%S") + f".{ns:06d}"


def parse_espn_response(data: dict) -> dict:
    """Extract score, status, state, last_ball from ESPN summary response."""
    header = data.get("header", {})
    competitions = header.get("competitions", [{}])
    if not competitions:
        return {}

    comp = competitions[0]
    status_obj = comp.get("status", {})
    status_type = status_obj.get("type", {})
    status = status_type.get("description", "")  # "Live", "Scheduled", "Complete"
    state = status_type.get("state", "")  # "in", "pre", "post"

    competitors = comp.get("competitors", [])
    batting_team = ""
    scores = []
    for team in competitors:
        abbr = team.get("team", {}).get("abbreviation", "?")
        linescores = team.get("linescores", [])
        if linescores:
            for ls in linescores:
                sc = ls.get("score", "")
                if sc:
                    scores.append(f"{abbr} {sc}")
                    if ls.get("isBatting") and ls.get("isCurrent"):
                        batting_team = abbr
        elif team.get("score"):
            scores.append(f"{abbr} {team['score']}")

    score_str = " | ".join(scores) if scores else status

    # Situation data (available during live matches)
    situation = data.get("situation", {})
    last_ball = ""
    if isinstance(situation, dict):
        last_ball = situation.get("lastBallSummary", "")

    return {
        "score": score_str,
        "batting_team": batting_team,
        "status": status,
        "state": state,
        "last_ball": last_ball,
    }


async def find_league_id(client: httpx.AsyncClient, match_id: int) -> int | None:
    """Try common league IDs to find which one has this match."""
    for lid in LEAGUE_IDS:
        try:
            url = ESPN_URL.format(league_id=lid, match_id=match_id)
            resp = await client.get(url)
            if resp.status_code == 200:
                d = resp.json()
                if d.get("header"):
                    return lid
        except Exception:
            continue
    return None


async def run(match_id: int, duration: int, poll_interval: float = 1.0):
    ts = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs("captures", exist_ok=True)
    outfile = f"captures/{ts}_scores_{match_id}.jsonl"

    print(f"\n{C_BOLD}ESPN Cricket Score Logger{C_RESET}")
    print(f"Match ID: {match_id}")
    print(f"Duration: {duration}s ({duration // 60}m)")
    print(f"Poll:     every {poll_interval}s")
    print(f"Output:   {outfile}")
    print(f"Started:  {now_ist()}")

    async with httpx.AsyncClient(timeout=10, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }) as client:
        # Auto-detect league ID
        print(f"Detecting league... ", end="", flush=True)
        league_id = await find_league_id(client, match_id)
        if league_id is None:
            print(f"{C_RED}Could not find match {match_id} in any league{C_RESET}")
            return
        print(f"{C_GREEN}found (league {league_id}){C_RESET}")

        url = ESPN_URL.format(league_id=league_id, match_id=match_id)
        print(f"{'=' * 70}\n")

        f = open(outfile, "a")
        seq = 0
        last_score = ""
        start = time.time()

        try:
            while time.time() - start < duration:
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        await asyncio.sleep(poll_interval)
                        continue

                    data = resp.json()
                    parsed = parse_espn_response(data)
                    if not parsed:
                        await asyncio.sleep(poll_interval)
                        continue

                    # Only log on change
                    score_key = f"{parsed['score']}|{parsed['last_ball']}"
                    if score_key == last_score and parsed["status"] != "Scheduled":
                        await asyncio.sleep(poll_interval)
                        continue

                    last_score = score_key
                    seq += 1

                    record = {
                        "seq": seq,
                        "ist": now_ist(),
                        "score": parsed["score"],
                        "batting_team": parsed["batting_team"],
                        "status": parsed["status"],
                        "state": parsed["state"],
                        "last_ball": parsed["last_ball"],
                    }

                    f.write(json.dumps(record) + "\n")
                    f.flush()

                    # Color based on state
                    if parsed["state"] == "post":
                        color = C_YELLOW
                    elif "W" in parsed.get("last_ball", ""):
                        color = C_RED
                    elif parsed["state"] == "in":
                        color = C_CYAN
                    else:
                        color = C_DIM

                    t = ist_short()
                    lb = f" | last: {parsed['last_ball']}" if parsed["last_ball"] else ""
                    print(f"{C_DIM}{t}{C_RESET} {color}#{seq:>4}{C_RESET} "
                          f"{C_BOLD}{parsed['score']}{C_RESET} "
                          f"[{parsed['status']}]{lb}")

                    # Stop if match is complete
                    if parsed["state"] == "post":
                        print(f"\n{C_YELLOW}Match complete.{C_RESET}")
                        break

                except Exception as e:
                    print(f"{C_DIM}{ist_short()}{C_RESET} {C_RED}ERROR{C_RESET} {e}")

                await asyncio.sleep(poll_interval)

        except KeyboardInterrupt:
            pass

        f.close()
        elapsed = int(time.time() - start)
        print(f"\n{'=' * 70}")
        print(f"Captured {seq} score changes in {elapsed}s")
        print(f"Output: {outfile}")


def main():
    parser = argparse.ArgumentParser(description="ESPN Cricket Score Logger")
    parser.add_argument("match_id", type=int, help="ESPN Cricinfo match ID")
    parser.add_argument("--duration", type=int, default=18000, help="Duration in seconds (default: 5h)")
    parser.add_argument("--poll", type=float, default=1.0, help="Poll interval in seconds (default: 1)")
    args = parser.parse_args()
    asyncio.run(run(args.match_id, args.duration, args.poll))


if __name__ == "__main__":
    main()
