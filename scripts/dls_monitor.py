#!/usr/bin/env python3
"""DLS Live Monitor — real-time T20 par scores + Polymarket odds comparison.

Connects to cricket SSE for ball-by-ball updates, polls Polymarket for
current odds, and prints DLS par / edge after every delivery.

Usage:
    python scripts/dls_monitor.py --slug cricipl-mum-pun-2026-04-16 \
        --match $(python scripts/cricket_match_lookup.py cricipl-mum-pun-2026-04-16 --key-only)

Requires CRICKET_API_KEY in .env.
"""

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
import requests
from dotenv import load_dotenv

load_dotenv()

IST = timezone(timedelta(hours=5, minutes=30))
GAMMA_API = "https://gamma-api.polymarket.com"
_dls_db: "DLSLogger | None" = None

# ── Terminal colors ────────────────────────────────────────────────────────

C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[90m"
C_GREEN = "\033[92m"
C_RED = "\033[91m"
C_YELLOW = "\033[93m"
C_CYAN = "\033[96m"
C_MAGENTA = "\033[95m"
C_WHITE = "\033[97m"
C_BG_GREEN = "\033[42m"
C_BG_RED = "\033[41m"
C_BG_YELLOW = "\033[43m"

# ── DLS T20 Standard Edition resource table ────────────────────────────────
#
# Rows: balls_remaining / 6 (index 0 = 0 balls, index 20 = 120 balls).
# Cols: wickets_lost 0..9.
# Values: percentage of 20-over innings resources remaining.

T20_TABLE = [
    # 0 balls remaining
    [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    # 6 balls (1 over) remaining
    [6.4, 6.4, 6.4, 6.4, 6.4, 6.2, 6.2, 6.0, 5.7, 4.4],
    # 12 balls (2 overs)
    [12.7, 12.5, 12.5, 12.4, 12.4, 12.0, 11.7, 11.0, 9.7, 6.5],
    # 18 balls (3 overs)
    [18.7, 18.6, 18.4, 18.2, 18.0, 17.5, 16.8, 15.4, 12.7, 7.4],
    # 24 balls (4 overs)
    [24.6, 24.4, 24.2, 23.9, 23.3, 22.4, 21.2, 18.9, 14.8, 8.0],
    # 30 balls (5 overs) -- minimum for DLS result
    [30.4, 30.0, 29.7, 29.2, 28.4, 27.2, 25.3, 22.1, 16.6, 8.1],
    # 36 balls (6 overs)
    [35.9, 35.5, 35.0, 34.3, 33.2, 31.4, 29.0, 24.6, 17.8, 8.1],
    # 42 balls (7 overs)
    [41.3, 40.8, 40.1, 39.2, 37.8, 35.5, 32.2, 26.9, 18.6, 8.3],
    # 48 balls (8 overs)
    [46.6, 45.9, 45.1, 43.8, 42.0, 39.4, 35.2, 28.6, 19.3, 8.3],
    # 54 balls (9 overs)
    [51.8, 51.1, 49.8, 48.4, 46.1, 42.8, 37.8, 30.2, 19.8, 8.3],
    # 60 balls (10 overs)
    [56.7, 55.8, 54.4, 52.7, 50.0, 46.1, 40.3, 31.6, 20.1, 8.3],
    # 66 balls (11 overs)
    [61.7, 60.4, 59.0, 56.7, 53.7, 49.1, 42.4, 32.7, 20.3, 8.3],
    # 72 balls (12 overs)
    [66.4, 65.0, 63.3, 60.6, 57.1, 51.9, 44.3, 33.6, 20.5, 8.3],
    # 78 balls (13 overs)
    [71.0, 69.4, 67.3, 64.5, 60.4, 54.4, 46.1, 34.5, 20.7, 8.3],
    # 84 balls (14 overs)
    [75.4, 73.7, 71.4, 68.0, 63.4, 56.9, 47.7, 35.2, 20.8, 8.3],
    # 90 balls (15 overs)
    [79.9, 77.9, 75.3, 71.6, 66.4, 59.2, 49.1, 35.7, 20.8, 8.3],
    # 96 balls (16 overs)
    [84.1, 81.8, 79.0, 74.7, 69.1, 61.3, 50.4, 36.2, 20.8, 8.3],
    # 102 balls (17 overs)
    [88.2, 85.7, 82.5, 77.9, 71.7, 63.3, 51.6, 36.6, 21.0, 8.3],
    # 108 balls (18 overs)
    [92.2, 89.6, 85.9, 81.1, 74.2, 65.0, 52.7, 36.9, 21.0, 8.3],
    # 114 balls (19 overs)
    [96.1, 93.3, 89.2, 83.9, 76.7, 66.6, 53.5, 37.3, 21.0, 8.3],
    # 120 balls (20 overs) = full innings
    [100.0, 96.8, 92.6, 86.7, 78.8, 68.2, 54.4, 37.5, 21.3, 8.3],
]


def resource_t20(balls_remaining: int, wickets_lost: int) -> float:
    """T20 DLS resource % remaining. Interpolates between over boundaries."""
    balls = min(max(balls_remaining, 0), 120)
    wkts = min(max(wickets_lost, 0), 9)
    lower_idx = balls // 6
    frac = (balls % 6) / 6.0
    lower = T20_TABLE[lower_idx][wkts]
    if frac == 0.0 or lower_idx + 1 >= len(T20_TABLE):
        return lower
    upper = T20_TABLE[lower_idx + 1][wkts]
    return lower + (upper - lower) * frac


def par_score(t1_total: int, balls_used_t2: int, wickets_t2: int) -> float:
    """DLS par for Team 2 at current chase state."""
    r1_full = 100.0  # resource_t20(120, 0)
    r2_remaining = resource_t20(120 - balls_used_t2, wickets_t2)
    r2_used = r1_full - r2_remaining
    return t1_total * r2_used / r1_full


def projected_total(runs: int, balls_used: int, wickets: int) -> float:
    """Project innings total from current state using DLS resources."""
    r_used = 100.0 - resource_t20(120 - balls_used, wickets)
    if r_used <= 0:
        return float(runs)
    return runs * 100.0 / r_used


def revised_target(t1_total: int, new_max_balls: int) -> int:
    """Target to win if chase is curtailed to new_max_balls."""
    r1 = 100.0
    r2 = resource_t20(min(new_max_balls, 120), 0)
    return int(t1_total * r2 / r1) + 1


def overs_to_balls(overs) -> int:
    """Convert overs representation to total legal balls bowled."""
    if isinstance(overs, list) and len(overs) == 2:
        return int(overs[0]) * 6 + int(overs[1])
    if isinstance(overs, (int, float)):
        ov_int = int(overs)
        balls_part = round((overs - ov_int) * 10)
        return ov_int * 6 + balls_part
    # String like "12.3"
    s = str(overs)
    if "." in s:
        parts = s.split(".")
        return int(parts[0]) * 6 + int(parts[1])
    return int(s) * 6


def balls_to_overs_str(balls: int) -> str:
    return f"{balls // 6}.{balls % 6}"


def ist_now():
    return datetime.now(IST).strftime("%H:%M:%S")


# ── Polymarket helpers ─────────────────────────────────────────────────────

def fetch_market(slug: str) -> dict:
    resp = requests.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=15)
    resp.raise_for_status()
    markets = resp.json()
    if not markets:
        print(f"{C_RED}No market found for slug '{slug}'{C_RESET}")
        sys.exit(1)
    return markets[0]


def parse_tokens(market: dict) -> tuple[list[str], list[str]]:
    tokens = market.get("clobTokenIds", "")
    outcomes = market.get("outcomes", "[]")
    if isinstance(tokens, str):
        tokens = json.loads(tokens) if tokens else []
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes) if outcomes else []
    return tokens, outcomes


