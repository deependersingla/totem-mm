#!/usr/bin/env python3
"""DP Win Probability Live Monitor — paper mode.

Runs alongside a live IPL match. After every ball:
1. Shows model win probability vs Polymarket price
2. Shows next-ball scenario probabilities (what if dot/4/6/W?)
3. Tracks whether previous prediction direction was correct

Usage (same pattern as dls_monitor.py):
    python scripts/dp_monitor.py --slug cricipl-roy-del-2026-04-18 \
        --match $(python scripts/cricket_match_lookup.py cricipl-roy-del-2026-04-18 --key-only)
"""

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
import requests
from dotenv import load_dotenv

load_dotenv()

# ── Add model-simulation to path ──────────────────────────────────────────
MODEL_DIR = Path(__file__).parent.parent / "model-simulation"
sys.path.insert(0, str(MODEL_DIR))

IST = timezone(timedelta(hours=5, minutes=30))
GAMMA_API = "https://gamma-api.polymarket.com"

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


def ist_now():
    return datetime.now(IST).strftime("%H:%M:%S")


def overs_to_balls(overs) -> int:
    if isinstance(overs, list) and len(overs) == 2:
        return int(overs[0]) * 6 + int(overs[1])
    if isinstance(overs, (int, float)):
        ov_int = int(overs)
        balls_part = round((overs - ov_int) * 10)
        return ov_int * 6 + balls_part
    s = str(overs)
    if "." in s:
        parts = s.split(".")
        return int(parts[0]) * 6 + int(parts[1])
    return int(s) * 6


def balls_to_overs_str(balls: int) -> str:
    return f"{balls // 6}.{balls % 6}"


# ── DP Model ──────────────────────────────────────────────────────────────

def load_dp_model():
    """Load DP table with modern era transitions."""
    import pandas as pd
    from src.dp.solver import DPTable
    from src.dp.states import TransitionProbs

    del_path = MODEL_DIR / "data" / "deliveries.parquet"
    df = pd.read_parquet(del_path)

    def season_to_year(s):
        s = str(s)
        return int(s.split("/")[0]) if "/" in s else int(s)

    df["year"] = df["season"].apply(season_to_year)
    modern = df[df["year"] >= 2023]

    phase_probs = {}
    for phase in ["powerplay", "middle", "death"]:
        sub = modern[modern["phase"] == phase]
        total = len(sub)
        if total == 0:
            continue
        counts = sub["outcome_class"].value_counts()
        probs = {}
        for outcome in ["dot", "single", "double", "triple", "four", "six", "wicket", "wide", "noball"]:
            probs[outcome] = counts.get(outcome, 0) / total
        phase_probs[phase] = TransitionProbs(**probs).normalize()

    dp = DPTable()

    def get_tp(b, w):
        overs_bowled = (120 - b) // 6
        if overs_bowled < 6:
            return phase_probs.get("powerplay", TransitionProbs.from_phase_averages("powerplay"))
        elif overs_bowled < 15:
            return phase_probs.get("middle", TransitionProbs.from_phase_averages("middle"))
        else:
            return phase_probs.get("death", TransitionProbs.from_phase_averages("death"))

    dp.solve(get_transition_probs=get_tp)
    return dp, phase_probs


# ── Polymarket helpers ────────────────────────────────────────────────────

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
    headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def book_best_prices(book: dict) -> tuple[float | None, float | None]:
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    best_bid = max((float(b["price"]) for b in bids), default=None) if bids else None
    best_ask = min((float(a["price"]) for a in asks), default=None) if asks else None
    return best_bid, best_ask


# ── Cricket API ───────────────────────────────────────────────────────────

def detect_match_state(match_key: str) -> dict:
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

    t1_info = teams[first_batting_key]
    t1_name = t1_info.get("name", t1_info.get("code", "Team1"))
    chasing_key = "b" if first_batting_key == "a" else "a"
    t2_info = teams.get(chasing_key, {})
    t2_name = t2_info.get("name", t2_info.get("code", "Team2"))

    result = {"team1_name": t1_name, "team2_name": t2_name,
              "first_batting_key": first_batting_key, "chasing_key": chasing_key}

    live_batting = live.get("batting_team", "")
    if live_batting == chasing_key:
        result["innings"] = 2
        t1_inn_key = f"{first_batting_key}_1"
        t1_inn = innings_data.get(t1_inn_key, {})
        t1_score = t1_inn.get("score", {})
        result["t1_total"] = t1_score.get("runs", target.get("runs", 0) - 1)
        result["t1_balls"] = t1_score.get("balls", 120)
        live_score = live.get("score", {})
        result["t2_runs"] = live_score.get("runs", 0)
        result["t2_wickets"] = live_score.get("wickets", 0)
        result["t2_balls"] = overs_to_balls(live_score.get("overs", [0, 0]))
    else:
        result["innings"] = 1

    return result


