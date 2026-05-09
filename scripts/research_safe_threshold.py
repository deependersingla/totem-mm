#!/usr/bin/env python3
"""
Find the empirical "safe-favorite" price threshold for IPL 2026.

For every IPL 2026 match: fetch the LOSING team's price history from Polymarket
CLOB prices-history endpoint, take its peak. The highest such peak across all
matches is the worst case — any threshold strictly above it would have been a
"team always wins" signal in this season so far.

Output: per-match max loser price, sorted descending; threshold candidates.
"""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

ESPN_PATH = Path("/Users/sobhagyaxd/DeepWork/totem-mm/captures/espn_ipl2026_ballbyball.xlsx")
NEWER = [
    "cricipl-luc-kol-2026-04-26",
    "cricipl-che-guj-2026-04-26",
    "cricipl-del-roy-2026-04-27",
]


def list_matches() -> list[str]:
    import openpyxl
    wb = openpyxl.load_workbook(ESPN_PATH, read_only=True)
    slugs = [f"cricipl-{name}" for name in wb.sheetnames if name != "_summary"]
    for s in NEWER:
        if s not in slugs:
            slugs.append(s)
    return slugs


def fetch_event(slug: str, client: httpx.Client) -> dict | None:
    r = client.get(f"{GAMMA}/events", params={"slug": slug})
    if r.status_code != 200:
        return None
    d = r.json()
    return d[0] if d else None


def main_market(event: dict) -> dict | None:
    for m in event.get("markets", []):
        s = m.get("slug", "")
        if "toss-winner" in s or "completed-match" in s:
            continue
        return m
    return None


def parse_market(market: dict) -> tuple[str, list[str], list[str], str | None]:
    cid = market["conditionId"]
    outcomes = json.loads(market["outcomes"]) if isinstance(market["outcomes"], str) else market["outcomes"]
    prices = json.loads(market["outcomePrices"]) if isinstance(market["outcomePrices"], str) else market["outcomePrices"]
    tokens = market.get("clobTokenIds")
    if isinstance(tokens, str):
        tokens = json.loads(tokens)
    winner = next((o for o, p in zip(outcomes, prices) if float(p) == 1.0), None)
    return cid, tokens, outcomes, winner


def loser_max_price(loser_token: str, client: httpx.Client, fidelity: int) -> float | None:
    """Fetch loser token's full price history and return max."""
    r = client.get(f"{CLOB}/prices-history",
                   params={"market": loser_token, "interval": "max", "fidelity": fidelity})
    if r.status_code != 200:
        return None
    h = r.json().get("history", [])
    if not h:
        return None
    return max(float(p["p"]) for p in h)


def analyze_one(slug: str) -> dict:
    out = {"slug": slug}
    try:
        with httpx.Client(timeout=30) as client:
            ev = fetch_event(slug, client)
            if not ev:
                out["error"] = "event not found"
                return out
            if not ev.get("closed"):
                out["error"] = "not closed"
                return out
            mkt = main_market(ev)
            if not mkt:
                out["error"] = "no main market"
                return out
            cid, tokens, outcomes, winner = parse_market(mkt)
            if winner is None:
                out["error"] = "unresolved"
                return out
            w_idx = outcomes.index(winner)
            l_idx = 1 - w_idx
            loser = outcomes[l_idx]
            loser_token = tokens[l_idx]
            # First pass: minute-level max
            mx_60 = loser_max_price(loser_token, client, fidelity=60)
            # Second pass: 1-second fidelity to catch spikes
            mx_1 = loser_max_price(loser_token, client, fidelity=1)
            mx = max(filter(None, [mx_60, mx_1])) if (mx_60 or mx_1) else None
            out.update({
                "winner": winner,
                "loser": loser,
                "loser_token": loser_token,
                "loser_max_60s": mx_60,
                "loser_max_1s": mx_1,
                "loser_max": mx,
            })
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def main():
    slugs = list_matches()
    print(f"Found {len(slugs)} IPL 2026 matches.\n", flush=True)
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(analyze_one, s): s for s in slugs}
        for fut in as_completed(futures):
            r = fut.result()
            results.append(r)
            if "error" in r:
                print(f"  [skip] {r['slug']}: {r['error']}", flush=True)
            else:
                print(f"  {r['slug']}: loser={r['loser']:30s} max_loser_p={r['loser_max']:.4f} (60s={r['loser_max_60s']:.4f}, 1s={r['loser_max_1s']:.4f})", flush=True)
    print()
    valid = [r for r in results if "loser_max" in r and r["loser_max"] is not None]
    valid.sort(key=lambda r: -r["loser_max"])

    print("=" * 80)
    print("RANKING — highest loser peak first (closest a losing team came to winning)")
    print("=" * 80)
    print(f"{'Rank':>4} {'Match':40s} {'Winner':10s} {'Loser':10s} {'Max p (60s)':>11} {'Max p (1s)':>10}")
    for i, r in enumerate(valid, 1):
        slug = r["slug"].replace("cricipl-", "")
        # short labels
        w = r["winner"].split()[0] if r["winner"] else "?"
        l = r["loser"].split()[0] if r["loser"] else "?"
        print(f"{i:>4} {slug:40s} {w:10s} {l:10s} {r['loser_max_60s']:>11.4f} {r['loser_max_1s']:>10.4f}")

    print()
    print("=" * 80)
    print("THRESHOLD ANALYSIS")
    print("=" * 80)
    if not valid:
        print("no valid matches")
        sys.exit(0)
    worst = valid[0]
    p_max = worst["loser_max"]
    print(f"Highest loser peak across {len(valid)} matches: {p_max:.4f}")
    print(f"  Match: {worst['slug']}  —  loser {worst['loser']} reached {p_max:.4f}")
    print()

    # Threshold candidates: minimum p > p_max, rounded to common ticks
    print(f"{'Threshold':>10} {'Safe?':>7} {'# matches loser ≥ T':>22}")
    for t in [0.90, 0.92, 0.94, 0.95, 0.96, 0.97, 0.98, 0.985, 0.99, 0.995, 0.999]:
        breaches = sum(1 for r in valid if r["loser_max"] >= t)
        safe = "YES" if breaches == 0 else "no"
        print(f"{t:>10.4f} {safe:>7} {breaches:>22}")

    # Save
    out = Path("/Users/sobhagyaxd/DeepWork/totem-mm/captures/research_safe_threshold.json")
    out.write_text(json.dumps({"matches": results, "highest_loser_peak": p_max}, indent=2, default=str))
    print(f"\nDetail JSON: {out}")


if __name__ == "__main__":
    main()