def fetch_poly_book(token_id: str) -> dict:
    url = f"https://clob.polymarket.com/book?token_id={token_id}"
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def book_best_prices(book: dict) -> tuple[float | None, float | None]:
    """Returns (best_bid, best_ask) as 0-1 prices."""
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    best_bid = max((float(b["price"]) for b in bids), default=None) if bids else None
    best_ask = min((float(a["price"]) for a in asks), default=None) if asks else None
    return best_bid, best_ask


# ── Auto-detect match state from cricket API ───────────────────────────────

def detect_match_state(match_key: str) -> dict:
    """Fetch match info from cricket API and determine batting order + score.

    Returns dict with:
        team1_name, team2_name (batting first, chasing)
        innings (1 or 2)
        t1_total, t1_wickets, t1_balls (if innings 2)
        t2_runs, t2_wickets, t2_balls (if innings 2, current chase state)
    """
    base = os.getenv("CRICKET_API_KEY", "").rstrip("/")
    if not base:
        return {}

    resp = httpx.get(f"{base}/events/cricket/match/recent.json", timeout=15)
    matches = resp.json() or {}
    m = matches.get(match_key, {})
    if not m:
        return {}

    teams = m.get("teams", {})
    play = m.get("play", {})
    first_batting_key = play.get("first_batting", "")
    live = play.get("live", {})
    innings_data = play.get("innings", {})
    target = play.get("target", {})

    if not first_batting_key or first_batting_key not in teams:
        return {}

    # Team names
    t1_info = teams[first_batting_key]
    t1_name = t1_info.get("name", t1_info.get("code", "Team1"))

    # Chasing team is the other one
    chasing_key = "b" if first_batting_key == "a" else "a"
    t2_info = teams.get(chasing_key, {})
    t2_name = t2_info.get("name", t2_info.get("code", "Team2"))

    result = {
        "team1_name": t1_name,
        "team2_name": t2_name,
        "first_batting_key": first_batting_key,
        "chasing_key": chasing_key,
    }

    # Check current innings from live data
    live_innings = live.get("innings", "")  # e.g. "b_1" means chasing team batting
    live_batting = live.get("batting_team", "")

    if live_batting == chasing_key or live_innings.startswith(chasing_key):
        result["innings"] = 2

        # First innings score from innings data
        t1_innings_key = f"{first_batting_key}_1"
        t1_inn = innings_data.get(t1_innings_key, {})
        t1_score = t1_inn.get("score", {})
        result["t1_total"] = t1_score.get("runs", target.get("runs", 0) - 1)
        result["t1_wickets"] = t1_inn.get("wickets", 0)
        result["t1_balls"] = t1_score.get("balls", 120)

        # Current chase state from live score
        live_score = live.get("score", {})
        result["t2_runs"] = live_score.get("runs", 0)
        result["t2_wickets"] = live_score.get("wickets", 0)
        overs = live_score.get("overs", [0, 0])
        result["t2_balls"] = overs_to_balls(overs)
    else:
        result["innings"] = 1

    return result