# ── Match State Tracker ───────────────────────────────────────────────────

class MatchState:
    def __init__(self, team1: str, team2: str):
        self.team1 = team1  # batting first
        self.team2 = team2  # chasing
        self.innings = 1
        self.inn1_runs = 0
        self.inn1_wickets = 0
        self.inn1_balls = 0
        self.runs = 0
        self.wickets = 0
        self.balls = 0
        self._prev_runs = 0
        self._prev_wickets = 0
        self._prev_balls = 0
        # Prediction tracking
        self.last_model_prob = None
        self.last_market_prob = None
        self.last_signal = None
        self.predictions_correct = 0
        self.predictions_total = 0

    def update(self, runs, wickets, balls) -> tuple[bool, str]:
        """Update state. Returns (changed, signal)."""
        if self.innings == 1 and self._prev_balls > 6 and balls < self._prev_balls - 6:
            self.inn1_runs = self._prev_runs
            self.inn1_wickets = self._prev_wickets
            self.inn1_balls = self._prev_balls
            self.innings = 2

        run_diff = runs - self._prev_runs
        wkt_diff = wickets - self._prev_wickets

        if self.innings == 1 and balls < self._prev_balls - 6:
            signal = "IO"
        elif wkt_diff > 0:
            signal = "W"
        elif run_diff == 6:
            signal = "6"
        elif run_diff == 4:
            signal = "4"
        elif run_diff >= 0:
            signal = str(run_diff)
        else:
            signal = "?"

        changed = (runs != self._prev_runs or wickets != self._prev_wickets
                   or balls != self._prev_balls)

        self.runs = runs
        self.wickets = wickets
        self.balls = balls
        self._prev_runs = runs
        self._prev_wickets = wickets
        self._prev_balls = balls

        return changed, signal


# ── Display ───────────────────────────────────────────────────────────────

def print_update(state: MatchState, dp, odds_store: dict, signal: str, outcome_names: list[str]):
    ts = ist_now()

    if state.innings == 1:
        ov = balls_to_overs_str(state.balls)
        batting = state.team1
        print(f"{C_DIM}{ts}{C_RESET} {C_CYAN}{signal:>3}{C_RESET}  "
              f"{C_BOLD}{batting} {state.runs}/{state.wickets} ({ov}){C_RESET}  "
              f"{C_DIM}1st innings{C_RESET}")
        _print_poly_simple(odds_store, outcome_names)
        return

    # 2nd innings — DP model kicks in
    target = state.inn1_runs + 1
    runs_needed = target - state.runs
    balls_remaining = 120 - state.balls
    wickets_in_hand = 10 - state.wickets
    ov = balls_to_overs_str(state.balls)
    chaser = state.team2

    if runs_needed <= 0:
        model_prob = 1.0
    elif wickets_in_hand <= 0 or balls_remaining <= 0:
        model_prob = 0.0
    else:
        model_prob = dp.lookup(balls_remaining, min(runs_needed, dp.MAX_RUNS), wickets_in_hand)

    # Get market price
    market_prob = _get_chaser_market_prob(odds_store, outcome_names, state)

    # Check last prediction
    pred_check = ""
    if state.last_model_prob is not None and state.last_market_prob is not None:
        # Was the model's direction of edge correct?
        # If model said chaser had MORE prob than market, and now market moved UP → correct
        last_edge = state.last_model_prob - state.last_market_prob
        market_moved = (market_prob - state.last_market_prob) if market_prob else 0

        if abs(last_edge) > 0.01 and market_prob is not None:
            state.predictions_total += 1
            if (last_edge > 0 and market_moved > 0) or (last_edge < 0 and market_moved < 0):
                state.predictions_correct += 1
                pred_check = f"{C_GREEN}PREV:OK{C_RESET}"
            else:
                pred_check = f"{C_RED}PREV:X{C_RESET}"

    # Edge
    edge = (model_prob - market_prob) if market_prob is not None else None
    if edge is not None:
        if abs(edge) > 0.05:
            edge_color = C_BG_GREEN if edge > 0 else C_BG_RED
            edge_str = f" {edge_color}{C_WHITE} EDGE {edge:+.1%} {C_RESET}"
        elif abs(edge) > 0.02:
            edge_color = C_GREEN if edge > 0 else C_RED
            edge_str = f" {edge_color}edge {edge:+.1%}{C_RESET}"
        else:
            edge_str = f" {C_DIM}={C_RESET}"
    else:
        edge_str = ""

    # Signal color
    sig_color = C_RED if signal == "W" else (C_GREEN if signal in ("4", "6") else C_CYAN)

    # Score line
    print(f"\n{C_DIM}{ts}{C_RESET} {sig_color}{C_BOLD}{signal:>3}{C_RESET}  "
          f"{C_BOLD}{chaser} {state.runs}/{state.wickets} ({ov}){C_RESET}  "
          f"chasing {target}  need {runs_needed} off {balls_remaining}b"
          f"  {pred_check}")

    # Model vs Market
    market_str = f"{market_prob:.1%}" if market_prob is not None else "N/A"
    print(f"      Model: {C_BOLD}{model_prob:.1%}{C_RESET}  "
          f"Market: {C_BOLD}{market_str}{C_RESET}{edge_str}")

    # Next ball scenarios
    if runs_needed > 0 and balls_remaining > 0 and wickets_in_hand > 0:
        scenarios = dp.get_scenarios(balls_remaining, min(runs_needed, dp.MAX_RUNS), wickets_in_hand)
        sc_parts = []
        for name, label, color in [("dot", "0", C_DIM), ("single", "1", C_CYAN),
                                    ("four", "4", C_GREEN), ("six", "6", C_GREEN),
                                    ("wicket", "W", C_RED)]:
            v = scenarios.get(name, model_prob)
            delta = v - model_prob
            sc_parts.append(f"{color}{label}→{v:.0%}({delta:+.1%}){C_RESET}")
        print(f"      Next: {' '.join(sc_parts)}")

    # Running score
    if state.predictions_total > 0:
        pct = state.predictions_correct / state.predictions_total * 100
        print(f"      {C_DIM}Prediction record: {state.predictions_correct}/{state.predictions_total} "
              f"({pct:.0f}%){C_RESET}")

    state.last_model_prob = model_prob
    state.last_market_prob = market_prob
    state.last_signal = signal