def match_outcome_to_team(outcome_names: list[str], team_name: str) -> int:
    """Find which Polymarket outcome index matches a cricket team name.
    Returns 0 or 1. Matches on substring (e.g. 'Mumbai Indians' matches 'Mumbai')."""
    team_lower = team_name.lower()
    for i, oname in enumerate(outcome_names):
        if team_lower in oname.lower() or oname.lower() in team_lower:
            return i
    return 0  # fallback


# ── SQLite Logger ─────────────────────────────────────────────────────────

class DLSLogger:
    def __init__(self, slug: str):
        db_dir = Path(__file__).parent.parent / "data"
        db_dir.mkdir(exist_ok=True)
        db_path = db_dir / f"dls_monitor_{slug}.sqlite"
        self.conn = sqlite3.connect(str(db_path))
        self.conn.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ist_time TEXT NOT NULL,
            ts_epoch REAL NOT NULL,
            innings INTEGER,
            signal TEXT,
            runs INTEGER,
            wickets INTEGER,
            balls INTEGER,
            overs TEXT,
            inn1_total INTEGER,
            target INTEGER,
            par_score REAL,
            par_diff REAL,
            projected_total REAL,
            rrr REAL,
            resource_pct REAL,
            t1_market_mid REAL,
            t2_market_mid REAL,
            team_batting TEXT,
            team_bowling TEXT
        )""")
        self.conn.commit()
        print(f"{C_DIM}DLS logging to {db_path}{C_RESET}")

    def log(self, dls, signal, poly_odds):
        ist = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        if dls.innings == 1:
            runs, wkts, balls = dls.innings_1_runs, dls.innings_1_wickets, dls.innings_1_balls
            batting, bowling = dls.team1_name, dls.team2_name
        else:
            runs, wkts, balls = dls.innings_2_runs, dls.innings_2_wickets, dls.innings_2_balls
            batting, bowling = dls.team2_name, dls.team1_name

        par = dls.get_par()
        diff = dls.get_par_diff()
        rrr = dls.required_run_rate()
        proj = projected_total(runs, balls, wkts) if dls.innings == 1 and balls > 6 else None
        r_used = 100.0 - resource_t20(120 - balls, wkts) if dls.innings == 1 else None

        t1_mid = None
        t2_mid = None
        if poly_odds:
            t1_bid = poly_odds.get(f"{dls.team1_name}_bid")
            t1_ask = poly_odds.get(f"{dls.team1_name}_ask")
            t2_bid = poly_odds.get(f"{dls.team2_name}_bid")
            t2_ask = poly_odds.get(f"{dls.team2_name}_ask")
            if t1_bid is not None and t1_ask is not None:
                t1_mid = (t1_bid + t1_ask) / 2
            if t2_bid is not None and t2_ask is not None:
                t2_mid = (t2_bid + t2_ask) / 2

        target = dls.innings_1_runs + 1 if dls.innings == 2 else None

        self.conn.execute(
            """INSERT INTO events (ist_time, ts_epoch, innings, signal, runs, wickets, balls, overs,
               inn1_total, target, par_score, par_diff, projected_total, rrr, resource_pct,
               t1_market_mid, t2_market_mid, team_batting, team_bowling)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ist, time.time(), dls.innings, signal, runs, wkts, balls,
             balls_to_overs_str(balls), dls.innings_1_runs if dls.innings == 2 else None,
             target, par, diff, proj, rrr, r_used,
             t1_mid, t2_mid, batting, bowling))
        self.conn.commit()

    def close(self):
        self.conn.close()


# ── DLS State ──────────────────────────────────────────────────────────────

class DlsState:
    def __init__(self, team1_name: str, team2_name: str):
        self.team1_name = team1_name   # batting first
        self.team2_name = team2_name   # chasing
        self.innings = 1
        self.innings_1_runs = 0
        self.innings_1_wickets = 0
        self.innings_1_balls = 0
        self.innings_2_runs = 0
        self.innings_2_wickets = 0
        self.innings_2_balls = 0
        # Track previous for innings-change detection
        self._prev_runs = 0
        self._prev_wickets = 0
        self._prev_balls = 0

    def update(self, runs: int, wickets: int, balls: int) -> bool:
        """Update from cumulative score. Returns True if state changed."""
        # Detect innings change: score resets
        if self.innings == 1 and self._prev_balls > 6 and balls < self._prev_balls - 6:
            # Lock innings 1 totals from previous state
            self.innings_1_runs = self._prev_runs
            self.innings_1_wickets = self._prev_wickets
            self.innings_1_balls = self._prev_balls
            self.innings = 2
            print(f"\n{C_BOLD}{C_MAGENTA}{'=' * 70}{C_RESET}")
            print(f"{C_BOLD}{C_MAGENTA}  INNINGS BREAK  |  {self.team1_name} finished: "
                  f"{self.innings_1_runs}/{self.innings_1_wickets} "
                  f"({balls_to_overs_str(self.innings_1_balls)} ov){C_RESET}")
            print(f"{C_BOLD}{C_MAGENTA}  {self.team2_name} to chase {self.innings_1_runs + 1}"
                  f"  |  DLS valid after 5.0 overs{C_RESET}")
            print(f"{C_BOLD}{C_MAGENTA}{'=' * 70}{C_RESET}\n")

        changed = (runs != self._prev_runs or wickets != self._prev_wickets
                    or balls != self._prev_balls)

        if self.innings == 1:
            self.innings_1_runs = runs
            self.innings_1_wickets = wickets
            self.innings_1_balls = balls
        else:
            self.innings_2_runs = runs
            self.innings_2_wickets = wickets
            self.innings_2_balls = balls

        self._prev_runs = runs
        self._prev_wickets = wickets
        self._prev_balls = balls
        return changed

    def force_innings_over(self):
        """Manually trigger innings break (if auto-detection fails)."""
        if self.innings == 1:
            self.innings_1_runs = self._prev_runs
            self.innings_1_wickets = self._prev_wickets
            self.innings_1_balls = self._prev_balls
            self.innings = 2
            self._prev_runs = 0
            self._prev_wickets = 0
            self._prev_balls = 0

    def get_par(self) -> float | None:
        if self.innings != 2 or self.innings_1_balls == 0:
            return None
        return par_score(self.innings_1_runs, self.innings_2_balls, self.innings_2_wickets)

    def get_par_diff(self) -> float | None:
        par = self.get_par()
        if par is None:
            return None
        return self.innings_2_runs - par

    def is_valid(self) -> bool:
        return self.innings == 2 and self.innings_2_balls >= 30

    def required_run_rate(self) -> float | None:
        """Required RR to reach par from here. None if not in chase."""
        if self.innings != 2 or self.innings_1_balls == 0:
            return None
        target = self.innings_1_runs + 1
        remaining_runs = target - self.innings_2_runs
        remaining_balls = 120 - self.innings_2_balls
        if remaining_balls <= 0:
            return None
        return remaining_runs / (remaining_balls / 6.0)