def _get_chaser_market_prob(odds_store: dict, outcome_names: list[str], state: MatchState) -> float | None:
    """Get chaser's market probability from odds_store."""
    for oname in outcome_names:
        if state.team2.lower() in oname.lower() or oname.lower() in state.team2.lower():
            bid = odds_store.get(f"{oname}_bid")
            ask = odds_store.get(f"{oname}_ask")
            if bid is not None and ask is not None:
                return (bid + ask) / 2
            if bid is not None:
                return bid
    # Fallback: use team1 and invert
    for oname in outcome_names:
        if state.team1.lower() in oname.lower() or oname.lower() in state.team1.lower():
            bid = odds_store.get(f"{oname}_bid")
            ask = odds_store.get(f"{oname}_ask")
            if bid is not None and ask is not None:
                return 1.0 - (bid + ask) / 2
    return None


def _print_poly_simple(odds_store: dict, outcome_names: list[str]):
    parts = []
    for oname in outcome_names:
        bid = odds_store.get(f"{oname}_bid")
        ask = odds_store.get(f"{oname}_ask")
        if bid and ask:
            mid = (bid + ask) / 2
            parts.append(f"{oname} {mid*100:.1f}c")
    if parts:
        print(f"      {C_DIM}Poly: {' / '.join(parts)}{C_RESET}")


# ── Polymarket polling ────────────────────────────────────────────────────

async def poll_polymarket(token_ids, outcome_names, odds_store, interval=3.0):
    while True:
        try:
            for tid, oname in zip(token_ids, outcome_names):
                book = fetch_poly_book(tid)
                bid, ask = book_best_prices(book)
                odds_store[f"{oname}_bid"] = bid
                odds_store[f"{oname}_ask"] = ask
        except Exception:
            pass
        await asyncio.sleep(interval)


# ── Cricket SSE ───────────────────────────────────────────────────────────

async def cricket_sse(match_key, state, dp, odds_store, outcome_names):
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

                    data_buf = []
                    async for line in resp.aiter_lines():
                        if line.startswith("event:"):
                            continue
                        if line.startswith("data:"):
                            data_buf.append(line[5:].strip())
                            continue
                        if line == "" and data_buf:
                            raw = "\n".join(data_buf)
                            data_buf = []
                            if raw == "null":
                                continue
                            try:
                                payload = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            _handle_score(payload, state, dp, odds_store, outcome_names)
        except Exception as e:
            print(f"{C_RED}SSE error: {e}{C_RESET}")
        await asyncio.sleep(2)