# ── Display ────────────────────────────────────────────────────────────────

def print_dls_update(dls: DlsState, poly_odds: dict | None, signal: str):
    """Print a single DLS update line."""
    ts = ist_now()

    if dls.innings == 1:
        runs = dls.innings_1_runs
        wkts = dls.innings_1_wickets
        balls = dls.innings_1_balls
        ov = balls_to_overs_str(balls)
        proj = projected_total(runs, balls, wkts) if balls > 6 else 0
        r_used = 100.0 - resource_t20(120 - balls, wkts)

        print(f"{C_DIM}{ts}{C_RESET} {C_CYAN}{signal:>3}{C_RESET}  "
              f"{C_BOLD}{dls.team1_name} {runs}/{wkts} ({ov} ov){C_RESET}  "
              f"{C_DIM}resources used: {r_used:.1f}%{C_RESET}", end="")
        if balls > 12:
            print(f"  {C_YELLOW}projected total: ~{proj:.0f}{C_RESET}", end="")
        _print_poly(poly_odds, dls, None)
        print()
        return

    # Innings 2: DLS matters
    runs = dls.innings_2_runs
    wkts = dls.innings_2_wickets
    balls = dls.innings_2_balls
    ov = balls_to_overs_str(balls)
    par = dls.get_par()
    diff = dls.get_par_diff()
    valid = dls.is_valid()
    rrr = dls.required_run_rate()

    # Color based on par diff
    if diff is not None and diff > 0:
        diff_color = C_GREEN
        status = f"{dls.team2_name} AHEAD"
    elif diff is not None and diff < 0:
        diff_color = C_RED
        status = f"{dls.team2_name} BEHIND"
    else:
        diff_color = C_YELLOW
        status = "LEVEL"

    valid_tag = f"{C_GREEN}VALID{C_RESET}" if valid else f"{C_RED}NOT VALID ({ov}/5.0 ov){C_RESET}"

    print(f"\n{C_DIM}{ts}{C_RESET} {C_CYAN}{signal:>3}{C_RESET}  "
          f"{C_BOLD}{dls.team2_name} {runs}/{wkts} ({ov} ov){C_RESET}  "
          f"chasing {dls.innings_1_runs + 1}")
    print(f"         DLS: par={C_BOLD}{par:.1f}{C_RESET}  "
          f"diff={diff_color}{C_BOLD}{diff:+.1f}{C_RESET}  "
          f"{diff_color}{C_BOLD}{status} by {abs(diff):.1f} runs{C_RESET}  "
          f"[{valid_tag}]", end="")
    if rrr is not None and rrr > 0:
        rrr_color = C_RED if rrr > 12 else (C_YELLOW if rrr > 9 else C_GREEN)
        print(f"  RRR={rrr_color}{rrr:.2f}{C_RESET}", end="")
    print()

    _print_poly(poly_odds, dls, diff)


def _print_poly(poly_odds: dict | None, dls: DlsState, par_diff: float | None):
    """Print Polymarket odds comparison line."""
    if not poly_odds:
        return

    t1_bid = poly_odds.get(f"{dls.team1_name}_bid")
    t1_ask = poly_odds.get(f"{dls.team1_name}_ask")
    t2_bid = poly_odds.get(f"{dls.team2_name}_bid")
    t2_ask = poly_odds.get(f"{dls.team2_name}_ask")

    t1_mid = None
    t2_mid = None
    if t1_bid is not None and t1_ask is not None:
        t1_mid = (t1_bid + t1_ask) / 2
    if t2_bid is not None and t2_ask is not None:
        t2_mid = (t2_bid + t2_ask) / 2

    parts = []
    if t1_mid is not None:
        parts.append(f"{dls.team1_name} {t1_mid * 100:.1f}c")
    if t2_mid is not None:
        parts.append(f"{dls.team2_name} {t2_mid * 100:.1f}c")

    if not parts:
        return

    odds_str = " / ".join(parts)
    print(f"         Poly: {C_DIM}{odds_str}{C_RESET}", end="")

    # Edge signal only in chase with valid DLS
    if par_diff is not None and t2_mid is not None and dls.is_valid():
        # Rough heuristic: if team2 is ahead on DLS but Polymarket says they're <50%
        if par_diff > 5 and t2_mid < 0.50:
            print(f"  {C_BG_GREEN}{C_WHITE} {dls.team2_name} looks CHEAP (DLS ahead +{par_diff:.0f}) {C_RESET}", end="")
        elif par_diff < -5 and t2_mid > 0.50:
            print(f"  {C_BG_RED}{C_WHITE} {dls.team2_name} looks EXPENSIVE (DLS behind {par_diff:.0f}) {C_RESET}", end="")
    print()


# ── Polymarket polling ─────────────────────────────────────────────────────

async def poll_polymarket(token_ids: list[str], outcome_names: list[str],
                          odds_store: dict, interval: float = 3.0):
    """Periodically fetch Polymarket books and update shared odds dict."""
    while True:
        try:
            for tid, oname in zip(token_ids, outcome_names):
                book = fetch_poly_book(tid)
                bid, ask = book_best_prices(book)
                odds_store[f"{oname}_bid"] = bid
                odds_store[f"{oname}_ask"] = ask
        except Exception as e:
            pass  # silently retry
        await asyncio.sleep(interval)


# ── Cricket SSE ────────────────────────────────────────────────────────────