def _handle_score(payload, state, dp, odds_store, outcome_names):
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

    runs = runs if runs is not None else state._prev_runs
    wickets = wickets if wickets is not None else state._prev_wickets
    balls = overs_to_balls(overs) if overs is not None else state._prev_balls

    changed, signal = state.update(runs, wickets, balls)
    if changed:
        print_update(state, dp, odds_store, signal, outcome_names)


# ── Stdin listener ────────────────────────────────────────────────────────

async def stdin_listener(state):
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
                state.inn1_runs = state._prev_runs
                state.inn1_wickets = state._prev_wickets
                state.inn1_balls = state._prev_balls
                state.innings = 2
                state._prev_runs = 0
                state._prev_wickets = 0
                state._prev_balls = 0
                print(f"\n{C_MAGENTA}Innings over. T1 = {state.inn1_runs}/{state.inn1_wickets} "
                      f"({balls_to_overs_str(state.inn1_balls)}). Chase target: {state.inn1_runs + 1}{C_RESET}\n")
            elif cmd.startswith("SEED "):
                try:
                    t1 = int(cmd.split()[1])
                    state.innings = 2
                    state.inn1_runs = t1
                    state.inn1_balls = 120
                    state.inn1_wickets = 0
                    print(f"\n{C_MAGENTA}Seeded T1={t1}. Chase target: {t1 + 1}{C_RESET}\n")
                except (ValueError, IndexError):
                    print(f"{C_RED}Usage: SEED <t1_total>{C_RESET}")
        except Exception:
            break


# ── Main ──────────────────────────────────────────────────────────────────

async def run(args):
    # Load DP model
    print(f"{C_BOLD}Loading DP model (2023-2026 transitions)...{C_RESET}")
    dp, phase_probs = load_dp_model()
    print(f"{C_GREEN}DP ready. target170={dp.lookup(120,170,10):.1%} target200={dp.lookup(120,200,10):.1%}{C_RESET}")

    # Resolve market
    print(f"\n{C_BOLD}Resolving market '{args.slug}'...{C_RESET}")
    market = fetch_market(args.slug)
    question = market.get("question", args.slug)
    token_ids, outcome_names = parse_tokens(market)

    if len(outcome_names) < 2:
        print(f"{C_RED}Need 2 outcomes{C_RESET}")
        sys.exit(1)

    # Detect match state
    info = detect_match_state(args.match) if args.match else {}
    team1 = info.get("team1_name", outcome_names[0])
    team2 = info.get("team2_name", outcome_names[1])

    state = MatchState(team1, team2)
    odds_store = {}

    # Auto-seed if chase in progress
    if info.get("innings") == 2:
        state.innings = 2
        state.inn1_runs = info.get("t1_total", 0)
        state.inn1_balls = info.get("t1_balls", 120)
        state.runs = info.get("t2_runs", 0)
        state.wickets = info.get("t2_wickets", 0)
        state.balls = info.get("t2_balls", 0)
        state._prev_runs = state.runs
        state._prev_wickets = state.wickets
        state._prev_balls = state.balls
    elif args.t1_total:
        state.innings = 2
        state.inn1_runs = args.t1_total
        state.inn1_balls = 120

    print(f"\n{'='*70}")
    print(f"  {C_BOLD}DP WIN PROBABILITY MONITOR — PAPER MODE{C_RESET}")
    print(f"  Market:      {question}")
    print(f"  Batting 1st: {C_BOLD}{team1}{C_RESET}")
    print(f"  Chasing:     {C_BOLD}{team2}{C_RESET}")
    if state.innings == 2:
        print(f"  Target:      {C_BOLD}{state.inn1_runs + 1}{C_RESET}")
        if state.runs > 0:
            print(f"  Chase at:    {state.runs}/{state.wickets} ({balls_to_overs_str(state.balls)})")
    print(f"  Model:       DP backward induction, 2023-2026 IPL transitions")
    print(f"  Limitation:  No player identity, no pitch conditions")
    print(f"  Commands:    IO = innings over, SEED <n> = set T1 total")
    print(f"{'='*70}\n")

    tasks = [
        cricket_sse(args.match, state, dp, odds_store, outcome_names),
        poll_polymarket(token_ids, outcome_names, odds_store),
    ]
    try:
        tasks.append(stdin_listener(state))
    except Exception:
        pass

    await asyncio.gather(*tasks)


def main():
    parser = argparse.ArgumentParser(description="DP Win Probability Live Monitor")
    parser.add_argument("--slug", required=True, help="Polymarket market slug")
    parser.add_argument("--match", required=True, help="Cricket match key (Firebase)")
    parser.add_argument("--t1-total", type=int, default=None, help="Seed T1 total if joining mid-chase")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