async def cricket_sse(match_key: str, dls: DlsState, odds_store: dict):
    """Stream live cricket score via Firebase SSE. Update DLS on each ball."""
    base = os.getenv("CRICKET_API_KEY", "").rstrip("/")
    if not base:
        print(f"{C_RED}CRICKET_API_KEY not set in .env{C_RESET}")
        return

    score_url = f"{base}/recent-matches/{match_key}/play/live/score.json"
    print(f"{C_DIM}SSE: {score_url}{C_RESET}\n")

    while True:
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("GET", score_url,
                                         headers={"Accept": "text/event-stream"}) as resp:
                    resp.raise_for_status()
                    print(f"{C_GREEN}Cricket SSE connected{C_RESET}\n")

                    event_type = None
                    data_buf = []

                    async for line in resp.aiter_lines():
                        if line.startswith("event:"):
                            event_type = line[6:].strip()
                            continue
                        if line.startswith("data:"):
                            data_buf.append(line[5:].strip())
                            continue

                        if line == "" and data_buf:
                            raw = "\n".join(data_buf)
                            data_buf = []

                            if raw == "null" or event_type == "keep-alive":
                                continue

                            try:
                                payload = json.loads(raw)
                            except json.JSONDecodeError:
                                continue

                            _handle_score(payload, dls, odds_store)

        except Exception as e:
            print(f"{C_RED}SSE error: {e}{C_RESET}")

        await asyncio.sleep(2)


def _handle_score(payload: dict, dls: DlsState, odds_store: dict):
    """Process a cricket score update."""
    if not isinstance(payload, dict):
        return

    data = payload.get("data", payload)
    if isinstance(data, dict) and "path" in data:
        data = data.get("data", data)
    if not isinstance(data, dict):
        return

    runs = data.get("runs")
    wickets = data.get("wickets")
    overs = data.get("overs")

    if runs is None and wickets is None:
        return

    runs = runs if runs is not None else dls._prev_runs
    wickets = wickets if wickets is not None else dls._prev_wickets

    if overs is not None:
        balls = overs_to_balls(overs)
    else:
        balls = dls._prev_balls

    # Detect signal type for display
    run_diff = runs - dls._prev_runs
    wicket_diff = wickets - dls._prev_wickets

    if dls.innings == 1 and balls < dls._prev_balls - 6:
        signal = "IO"
    elif wicket_diff > 0:
        signal = "W"
    elif run_diff == 6:
        signal = "6"
    elif run_diff == 4:
        signal = "4"
    elif run_diff >= 0:
        signal = str(run_diff)
    else:
        signal = "?"

    changed = dls.update(runs, wickets, balls)
    if changed:
        print_dls_update(dls, odds_store, signal)
        if _dls_db:
            _dls_db.log(dls, signal, odds_store)


# ── Stdin listener for manual commands ─────────────────────────────────────

async def stdin_listener(dls: DlsState):
    """Listen for manual commands: IO (innings over), SEED t1_total."""
    loop = asyncio.get_event_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    while True:
        try:
            line = await reader.readline()
            if not line:
                break
            cmd = line.decode().strip().upper()
            if cmd == "IO":
                dls.force_innings_over()
                print(f"\n{C_MAGENTA}Manually triggered innings over.{C_RESET}")
                print(f"{C_MAGENTA}T1 total = {dls.innings_1_runs}/{dls.innings_1_wickets} "
                      f"({balls_to_overs_str(dls.innings_1_balls)} ov){C_RESET}\n")
            elif cmd.startswith("SEED "):
                try:
                    t1 = int(cmd.split()[1])
                    dls.innings = 2
                    dls.innings_1_runs = t1
                    dls.innings_1_balls = 120
                    dls.innings_1_wickets = 0
                    print(f"\n{C_MAGENTA}Seeded T1 total = {t1}. Now tracking chase.{C_RESET}\n")
                except (ValueError, IndexError):
                    print(f"{C_RED}Usage: SEED <t1_total> (e.g. SEED 180){C_RESET}")
        except Exception:
            break


# ── Main ───────────────────────────────────────────────────────────────────

async def run(args):
    # Resolve Polymarket market
    print(f"{C_BOLD}Resolving market '{args.slug}'...{C_RESET}")
    market = fetch_market(args.slug)
    question = market.get("question", args.slug)
    token_ids, outcome_names = parse_tokens(market)

    if not token_ids or len(outcome_names) < 2:
        print(f"{C_RED}Need 2 outcomes, got {len(outcome_names)}{C_RESET}")
        sys.exit(1)

    # Auto-detect match state from cricket API
    print(f"{C_DIM}Detecting match state from cricket API...{C_RESET}")
    info = detect_match_state(args.match)

    if info.get("team1_name"):
        team1_name = info["team1_name"]
        team2_name = info["team2_name"]
    elif args.first_batting:
        idx = args.first_batting - 1
        team1_name = outcome_names[idx]
        team2_name = outcome_names[1 - idx]
    else:
        team1_name = outcome_names[0]
        team2_name = outcome_names[1]

    dls = DlsState(team1_name, team2_name)
    odds_store: dict = {}

    global _dls_db
    _dls_db = DLSLogger(args.slug)

    # Auto-seed from API if we're in innings 2
    if info.get("innings") == 2:
        t1_total = args.t1_total or info.get("t1_total", 0)
        t1_wkts = info.get("t1_wickets", 0)
        t1_balls = info.get("t1_balls", 120)
        dls.innings = 2
        dls.innings_1_runs = t1_total
        dls.innings_1_wickets = t1_wkts
        dls.innings_1_balls = t1_balls

        # Seed current chase state
        t2_runs = info.get("t2_runs", 0)
        t2_wkts = info.get("t2_wickets", 0)
        t2_balls = info.get("t2_balls", 0)
        dls.innings_2_runs = t2_runs
        dls.innings_2_wickets = t2_wkts
        dls.innings_2_balls = t2_balls
        dls._prev_runs = t2_runs
        dls._prev_wickets = t2_wkts
        dls._prev_balls = t2_balls
    elif args.t1_total:
        dls.innings = 2
        dls.innings_1_runs = args.t1_total
        dls.innings_1_balls = 120
        dls.innings_1_wickets = 0

    print(f"\n  Market:       {question}")
    print(f"  Batting 1st:  {C_BOLD}{team1_name}{C_RESET}")
    print(f"  Chasing:      {C_BOLD}{team2_name}{C_RESET}")
    print(f"  Outcomes:     {outcome_names}")
    if dls.innings == 2:
        par = dls.get_par()
        diff = dls.get_par_diff()
        print(f"  T1 Score:     {C_BOLD}{dls.innings_1_runs}/{dls.innings_1_wickets}{C_RESET} "
              f"({balls_to_overs_str(dls.innings_1_balls)} ov)")
        print(f"  Chase at:     {C_BOLD}{dls.innings_2_runs}/{dls.innings_2_wickets}{C_RESET} "
              f"({balls_to_overs_str(dls.innings_2_balls)} ov)")
        if par is not None:
            color = C_GREEN if diff > 0 else C_RED
            print(f"  DLS Par:      {C_BOLD}{par:.1f}{C_RESET}  "
                  f"diff={color}{C_BOLD}{diff:+.1f}{C_RESET}  "
                  f"valid={'yes' if dls.is_valid() else 'no'}")
    print(f"  Match:        {args.match}")
    print(f"\n{'=' * 70}")
    print(f"  Commands: type IO = innings over, SEED <n> = set T1 total mid-match")
    print(f"{'=' * 70}\n")

    tasks = [
        cricket_sse(args.match, dls, odds_store),
        poll_polymarket(token_ids, outcome_names, odds_store, interval=3.0),
    ]

    # stdin listener may not work in all terminals with asyncio
    try:
        tasks.append(stdin_listener(dls))
    except Exception:
        pass

    await asyncio.gather(*tasks)


def main():
    parser = argparse.ArgumentParser(description="DLS Live Monitor")
    parser.add_argument("--slug", required=True, help="Polymarket market slug")
    parser.add_argument("--match", required=True, help="Cricket match key (Firebase)")
    parser.add_argument("--first-batting", type=int, default=None, choices=[1, 2],
                        help="Override: which Polymarket outcome bats first (auto-detected from API)")
    parser.add_argument("--t1-total", type=int, default=None,
                        help="Seed Team 1 total if starting mid-chase")
    args = parser.parse_args()

    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print(f"\n{C_BOLD}Stopped.{C_RESET}")


if __name__ == "__main__":
    main()
